"""loader / mods / pack 三个子命令组。

命令层只做三件事：解析参数 → 调用 ``loaders`` / ``mods`` / ``pack`` → 统一输出。
所有业务逻辑都在模块里，方便测试直接调用 API 而不经过命令行。
"""

from __future__ import annotations

from pathlib import Path

from .. import loaders, mods, pack, util


# =========================================================================== loader


def cmd_loader_install(args) -> int:
    from . import Context

    ctx = Context(args)
    from .. import installer  # 延迟导入：启动核心尚未就绪时不影响其他命令注册

    cfg = ctx.cfg
    base = args.game
    loader_type = (args.loader_type or "fabric").lower()
    if not args.skip_assets:
        ctx.info(f"① 确保基础版本 {base} 已安装…")
    installer.install_version(
        cfg,
        base,
        downloader=ctx.downloader,
        progress=ctx.progress,
        skip_assets=bool(args.skip_assets),
    )
    ctx.info(f"② 安装 {loader_type} 加载器…")
    loader_id = loaders.install_loader(
        cfg,
        base,
        loader_type=loader_type,
        loader_version=args.loader_version,
        downloader=ctx.downloader,
    )
    ctx.info(f"③ 下载 {loader_id} 需要的库…")
    installer.install_version(
        cfg,
        loader_id,
        downloader=ctx.downloader,
        progress=ctx.progress,
        skip_assets=bool(args.skip_assets),
    )
    launch_cmd = f"python -m mcdecent launch {loader_id}"
    payload = {
        "game_version": base,
        "loader": {"type": loader_type, "id": loader_id},
        "version_id": loader_id,
        "launch": launch_cmd,
    }
    if ctx.json_mode:
        ctx.emit(payload)
    else:
        ctx.info("")
        ctx.info(f"完成！已安装版本 {loader_id}")
        ctx.info(f"启动命令：{launch_cmd}")
    return 0


def cmd_loader_versions(args) -> int:
    from . import Context

    ctx = Context(args)
    loader_type = (args.loader_type or "fabric").lower()
    entries = loaders.fetch_loader_versions(
        ctx.cfg, args.game, loader_type=loader_type, downloader=ctx.downloader
    )
    rows = [
        {"version": entry["loader"]["version"], "stable": entry["loader"]["stable"]}
        for entry in entries
    ]
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]
    if ctx.json_mode:
        ctx.emit({"game_version": args.game, "type": loader_type, "versions": rows})
    else:
        ctx.info(f"{loader_type} 加载器（游戏 {args.game}，共 {len(rows)} 个）：")
        for row in rows:
            ctx.info(f"  {row['version']}{'  [stable]' if row['stable'] else ''}")
    return 0


def cmd_loader_list(args) -> int:
    from . import Context

    ctx = Context(args)
    rows = loaders.installed_loaders(ctx.cfg)
    if ctx.json_mode:
        ctx.emit({"count": len(rows), "loaders": rows})
    elif not rows:
        ctx.info("还没有安装过加载器。可用：python -m mcdecent loader install <游戏版本>")
    else:
        for row in rows:
            ctx.info(f"{row['id']}  ({row['type']} {row['version']} / 游戏 {row['game_version']})")
    return 0


# =========================================================================== mods


def cmd_mods_add(args) -> int:
    from . import Context

    ctx = Context(args)
    installed: list[dict] = []
    failed: list[dict] = []
    for raw in args.files:
        try:
            installed.append(
                mods.install_mod_file(ctx.cfg, Path(raw), enable=not args.disable, source="local")
            )
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响其余文件
            failed.append({"file": raw, "error": str(exc)})
            ctx.warn(f"安装失败 {raw}：{exc}")
    if ctx.json_mode:
        ctx.emit({"installed": installed, "failed": failed})
    else:
        for item in installed:
            ctx.info(
                f"已安装 {item['name']}  {util.human_size(item['size'])}  sha256 {item['sha256'][:12]}…"
            )
        ctx.info(f"共安装 {len(installed)} 个模组，仍有 {len(failed)} 个失败")
    return 0 if not failed else 1


def cmd_mods_list(args) -> int:
    from . import Context

    ctx = Context(args)
    items = mods.list_mods(ctx.cfg)
    if ctx.json_mode:
        ctx.emit({"count": len(items), "mods": items})
    elif not items:
        ctx.info(f"mods 目录为空：{mods.mods_dir(ctx.cfg)}")
    else:
        for item in items:
            flag = "启用" if item["enabled"] else "禁用"
            ctx.info(
                f"[{flag}] {item['name']}  {util.human_size(item['size'])}  "
                f"{item['loader_hint']}"
            )
        ctx.info(f"共 {len(items)} 个模组")
    return 0


def cmd_mods_remove(args) -> int:
    from . import Context

    ctx = Context(args)
    removed: list[str] = []
    missing: list[str] = []
    for name in args.names:
        if mods.remove_mod(ctx.cfg, name):
            removed.append(name)
        else:
            missing.append(name)
            ctx.warn(f"没有找到匹配的模组：{name}")
    if ctx.json_mode:
        ctx.emit({"removed": removed, "missing": missing})
    else:
        ctx.info(f"已删除 {len(removed)} 项；{len(missing)} 项未找到")
    return 0 if not missing else 1


def _set_enabled_batch(args, enabled: bool) -> int:
    from . import Context

    ctx = Context(args)
    changed: list[dict] = []
    failed: list[dict] = []
    for name in args.names:
        try:
            path = mods.set_enabled(ctx.cfg, name, enabled)
            changed.append({"name": path.name, "path": str(path), "enabled": enabled})
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": name, "error": str(exc)})
            ctx.warn(f"切换失败 {name}：{exc}")
    if ctx.json_mode:
        ctx.emit({"changed": changed, "failed": failed})
    else:
        verb = "启用" if enabled else "禁用"
        for item in changed:
            ctx.info(f"{verb} {item['name']}")
    return 0 if not failed else 1


def cmd_mods_enable(args) -> int:
    return _set_enabled_batch(args, True)


def cmd_mods_disable(args) -> int:
    return _set_enabled_batch(args, False)


def cmd_mods_search(args) -> int:
    from . import Context

    ctx = Context(args)
    results = mods.search_modrinth(
        ctx.cfg,
        args.query,
        loader=args.loader,
        game_version=args.game_version,
        limit=args.limit,
        downloader=ctx.downloader,
    )
    if ctx.json_mode:
        ctx.emit({"count": len(results), "results": results})
    elif not results:
        ctx.info("没有找到匹配的模组")
    else:
        for item in results:
            ctx.info(f"{item['title']}  ({item['slug']})  ↓{item['downloads']}")
            if item["description"]:
                ctx.info(f"    {item['description'][:100]}")
        ctx.info(f"共 {len(results)} 个结果；安装用：python -m mcdecent mods install <slug>")
    return 0


def cmd_mods_install(args) -> int:
    from . import Context

    ctx = Context(args)
    info = mods.install_modrinth(
        ctx.cfg,
        args.slug,
        loader=args.loader,
        game_version=args.game_version,
        version_id=args.version_id,
        downloader=ctx.downloader,
    )
    if ctx.json_mode:
        ctx.emit(info)
    else:
        ctx.info(f"已安装 {info['name']}  {util.human_size(info['size'])}")
        ctx.info(f"  来自 {info['source']}，版本 {info.get('version_number') or '?'}")
    return 0


# =========================================================================== pack


def cmd_pack_export(args) -> int:
    from . import Context

    ctx = Context(args)
    include = tuple(args.include) if args.include else ("mods", "config")
    result = pack.export_pack(
        ctx.cfg,
        args.name,
        game_version=args.game_version,
        version=args.version,
        include=include,
        out=Path(args.out) if args.out else None,
        key=args.sign_key,
    )
    if ctx.json_mode:
        ctx.emit(result)
    else:
        ctx.info(f"已导出整合包 {result['name']} {result['version']}（{result['files']} 个文件，{util.human_size(result['bytes'])}）")
        ctx.info(f"内容地址：{result['url']}")
        ctx.info(f"本地副本：{result['path']}")
        ctx.info(f"签名：{'已签名' if result['signed'] else '未签名'}")
    return 0


def cmd_pack_import(args) -> int:
    from . import Context

    ctx = Context(args)
    result = pack.import_pack(
        ctx.cfg,
        args.ref,
        peer=args.peer,
        no_mods=bool(args.no_mods),
        strict_signature=bool(args.strict_signature),
    )
    if ctx.json_mode:
        ctx.emit(result)
    else:
        ctx.info(f"已导入整合包 {result['name']} {result['version']}（游戏 {result['game_version']}）")
        ctx.info(f"写入 {len(result['written'])} 个文件，跳过 {len(result['skipped'])} 个")
        if result["signature_valid"] is False:
            ctx.warn("整合包签名无效！内容可能被篡改")
        elif result["signature_valid"] is None:
            ctx.info("签名：无签名或无法校验")
        else:
            ctx.info("签名：校验通过")
    return 0


def cmd_pack_list(args) -> int:
    from . import Context

    ctx = Context(args)
    items = pack.list_packs(ctx.cfg)
    if ctx.json_mode:
        ctx.emit({"count": len(items), "packs": items})
    elif not items:
        ctx.info("还没有导出过整合包。用：python -m mcdecent pack export <名字> --game-version <版本>")
    else:
        for item in items:
            sig = "签名" if item["signed"] else "未签名"
            ctx.info(
                f"{item['name']} {item['version']}  游戏 {item['game_version']}  "
                f"{item['files']} 文件  {sig}  {item['digest'][:12]}…"
            )
    return 0


# =========================================================================== 注册


def register(subparsers, parent) -> None:
    """注册 loader / mods / pack 三组子命令（每个最终 parser 都 set_defaults(func=...)）。"""
    from . import make_parser

    # ---------------------------------------------------------------- loader
    loader_parser = make_parser(subparsers, parent, "loader", "安装/管理 Fabric、Quilt 加载器")
    loader_sub = loader_parser.add_subparsers(dest="loader_action", metavar="<操作>")

    p = loader_sub.add_parser("install", parents=[parent], help="安装加载器（自动先装基础版本）")
    p.add_argument("game", help="基础游戏版本，如 1.21.4")
    p.add_argument("--type", dest="loader_type", choices=list(loaders.LOADER_TYPES), default="fabric", help="加载器类型（默认 fabric）")
    p.add_argument("--loader-version", default=None, help="加载器版本；省略则取最新稳定版")
    p.add_argument("--skip-assets", action="store_true", help="跳过资源文件下载（更快，但可能无法进游戏）")
    p.set_defaults(func=cmd_loader_install)

    p = loader_sub.add_parser("versions", parents=[parent], help="列出可用的加载器版本")
    p.add_argument("game", help="游戏版本，如 1.21.4")
    p.add_argument("--type", dest="loader_type", choices=list(loaders.LOADER_TYPES), default="fabric", help="加载器类型（默认 fabric）")
    p.add_argument("--limit", type=int, default=0, help="最多显示多少个（0 = 全部）")
    p.set_defaults(func=cmd_loader_versions)

    p = loader_sub.add_parser("list", parents=[parent], help="列出已安装的加载器")
    p.set_defaults(func=cmd_loader_list)

    # ---------------------------------------------------------------- mods
    mods_parser = make_parser(subparsers, parent, "mods", "本地模组管理 + Modrinth 检索/安装")
    mods_sub = mods_parser.add_subparsers(dest="mods_action", metavar="<操作>")

    p = mods_sub.add_parser("add", parents=[parent], help="安装本地 jar 模组（自动入 CAS）")
    p.add_argument("files", nargs="+", help="一个或多个本地 .jar 文件")
    p.add_argument("--disable", action="store_true", help="安装后默认禁用（.jar.disabled）")
    p.set_defaults(func=cmd_mods_add)

    p = mods_sub.add_parser("list", parents=[parent], help="列出已安装模组")
    p.set_defaults(func=cmd_mods_list)

    p = mods_sub.add_parser("rm", parents=[parent], help="删除模组（支持通配符）")
    p.add_argument("names", nargs="+", help="模组文件名、去后缀名或通配符")
    p.set_defaults(func=cmd_mods_remove)

    p = mods_sub.add_parser("enable", parents=[parent], help="启用模组（去掉 .disabled）")
    p.add_argument("names", nargs="+", help="模组文件名、去后缀名或通配符")
    p.set_defaults(func=cmd_mods_enable)

    p = mods_sub.add_parser("disable", parents=[parent], help="禁用模组（加 .disabled）")
    p.add_argument("names", nargs="+", help="模组文件名、去后缀名或通配符")
    p.set_defaults(func=cmd_mods_disable)

    p = mods_sub.add_parser("search", parents=[parent], help="在 Modrinth 上检索模组")
    p.add_argument("query", help="关键词")
    p.add_argument("--loader", default="fabric", help="加载器（默认 fabric）")
    p.add_argument("--game-version", default=None, help="游戏版本，如 1.21.4")
    p.add_argument("--limit", type=int, default=10, help="结果数量（默认 10）")
    p.set_defaults(func=cmd_mods_search)

    p = mods_sub.add_parser("install", parents=[parent], help="从 Modrinth 下载并安装模组")
    p.add_argument("slug", help="工程 slug 或 project id")
    p.add_argument("--loader", default="fabric", help="加载器（默认 fabric）")
    p.add_argument("--game-version", default=None, help="游戏版本，如 1.21.4")
    p.add_argument("--version-id", default=None, help="指定 Modrinth 版本 id（默认取最新兼容版）")
    p.set_defaults(func=cmd_mods_install)

    # ---------------------------------------------------------------- pack
    pack_parser = make_parser(subparsers, parent, "pack", "整合包导出/导入（内容寻址 + 可选签名）")
    pack_sub = pack_parser.add_subparsers(dest="pack_action", metavar="<操作>")

    p = pack_sub.add_parser("export", parents=[parent], help="导出整合包清单（文件入 CAS）")
    p.add_argument("name", help="整合包名称")
    p.add_argument("--game-version", required=True, help="对应的游戏版本，如 1.21.4")
    p.add_argument("--version", default="1.0.0", help="整合包版本号（默认 1.0.0）")
    p.add_argument("--include", nargs="*", default=None, help="要包含的目录/文件（默认 mods config）")
    p.add_argument("--out", default=None, help="额外导出一份清单到指定路径")
    p.add_argument("--sign-key", default=None, help="Ed25519 私钥（hex）；可选，签名模块未就绪时自动降级")
    p.set_defaults(func=cmd_pack_export)

    p = pack_sub.add_parser("import", parents=[parent], help="导入整合包（本地路径/sha256/mcdecent://）")
    p.add_argument("ref", help="清单路径、64 位 sha256，或 mcdecent://pack/<sha256>")
    p.add_argument("--peer", default=None, help="从指定 P2P 节点取缺失对象")
    p.add_argument("--no-mods", action="store_true", help="只还原其他文件，跳过 mods/")
    p.add_argument("--strict-signature", action="store_true", help="签名无效或无法校验时拒绝导入")
    p.set_defaults(func=cmd_pack_import)

    p = pack_sub.add_parser("list", parents=[parent], help="列出本地导出过的整合包")
    p.set_defaults(func=cmd_pack_list)
