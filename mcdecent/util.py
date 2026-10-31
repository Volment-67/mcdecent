"""通用工具：哈希、原子写入、安全解压、体积/时间格式化、日志。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Iterable, Iterator

LOG = logging.getLogger("mcdecent")
CHUNK = 1 << 20  # 1 MiB

# --------------------------------------------------------------------------- 哈希


def _hash_file(path: os.PathLike | str, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def sha1_file(path: os.PathLike | str) -> str:
    return _hash_file(path, "sha1")


def sha256_file(path: os.PathLike | str) -> str:
    return _hash_file(path, "sha256")


def sha512_file(path: os.PathLike | str) -> str:
    return _hash_file(path, "sha512")


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: os.PathLike | str, algo: str = "sha256") -> str:
    return _hash_file(path, algo)


def verify_file(path: os.PathLike | str, algo: str | None, digest: str | None) -> bool:
    """校验文件摘要；algo/digest 缺失时视为通过（调用方自行决定策略）。"""
    if not algo or not digest:
        return True
    p = Path(path)
    if not p.is_file():
        return False
    try:
        return _hash_file(p, algo).lower() == digest.lower()
    except OSError:
        return False


# --------------------------------------------------------------------------- 文件系统


def ensure_dir(path: os.PathLike | str) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write_bytes(path: os.PathLike | str, data: bytes) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def atomic_write_text(path: os.PathLike | str, text: str, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, text.encode(encoding))


def read_json(path: os.PathLike | str, default=None):
    p = Path(path)
    if not p.is_file():
        return default
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: os.PathLike | str, data) -> Path:
    return atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def remove_tree(path: os.PathLike | str) -> None:
    p = Path(path)
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    elif p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def safe_extract_zip(
    zip_path: os.PathLike | str,
    dest: os.PathLike | str,
    members: Iterable[str] | None = None,
    skip_meta: bool = True,
) -> list[str]:
    """安全解压 zip：拒绝路径穿越、跳过符号链接与 META-INF。

    返回实际写出的相对路径列表。
    """
    dest_p = Path(dest).resolve()
    ensure_dir(dest_p)
    written: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        names = list(members) if members is not None else zf.namelist()
        for name in names:
            if not name or name.endswith("/"):
                continue
            norm = name.replace("\\", "/")
            if skip_meta and norm.upper().startswith("META-INF/"):
                continue
            if norm.startswith("/") or ".." in Path(norm).parts:
                LOG.warning("跳过可疑压缩包路径: %s", name)
                continue
            info = zf.getinfo(name)
            # 跳过符号链接/目录条目
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                LOG.warning("跳过符号链接: %s", name)
                continue
            target = (dest_p / norm).resolve()
            if target != dest_p and dest_p not in target.parents:
                LOG.warning("跳过越界路径: %s", name)
                continue
            ensure_dir(target.parent)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, CHUNK)
            written.append(norm)
    return written


def zip_dir(src: os.PathLike | str, zip_path: os.PathLike | str, base: os.PathLike | str | None = None) -> Path:
    """把目录打包成 zip（用于整合包分发）。"""
    src_p = Path(src).resolve()
    base_p = Path(base).resolve() if base else src_p
    out = Path(zip_path)
    ensure_dir(out.parent)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _dirs, files in os.walk(src_p):
            for fname in files:
                full = Path(root) / fname
                rel = full.relative_to(base_p)
                zf.write(full, rel.as_posix())
    return out


# --------------------------------------------------------------------------- 格式化


def human_size(num: float | int | None) -> str:
    if num is None:
        return "?"
    n = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def human_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds != seconds:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# --------------------------------------------------------------------------- 日志


def setup_logging(log_file: os.PathLike | str | None = None, verbose: bool = False, quiet: bool = False) -> None:
    """配置根日志器：控制台 + 可选文件。"""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("[%(levelname)s] %(message)s")

    if not quiet:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(fmt)
        root.addHandler(console)

    if log_file:
        ensure_dir(Path(log_file).parent)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(fh)


class Progress:
    """线程安全的单行进度汇报器（终端友好，非 tty 时降级为低频打印）。"""

    def __init__(self, total_files: int = 0, total_bytes: int | None = None, enabled: bool = True):
        self.enabled = enabled and not os.environ.get("MCDECENT_NO_PROGRESS")
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.done_files = 0
        self.done_bytes = 0
        self.start = time.time()
        self._last_print = 0.0
        self._last_bytes = 0
        self._lock = __import__("threading").RLock()
        self._tty = bool(getattr(__import__("sys").stdout, "isatty", lambda: False)())
        self.current = ""

    def add_file(self, name: str = "", size: int = 0) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.done_files += 1
            self.done_bytes += size
            if name:
                self.current = name
            self._maybe_print(force=False)

    def add_bytes(self, n: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.done_bytes += n
            self._maybe_print(force=False)

    def _maybe_print(self, force: bool) -> None:
        now = time.time()
        if not force and now - self._last_print < 0.2:
            return
        self._last_print = now
        elapsed = max(now - self.start, 1e-6)
        speed = self.done_bytes / elapsed
        pct = ""
        if self.total_bytes:
            pct = f" ({self.done_bytes * 100 / self.total_bytes:5.1f}%)"
        line = f"{self.done_files}/{self.total_files or '?'} 文件 | {human_size(self.done_bytes)}"
        if self.total_bytes:
            line += f"/{human_size(self.total_bytes)}"
        line += f"{pct} | {human_size(speed)}/s"
        if self.current:
            line += f" | {self.current[:60]}"
        if self._tty:
            print("\r" + line + " " * 10, end="", flush=True)
        else:
            if force or now - self._last_print >= 5:
                print(line, flush=True)

    def finish(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._maybe_print(force=True)
            if self._tty:
                print()
            elapsed = max(time.time() - self.start, 1e-6)
            LOG.info("传输完成：%d 个文件，%s，用时 %s（平均 %s/s）",
                     self.done_files, human_size(self.done_bytes),
                     human_time(elapsed), human_size(self.done_bytes / elapsed))


def short_id(text: str, n: int = 12) -> str:
    return text if len(text) <= n else text[:n] + "…"


def chunked(seq: list, size: int) -> Iterator[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
