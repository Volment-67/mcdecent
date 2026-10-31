"""P2P 层测试：只读 HTTP 节点、拉取校验、注册表与签名、多播降级。

全部使用 ``tmp_path`` 独立临时目录 + ``127.0.0.1`` 随机端口，可并行/重复运行；
每个用例收尾都会 ``stop()`` 服务端，不依赖外网。
"""

from __future__ import annotations

import http.server
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from mcdecent import cli, ed25519, p2p, registry
from mcdecent.cas import ContentStore
from mcdecent.config import Config


# --------------------------------------------------------------------------- 夹具 / 工具


@pytest.fixture
def cfg(tmp_path) -> Config:
    config = Config(home=tmp_path / "home")
    config.ensure_layout()
    return config


def _client_cfg(tmp_path) -> Config:
    """模拟"另一台机器"的配置：CAS 与节点隔离，才能真正测到拉取路径。"""
    client = Config(home=tmp_path / "client")
    client.ensure_layout()
    return client


@pytest.fixture
def node(cfg):
    """绑定 127.0.0.1:0 的只读节点；用例结束一定关停。"""
    server = p2p.PeerServer(cfg, host="127.0.0.1", port=0, name="测试节点")
    server.start()
    assert server.port > 0
    try:
        yield server
    finally:
        server.stop()


def _get_status(url: str, timeout: float = 10.0):
    """GET，返回 (状态码, 响应体)；4xx/5xx 不抛异常。"""
    request = urllib.request.Request(url, headers={"User-Agent": "mcdecent-test"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _get_json(url: str, timeout: float = 10.0):
    status, body = _get_status(url, timeout)
    return status, json.loads(body.decode("utf-8"))


def _post(url: str, timeout: float = 10.0):
    request = urllib.request.Request(url, data=b"{}", method="POST", headers={"User-Agent": "mcdecent-test"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class _MismatchServer:
    """始终返回错误内容的假节点，用来测"摘要不符"路径。"""

    def __init__(self, payload: bytes = b"not-the-right-bytes"):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: ARG002
                pass

            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


# --------------------------------------------------------------------------- HTTP 节点


def test_health(node, cfg):
    status, data = _get_json(f"{node.url}/health")
    assert status == 200
    assert data["protocol"] == p2p.PEER_PROTOCOL
    assert data["peer_id"] == p2p.peer_id(cfg)
    assert data["name"] == "测试节点"
    assert data["url"] == node.url
    assert isinstance(data["objects"], int) and isinstance(data["packs"], int)
    assert data["version"]


def test_index_and_cas_object(node, cfg):
    store = ContentStore(cfg.cas_dir)
    payload = b"hello decentralized world\n" * 100
    digest = store.put_bytes(payload)

    status, index = _get_json(f"{node.url}/index")
    assert status == 200
    assert index["objects"][digest] == len(payload)
    assert isinstance(index["packs"], list)

    status, body = _get_status(f"{node.url}/cas/{digest}")
    assert status == 200
    assert body == payload


def test_cas_errors(node):
    status, body = _get_status(f"{node.url}/cas/{'a' * 64}")
    assert status == 404
    assert "不存在" in json.loads(body)["error"]

    for bad in ("not-a-hash", "A" * 64, "abc", "g" * 64):
        status, body = _get_status(f"{node.url}/cas/{bad}")
        assert status == 400, f"{bad!r} 应返回 400"
        assert "error" in json.loads(body)


def test_pack_route(node, cfg):
    store = ContentStore(cfg.cas_dir)
    manifest = {
        "format": "mcdecent-pack/1",
        "name": "demo",
        "version": "1.0.0",
        "files": [],
    }
    digest = store.put_bytes(json.dumps(manifest).encode("utf-8"))

    status, data = _get_json(f"{node.url}/pack/{digest}")
    assert status == 200
    assert data["name"] == "demo"

    # 存在但不是 JSON / 不是 JSON 对象 → 404
    not_json = store.put_bytes(b"plain text")
    status, _ = _get_status(f"{node.url}/pack/{not_json}")
    assert status == 404
    not_object = store.put_bytes(b"[1, 2, 3]")
    status, _ = _get_status(f"{node.url}/pack/{not_object}")
    assert status == 404
    # 不存在 / 非法摘要
    assert _get_status(f"{node.url}/pack/{'b' * 64}")[0] == 404
    assert _get_status(f"{node.url}/pack/xyz")[0] == 400


def test_methods_and_unknown_paths(node):
    status, body = _post(f"{node.url}/health")
    assert status == 405
    assert json.loads(body)["error"]
    assert _get_status(f"{node.url}/nope")[0] == 404


def test_rate_limited_node(cfg):
    """限速节点仍然能正确传输（限速只影响速度）。"""
    server = p2p.PeerServer(cfg, host="127.0.0.1", port=0, rate_limit_kbps=100_000)
    server.start()
    try:
        payload = b"z" * 4096
        digest = ContentStore(cfg.cas_dir).put_bytes(payload)
        status, body = _get_status(f"{server.url}/cas/{digest}")
        assert status == 200 and body == payload
    finally:
        server.stop()


def test_server_stop_is_idempotent(cfg):
    server = p2p.PeerServer(cfg, host="127.0.0.1", port=0)
    server.start()
    server.stop()
    server.stop()  # 不应抛异常
    with pytest.raises(OSError):
        p2p._http_get(f"{server.url}/health", timeout=0.5)


# --------------------------------------------------------------------------- 客户拉取


def test_fetch_blob_from_peer(node, tmp_path):
    payload = os.urandom(3000)
    digest = node.cas.put_bytes(payload)  # 对象在"服务端"的 CAS 里
    client = _client_cfg(tmp_path)  # 客户端本地没有

    path = p2p.fetch_blob(client, digest, peers=[f"127.0.0.1:{node.port}"], timeout=5)
    assert path == ContentStore(client.cas_dir).path_for(digest)
    assert path.read_bytes() == payload


def test_fetch_blob_uses_local_copy_without_peer(tmp_path):
    client = _client_cfg(tmp_path)
    digest = ContentStore(client.cas_dir).put_bytes(b"local only")
    path = p2p.fetch_blob(client, digest, peers=[], timeout=5)  # 没有节点也应直接命中本地
    assert path.is_file()


def test_fetch_blob_rejects_bad_digest(cfg):
    with pytest.raises(p2p.PeerError):
        p2p.fetch_blob(cfg, "not-a-digest", peers=["127.0.0.1:1"], timeout=1)


def test_fetch_blob_all_peers_fail(cfg):
    digest = "c" * 64
    with pytest.raises(p2p.PeerError) as excinfo:
        p2p.fetch_blob(cfg, digest, peers=["127.0.0.1:1", "127.0.0.1:2"], timeout=1)
    assert "所有节点" in str(excinfo.value)


def test_fetch_blob_detects_content_mismatch(tmp_path):
    client = _client_cfg(tmp_path)
    fake = _MismatchServer()
    try:
        with pytest.raises(p2p.PeerError) as excinfo:
            p2p.fetch_blob(client, "d" * 64, peers=[f"127.0.0.1:{fake.port}"], timeout=5)
        assert "摘要不符" in str(excinfo.value)
        assert not ContentStore(client.cas_dir).has("d" * 64)
    finally:
        fake.close()


def test_fetch_pack_from_peer(node, tmp_path):
    manifest = {"format": "mcdecent-pack/1", "name": "p2p-demo", "version": "2.0.0", "files": []}
    raw = json.dumps(manifest).encode("utf-8")
    digest = node.cas.put_bytes(raw)  # 清单只在服务端
    client = _client_cfg(tmp_path)

    data = p2p.fetch_pack(client, digest, peers=[{"host": "127.0.0.1", "port": node.port}])
    assert data == manifest
    assert ContentStore(client.cas_dir).has(digest)


def test_probe_peer(node):
    probe = p2p.probe_peer("127.0.0.1", node.port, timeout=5)
    assert probe is not None
    assert probe["peer_id"] == node.peer_id
    assert probe["protocol"] == p2p.PEER_PROTOCOL
    assert p2p.probe_peer("127.0.0.1", 1, timeout=1) is None
    assert p2p.probe_peer("not a peer", None) is None


# --------------------------------------------------------------------------- 已知节点


def test_remember_and_known_peers(cfg, node):
    peer = {"host": "127.0.0.1", "port": node.port, "name": "邻居", "objects": 3}
    saved = p2p.remember_peer(cfg, peer)
    assert saved["last_seen"]
    assert cfg.peers_file.is_file()

    p2p.remember_peer(cfg, peer)  # 重复记住应去重
    peers = p2p.known_peers(cfg)
    same = [p for p in peers if p["host"] == "127.0.0.1" and p["port"] == node.port]
    assert len(same) == 1
    assert same[0]["name"] == "邻居"

    data = json.loads(cfg.peers_file.read_text(encoding="utf-8"))
    assert data["format"] == "mcdecent-peers/1"
    assert p2p.parse_peer("127.0.0.1:25577") == ("127.0.0.1", 25577)
    assert p2p.parse_peer("http://127.0.0.1:25577") == ("127.0.0.1", 25577)
    assert p2p.parse_peer("没端口") is None


def test_remember_peer_limit(cfg):
    for index in range(p2p.MAX_PEERS + 20):
        p2p.remember_peer(cfg, {"host": f"10.0.0.{index % 250 + 1}", "port": 20000 + index})
    assert len(p2p.known_peers(cfg)) <= p2p.MAX_PEERS


def test_peers_route_lists_known(node, cfg):
    p2p.remember_peer(cfg, {"host": "127.0.0.1", "port": 23456, "name": "记住的节点"})
    status, data = _get_json(f"{node.url}/peers")
    assert status == 200
    assert any(peer.get("port") == 23456 for peer in data["peers"])


# --------------------------------------------------------------------------- 多播降级 / 手工列表


def test_discover_degrades_without_multicast(cfg, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("没有可用网卡")

    monkeypatch.setattr(p2p, "_open_multicast_socket", _boom)
    discovery = p2p.PeerDiscovery(cfg)
    assert discovery.discover(timeout=0.2) == []  # 不抛异常

    result = discovery.announce()
    assert result["sent"] is False
    assert result["peers"] == []
    discovery.stop_service()  # 未启动也应幂等


def test_discover_with_manual_peer(cfg, node):
    """多播不可用时，--peer 手工列表仍能发现节点（用 probe_peer 同款路径）。"""
    probe = p2p.probe_peer(*p2p.parse_peer(f"127.0.0.1:{node.port}"))
    assert probe and probe["url"].endswith(str(node.port))
    p2p.remember_peer(cfg, probe)
    known_urls = {p.get("url") for p in p2p.known_peers(cfg)}
    assert f"http://127.0.0.1:{node.port}" in known_urls


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("MCDECENT_TEST_MCAST") != "1",
    reason="多播回环在部分机器/防火墙下不可用；设置 MCDECENT_TEST_MCAST=1 启用",
)
def test_multicast_loopback(cfg):
    """同一台机器上两个发现实例通过多播互相看见（可选联网测试）。"""
    port = 30000 + (os.getpid() % 20000)
    first = p2p.PeerDiscovery(cfg, port=port, name="A", tcp_port=25577)
    second = p2p.PeerDiscovery(cfg, port=port, name="B", tcp_port=25578)
    try:
        second.start_service()
        found = first.discover(timeout=3.0)
        assert any(peer["name"] == "B" for peer in found)
    finally:
        second.stop_service()


# --------------------------------------------------------------------------- 注册表 / 签名


def test_registry_crud(cfg):
    pack_registry = registry.PackRegistry(cfg)
    digest = "e" * 64
    entry = pack_registry.add("demo", digest, version="1.0.0")
    assert entry["name"] == "demo" and entry["sha256"] == digest
    assert pack_registry.lookup("demo")["version"] == "1.0.0"
    assert pack_registry.lookup("不存在") is None
    assert [e["name"] for e in pack_registry.list()] == ["demo"]
    assert pack_registry.remove("demo") is True
    assert pack_registry.remove("demo") is False
    assert pack_registry.lookup("demo") is None
    assert (cfg.packs_dir / registry.INDEX_NAME).is_file()


def test_registry_rejects_bad_values(cfg):
    pack_registry = registry.PackRegistry(cfg)
    with pytest.raises(ValueError):
        pack_registry.add("", "e" * 64)
    with pytest.raises(ValueError):
        pack_registry.add("x", "不是摘要")


def test_registry_signature_roundtrip(cfg):
    secret, public = ed25519.generate_keypair()
    manifest = {
        "format": "mcdecent-pack/1",
        "name": "signed-pack",
        "version": "1.0.0",
        "files": [{"path": "mods/a.jar", "sha256": "f" * 64, "size": 123}],
    }
    signed = registry.sign_manifest(manifest, secret)
    assert signed["signature"]["alg"] == "ed25519"
    assert signed["signature"]["public_key"] == public.hex()

    raw = json.dumps(signed).encode("utf-8")
    digest = ContentStore(cfg.cas_dir).put_bytes(raw)
    pack_registry = registry.PackRegistry(cfg)
    entry = pack_registry.add(
        "signed-pack",
        digest,
        version="1.0.0",
        public_key=public.hex(),
        signature=signed["signature"]["sig"],
    )
    assert pack_registry.verify(entry, signed) is True
    assert pack_registry.verify_bytes("signed-pack", raw) is True

    tampered = dict(signed)
    tampered["version"] = "9.9.9"
    assert pack_registry.verify(entry, tampered) is False
    # 签名覆盖的是"去掉 signature 后的正文"，所以传入未签名的同一正文也应成立
    assert pack_registry.verify(entry, manifest) is True
    assert pack_registry.verify_bytes("signed-pack", raw.replace(b"1.0.0", b"9.9.9")) is False


def test_registry_unsigned_entry_passes(cfg):
    """条目没公钥/签名时不做签名校验（内容完整性由 CAS 摘要保证）。"""
    pack_registry = registry.PackRegistry(cfg)
    manifest = {"format": "mcdecent-pack/1", "name": "plain", "files": []}
    entry = pack_registry.add("plain", "1" * 64)
    assert pack_registry.verify(entry, manifest) is True
    assert pack_registry.verify({"name": "plain"}, manifest) is True
    assert pack_registry.verify({}, "不是字典") is False  # type: ignore[arg-type]


def test_registry_get_keeps_old_signature_only_when_digest_same(cfg):
    secret, public = ed25519.generate_keypair()
    pack_registry = registry.PackRegistry(cfg)
    first = pack_registry.add("x", "2" * 64, public_key=public.hex(), signature="ab" * 64)
    same = pack_registry.add("x", "2" * 64)
    assert same["signature"] == "ab" * 64
    changed = pack_registry.add("x", "3" * 64)
    assert changed["signature"] is None
    assert first["added"] == changed["added"]


# --------------------------------------------------------------------------- CLI 集成


def test_cli_peer_info(cfg, tmp_path, capsys):
    home = str(cfg.root)
    rc = cli.main(["--home", home, "--json", "peer", "info"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["peer_id"] == p2p.peer_id(cfg)
    assert payload["port"] == 25577
    assert payload["cas"]["root"].endswith("cas")


def test_cli_peer_discover_manual_peer(cfg, node, capsys):
    rc = cli.main(
        ["--home", str(cfg.root), "--json", "peer", "discover", "--no-multicast", "--peer", f"127.0.0.1:{node.port}"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["peers"][0]["peer_id"] == node.peer_id


def test_cli_peer_publish_and_pull(node, tmp_path, capsys):
    """发布端（节点所属 home）与拉取端（另一台机器）分离，验证完整跨机流程。"""
    server_home = str(node.cfg.root)
    client_home = str(_client_cfg(tmp_path).root)

    # 1) 发布：清单入 CAS + 登记注册表（带签名）
    secret, public = ed25519.generate_keypair()
    manifest = {
        "format": "mcdecent-pack/1",
        "name": "cli-pack",
        "version": "0.1.0",
        "files": [],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    rc = cli.main(["--home", server_home, "--json", "peer", "publish", str(manifest_path), "--sign-key", secret.hex()])
    assert rc == 0
    published = json.loads(capsys.readouterr().out)
    assert published["signed"] is True
    digest = published["sha256"]
    assert node.cas.has(digest)

    # 2) 节点应能把它作为 /pack/<hash> 提供
    status, data = _get_json(f"{node.url}/pack/{digest}")
    assert status == 200 and data["name"] == "cli-pack"

    # 3) 拉取端本地没有 → 从节点 pull 回来
    rc = cli.main(
        ["--home", client_home, "--json", "peer", "pull", digest, "--peer", f"127.0.0.1:{node.port}"]
    )
    assert rc == 0
    pulled = json.loads(capsys.readouterr().out)
    assert pulled["sha256"] == digest
    client_store = ContentStore(Config(home=client_home).cas_dir)
    assert client_store.has(digest)

    # 4) 拉取端的注册表（同名登记）能校验这份清单
    client_registry = registry.PackRegistry(Config(home=client_home))
    client_registry.add(
        "cli-pack",
        digest,
        public_key=published["public_key"],
        signature=json.loads(client_store.path_for(digest).read_text(encoding="utf-8"))["signature"]["sig"],
    )
    assert client_registry.verify_bytes("cli-pack", client_store.path_for(digest).read_bytes())
