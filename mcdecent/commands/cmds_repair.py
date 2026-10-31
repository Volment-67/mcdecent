"""`repair` 命令：诊断常见故障并一键修复（底层是 mcdecent.repair）。"""

from __future__ import annotations

from . import Context, make_parser


def _load_repair():
    try:
        from .. import repair
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"修复模块不可用：{exc!r}") from exc
    return repair


def cmd_repair(args) -> int:
    ctx = Context(args)
    try:
        repair = _load_repair()
    except RuntimeError as exc:
        ctx.error(str(exc))
        return 1

    if args.all or args.issue:
        targets = []
        if args.issue:
            targets = [args.issue]
        else:
            issues = repair.diagnose(ctx.cfg, ctx.instance, include_network=args.network)
            targets = [issue.id for issue in issues if getattr(issue, "auto_fixable", False)]
        results = []
        for issue_id in targets:
            try:
                result = repair.apply_fix(ctx.cfg, issue_id, instance=ctx.instance, progress=ctx.progress)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "issue_id": issue_id, "message": repr(exc), "actions": []}
            results.append(result)
            ctx.emit(result, f"[{'OK' if result.get('ok') else '失败'}] {issue_id}：{result.get('message', '')}")
        if ctx.json_mode:
            ctx.emit({"fixed": results})
        return 0 if all(item.get("ok") for item in results) else 1

    issues = repair.diagnose(ctx.cfg, ctx.instance, include_network=args.network)
    if ctx.json_mode:
        from dataclasses import asdict

        ctx.emit({"issues": [asdict(issue) for issue in issues]})
        return 0 if not issues else 0
    if not issues:
        print("没有发现问题，一切正常。")
        return 0
    print(f"发现 {len(issues)} 个问题：\n")
    for issue in issues:
        print(f"[{issue.severity}] {issue.id}: {issue.title}")
        print(f"  原因：{issue.detail}")
        for cause in issue.causes:
            print(f"  · {cause}")
        for fix in issue.fixes:
            print(f"  修复（{fix.action}）：{fix.label} {fix.description}")
        print()
    if args.fix:
        return cmd_repair(_with_args(args, all=True))
    return 0


def _with_args(args, **updates):
    import argparse

    data = vars(args).copy()
    data.update(updates)
    return argparse.Namespace(**data)


def register(subparsers, parent) -> None:
    p = make_parser(subparsers, parent, "repair", "诊断常见故障并给出/执行修复方案")
    p.add_argument("--network", action="store_true", help="额外探测网络与镜像")
    p.add_argument("--fix", action="store_true", help="修复所有可自动修复的问题")
    p.add_argument("--all", action="store_true", help="同上（执行全部自动修复）")
    p.add_argument("--issue", default=None, help="只修复指定问题 id")
    p.set_defaults(func=cmd_repair)
