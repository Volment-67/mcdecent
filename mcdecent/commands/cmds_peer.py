"""``mcdecent peer`` 子命令组：P2P 节点服务、局域网发现、拉取、注册表发布。

子命令：
    serve     启动只读 CAS HTTP 节点，并按 announce_interval 广播自己
    discover  发现局域网节点（多播 + 手工 --peer）
    pull      拉取对象（或 --all-index 同步整个节点索引）
    peers     列出已知节点
    info      本机节点身份与共享状态
    publish   把整合包清单入 CAS 并登记到本地注册表（可签名）
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import ed25519, p2p, registry, util
from . import Context, make_parser

PEER_GROUP_HELP = "P2P 节点：共享 CAS、局域网发现、拉取与整合包发布"


def register(subparsers, parent) -> None:
    parser = make_parser(subparsers, parent, "peer", PEER_GROUP_HELP)
    sub = parser.add_subparsers(dest="peer_action", metavar="<子命令>")

    serve = sub.add_parser("serve", parents=[parent], help="启动只读 CAS HTTP 节点并加入多播发现")
    serve.add_argument("--port", type=int, default=None, help="HTTP 监听端口（默认取配置 share.port）")
    serve.add_argument("--bind", default=None, help="绑定地址（默认取配置 share.bind，即 0.0.0.0）")
    serve.add_argument("--name", default=None, help="节点显示名（默认取机器名）")
    serve.add_argument("--rate-limit", type=int, default=None, metavar="KBPS", help="单连接限速（KB/s，默认不限）")
    serve.add_argument("--no-multicast", action="store_true", help="只开 HTTP 节点，不参与多播发现")
    serve.add_argument("--duration", type=float, default=None, metavar="秒", help="运行 N 秒后自动退出（默认常驻）")
    serve.set_defaults(func=_cmd_serve)

    discover = sub.add_parser("discover", parents=[parent], help="发现局域网内的节点")
    discover.add_argument("--timeout", type=float, default=5.0, help="多播发现时长（秒，默认 5）")
    discover.add_argument("--peer", action="append", default=[], metavar="HOST:PORT", help="手工指定节点（可重复）")
    discover.add_argument("--no-multicast", action="store_true", help="只用 --peer 列表，不做多播")
    discover.add_argument("--remember", action="store_true", help="把发现的节点写入 peers.json")
    discover.set_defaults(func=_cmd_discover)

    pull = sub.add_parser("pull", parents=[parent], help="从节点拉取对象并校验入 CAS")
    pull.add_argument("digest", help="对象的 SHA-256 摘要（64 位十六进制）")
    pull.add_argument("--peer", action="append", default=[], metavar="HOST:PORT", help="指定节点（可重复）")
    pull.add_argument("--timeout", type=float, default=10.0, help="单节点超时（秒，默认 10）")
    pull.add_argument("--all-index", action="store_true", help="同步该节点索引里的全部对象")
    pull.set_defaults(func=_cmd_pull)

    peers = sub.add_parser("peers", parents=[parent], help="列出已知节点")
    peers.set_defaults(func=_cmd_peers)

    info = sub.add_parser("info", parents=[parent], help="查看本机节点身份与共享状态")
    info.set_defaults(func=_cmd_info)

    publish = sub.add_parser("publish", parents=[parent], help="把整合包清单入 CAS 并登记到本地注册表")
    publish.add_argument("manifest", help="清单 JSON 文件路径")
    publish.add_argument("--sign-key", default=None, metavar="HEX", help="Ed25519 私钥（64 位 hex）；提供则签名")
    publish.add_argument("--name", default=None, help="覆盖清单里的 name")
    publish.add_argument("--version", default=None, help="覆盖清单里的 version")
    publish.add_argument("--files-root", default=None, help="清单 files[].path 的本地根目录（存在则一并入 CAS）")
    publish.set_defaults(func=_cmd_publish)

    parser.set_defaults(func=lambda args: _no_action(parser))


def _no_action(parser) -> int:
    parser.print_help()
    return 2


# --------------------------------------------------------------------------- serve


def _cmd_serve(args) -> int:
    ctx = Context(args)
    cfg = ctx.cfg
    server = p2p.PeerServer(
        cfg,
        host=args.bind,
        port=args.port,
        name=args.name,
        rate_limit_kbps=args.rate_limit,
    )
    discovery = None
    try:
        server.start()
        share = cfg.get("share") or {}
        interval = float(share.get("announce_interval") or 10.0)
        if not args.no_multicast:
            discovery = p2p.PeerDiscovery(cfg, name=server.name, tcp_port=server.port, host=server.advertise_host)
            discovery.start_service()
        stat = server.cas.stat()
        payload = {
            "url": server.url,
            "peer_id": server.peer_id,
            "name": server.name,
            "bind": server.host,
            "port": server.port,
            "objects": stat["objects"],
            "bytes": stat["bytes"],
            "multicast": bool(discovery),
            "multicast_group": p2p._share_conf(cfg).get("multicast_group"),
            "multicast_port": p2p._share_conf(cfg).get("multicast_port"),
            "announce_interval": interval,
        }
        if ctx.json_mode:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            ctx.info(f"P2P 节点已启动：{server.url}")
            ctx.info(f"  peer_id：{server.peer_id}")
            ctx.info(f"  共享对象：{stat['objects']} 个，共 {util.human_size(stat['bytes'])}")
            if discovery is not None:
                ctx.info(f"  多播发现：{payload['multicast_group']}:{payload['multicast_port']}，每 {interval:g}s 广播一次")
            else:
                ctx.info("  多播发现：已关闭（--no-multicast）")
            ctx.info("按 Ctrl+C 停止")
        deadline = None if args.duration is None else time.time() + max(args.duration, 0.0)
        next_announce = 0.0
        while True:
            now = time.time()
            if deadline is not None and now >= deadline:
                break
            if discovery is not None and now >= next_announce:
                result = discovery.announce()
                if result.get("count"):
                    names = "、".join(peer.get("name") or peer.get("url") or "?" for peer in result["peers"][:5])
                    ctx.info(f"发现 {result['count']} 个节点：{names}")
                next_announce = now + interval
            time.sleep(0.2)
    except KeyboardInterrupt:
        ctx.info("正在停止节点…")
    finally:
        if discovery is not None:
            discovery.stop_service()
        server.stop()
    return 0


# --------------------------------------------------------------------------- discover


def _merge_peer(bucket: dict, peer: dict) -> None:
    key = peer.get("peer_id") or peer.get("url") or f"{peer.get('host')}:{peer.get('port')}"
    old = bucket.get(key)
    if old is None:
        bucket[key] = dict(peer)
    else:
        for name, value in peer.items():
            if value not in (None, "", 0) or name not in old:
                old[name] = value


def _cmd_discover(args) -> int:
    ctx = Context(args)
    cfg = ctx.cfg
    bucket: dict[str, dict] = {}
    notes: list[str] = []
    for spec in args.peer or []:
        host_port = p2p.parse_peer(spec)
        if not host_port:
            notes.append(f"忽略非法节点：{spec}")
            continue
        peer = p2p.probe_peer(*host_port, timeout=min(max(args.timeout, 1.0), 5.0))
        if peer:
            _merge_peer(bucket, peer)
        else:
            notes.append(f"{host_port[0]}:{host_port[1]} 无响应")
    if not args.no_multicast or not (args.peer or []):
        discovery = p2p.PeerDiscovery(cfg)
        for peer in discovery.discover(timeout=args.timeout):
            _merge_peer(bucket, peer)
    peers = list(bucket.values())
    if args.remember:
        for peer in peers:
            try:
                p2p.remember_peer(cfg, peer)
            except p2p.PeerError as exc:
                notes.append(str(exc))
    if ctx.json_mode:
        print(json.dumps({"peers": peers, "count": len(peers), "notes": notes}, ensure_ascii=False, indent=2))
        return 0
    if not peers:
        ctx.info("未发现节点。多播可能被防火墙/网卡禁用，可用 --peer host:port 手工指定。")
    else:
        ctx.info(f"发现 {len(peers)} 个节点：")
        for peer in peers:
            objects = peer.get("objects")
            ctx.info(
                f"  {peer.get('name') or '?'}  {peer.get('url') or peer.get('host') + ':' + str(peer.get('port'))}"
                f"  peer_id={peer.get('peer_id') or '?'}"
                + (f"  对象={objects}" if objects is not None else "")
            )
    for note in notes:
        ctx.warn(note)
    return 0


# --------------------------------------------------------------------------- pull


def _cmd_pull(args) -> int:
    ctx = Context(args)
    cfg = ctx.cfg
    digest = (args.digest or "").strip().lower()
    if not args.all_index:
        try:
            path = p2p.fetch_blob(cfg, digest, peers=args.peer, timeout=args.timeout)
        except p2p.PeerError as exc:
            ctx.error(str(exc))
            return 1
        size = path.stat().st_size
        if ctx.json_mode:
            print(json.dumps({"sha256": digest, "path": str(path), "size": size}, ensure_ascii=False, indent=2))
        else:
            ctx.info(f"已拉取并校验：{digest}")
            ctx.info(f"  本地路径：{path}（{util.human_size(size)}）")
        return 0

    # --all-index：从某个节点同步整个索引
    candidates = [spec for spec in (args.peer or []) if p2p.parse_peer(spec)] or [
        peer.get("url") or f"{peer.get('host')}:{peer.get('port')}" for peer in p2p.known_peers(cfg)
    ]
    if not candidates:
        ctx.error("没有可用节点：请用 --peer host:port 指定一个节点")
        return 1
    index = None
    used = None
    errors: list[str] = []
    for candidate in candidates:
        try:
            index = p2p.fetch_index(cfg, candidate, timeout=args.timeout)
            used = candidate
            break
        except p2p.PeerError as exc:
            errors.append(str(exc))
    if index is None:
        ctx.error("读取节点索引失败：" + "；".join(errors[:3]))
        return 1
    objects = index.get("objects") or {}
    ok, failed = 0, []
    for obj_digest in objects:
        try:
            p2p.fetch_blob(cfg, obj_digest, peers=[used], timeout=args.timeout)
            ok += 1
        except p2p.PeerError as exc:
            failed.append(f"{obj_digest[:12]}: {exc}")
    if ctx.json_mode:
        print(
            json.dumps(
                {"peer": used, "total": len(objects), "fetched": ok, "failed": failed},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        ctx.info(f"已从 {used} 同步 {ok}/{len(objects)} 个对象")
        for line in failed[:5]:
            ctx.warn(line)
    return 0 if not failed else 1


# --------------------------------------------------------------------------- peers / info


def _cmd_peers(args) -> int:
    ctx = Context(args)
    peers = p2p.known_peers(ctx.cfg)
    if ctx.json_mode:
        print(json.dumps({"peers": peers, "count": len(peers)}, ensure_ascii=False, indent=2))
        return 0
    if not peers:
        ctx.info("还没有已知节点。用 mcdecent peer discover 发现，或手工编辑 peers.json。")
        return 0
    ctx.info(f"已知节点 {len(peers)} 个：")
    for peer in peers:
        ctx.info(
            f"  {peer.get('name') or '?'}  {peer.get('url') or peer.get('host') + ':' + str(peer.get('port'))}"
            f"  peer_id={peer.get('peer_id') or '?'}  最近见到={peer.get('last_seen') or '?'}"
        )
    return 0


def _cmd_info(args) -> int:
    ctx = Context(args)
    cfg = ctx.cfg
    share = cfg.get("share") or {}
    reg = registry.PackRegistry(cfg)
    stat = ctx.cas.stat()
    peers = p2p.known_peers(cfg)
    payload = {
        "peer_id": p2p.peer_id(cfg),
        "name": share.get("name") or p2p.machine_name(),
        "root": str(cfg.root),
        "url": f"http://{p2p.local_ip()}:{share.get('port') or 25577}",
        "bind": share.get("bind"),
        "port": share.get("port"),
        "multicast_group": share.get("multicast_group"),
        "multicast_port": share.get("multicast_port"),
        "announce_interval": share.get("announce_interval"),
        "cas": stat,
        "packs": reg.list(),
        "known_peers": len(peers),
    }
    if ctx.json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    ctx.info(f"本机节点：{payload['name']}（{payload['peer_id']}）")
    ctx.info(f"  地址：{payload['url']}（绑定 {payload['bind']}）")
    ctx.info(f"  多播：{payload['multicast_group']}:{payload['multicast_port']}，每 {payload['announce_interval']}s 广播")
    ctx.info(f"  CAS：{stat['objects']} 个对象，共 {util.human_size(stat['bytes'])}（{stat['root']}）")
    ctx.info(f"  已登记整合包：{len(payload['packs'])} 个；已知节点：{len(peers)} 个")
    return 0


# --------------------------------------------------------------------------- publish


def _validate_manifest(manifest: dict) -> list[str]:
    """清单基本格式检查，返回问题列表（空表示通过）。"""
    problems: list[str] = []
    fmt = str(manifest.get("format") or "")
    if not fmt.startswith(registry.PACK_FORMAT_PREFIX):
        problems.append(f"format 不是 {registry.PACK_FORMAT_PREFIX}1：{fmt!r}")
    if not str(manifest.get("name") or "").strip():
        problems.append("缺少 name 字段")
    files = manifest.get("files")
    if files is not None:
        if not isinstance(files, list):
            problems.append("files 必须是数组")
        else:
            for i, item in enumerate(files):
                if not isinstance(item, dict):
                    problems.append(f"files[{i}] 不是对象")
                    continue
                if not str(item.get("path") or "").strip():
                    problems.append(f"files[{i}] 缺少 path")
                sha = str(item.get("sha256") or "").lower()
                if sha and not registry._is_sha256(sha):
                    problems.append(f"files[{i}].sha256 非法：{sha!r}")
                if item.get("size") is not None:
                    try:
                        int(item["size"])
                    except (TypeError, ValueError):
                        problems.append(f"files[{i}].size 不是整数")
    return problems


def _cmd_publish(args) -> int:
    ctx = Context(args)
    cfg = ctx.cfg
    path = Path(args.manifest).expanduser()
    if not path.is_file():
        ctx.error(f"清单不存在：{path}")
        return 1
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        ctx.error(f"读取清单失败：{exc}")
        return 1
    if not isinstance(manifest, dict):
        ctx.error("清单必须是 JSON 对象")
        return 1
    problems = _validate_manifest(manifest)
    if problems:
        ctx.error("清单格式有问题：" + "；".join(problems))
        return 1

    public_hex = signature_hex = None
    if args.sign_key:
        try:
            secret = ed25519.secret_from_hex(args.sign_key)
        except ValueError as exc:
            ctx.error(str(exc))
            return 1
        manifest = registry.sign_manifest(manifest, secret)
        public_hex = ed25519.public_to_hex(ed25519.public_key(secret))
        signature_hex = manifest["signature"]["sig"]
    elif isinstance(manifest.get("signature"), dict):
        block = manifest["signature"]
        alg = str(block.get("alg") or "ed25519").lower()
        if alg != "ed25519":
            ctx.error(f"不支持的签名算法：{alg}")
            return 1
        public_hex = str(block.get("public_key") or "").strip() or None
        signature_hex = str(block.get("sig") or "").strip() or None
        if bool(public_hex) != bool(signature_hex):
            ctx.error("清单 signature 字段不完整（public_key 与 sig 必须同时存在）")
            return 1

    raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    digest = ctx.cas.put_bytes(raw)

    added = skipped = 0
    if args.files_root:
        root = Path(args.files_root).expanduser()
        for item in manifest.get("files") or []:
            rel = str(item.get("path") or "").strip()
            if not rel:
                continue
            local = root / rel
            if not local.is_file():
                skipped += 1
                continue
            want = str(item.get("sha256") or "").lower()
            got = util.sha256_file(local)
            if want and got != want:
                ctx.warn(f"跳过摘要不符的文件：{rel}")
                skipped += 1
                continue
            ctx.cas.put_file(local, verify=False)
            added += 1

    name = (args.name or manifest.get("name")).strip()
    version = args.version or manifest.get("version")
    try:
        entry = registry.PackRegistry(cfg).add(
            name, digest, version=version, public_key=public_hex, signature=signature_hex
        )
    except ValueError as exc:
        ctx.error(str(exc))
        return 1
    url = f"http://{p2p.local_ip()}:{cfg.get('share.port') or 25577}/pack/{digest}"
    payload = {
        "name": name,
        "version": entry.get("version"),
        "sha256": digest,
        "signed": bool(public_hex and signature_hex),
        "public_key": public_hex,
        "manifest": str(path),
        "url": url,
        "files_added": added,
        "files_skipped": skipped,
        "registry": str(Path(cfg.packs_dir) / registry.INDEX_NAME),
    }
    if ctx.json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        ctx.info(f"已登记整合包：{name} {version or ''}".rstrip())
        ctx.info(f"  清单摘要：{digest}")
        ctx.info(f"  签名：{'是（Ed25519）' if payload['signed'] else '否'}")
        if args.files_root:
            ctx.info(f"  附带文件：入 CAS {added} 个，跳过 {skipped} 个")
        ctx.info(f"  其他节点可从这里取：{url}")
        ctx.info(f"  注册表：{payload['registry']}")
    return 0
