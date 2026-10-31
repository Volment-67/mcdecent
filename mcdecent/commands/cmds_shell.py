"""`mcdecent shell`：进入交互式命令模式。"""

from __future__ import annotations

from . import Context, make_parser


def cmd_shell(args) -> int:
    from .. import shell

    return shell.run(home=getattr(args, "home", None), quiet=getattr(args, "quiet", False))


def register(subparsers, parent) -> None:
    p = make_parser(subparsers, parent, "shell", "进入交互式命令模式（在 cmd/PowerShell 里直接敲命令）")
    p.set_defaults(func=cmd_shell)
