"""命令行入口：全局参数 + 命令模块自动发现。"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from . import __version__, config, util
from .commands import discover


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcdecent",
        description="去中心化 Minecraft 命令行启动器（内容寻址 + P2P + 多镜像）",
        epilog="示例：mcdecent install 1.21.4 --loader fabric && mcdecent launch",
    )
    parser.add_argument("--version", action="version", version=f"mcdecent {__version__}")
    parser.add_argument("--home", default=None, help="启动器数据目录（默认 ~/.mcdecent）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出错误")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果（脚本友好）")
    parser.add_argument("--no-progress", action="store_true", help="关闭进度条")
    parser.add_argument("--instance", default=None, help="游戏实例名（默认取默认实例）")

    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--home", default=argparse.SUPPRESS, help="启动器数据目录")
    parent.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    parent.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)
    parent.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parent.add_argument("--no-progress", action="store_true", default=argparse.SUPPRESS)
    parent.add_argument("--instance", default=argparse.SUPPRESS, help="游戏实例名")

    subparsers = parser.add_subparsers(dest="command", metavar="<命令>")
    for module in discover():
        try:
            module.register(subparsers, parent)
        except Exception as exc:  # noqa: BLE001 - 单个命令模块故障不应拖垮整个 CLI
            util.LOG.debug("命令模块 %s 注册失败：%r", getattr(module, "__name__", module), exc)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "func", None):
        # 没有子命令：交互式终端直接进入 shell，非交互（管道/脚本）则打印帮助
        if sys.stdin.isatty():
            from . import shell

            return shell.run(home=getattr(args, "home", None))
        parser.print_help()
        print("\n提示：直接运行 `mcdecent`（无参数）可进入交互模式。")
        return 2
    try:
        rc = args.func(args)
        return int(rc or 0)
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，给出人类可读的错误
        from .net import DownloadError

        if isinstance(exc, DownloadError):
            print(f"错误：{exc}", file=sys.stderr)
            for line in exc.errors[:5]:
                print(f"  - {line}", file=sys.stderr)
        else:
            print(f"错误：{exc}", file=sys.stderr)
            if getattr(args, "verbose", False):
                import traceback

                traceback.print_exc()
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
