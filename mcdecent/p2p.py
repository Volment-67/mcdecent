"""P2P 层：只读 CAS HTTP 节点 + UDP 多播发现 + 拉取客户端。

设计要点：
  * **服务端只读**：只暴露 CAS 里已有的对象，URL 里除 SHA-256 之外不接受任何输入，
    因此不存在路径穿越/写操作/命令执行面；
  * **内容即地址**：``/cas/<sha256>`` 返回的字节必然能被对方用同一个摘要校验，
    传输层不可信也不影响正确性；
  * **零配置发现**：同一局域网内通过 UDP 多播 239.255.77.77:25578 互相通告，
    多播被禁用/无网卡时静默降级（记一条 warning，返回空列表，绝不抛异常），
    此时仍可用 ``--peer host:port`` 手工连；
  * **无中心**：任何实例既是服务端也是客户端，节点列表只是本地缓存（peers.json）。
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import platform
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import util
from .cas import ContentStore
from .util import LOG

PEER_PROTOCOL = 1
DEFAULT_MULTICAST_GROUP = "239.255.77.77"
DEFAULT_MULTICAST_PORT = 25578
MAX_ANNOUNCE_BYTES = 1200  # 单行多播报文上限，超过就截断名字
MAX_PEERS = 256  # peers.json 最多保留的节点数
HTTP_CHUNK = 64 * 1024
USER_AGENT = "mcdecent/0.1 (decentralized minecraft launcher; p2p)"

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


class PeerError(RuntimeError):
    """P2P 操作失败（无可用节点、摘要不符、清单非法等）。"""


# --------------------------------------------------------------------------- 身份


def machine_name() -> str:
    """本机显示名（节点名缺省值）。"""
    name = platform.node() or ""
    return name.strip() or "mcdecent"


def peer_id(cfg) -> str:
    """本机节点 ID：首次生成 uuid4 并持久化到 ``<root>/peer_id``，之后保持不变。

    持久化失败（只读目录等）时退化为基于根路径/机器名的哈希，仍保证进程内稳定。
    """
    path = Path(cfg.root) / "peer_id"
    try:
        text = path.read_text(encoding="utf-8").strip().lower()
        if _HEX32_RE.match(text):
            return text
    except OSError:
        pass
    value = uuid.uuid4().hex
    try:
        util.ensure_dir(path.parent)
        util.atomic_write_text(path, value + "\n")
    except OSError as exc:  # 只读环境：仍是稳定 ID，只是不跨进程
        LOG.debug("peer_id 持久化失败，使用派生 ID：%r", exc)
        seed = f"{cfg.root}|{machine_name()}|{uuid.uuid4().hex}".encode("utf-8", "replace")
        value = hashlib.sha256(seed).hexdigest()[:32]
    return value


def local_ip() -> str:
    """探测本机对外网卡地址（不真的发包）；失败退回 127.0.0.1。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((DEFAULT_MULTICAST_GROUP, 9))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _share_conf(cfg) -> dict:
    share = cfg.get("share")
    return share if isinstance(share, dict) else {}


# --------------------------------------------------------------------------- 节点地址


def parse_peer(spec) -> tuple[str, int] | None:
    """把 ``host:port`` / ``http://host:port`` / dict 解析成 (host, port)；非法返回 None。"""
    if isinstance(spec, dict):
        host = spec.get("host")
        port = spec.get("port")
        if host and port:
            try:
                port_i = int(port)
            except (TypeError, ValueError):
                return None
            return _valid_host_port(str(host), port_i)
        url = spec.get("url")
        return parse_peer(url) if url else None
    text = str(spec or "").strip()
    if not text:
        return None
    if "://" in text:
        try:
            text = urllib.parse.urlsplit(text).netloc or ""
        except ValueError:
            return None
    if not text:
        return None
    host, sep, port_text = text.rpartition(":")
    if not sep:
        return None
    try:
        port = int(port_text)
    except ValueError:
        return None
    return _valid_host_port(host, port)


def _valid_host_port(host: str, port: int) -> tuple[str, int] | None:
    host = (host or "").strip().strip("[]")
    if not host or not (0 < port < 65536):
        return None
    return host, port


def normalize_peer(item) -> dict | None:
    """把各种来源（字符串/dict）归一成统一的节点字典；非法返回 None。"""
    if isinstance(item, str):
        host_port = parse_peer(item)
        if not host_port:
            return None
        host, port = host_port
        return {
            "peer_id": "",
            "name": f"{host}:{port}",
            "host": host,
            "port": port,
            "url": f"http://{host}:{port}",
            "objects": None,
            "last_seen": None,
        }
    if not isinstance(item, dict):
        return None
    host_port = parse_peer(item)
    if not host_port:
        return None
    host, port = host_port
    pid = str(item.get("peer_id") or "").strip().lower()
    if pid and not _HEX32_RE.match(pid):
        pid = ""
    objects = item.get("objects")
    try:
        objects = int(objects) if objects is not None else None
    except (TypeError, ValueError):
        objects = None
    name = str(item.get("name") or "").strip() or (pid[:8] if pid else f"{host}:{port}")
    return {
        "peer_id": pid,
        "name": name[:64],
        "host": host,
        "port": port,
        "url": str(item.get("url") or f"http://{host}:{port}").rstrip("/"),
        "objects": objects,
        "last_seen": str(item.get("last_seen") or "").strip() or None,
    }


def _peer_key(peer: dict) -> str:
    return peer.get("peer_id") or peer.get("url") or f"{peer.get('host')}:{peer.get('port')}"


def _merge_peer(bucket: dict[str, dict], peer: dict) -> None:
    key = _peer_key(peer)
    old = bucket.get(key)
    if old is None:
        bucket[key] = peer
        return
    # 用新信息补齐旧条目的空字段（旧的 last_seen 只在新的缺失时保留）
    for name, value in peer.items():
        if value not in (None, "", 0) or name not in old:
            old[name] = value


def mcast_cache_file(cfg) -> Path:
    """多播发现结果的本地缓存文件。"""
    return Path(cfg.root) / "mcast_peers.json"


def _load_cached_peers(cfg) -> list[dict]:
    data = util.read_json(mcast_cache_file(cfg), {})
    items = data.get("peers") if isinstance(data, dict) else data
    out = []
    for item in items or []:
        peer = normalize_peer(item)
        if peer:
            out.append(peer)
    return out


def _cache_peers(cfg, peers: list[dict]) -> None:
    """把多播发现的节点并入缓存（按 peer_id/url 去重，最多 MAX_PEERS 条）。"""
    bucket: dict[str, dict] = {}
    for peer in _load_cached_peers(cfg):
        _merge_peer(bucket, peer)
    for peer in peers:
        normalized = normalize_peer(peer)
        if normalized:
            normalized["last_seen"] = normalized.get("last_seen") or util.now_iso()
            _merge_peer(bucket, normalized)
    items = list(bucket.values())
    items.sort(key=lambda p: p.get("last_seen") or "", reverse=True)
    try:
        util.write_json(
            mcast_cache_file(cfg),
            {"format": "mcdecent-mcast-cache/1", "updated": util.now_iso(), "peers": items[:MAX_PEERS]},
        )
    except OSError as exc:
        LOG.debug("多播缓存写入失败：%r", exc)


def known_peers(cfg) -> list[dict]:
    """已知节点列表：peers.json（历史/手工）+ 多播缓存 + 配置里的 share.peers。"""
    bucket: dict[str, dict] = {}
    for peer in _load_cached_peers(cfg):  # 多播最新的排前面
        _merge_peer(bucket, peer)
    data = util.read_json(cfg.peers_file, {})
    items = data.get("peers") if isinstance(data, dict) else data
    for item in items or []:
        peer = normalize_peer(item)
        if peer:
            _merge_peer(bucket, peer)
    for spec in _share_conf(cfg).get("peers") or []:
        peer = normalize_peer(spec)
        if peer:
            peer.setdefault("source", "config")
            bucket.setdefault(_peer_key(peer), peer)
    result = list(bucket.values())
    result.sort(key=lambda p: (p.get("last_seen") or "", p.get("name") or ""), reverse=True)
    return result


def remember_peer(cfg, peer: dict) -> dict:
    """记住一个节点（写 peers.json，去重并限制总数）。"""
    normalized = normalize_peer(peer)
    if not normalized:
        raise PeerError(f"非法节点信息：{peer!r}")
    normalized["last_seen"] = normalized.get("last_seen") or util.now_iso()
    data = util.read_json(cfg.peers_file, {})
    items = data.get("peers") if isinstance(data, dict) else data
    bucket: dict[str, dict] = {}
    _merge_peer(bucket, normalized)
    for item in items or []:
        old = normalize_peer(item)
        if old and _peer_key(old) != _peer_key(normalized):
            _merge_peer(bucket, old)
    merged = list(bucket.values())[:MAX_PEERS]
    try:
        util.write_json(
            cfg.peers_file,
            {"format": "mcdecent-peers/1", "updated": util.now_iso(), "peers": merged},
        )
    except OSError as exc:
        raise PeerError(f"写入节点列表失败：{exc}") from exc
    return normalized


# --------------------------------------------------------------------------- 服务端


class _PeerHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):  # noqa: ARG002
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            LOG.debug("P2P 连接被对端中断：%r", exc)
            return
        LOG.warning("P2P 请求处理异常：%r", exc)


def _make_handler(node: "PeerServer"):
    """为给定节点生成 HTTP 处理器类（处理器只读，全部路由 GET）。"""

    class _PeerHandler(http.server.BaseHTTPRequestHandler):
        server_version = "mcdecent-peer/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:  # 静音 access log
            LOG.debug("P2P HTTP %s - " + fmt, self.address_string(), *args)

        def do_GET(self) -> None:  # noqa: N802
            node.handle_get(self)

        def do_HEAD(self) -> None:  # noqa: N802
            node.reject_method(self)

        def do_POST(self) -> None:  # noqa: N802
            node.reject_method(self)

        def do_PUT(self) -> None:  # noqa: N802
            node.reject_method(self)

        def do_DELETE(self) -> None:  # noqa: N802
            node.reject_method(self)

        def do_PATCH(self) -> None:  # noqa: N802
            node.reject_method(self)

        def do_OPTIONS(self) -> None:  # noqa: N802
            node.reject_method(self)

    return _PeerHandler


class PeerServer:
    """只读 CAS HTTP 节点。

    路由：
        GET /health          节点身份与共享统计
        GET /index           {"objects": {sha256: size}, "packs": [...]}
        GET /cas/<sha256>    对象原始字节
        GET /pack/<sha256>   整合包清单 JSON（对象必须能解析成 JSON 对象）
        GET /peers           已知节点
    """

    def __init__(self, cfg, *, host=None, port=None, name=None, rate_limit_kbps=None):
        self.cfg = cfg
        self.cas = ContentStore(cfg.cas_dir)
        share = _share_conf(cfg)
        self.host = str(host or share.get("bind") or "0.0.0.0")
        requested_port = int(port if port is not None else share.get("port", 25577))
        self._requested_port = requested_port
        self.port = requested_port
        self.name = str(name or share.get("name") or machine_name())
        self.peer_id = peer_id(cfg)
        self.rate_limit_kbps = float(rate_limit_kbps) if rate_limit_kbps else None
        self._httpd: _PeerHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.started_at: str | None = None

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """启动后台服务线程（幂等）。端口传 0 时由系统分配，实际端口见 ``port``。"""
        if self._httpd is not None:
            return
        self.cfg.ensure_layout()
        httpd = _PeerHTTPServer((self.host, self._requested_port), _make_handler(self))
        self._httpd = httpd
        self.port = int(httpd.server_address[1])
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.1},
            name=f"mcdecent-peer-http-{self.port}",
            daemon=True,
        )
        self._thread.start()
        self.started_at = util.now_iso()
        LOG.info("P2P 节点已启动：%s（peer_id=%s，绑定 %s:%s）", self.url, self.peer_id, self.host, self.port)

    def stop(self) -> None:
        """停止服务并释放端口（幂等，可在 finally/夹具里安全调用）。"""
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception as exc:  # noqa: BLE001 - 关停不应抛给调用方
                LOG.debug("P2P 服务 shutdown 异常：%r", exc)
            try:
                httpd.server_close()
            except Exception as exc:  # noqa: BLE001
                LOG.debug("P2P 服务 server_close 异常：%r", exc)
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    @property
    def advertise_host(self) -> str:
        """对外通告的主机名：绑定 0.0.0.0 时探测本机内网 IP。"""
        if self.host in ("", "0.0.0.0", "::", "[::]"):
            return local_ip()
        return self.host

    @property
    def url(self) -> str:
        return f"http://{self.advertise_host}:{self.port}"

    # ------------------------------------------------------------ 数据视图
    def registry(self):
        from .registry import PackRegistry

        return PackRegistry(self.cfg)

    def packs(self) -> list[dict]:
        try:
            entries = self.registry().list()
        except Exception as exc:  # noqa: BLE001 - 注册表损坏不应让节点挂掉
            LOG.debug("读取注册表失败：%r", exc)
            return []
        return [
            {"name": e.get("name"), "version": e.get("version"), "sha256": e.get("sha256")}
            for e in entries
        ]

    def health(self) -> dict:
        stat = self.cas.stat()
        return {
            "protocol": PEER_PROTOCOL,
            "peer_id": self.peer_id,
            "name": self.name,
            "version": _version(),
            "objects": stat["objects"],
            "bytes": stat["bytes"],
            "packs": len(self.packs()),
            "url": self.url,
        }

    def index(self) -> dict:
        return {
            "protocol": PEER_PROTOCOL,
            "peer_id": self.peer_id,
            "objects": self.cas.index(),
            "packs": self.packs(),
        }

    # ------------------------------------------------------------ HTTP 实现
    def handle_get(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        try:
            path = urllib.parse.urlsplit(handler.path).path
        except ValueError:
            return self._send_json(handler, 400, {"error": "非法请求路径"})
        if path in ("/health", "/healthz"):
            return self._send_json(handler, 200, self.health())
        if path == "/index":
            return self._send_json(handler, 200, self.index())
        if path == "/peers":
            return self._send_json(handler, 200, {"peers": known_peers(self.cfg)})
        if path.startswith("/cas/"):
            return self._serve_cas(handler, path[len("/cas/"):])
        if path.startswith("/pack/"):
            return self._serve_pack(handler, path[len("/pack/"):])
        return self._send_json(handler, 404, {"error": f"未知路径：{path}"})

    def reject_method(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        """非 GET 一律 405：本节点是纯只读服务。"""
        handler.close_connection = True
        self._send_json(handler, 405, {"error": "只读节点：仅支持 GET", "protocol": PEER_PROTOCOL}, {"Allow": "GET"})

    def _digest_from_tail(self, tail: str) -> str | None:
        """从 URL 尾部取出 SHA-256 并严格校验（只允许 64 位小写 hex，拒绝大小写混写）。"""
        digest = urllib.parse.unquote((tail or "").split("?")[0].split("#")[0]).strip()
        return digest if ContentStore.is_hash(digest) else None

    def _serve_cas(self, handler, tail: str) -> None:
        digest = self._digest_from_tail(tail)
        if digest is None:
            return self._send_json(handler, 400, {"error": "非法 SHA-256 摘要（需 64 位小写十六进制）"})
        path = self.cas.get(digest)
        if path is None:
            return self._send_json(handler, 404, {"error": "对象不存在", "sha256": digest})
        try:
            size = path.stat().st_size
        except OSError:
            return self._send_json(handler, 404, {"error": "对象不可读", "sha256": digest})
        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Content-Length", str(size))
        handler.send_header("X-Mcdecent-Digest", digest)
        handler.end_headers()
        self._stream_file(handler, path, size)

    def _stream_file(self, handler, path: Path, size: int) -> None:
        """按块发送文件，可选限速（KB/s → 字节/秒）。"""
        bytes_per_sec = (self.rate_limit_kbps * 1000.0 / 8.0) if self.rate_limit_kbps else 0.0
        sent = 0
        try:
            with open(path, "rb") as fh:
                while sent < size:
                    chunk = fh.read(HTTP_CHUNK)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    sent += len(chunk)
                    if bytes_per_sec > 0:
                        time.sleep(len(chunk) / bytes_per_sec)
        except (BrokenPipeError, ConnectionResetError) as exc:
            LOG.debug("对象传输中断（对端关闭）：%r", exc)

    def _serve_pack(self, handler, tail: str) -> None:
        digest = self._digest_from_tail(tail)
        if digest is None:
            return self._send_json(handler, 400, {"error": "非法 SHA-256 摘要（需 64 位小写十六进制）"})
        path = self.cas.get(digest)
        if path is None:
            return self._send_json(handler, 404, {"error": "清单不存在", "sha256": digest})
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._send_json(handler, 404, {"error": "该对象不是合法 JSON 清单", "sha256": digest})
        if not isinstance(data, dict):
            return self._send_json(handler, 404, {"error": "该对象不是 JSON 对象（整合包清单）", "sha256": digest})
        return self._send_json(handler, 200, data)

    def _send_json(self, handler, status: int, payload, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or {}).items():
                handler.send_header(key, value)
            handler.end_headers()
            if getattr(handler, "command", "GET") != "HEAD":
                handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _version() -> str:
    from . import __version__

    return __version__


# --------------------------------------------------------------------------- 发现


def _open_multicast_socket(group: str, port: int, *, timeout: float = 1.0) -> socket.socket:
    """打开并加入多播组的 UDP socket；失败抛 OSError（由调用方降级处理）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", port))
        membership = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)  # 只在本局域网
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)  # 同机多实例也能互相看见
        except OSError:
            pass
        sock.settimeout(timeout)
        return sock
    except OSError:
        sock.close()
        raise


class PeerDiscovery:
    """UDP 多播发现：广播本机信息、收集应答、并可后台应答别人。

    报文是单行 JSON（≤1200 字节）::

        {"mcdecent":1,"peer_id":"…","name":"…","port":25577,"objects":42,
         "url":"http://192.168.1.8:25577","kind":"announce"}

    ``port`` 是 **HTTP（TCP）端口**，多播端口由 ``multicast_port`` 决定。
    """

    def __init__(self, cfg, *, group=None, port=None, name=None, tcp_port=None, host=None):
        share = _share_conf(cfg)
        self.cfg = cfg
        self.group = str(group or share.get("multicast_group") or DEFAULT_MULTICAST_GROUP)
        self.port = int(port if port is not None else share.get("multicast_port") or DEFAULT_MULTICAST_PORT)
        self.name = str(name or share.get("name") or machine_name())
        self.peer_id = peer_id(cfg)
        self.tcp_port = int(tcp_port if tcp_port is not None else share.get("port") or 25577)
        self.advertise_host = str(host or local_ip())
        self._objects_cache: tuple[float, int] | None = None
        self._service_sock: socket.socket | None = None
        self._service_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

    # ------------------------------------------------------------ 报文
    def _object_count(self) -> int:
        """CAS 对象数量（30 秒缓存，避免每次广播都遍历目录）。"""
        now = time.time()
        if self._objects_cache and now - self._objects_cache[0] < 30.0:
            return self._objects_cache[1]
        try:
            count = sum(1 for _ in ContentStore(self.cfg.cas_dir).iter_objects())
        except OSError:
            count = 0
        self._objects_cache = (now, count)
        return count

    def announce_message(self, kind: str = "announce") -> dict:
        return {
            "mcdecent": PEER_PROTOCOL,
            "peer_id": self.peer_id,
            "name": self.name[:64],
            "port": self.tcp_port,
            "objects": self._object_count(),
            "url": f"http://{self.advertise_host}:{self.tcp_port}",
            "kind": kind,
        }

    def _encode(self, message: dict) -> bytes:
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
        while len(raw) > MAX_ANNOUNCE_BYTES and message.get("name"):
            message["name"] = str(message["name"])[: max(0, len(str(message["name"])) - 8)]
            raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
        return raw

    def _parse_packet(self, data: bytes, addr: tuple[str, int]) -> dict | None:
        """解析一条多播报文，返回归一化节点；非法/自己的包返回 None。"""
        try:
            text = data.decode("utf-8", "replace").strip()
            message = json.loads(text)
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(message, dict):
            return None
        try:
            protocol = int(message.get("mcdecent", 0))
        except (TypeError, ValueError):
            return None
        if protocol != PEER_PROTOCOL:
            return None
        pid = str(message.get("peer_id") or "").strip().lower()
        if not pid or pid == self.peer_id:  # 忽略自己的广播
            return None
        host = str(message.get("host") or addr[0])
        try:
            port = int(message.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not (0 < port < 65536):
            return None
        try:
            objects = int(message.get("objects") or 0)
        except (TypeError, ValueError):
            objects = 0
        return normalize_peer(
            {
                "peer_id": pid,
                "name": message.get("name"),
                "host": host,
                "port": port,
                "url": message.get("url"),
                "objects": max(objects, 0),
                "last_seen": util.now_iso(),
            }
        )

    # ------------------------------------------------------------ 主动发现
    def announce(self) -> dict:
        """单次广播并收集 1 秒内的应答。

        返回 ``{"group","port","sent","peers","count"}``；多播不可用时 ``peers`` 为空、
        ``sent`` 为 False，并通过 LOG.warning 说明原因（不抛异常）。
        """
        result = {"group": self.group, "port": self.port, "sent": False, "peers": [], "count": 0}
        try:
            sock = _open_multicast_socket(self.group, self.port, timeout=0.5)
        except OSError as exc:
            LOG.warning("多播不可用（%s）；跳过广播，可用 --peer 手工指定节点", exc)
            return result
        peers: dict[str, dict] = {}
        try:
            payload = self._encode(self.announce_message("announce"))
            try:
                sock.sendto(payload, (self.group, self.port))
                result["sent"] = True
            except OSError as exc:
                LOG.warning("多播发送失败（%s）；可用 --peer 手工指定节点", exc)
            deadline = time.time() + 1.0
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(MAX_ANNOUNCE_BYTES * 2)
                except socket.timeout:
                    continue
                except OSError:
                    break
                peer = self._parse_packet(data, addr)
                if peer:
                    _merge_peer(peers, peer)
        finally:
            try:
                sock.close()
            except OSError:
                pass
        found = list(peers.values())
        if found:
            _cache_peers(self.cfg, found)
        result.update({"peers": found, "count": len(found)})
        return result

    def discover(self, timeout: float = 5.0) -> list[dict]:
        """在 timeout 秒内周期性广播并收集节点，返回去重后的节点列表。

        多播不可用时记 warning 并返回 ``[]``（绝不抛异常）。
        """
        timeout = max(float(timeout), 0.0)
        peers: dict[str, dict] = {}
        try:
            sock = _open_multicast_socket(self.group, self.port, timeout=0.3)
        except OSError as exc:
            LOG.warning("多播不可用（%s）；无法自动发现节点，请用 --peer 手工指定", exc)
            return []
        try:
            payload = self._encode(self.announce_message("announce"))
            last_send = 0.0
            deadline = time.time() + timeout
            while True:
                now = time.time()
                if now - last_send >= 1.0:  # 首次立即发，之后每秒重发一次
                    try:
                        sock.sendto(payload, (self.group, self.port))
                    except OSError as exc:
                        LOG.debug("多播发送失败：%r", exc)
                    last_send = now
                if now >= deadline:
                    break
                try:
                    data, addr = sock.recvfrom(MAX_ANNOUNCE_BYTES * 2)
                except socket.timeout:
                    continue
                except OSError:
                    break
                peer = self._parse_packet(data, addr)
                if peer:
                    _merge_peer(peers, peer)
        finally:
            try:
                sock.close()
            except OSError:
                pass
        found = list(peers.values())
        if found:
            _cache_peers(self.cfg, found)
        LOG.info("多播发现完成：%d 个节点（%s:%s）", len(found), self.group, self.port)
        return found

    # ------------------------------------------------------------ 后台应答器
    def start_service(self) -> None:
        """启动后台应答线程：别人广播发现时，回一条自己的信息。失败只 warning。"""
        if self._service_thread is not None:
            return
        try:
            sock = _open_multicast_socket(self.group, self.port, timeout=0.5)
        except OSError as exc:
            LOG.warning("多播应答器启动失败（%s）；本机仍可主动发现其他节点", exc)
            return
        self._service_sock = sock
        self._stop_event = threading.Event()
        self._service_thread = threading.Thread(
            target=self._service_loop, name=f"mcdecent-peer-mcast-{self.port}", daemon=True
        )
        self._service_thread.start()
        LOG.info("多播应答器已启动（组 %s:%s）", self.group, self.port)

    def _service_loop(self) -> None:
        sock = self._service_sock
        stop_event = self._stop_event
        if sock is None or stop_event is None:
            return
        last_reply: dict[str, float] = {}
        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(MAX_ANNOUNCE_BYTES * 2)
            except socket.timeout:
                continue
            except OSError:
                break
            peer = self._parse_packet(data, addr)
            if not peer:
                continue
            try:
                message = json.loads(data.decode("utf-8", "replace"))
            except (UnicodeDecodeError, ValueError):
                continue
            if str(message.get("kind") or "announce") != "announce":
                continue  # 只回应主动查询，避免应答风暴
            key = _peer_key(peer)
            now = time.time()
            if now - last_reply.get(key, 0.0) < 2.0:
                continue  # 同一节点 2 秒内只回一次
            last_reply[key] = now
            try:
                sock.sendto(self._encode(self.announce_message("reply")), addr)
            except OSError as exc:
                LOG.debug("多播应答发送失败：%r", exc)

    def stop_service(self) -> None:
        """停止后台应答线程（幂等）。"""
        stop_event, self._stop_event = self._stop_event, None
        if stop_event is not None:
            stop_event.set()
        sock, self._service_sock = self._service_sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread, self._service_thread = self._service_thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)


# --------------------------------------------------------------------------- 客户端


def _candidate_urls(cfg, peers=None, *, include_known: bool = True) -> list[str]:
    """汇总候选节点基址：显式 peers → 配置 share.peers → 已知节点（去重保序）。"""
    urls: list[str] = []

    def add(value) -> None:
        host_port = parse_peer(value)
        if host_port:
            url = f"http://{host_port[0]}:{host_port[1]}"
        elif isinstance(value, dict) and value.get("url"):
            url = str(value["url"]).rstrip("/")
        else:
            return
        if url not in urls:
            urls.append(url)

    for item in peers or []:
        add(item)
    for item in _share_conf(cfg).get("peers") or []:
        add(item)
    if include_known:
        for peer in known_peers(cfg):
            add(peer)
    return urls


def _http_get(url: str, *, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)


def probe_peer(host, port: int | None = None, timeout: float = 5.0) -> dict | None:
    """探测节点 ``GET /health``；无响应/非本协议返回 None。"""
    if port is None:
        host_port = parse_peer(host)
        if not host_port:
            return None
        host, port = host_port
    try:
        with _http_get(f"http://{host}:{port}/health", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    peer = normalize_peer(
        {
            "peer_id": data.get("peer_id"),
            "name": data.get("name"),
            "host": host,
            "port": port,
            "url": data.get("url") or f"http://{host}:{port}",
            "objects": data.get("objects"),
            "last_seen": util.now_iso(),
        }
    )
    if not peer:
        return None
    peer["protocol"] = data.get("protocol")
    peer["version"] = data.get("version")
    return peer


def fetch_index(cfg, peer, *, timeout: float = 10.0) -> dict:
    """读取某个节点的 ``/index``（用于 ``peer pull --all-index``）。"""
    host_port = parse_peer(peer)
    if not host_port:
        raise PeerError(f"非法节点：{peer!r}")
    url = f"http://{host_port[0]}:{host_port[1]}/index"
    try:
        with _http_get(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PeerError(f"读取节点索引失败（{url}）：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("objects"), dict):
        raise PeerError(f"节点返回了非法索引（{url}）")
    return data


def fetch_blob(cfg, digest: str, *, peers=None, timeout: float = 10.0) -> Path:
    """从任意已知节点拉取一个对象，校验 SHA-256 后放入 CAS，返回本地路径。

    已在本地的对象直接返回（P2P 是"补缺"而不是"重新下载"）。
    """
    digest = (digest or "").strip().lower()
    if not ContentStore.is_hash(digest):
        raise PeerError(f"非法 SHA-256：{digest!r}")
    store = ContentStore(cfg.cas_dir)
    local = store.get(digest)
    if local is not None:
        return local
    candidates = _candidate_urls(cfg, peers)
    if not candidates:
        raise PeerError("没有可用节点：请用 --peer 指定，或先运行 mcdecent peer discover")
    util.ensure_dir(cfg.cache_dir)
    errors: list[str] = []
    for base in candidates:
        url = f"{base}/cas/{digest}"
        fd, tmp_name = tempfile.mkstemp(prefix=f".p2p-{digest[:12]}-", suffix=".part", dir=str(cfg.cache_dir))
        tmp_path = Path(tmp_name)
        try:
            hasher = hashlib.sha256()
            total = 0
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout
            ) as response:
                with os.fdopen(fd, "wb") as out:
                    for chunk in iter(lambda: response.read(HTTP_CHUNK), b""):
                        hasher.update(chunk)
                        out.write(chunk)
                        total += len(chunk)
            fd = -1  # 已交给 fdopen 关闭
            got = hasher.hexdigest()
            if got != digest:
                errors.append(f"{url}: 摘要不符（{got[:12]}…）")
                continue
            store.put_file(tmp_path, verify=False)
            LOG.info("已从 %s 拉取 %s（%s）", base, digest[:12], util.human_size(total))
            return store.path_for(digest)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"{url}: {exc}")
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
    raise PeerError("所有节点都未能提供该对象：" + "；".join(errors[:5]))


def fetch_pack(cfg, digest: str, *, peers=None, timeout: float = 10.0) -> dict:
    """获取整合包清单 JSON（先走节点 ``/pack/``，再退回本地 CAS）。"""
    digest = (digest or "").strip().lower()
    if not ContentStore.is_hash(digest):
        raise PeerError(f"非法 SHA-256：{digest!r}")
    store = ContentStore(cfg.cas_dir)
    errors: list[str] = []
    for base in _candidate_urls(cfg, peers):
        url = f"{base}/pack/{digest}"
        try:
            with _http_get(url, timeout=timeout) as response:
                raw = response.read()
        except (urllib.error.URLError, OSError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        if util.sha256_bytes(raw) != digest:
            errors.append(f"{url}: 清单摘要不符")
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            errors.append(f"{url}: 清单不是合法 JSON")
            continue
        if not isinstance(data, dict):
            errors.append(f"{url}: 清单不是 JSON 对象")
            continue
        try:
            store.put_bytes(raw)
        except OSError as exc:
            LOG.debug("清单写入 CAS 失败：%r", exc)
        return data
    local = store.get(digest)
    if local is not None:
        try:
            raw = local.read_bytes()
            if util.sha256_bytes(raw) != digest:
                raise ValueError("本地副本摘要损坏")
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"本地 CAS: {exc}")
    raise PeerError("未能获取整合包清单：" + "；".join(errors[:5]))
