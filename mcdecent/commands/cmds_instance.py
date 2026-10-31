"""`instance` 命令：实例（版本隔离）管理。"""

from __future__ import annotations

from .. import util, versions
from ..instances import DEFAULT_INSTANCE_NAME, InstanceManager, format_play_time
from . import Context, make_parser


def _manager(ctx: Context) -> InstanceManager:
    return InstanceManager(ctx.cfg)


def cmd_instance_list(args) -> int:
    ctx = Context(args)
    manager = _manager(ctx)
    manager.ensure_default()
    items = [manager.describe(inst) for inst in manager.list()]
    default = manager.default()
    if ctx.json_mode:
        ctx.emit({"default": default.name if default else None, "instances": items})
        return 0
    if not items:
        print("没有实例。用 `mcdecent instance create <名字> --version <版本>` 创建。")
        return 0
    rows = []
    for item in items:
        loader = item.get("loader") or {}
        rows.append([
            item["name"], item["version"], f"{loader.get('type', '')} {loader.get('version', '')}".strip() or "原版",
            "隔离" if item["isolated"] else "共享", item.get("mods", 0), item.get("saves", 0),
            "是" if default and item["name"] == default.name else "",
        ])
    widths = [len(h) for h in ("实例", "版本", "加载器", "目录", "模组", "存档", "默认")]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    header = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(("实例", "版本", "加载器", "目录", "模组", "存档", "默认")))
    print(header)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
    return 0


def cmd_instance_create(args) -> int:
    ctx = Context(args)
    manager = _manager(ctx)
    manager.ensure_default()
    version = args.version
    if not version:
        installed = versions.installed_versions(ctx.cfg)
        if not installed:
            ctx.error("还没有已安装版本，请先运行 `mcdecent install <版本>`")
            return 1
        version = installed[0]["id"]
    try:
        from ..instances import default_isolation

        inst = manager.create(
            args.name or "新实例",
            version,
            isolated=False if args.no_isolate else default_isolation(ctx.cfg, version),
            game_dir=args.game_dir,
        )
    except Exception as exc:  # noqa: BLE001
        ctx.error(f"创建实例失败：{exc}")
        return 1
    payload = manager.describe(inst)
    ctx.emit(payload, f"已创建实例「{inst.name}」：版本 {inst.version_id}，目录 {manager.game_dir(inst)}")
    return 0


def cmd_instance_remove(args) -> int:
    ctx = Context(args)
    manager = _manager(ctx)
    inst = manager.get(args.name)
    if inst is None:
        ctx.error(f"实例不存在：{args.name}")
        return 1
    if manager.default() is inst and len(manager.list()) > 1:
        ctx.warn("删除的是默认实例，将自动切换到其它实例。")
    ok = manager.delete(inst.name, delete_files=args.delete_files)
    ctx.emit({"deleted": ok, "name": inst.name, "delete_files": bool(args.delete_files)},
             f"已删除实例「{inst.name}」" + ("（含游戏文件）" if args.delete_files else "（保留游戏文件）"))
    return 0 if ok else 1


def cmd_instance_rename(args) -> int:
    ctx = Context(args)
    manager = _manager(ctx)
    inst = manager.rename(args.name, args.new_name)
    if inst is None:
        ctx.error(f"实例不存在：{args.name}")
        return 1
    ctx.emit({"name": inst.name}, f"已重命名为「{inst.name}」")
    return 0


def cmd_instance_use(args) -> int:
    ctx = Context(args)
    manager = _manager(ctx)
    try:
        inst = manager.set_default(args.name)
    except KeyError as exc:
        ctx.error(str(exc))
        return 1
    ctx.emit(manager.describe(inst), f"默认实例已切换为「{inst.name}」（版本 {inst.version_id}）")
    return 0


def cmd_instance_info(args) -> int:
    ctx = Context(args)
    manager = _manager(ctx)
    inst = manager.get(args.name) if args.name else manager.default()
    if inst is None:
        ctx.error("没有实例。")
        return 1
    info = manager.describe(inst)
    info["play_time"] = format_play_time(inst.play_time_seconds)
    if ctx.json_mode:
        ctx.emit(info)
        return 0
    text = "\n".join([
        f"实例：{inst.name}（{'隔离' if inst.isolated else '共享'}目录）",
        f"版本：{inst.version_id or '-'}    加载器：{info.get('loader') or '原版'}",
        f"目录：{info['dir']}",
        f"模组 {info['mods']} · 存档 {info['saves']} · 资源包 {info['resourcepacks']} · 光影 {info['shaderpacks']}",
        f"内存：{inst.memory_mb or '跟随全局'}    JVM：{' '.join(inst.jvm_args) or '-'}",
        f"启动前命令：{inst.pre_launch or '-'}",
        f"最近游玩：{inst.last_played_at or '从未'}    累计：{format_play_time(inst.play_time_seconds)}",
    ])
    ctx.emit(info, text)
    return 0


def register(subparsers, parent) -> None:
    p = make_parser(subparsers, parent, "instance", "实例（版本隔离）管理")
    sub = p.add_subparsers(dest="instance_action", metavar="<操作>")

    sp = sub.add_parser("list", parents=[parent], help="列出实例")
    sp.set_defaults(func=cmd_instance_list)

    sp = sub.add_parser("create", parents=[parent], help="创建实例")
    sp.add_argument("name", nargs="?", default="新实例", help="实例名")
    sp.add_argument("--version", default=None, help="游戏版本，默认最近安装的版本")
    sp.add_argument("--no-isolate", action="store_true", help="使用共享游戏目录（不隔离）")
    sp.add_argument("--game-dir", default=None, help="自定义游戏目录")
    sp.set_defaults(func=cmd_instance_create)

    sp = sub.add_parser("remove", parents=[parent], help="删除实例")
    sp.add_argument("name")
    sp.add_argument("--delete-files", action="store_true", help="同时删除实例目录文件")
    sp.set_defaults(func=cmd_instance_remove)

    sp = sub.add_parser("rename", parents=[parent], help="重命名实例")
    sp.add_argument("name")
    sp.add_argument("new_name")
    sp.set_defaults(func=cmd_instance_rename)

    sp = sub.add_parser("use", parents=[parent], help="设为默认实例")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_instance_use)

    sp = sub.add_parser("info", parents=[parent], help="查看实例详情")
    sp.add_argument("name", nargs="?", default=None)
    sp.set_defaults(func=cmd_instance_info)
