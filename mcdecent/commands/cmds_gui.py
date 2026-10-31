"""`mcdecent gui`：打开图形界面。"""

from __future__ import annotations

from . import make_parser


def cmd_gui(args) -> int:
    from .. import gui

    return gui.main()


def register(subparsers, parent) -> None:
    p = make_parser(subparsers, parent, "gui", "打开图形界面（双击版启动器）")
    p.set_defaults(func=cmd_gui)
