"""HMCL 风格配色与字体（尽量贴近 HMCL 3.15 的视觉）。"""

from __future__ import annotations

import sys
import tkinter.font as tkfont

# 主色（取自 HMCL 顶栏/按钮截图）
PRIMARY = "#5b5bd6"
PRIMARY_HOVER = "#4d4dc4"
PRIMARY_ACTIVE = "#4444b8"
PRIMARY_LIGHT = "#ececfb"

# 文本
TEXT = "#23262b"
TEXT_SECONDARY = "#8b8f98"
TEXT_DISABLED = "#b9bcc4"
WHITE = "#ffffff"

# 背景
WINDOW_BG = "#eef1f8"
SIDEBAR_BG = "#f4f4f6"
SIDEBAR_SECTION = "#8c8c94"
CARD_BG = "#ffffff"
CARD_BORDER = "#e6e7ee"
ROW_HOVER = "#f6f7fb"
DIVIDER = "#e3e4ea"
INPUT_BG = "#f3f4f8"

# 功能色
GREEN = "#43a047"
RED = "#e5484d"

FONT_FAMILY = "Microsoft YaHei UI" if sys.platform.startswith("win") else "Noto Sans CJK SC"
MONO_FAMILY = "Consolas" if sys.platform.startswith("win") else "DejaVu Sans Mono"

_FONTS: dict[str, tkfont.Font] = {}


def font(size: int = 10, bold: bool = False, family: str | None = None) -> tuple:
    """返回 Tk 字体规格元组（不缓存 Font 对象，避免绑定到已销毁的 Tk 根）。"""
    return (family or FONT_FAMILY, size, "bold" if bold else "normal")
