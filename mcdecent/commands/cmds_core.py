"""核心命令：install / launch / list / doctor / java / mirror / config / info。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path

from .. import __version__, auth, cas, config, installer, java, launch, net, util, versions
from . import Context, make_parser

# --------------------------------------------------------------------------- 公共小工具


def _pick_version(ctx: Context, requested: str | None) -> str:
    if requested:
        return requested
    configured = ctx.cfg.get("default_version")
    if configured:
        return configured
    installed = versions.installed_versions(ctx.cfg)
    if installed:
        return installed[0]["id"]
    return versions.latest_release(ctx.cfg, ctx.downloader)


def _parse_args_list(items) -> list[str]:
    out: list[str] = []
    for item in items or []:
        out.extend(str(item).split())
    return out


def _format_table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    lines = ["  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


# --------------------------------------------------------------------------- install


def cmd_install(args) -> int:
    ctx = Context(args)
    version = _pick_version(ctx, args.version)
    loader = args.loader if args.loader is not None else ctx.cfg.get("default_loader")
    if loader in (None, "", "none", "vanilla"):
        loader = None
    start = time.time()
    progress = ctx.start_progress()
    downloader = ctx.downloader
    summaries = []
    if loader:
        util.LOG.info("先安装基础版本 %s", version)
        summaries.append(installer.install_version(
            ctx.cfg, version, downloader=downloader, progress=progress,
            skip_assets=args.skip_assets, skip_java=args.skip_java,
            auto_java=not args.no_auto_java, refresh=args.refresh,
        ))
        try:
            from .. import loaders  # 由模组子系统提供
        except Exception as exc:  # noqa: BLE001
            ctx.error(f"加载器子系统不可用：{exc!r}")
            return 1
        try:
            loader_id = loaders.install_loader(ctx.cfg, version, loader, args.loader_version)
        except Exception as exc:  # noqa: BLE001
            ctx.error(f"安装 {loader} 加载器失败：{exc}")
            return 1
        util.LOG.info("加载器版本：%s", loader_id)
        summaries.append(installer.install_version(
            ctx.cfg, loader_id, downloader=downloader, progress=progress,
            skip_assets=True, skip_java=True, auto_java=False, refresh=False,
        ))
        target = loader_id
    else:
        summaries.append(installer.install_version(
            ctx.cfg, version, downloader=downloader, progress=progress,
            skip_assets=args.skip_assets, skip_java=args.skip_java,
            auto_java=not args.no_auto_java, refresh=args.refresh,
        ))
        target = version
    ctx.cfg.set("default_version", target)
    ctx.cfg.save()
    elapsed = time.time() - start
    payload = {
        "target": target,
        "versions": summaries,
        "elapsed_seconds": round(elapsed, 1),
        "next": f"mcdecent launch {target}",
    }
    lines = [f"完成：{target}（用时 {util.human_time(elapsed)}）"]
    java_info = summaries[-1].get("java")
    if java_info:
        lines.append(f"Java：{java_info.get('version', '')}（{java_info.get('path')}）")
    lines.append(f"下一步：{payload['next']}")
    ctx.emit(payload, "\n".join(lines))
    return 0


# --------------------------------------------------------------------------- launch


def cmd_launch(args) -> int:
    ctx = Context(args)
    inst = getattr(ctx, "instance", None)
    version = args.version or (inst.version_id if inst and inst.version_id else None) or _pick_version(ctx, None)
    username = args.username or (ctx.cfg.get("player.name") or "Player")
    if args.access_token and args.uuid:
        profile = auth.online_profile(username, args.uuid, args.access_token, user_type=args.user_type)
    else:
        profile = auth.offline_profile(username)
    memory = args.memory if args.memory is not None else (inst.memory_mb if inst else None)
    jvm_args = list(inst.jvm_args if inst else []) + _parse_args_list(args.jvm_arg)
    game_args = list(inst.game_args if inst else []) + _parse_args_list(args.game_arg)
    try:
        plan = launch.build_plan(
            ctx.cfg, version, profile,
            memory_mb=memory, jvm_args=jvm_args,
            game_args=game_args,
            width=args.width, height=args.height, demo=args.demo,
            window_type=getattr(args, "window_type", None),
            quick_play=getattr(args, "quick_play", None),
            quick_play_value=getattr(args, "quick_play_value", None),
            wrapper=getattr(args, "wrapper", None),
            priority=getattr(args, "priority", None),
            auto_java=not args.no_auto_java, downloader=ctx.downloader, progress=ctx.progress,
        )
    except versions.VersionNotInstalled as exc:
        ctx.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        ctx.error(f"准备启动失败：{exc}")
        return 1
    problems = launch.check_plan(plan)
    if problems and not args.dry_run:
        ctx.error("安装不完整，无法启动：")
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"修复：mcdecent install {version}", file=sys.stderr)
        return 1
    if args.dry_run:
        payload = plan.summary()
        payload["problems"] = problems
        payload["command"] = plan.command_line(max_len=20000)
        text = "\n".join([
            f"版本：{plan.resolved.id}（{plan.resolved.java_requirement}）",
            f"Java：{plan.java.path}",
            f"工作目录：{plan.cwd}",
            f"classpath：{len(plan.classpath)} 项",
            "命令：" + plan.command_line(),
        ])
        ctx.emit(payload, text)
        return 0
    try:
        return launch.run_plan(plan, detach=args.detach, quiet=args.quiet)
    except FileNotFoundError as exc:
        ctx.error(f"启动失败，找不到可执行文件：{exc}")
        return 1


# --------------------------------------------------------------------------- list / info


def cmd_list(args) -> int:
    ctx = Context(args)
    installed = versions.installed_versions(ctx.cfg)
    if args.available:
        available = versions.available_versions(ctx.cfg, ctx.downloader, limit=args.limit or 25)
        if ctx.json_mode:
            ctx.emit({"installed": installed, "available": available})
            return 0
        rows = [[v["id"], v["type"], "是" if v["latest"] else ""] for v in available]
        print("可安装版本（来自 Mojang 清单）：")
        print(_format_table(rows, ["版本", "类型", "最新正式版"]))
        if not installed:
            return 0
        print("\n已安装：")
    if ctx.json_mode:
        ctx.emit({"installed": installed})
        return 0
    if not installed:
        print("尚未安装任何版本。先运行：mcdecent install <版本>")
        return 0
    rows = []
    default_id = ctx.cfg.get("default_version")
    for item in installed:
        loader = item.get("loader") or {}
        loader_text = f"{loader.get('type')} {loader.get('version')}" if loader else "原版"
        rows.append([item["id"], loader_text, str(item.get("java_major") or "?"),
                     "就绪" if item.get("client_ready") else "缺文件", item.get("base") or "-",
                     "默认" if item["id"] == default_id else ""])
    print(_format_table(rows, ["版本", "加载器", "Java", "状态", "基础版本", "标记"]))
    return 0


def cmd_info(args) -> int:
    ctx = Context(args)
    version = _pick_version(ctx, args.version)
    try:
        resolved = versions.resolve(ctx.cfg, version)
    except Exception as exc:  # noqa: BLE001
        ctx.error(f"无法解析版本 {version}：{exc}")
        return 1
    report = installer.installation_report(ctx.cfg, version)
    payload = {
        "id": resolved.id,
        "base": resolved.base_id,
        "type": resolved.type,
        "loader": resolved.loader,
        "main_class": resolved.main_class,
        "java_major": resolved.java_major,
        "java_component": resolved.java_component,
        "assets_index": resolved.assets_index_id,
        "libraries": len(resolved.libraries),
        "client_jar": str(resolved.client_jar) if resolved.client_jar else None,
        "native_dir": str(resolved.native_dir) if resolved.native_dir else None,
        "installation": report,
        "home": str(ctx.cfg.root),
    }
    text = "\n".join([
        f"版本：{resolved.id}（基础 {resolved.base_id}，类型 {resolved.type}）",
        f"加载器：{resolved.loader or '原版'}",
        f"主类：{resolved.main_class}",
        f"Java：{resolved.java_requirement}（组件 {resolved.java_component or '-'}）",
        f"资源索引：{resolved.assets_index_id}",
        f"库数量：{len(resolved.libraries)}",
        f"安装完整：{'是' if report.get('ok') else '否，缺失 ' + str(report.get('missing_count')) + ' 个文件'}",
        f"数据目录：{ctx.cfg.root}",
    ])
    ctx.emit(payload, text)
    return 0


# --------------------------------------------------------------------------- doctor


def cmd_doctor(args) -> int:
    ctx = Context(args)
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))

    add("Python", sys.version_info >= (3, 10), platform.python_version())
    add("数据目录", ctx.cfg.root.is_dir(), str(ctx.cfg.root))
    try:
        usage = shutil.disk_usage(str(ctx.cfg.root))
        add("磁盘空间", usage.free > 2 * 1024**3, f"剩余 {util.human_size(usage.free)} / 共 {util.human_size(usage.total)}")
    except OSError as exc:
        add("磁盘空间", False, repr(exc))

    # 网络与镜像
    probe_url = versions.MANIFEST_URL
    for mirror in ctx.cfg.mirror_objects():
        if not mirror.enabled:
            continue
        rewritten = mirror.rewrite(probe_url)
        if not rewritten:
            continue
        ok, detail = ctx.downloader.head_ok(rewritten, timeout=args.timeout)
        add(f"镜像 {mirror.name}", ok, detail)
    direct_ok, direct_detail = ctx.downloader.head_ok(probe_url, timeout=args.timeout)
    add("官方源", direct_ok, direct_detail)

    runtime = java.find_java(ctx.cfg)
    if runtime:
        add("Java", True, f"{runtime.major}（{runtime.source}）{runtime.path}")
    else:
        add("Java", False, "未找到；install 时会自动下载官方运行时")
    installed = versions.installed_versions(ctx.cfg)
    add("已安装版本", bool(installed), f"{len(installed)} 个")
    store = ctx.cas
    stat = store.stat()
    add("内容寻址存储", True, f"{stat['objects']} 个对象，{util.human_size(stat['bytes'])}")

    payload = {"checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks],
               "config": ctx.cfg.describe(), "summary": {}}
    if ctx.json_mode:
        ctx.emit(payload)
    else:
        print(f"mcdecent {__version__} 体检\n")
        for name, ok, detail in checks:
            print(f" [{'✓' if ok else '×'}] {name}: {detail}")
        hard_failures = [c for c in checks if not c[1] and c[0] in ("Python", "数据目录", "磁盘空间", "Java")]
        print()
        if hard_failures:
            print("存在问题：" + "、".join(c[0] for c in hard_failures))
        else:
            print("核心检查通过。")
    # Java 缺失不算硬错误（install 会自动装），但如果已装版本还缺 Java 则提示
    return 0 if all(ok for n, ok, _ in checks if n in ("Python", "数据目录", "磁盘空间")) else 1


# --------------------------------------------------------------------------- java


def cmd_java(args) -> int:
    ctx = Context(args)
    if args.java_action == "list":
        runtimes = java.list_installed_runtimes(ctx.cfg)
        current = java.find_java(ctx.cfg)
        payload = {"managed": [r.describe() for r in runtimes],
                   "current": current.describe() if current else None}
        if ctx.json_mode:
            ctx.emit(payload)
        else:
            print(f"当前使用：{current or '无'}")
            print(f"启动器托管运行时：{len(runtimes)} 个")
            for runtime in runtimes:
                print(f"  Java {runtime.major}  {runtime.path}")
        return 0
    if args.java_action == "install":
        if not args.major:
            ctx.error("请用 --major 指定 Java 主版本（例如 --major 21）")
            return 2
        runtime = java.install_mojang_runtime(ctx.cfg, f"java-runtime-{args.component}",
                                              major=args.major, downloader=ctx.downloader,
                                              progress=ctx.progress) if args.provider == "mojang" else \
            java.install_adoptium_runtime(ctx.cfg, args.major, downloader=ctx.downloader, progress=ctx.progress)
        ctx.emit(runtime.describe(), f"已安装：{runtime}")
        return 0
    if args.java_action == "path":
        runtime = java.find_java(ctx.cfg)
        if not runtime:
            ctx.error("未找到 Java")
            return 1
        ctx.emit(runtime.describe(), str(runtime.path))
        return 0
    ctx.error("未知 java 子命令")
    return 2


# --------------------------------------------------------------------------- mirror


def cmd_mirror(args) -> int:
    ctx = Context(args)
    mirrors = ctx.cfg.mirror_objects()
    if args.mirror_action == "test":
        probe_url = versions.MANIFEST_URL
        rows = []
        for mirror in mirrors:
            rewritten = mirror.rewrite(probe_url)
            if not rewritten:
                rows.append([mirror.name, "不覆盖", "", ""])
                continue
            ok, detail = ctx.downloader.head_ok(rewritten, timeout=args.timeout)
            rows.append([mirror.name, "启用" if mirror.enabled else "停用", "可用" if ok else "失败", detail])
        ok_direct, direct_detail = ctx.downloader.head_ok(probe_url, timeout=args.timeout)
        rows.append(["官方源", "兜底", "可用" if ok_direct else "失败", direct_detail])
        payload = {"mirror_mode": ctx.cfg.get("mirror_mode"),
                   "probes": [{"name": r[0], "state": r[1], "result": r[2], "detail": r[3]} for r in rows]}
        if ctx.json_mode:
            ctx.emit(payload)
        else:
            print(f"镜像模式：{ctx.cfg.get('mirror_mode')}")
            print(_format_table(rows, ["名称", "状态", "探测", "详情"]))
        return 0
    payload = {"mirror_mode": ctx.cfg.get("mirror_mode"),
               "mirrors": [{"name": m.name, "enabled": m.enabled, "hosts": list(m.hosts)} for m in mirrors]}
    if ctx.json_mode:
        ctx.emit(payload)
    else:
        print(f"镜像模式：{ctx.cfg.get('mirror_mode')}")
        for mirror in mirrors:
            print(f"  {'✓' if mirror.enabled else '·'} {mirror.name}: {', '.join(mirror.hosts) or '（无映射）'}")
    return 0


# --------------------------------------------------------------------------- config


def cmd_config(args) -> int:
    ctx = Context(args)
    if args.config_action in (None, "list"):
        ctx.emit(ctx.cfg.data, json.dumps(ctx.cfg.data, ensure_ascii=False, indent=2))
        return 0
    if args.config_action == "path":
        ctx.emit({"config": str(ctx.cfg.file), "home": str(ctx.cfg.root)}, f"{ctx.cfg.file}\n{ctx.cfg.root}")
        return 0
    if args.config_action == "get":
        value = ctx.cfg.get(args.key)
        if ctx.json_mode:
            ctx.emit({"key": args.key, "value": value})
        else:
            print(json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value)
        return 0
    if args.config_action == "set":
        try:
            value = json.loads(args.value)
        except ValueError:
            value = args.value
        ctx.cfg.set(args.key, value)
        ctx.cfg.save()
        ctx.emit({"key": args.key, "value": value}, f"{args.key} = {json.dumps(value, ensure_ascii=False)}")
        return 0
    ctx.error("未知 config 子命令")
    return 2


# --------------------------------------------------------------------------- 注册


def register(subparsers, parent) -> None:
    p = make_parser(subparsers, parent, "install", "下载并安装版本（客户端、库、资源、Java、可选加载器）")
    p.add_argument("version", nargs="?", help="版本号，如 26.3；省略则用配置/最新正式版")
    p.add_argument("--loader", choices=["fabric", "quilt", "none"], default=None, help="同时安装模组加载器")
    p.add_argument("--loader-version", default=None, help="加载器版本，默认最新稳定版")
    p.add_argument("--skip-assets", action="store_true", help="跳过资源文件（省流量，但游戏可能缺贴图）")
    p.add_argument("--skip-java", action="store_true", help="跳过 Java 运行时检查/安装")
    p.add_argument("--no-auto-java", action="store_true", help="缺少 Java 时不自动下载")
    p.add_argument("--refresh", action="store_true", help="强制刷新版本清单")
    p.set_defaults(func=cmd_install)

    p = make_parser(subparsers, parent, "launch", "启动游戏")
    p.add_argument("version", nargs="?", help="版本号，默认最近安装/配置的版本")
    p.add_argument("--username", default=None, help="玩家名（默认取配置 player.name）")
    p.add_argument("--uuid", default=None, help="在线模式 UUID（配合 --access-token）")
    p.add_argument("--access-token", default=None, help="在线模式访问令牌")
    p.add_argument("--user-type", default="msa", choices=["msa", "legacy"], help="在线模式用户类型")
    p.add_argument("--memory", type=int, default=None, help="最大内存 MB（-Xmx）")
    p.add_argument("--jvm-arg", action="append", default=[], help="追加 JVM 参数，可重复")
    p.add_argument("--game-arg", action="append", default=[], help="追加游戏参数，可重复")
    p.add_argument("--width", type=int, default=None, help="窗口宽")
    p.add_argument("--height", type=int, default=None, help="窗口高")
    p.add_argument("--window-type", default=None, choices=["windowed", "fullscreen", "maximized"],
                   help="窗口类型（默认取设置）")
    p.add_argument("--quick-play", default=None, choices=["none", "singleplayer", "multiplayer", "realms"],
                   help="快速游戏：直接进入存档/服务器")
    p.add_argument("--quick-play-value", default=None, help="存档目录名或服务器地址")
    p.add_argument("--priority", default=None,
                   choices=["low", "below_normal", "normal", "above_normal", "high"], help="进程优先级")
    p.add_argument("--wrapper", default=None, help="包装命令（如 optirun）")
    p.add_argument("--demo", action="store_true", help="以演示模式启动")
    p.add_argument("--dry-run", action="store_true", help="只打印启动命令，不真正启动")
    p.add_argument("--detach", action="store_true", help="后台启动，立刻返回")
    p.add_argument("--no-auto-java", action="store_true", help="缺少 Java 时不自动下载")
    p.set_defaults(func=cmd_launch)

    p = make_parser(subparsers, parent, "list", "列出已安装/可安装版本")
    p.add_argument("--available", action="store_true", help="同时列出可安装版本")
    p.add_argument("--limit", type=int, default=25, help="可安装版本数量上限")
    p.set_defaults(func=cmd_list)

    p = make_parser(subparsers, parent, "info", "查看某个版本的解析结果与安装完整性")
    p.add_argument("version", nargs="?", help="版本号，默认取配置/最近安装")
    p.set_defaults(func=cmd_info)

    p = make_parser(subparsers, parent, "doctor", "环境体检：目录、磁盘、镜像、Java、已安装版本、CAS")
    p.add_argument("--timeout", type=float, default=10.0, help="单项探测超时秒数")
    p.set_defaults(func=cmd_doctor)

    p = make_parser(subparsers, parent, "java", "管理 Java 运行时")
    java_sub = p.add_subparsers(dest="java_action", metavar="<子命令>")
    jp = java_sub.add_parser("list", parents=[parent], help="列出运行时")
    jp.set_defaults(func=cmd_java, java_action="list")
    jp = java_sub.add_parser("install", parents=[parent], help="安装运行时")
    jp.add_argument("--major", type=int, default=21, help="Java 主版本，如 21")
    jp.add_argument("--provider", choices=["mojang", "adoptium"], default="mojang", help="来源")
    jp.add_argument("--component", default="epsilon", help="Mojang 运行时组件名后缀（epsilon 对应 Java 25）")
    jp.set_defaults(func=cmd_java, java_action="install")
    jp = java_sub.add_parser("path", parents=[parent], help="打印当前 Java 路径")
    jp.set_defaults(func=cmd_java, java_action="path")

    p = make_parser(subparsers, parent, "mirror", "查看/探测下载镜像")
    mirror_sub = p.add_subparsers(dest="mirror_action", metavar="<子命令>")
    mp = mirror_sub.add_parser("list", parents=[parent], help="列出镜像配置")
    mp.set_defaults(func=cmd_mirror, mirror_action="list")
    mp = mirror_sub.add_parser("test", parents=[parent], help="探测镜像可用性")
    mp.add_argument("--timeout", type=float, default=10.0)
    mp.set_defaults(func=cmd_mirror, mirror_action="test")

    p = make_parser(subparsers, parent, "config", "查看/修改配置")
    config_sub = p.add_subparsers(dest="config_action", metavar="<子命令>")
    cp = config_sub.add_parser("list", parents=[parent], help="打印完整配置")
    cp.set_defaults(func=cmd_config, config_action="list")
    cp = config_sub.add_parser("path", parents=[parent], help="配置文件与数据目录")
    cp.set_defaults(func=cmd_config, config_action="path")
    cp = config_sub.add_parser("get", parents=[parent], help="读取某项配置")
    cp.add_argument("key")
    cp.set_defaults(func=cmd_config, config_action="get")
    cp = config_sub.add_parser("set", parents=[parent], help="写入某项配置")
    cp.add_argument("key")
    cp.add_argument("value")
    cp.set_defaults(func=cmd_config, config_action="set")
