"""HTTP 传输层。

特性：
  * 多镜像回退：同一 URL 依次尝试 官方源 + 各社区镜像；
  * 下载即校验：边下边算 SHA-1/SHA-256，摘要不符自动换源重试；
  * 并发下载：线程池，进度聚合为单行；
  * JSON 缓存：版本清单等元数据落盘，支持离线/降级使用。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import util

LOG = util.LOG
USER_AGENT = "mcdecent/0.1 (decentralized minecraft launcher)"

CONNECT_TIMEOUT = 20
READ_TIMEOUT = 60


class DownloadError(RuntimeError):
    def __init__(self, message: str, url: str = "", errors: Sequence[str] = ()):
        super().__init__(message)
        self.url = url
        self.errors = list(errors)


class _HashMismatch(IOError):
    """下载内容摘要与期望不符（用于触发换源重试）。"""


def _is_conn_error(exc: BaseException) -> bool:
    """连接层错误：多半是复用了服务器已关闭的 keep-alive 连接，换新连接重试一次即可。"""
    return isinstance(exc, (
        http.client.RemoteDisconnected,
        http.client.BadStatusLine,
        http.client.IncompleteRead,
        ConnectionResetError,
        ConnectionAbortedError,
        BrokenPipeError,
        TimeoutError,
        socket.timeout,
        OSError,
    ))


def _is_tls_error(exc: BaseException) -> bool:
    return isinstance(exc, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(exc)


@dataclass
class Mirror:
    """镜像：把某个官方主机重写到镜像基址。"""

    name: str
    enabled: bool = True
    hosts: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Mirror":
        hosts = {}
        for host, rule in (data.get("hosts") or {}).items():
            if isinstance(rule, str):
                rule = [rule, ""]
            hosts[str(host).lower()] = [str(rule[0]), str(rule[1] if len(rule) > 1 else "")]
        return cls(name=str(data.get("name") or "unnamed"), enabled=bool(data.get("enabled", True)), hosts=hosts)

    def rewrite(self, url: str) -> str | None:
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            return None
        rule = self.hosts.get((parts.hostname or "").lower())
        if not rule:
            return None
        base, prefix = rule
        base = base.rstrip("/")
        prefix = prefix.rstrip("/")
        path = (prefix + parts.path) if prefix else parts.path
        out = base + path
        if parts.query:
            out += "?" + parts.query
        return out

    def covers(self, url: str) -> bool:
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            return False
        return host in self.hosts


@dataclass
class DownloadTask:
    url: str
    dest: Path
    sha1: str | None = None
    sha256: str | None = None
    size: int | None = None
    desc: str = ""
    optional: bool = False


@dataclass
class DownloadResult:
    task: DownloadTask
    status: str  # cached | downloaded | failed | skipped
    bytes: int = 0
    seconds: float = 0.0
    source: str = ""
    error: str = ""


class _ProgressAdapter:
    """把 util.Progress 的调用适配为可空对象。"""

    def __init__(self, progress):
        self.progress = progress

    def add_bytes(self, n: int) -> None:
        if self.progress:
            self.progress.add_bytes(n)

    def add_file(self, name: str = "") -> None:
        if self.progress:
            self.progress.add_file(name)

    def finish(self) -> None:
        if self.progress:
            self.progress.finish()


class Downloader:
    def __init__(
        self,
        cache_dir: str | os.PathLike,
        mirrors: Iterable[Mirror] = (),
        mirror_mode: str = "auto",
        workers: int = 8,
        progress: util.Progress | None = None,
        quiet: bool = False,
        retries: int = 3,
        tls_verify: str = "fallback",
    ):
        self.cache_dir = util.ensure_dir(cache_dir)
        self.mirrors = [m for m in mirrors if m.enabled]
        self.mirror_mode = mirror_mode if mirror_mode in ("auto", "off", "only") else "auto"
        self.workers = max(1, int(workers))
        self.progress = _ProgressAdapter(progress)
        self.quiet = quiet
        self.retries = max(1, retries)
        self._ssl = ssl.create_default_context()
        self._lock = threading.Lock()
        self._counter = {"bytes": 0, "files": 0}
        # 每线程复用连接：资源库几千个小文件，TLS 握手是主要开销
        self._tls = threading.local()
        # 连接空闲超过该秒数就丢弃（服务器侧 keep-alive 超时通常很短）
        self.conn_ttl = float(os.environ.get("MCDECENT_CONN_TTL", "3.0"))
        # 学习到的"每个主机的最佳来源"，避免每次都先撞失败的镜像
        self._pref: dict[str, str] = {}
        # 被限流/拒绝的来源（URL -> 解封时间）
        self._blocked: dict[str, float] = {}
        # TLS 校验策略：strict=严格；fallback=证书链无法验证时对"有摘要校验的内容"降级重试
        self.tls_verify = tls_verify if tls_verify in ("strict", "fallback") else "fallback"
        self._ssl_insecure = ssl._create_unverified_context()
        self._tls_warned = False

    def _remember_source(self, url: str, source: str) -> None:
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            return
        with self._lock:
            self._pref[host] = source

    def _block(self, candidate: str, seconds: float = 90.0) -> None:
        """对返回 429/403 的来源做短期拉黑，避免整批文件反复撞限流。"""
        with self._lock:
            self._blocked[candidate] = time.time() + seconds

    def _is_blocked(self, candidate: str) -> bool:
        with self._lock:
            until = self._blocked.get(candidate, 0.0)
        return time.time() < until

    # ------------------------------------------------------------ URL 解析
    def resolve_urls(self, url: str) -> list[str]:
        """返回按优先级排序的候选 URL（去重）。"""
        candidates: list[str] = []
        if self.mirror_mode == "off":
            return [url]
        if self.mirror_mode == "only":
            for m in self.mirrors:
                rewritten = m.rewrite(url)
                if rewritten:
                    candidates.append(rewritten)
            return candidates or [url]
        # auto：镜像优先（镜像通常对国内更友好），官方兜底
        for m in self.mirrors:
            rewritten = m.rewrite(url)
            if rewritten:
                candidates.append(rewritten)
        candidates.append(url)
        seen, out = set(), []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                out.append(c)
        # 若已学到该主机的最佳来源，提到最前面
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        preferred = self._pref.get(host)
        if preferred and preferred in out:
            out.remove(preferred)
            out.insert(0, preferred)
        # 被限流的来源排到最后（仍作为最后手段）
        out.sort(key=lambda c: 1 if self._is_blocked(c) else 0)
        return out

    # ------------------------------------------------------------ 传输（HTTP keep-alive）
    def _conns(self) -> dict:
        conns = getattr(self._tls, "conns", None)
        if conns is None:
            conns = self._tls.conns = {}
        return conns

    def _request(self, url: str, *, head: bool = False, timeout: float | None = None, redirects: int = 5,
                 insecure: bool = False):
        """发起请求，返回 (response, conn, pool_key)。

        连接按 (scheme, host, port) 在当前线程复用；读完响应后调用 ``_put`` 归还，
        异常路径调用 ``_discard`` 丢弃。资源库有数千个小文件，复用连接能省掉大量 TLS 握手。
        ``insecure=True`` 时使用不校验证书的上下文（仅在证书链无法验证且内容有摘要校验时降级使用）。
        """
        context = self._ssl_insecure if insecure else self._ssl
        for _ in range(redirects + 1):
            parts = urllib.parse.urlsplit(url)
            scheme = (parts.scheme or "https").lower()
            host = parts.hostname
            if not host:
                raise ValueError(f"非法 URL：{url}")
            key = (scheme, host, parts.port)
            pooled = self._conns().pop(key, None)
            conn = None
            if pooled is not None:
                candidate_conn, last_used = pooled
                if time.time() - last_used <= self.conn_ttl:
                    conn = candidate_conn
                else:
                    self._discard(candidate_conn)
            if conn is None:
                if scheme == "https":
                    conn = http.client.HTTPSConnection(host, parts.port, timeout=timeout or READ_TIMEOUT, context=context)
                else:
                    conn = http.client.HTTPConnection(host, parts.port, timeout=timeout or READ_TIMEOUT)
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            try:
                conn.request("HEAD" if head else "GET", path, headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Encoding": "identity",
                })
                resp = conn.getresponse()
            except Exception:
                self._discard(conn)
                raise
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                try:
                    resp.read()
                finally:
                    self._put(conn, key)
                if not location:
                    raise IOError(f"重定向缺少 Location：{url}")
                url = urllib.parse.urljoin(url, location)
                continue
            if resp.status >= 400:
                try:
                    body = resp.read(256)
                except Exception:  # noqa: BLE001
                    body = b""
                self._discard(conn)
                raise IOError(f"HTTP {resp.status} {resp.reason}：{url} ({body[:80]!r})")
            return resp, conn, key
        raise IOError(f"重定向次数过多：{url}")

    def _put(self, conn, key) -> None:
        self._conns()[key] = (conn, time.time())

    def _discard(self, conn) -> None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _warn_tls(self) -> None:
        if not self._tls_warned:
            self._tls_warned = True
            LOG.warning(
                "无法验证服务器证书链（常见于系统根证书过旧的 Windows）。"
                "已对带 SHA 摘要校验的内容降级重试；内容完整性仍由摘要保证。"
                "如需强制严格校验：mcdecent config set tls_verify strict"
            )

    # ------------------------------------------------------------ 简单请求
    def fetch_bytes(self, url: str, *, limit: int | None = None, desc: str = "") -> bytes:
        errors: list[str] = []
        for attempt in range(self.retries):
            for candidate in self.resolve_urls(url):
                resp = conn = key = None
                try:
                    resp, conn, key = self._request(candidate)
                    data = resp.read()
                    self._put(conn, key)
                    return data[:limit] if limit else data
                except Exception as exc:  # noqa: BLE001 - 网络错误种类繁多
                    if conn is not None:
                        self._discard(conn)
                    errors.append(f"{candidate} -> {exc!r}")
                    LOG.debug("请求失败 %s：%r", candidate, exc)
                    if _is_tls_error(exc) and self.tls_verify != "strict":
                        r2 = c2 = k2 = None
                        try:
                            r2, c2, k2 = self._request(candidate, insecure=True)
                            data = r2.read()
                            self._put(c2, k2)
                            self._warn_tls()
                            return data[:limit] if limit else data
                        except Exception as exc2:  # noqa: BLE001
                            if c2 is not None:
                                self._discard(c2)
                            errors.append(f"{candidate}（不校验 TLS） -> {exc2!r}")
            if attempt + 1 < self.retries:
                time.sleep(0.6 * (attempt + 1))
        raise DownloadError(f"无法获取 {desc or url}", url, errors)

    def head_ok(self, url: str, timeout: float = 15) -> tuple[bool, str]:
        resp = conn = key = None
        try:
            resp, conn, key = self._request(url, head=True, timeout=timeout)
            self._put(conn, key)
            return 200 <= resp.status < 400, f"HTTP {resp.status}"
        except Exception as exc:  # noqa: BLE001
            if conn is not None:
                self._discard(conn)
            return False, repr(exc)[:120]

    def fetch_json(self, url: str, *, cache_path: str | os.PathLike | None = None, max_age: float | None = None, desc: str = "") -> dict:
        cache = Path(cache_path) if cache_path else None
        if cache and cache.is_file():
            age = time.time() - cache.stat().st_mtime
            if max_age is None or age <= max_age:
                data = util.read_json(cache)
                if isinstance(data, dict):
                    return data
        try:
            raw = self.fetch_bytes(url, desc=desc or url)
            data = json.loads(raw.decode("utf-8"))
        except DownloadError:
            if cache and cache.is_file():
                data = util.read_json(cache)
                if isinstance(data, dict):
                    LOG.warning("网络不可用，使用缓存元数据：%s", cache)
                    return data
            raise
        if cache:
            util.atomic_write_bytes(cache, raw)
        return data

    # ------------------------------------------------------------ 下载
    def download_one(self, task: DownloadTask) -> DownloadResult:
        start = time.time()
        dest = Path(task.dest)
        if self.progress.progress is not None:
            self.progress.progress.total_files += 1
            if task.size:
                self.progress.progress.total_bytes = (self.progress.progress.total_bytes or 0) + task.size
        # 已存在且校验通过 -> 直接复用（本地优先）
        if dest.is_file():
            if self._verify(dest, task.sha256, task.sha1):
                self.progress.add_bytes(dest.stat().st_size)
                self.progress.add_file(task.desc)
                self._bump(dest.stat().st_size)
                return DownloadResult(task, "cached", dest.stat().st_size, time.time() - start)
            LOG.debug("校验失败，重新下载：%s", dest)
            try:
                dest.unlink()
            except OSError:
                pass
        util.ensure_dir(dest.parent)
        errors: list[str] = []
        for attempt in range(self.retries):
            urls = self.resolve_urls(task.url)
            for candidate in urls:
                try:
                    size = self._stream(candidate, dest, task)
                    self._bump(size)
                    self.progress.add_file(task.desc)
                    self._remember_source(task.url, candidate)
                    return DownloadResult(task, "downloaded", size, time.time() - start, source=candidate)
                except _HashMismatch as exc:
                    errors.append(f"{candidate} -> 摘要不符 {exc}")
                    LOG.warning("摘要不符，换源重试：%s", candidate)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{candidate} -> {exc!r}")
                    LOG.debug("下载失败 %s：%r", candidate, exc)
                    if "HTTP 429" in str(exc) or "HTTP 403" in str(exc):
                        self._block(candidate)
                    if _is_tls_error(exc) and self.tls_verify != "strict" and (task.sha1 or task.sha256):
                        # 证书链无法验证：内容有摘要校验，可安全降级（摘要仍会拦住篡改）
                        try:
                            size = self._stream(candidate, dest, task, insecure=True)
                            self._bump(size)
                            self.progress.add_file(task.desc)
                            self._remember_source(task.url, candidate)
                            self._warn_tls()
                            return DownloadResult(task, "downloaded", size, time.time() - start, source=candidate)
                        except Exception as retry_exc:  # noqa: BLE001
                            errors.append(f"{candidate}（不校验 TLS） -> {retry_exc!r}")
                            LOG.debug("降级 TLS 仍失败 %s：%r", candidate, retry_exc)
                    if _is_conn_error(exc):
                        # 复用了空闲过久的 keep-alive 连接，立即用新连接再试一次
                        try:
                            size = self._stream(candidate, dest, task)
                            self._bump(size)
                            self.progress.add_file(task.desc)
                            self._remember_source(task.url, candidate)
                            return DownloadResult(task, "downloaded", size, time.time() - start, source=candidate)
                        except Exception as retry_exc:  # noqa: BLE001
                            errors.append(f"{candidate}（重连） -> {retry_exc!r}")
                            LOG.debug("重连仍失败 %s：%r", candidate, retry_exc)
            if attempt + 1 < self.retries:
                time.sleep(0.8 * (attempt + 1))
        msg = f"下载失败：{task.desc or task.url}"
        if task.optional:
            return DownloadResult(task, "skipped", 0, time.time() - start, error=msg + " | " + "; ".join(errors[-3:]))
        return DownloadResult(task, "failed", 0, time.time() - start, error=msg + " | " + "; ".join(errors[-3:]))

    def _stream(self, url: str, dest: Path, task: DownloadTask, *, insecure: bool = False) -> int:
        tmp = dest.with_name(f".{dest.name}.part")
        h1, h2 = hashlib.sha1(), hashlib.sha256()
        written = 0
        resp = conn = key = None
        try:
            resp, conn, key = self._request(url, insecure=insecure)
            expected = int(resp.headers.get("Content-Length") or 0) or task.size or 0
            with open(tmp, "wb") as fh:
                while True:
                    block = resp.read(util.CHUNK)
                    if not block:
                        break
                    fh.write(block)
                    h1.update(block)
                    h2.update(block)
                    written += len(block)
                    self.progress.add_bytes(len(block))
                    if expected and written > expected:
                        raise IOError("服务器返回长度超过预期")
            self._put(conn, key)
            if task.size and written != task.size:
                raise IOError(f"长度不符：期望 {task.size}，实际 {written}")
            if task.sha1 and h1.hexdigest() != task.sha1.lower():
                raise _HashMismatch(f"sha1 期望 {task.sha1}，实际 {h1.hexdigest()}")
            if task.sha256 and h2.hexdigest() != task.sha256.lower():
                raise _HashMismatch(f"sha256 期望 {task.sha256}，实际 {h2.hexdigest()}")
            os.replace(tmp, dest)
            return written
        except BaseException:
            if conn is not None:
                self._discard(conn)
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _verify(self, path: Path, sha256: str | None, sha1: str | None) -> bool:
        if not sha256 and not sha1:
            return True
        try:
            if sha256:
                return util.sha256_file(path) == sha256.lower()
            return util.sha1_file(path) == (sha1 or "").lower()
        except OSError:
            return False

    def _bump(self, n: int) -> None:
        with self._lock:
            self._counter["bytes"] += n
            self._counter["files"] += 1

    def download_all(self, tasks: Iterable[DownloadTask], *, desc: str = "") -> list[DownloadResult]:
        tasks = list(tasks)
        if not tasks:
            return []
        total_bytes = sum(t.size or 0 for t in tasks)
        if self.progress.progress is not None:
            self.progress.progress.total_files += len(tasks)
            if total_bytes:
                self.progress.progress.total_bytes = (self.progress.progress.total_bytes or 0) + total_bytes
        LOG.info("开始下载 %s：%d 个文件%s", desc or "资源", len(tasks), f"，约 {util.human_size(total_bytes)}" if total_bytes else "")
        results: list[DownloadResult] = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.download_one, t): t for t in tasks}
            for fut in as_completed(futures):
                task = futures[fut]
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001
                    res = DownloadResult(task, "failed", error=repr(exc))
                if res.status == "failed":
                    LOG.error("✗ %s：%s", task.desc or task.url, res.error)
                elif res.status == "skipped":
                    LOG.warning("跳过（可选）：%s", task.desc or task.url)
                results.append(res)
        failed = [r for r in results if r.status == "failed"]
        if failed:
            raise DownloadError(f"{len(failed)} 个文件下载失败", errors=[r.error for r in failed[:5]])
        return results
