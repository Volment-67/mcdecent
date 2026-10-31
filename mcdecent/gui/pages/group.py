"""官方群组页（HMCL 对应页的占位实现）。"""

from __future__ import annotations

import tkinter as tk

from .. import theme
from ..widgets import RoundedCard
from . import PageBase

TITLE = "官方群组"


class Page(PageBase):
    def _build(self) -> None:
        body = tk.Frame(self, bg=theme.WINDOW_BG)
        body.pack(fill="both", expand=True, padx=18, pady=14)
        tk.Label(body, text="官方群组 / 关于 mcdecent", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(12, bold=True), anchor="w").pack(fill="x")
        tk.Label(body, text="本项目为学习用途的去中心化 Minecraft 启动器，不依赖任何中心服务。",
                 bg=theme.WINDOW_BG, fg=theme.TEXT_SECONDARY, font=theme.font(9),
                 anchor="w").pack(fill="x", pady=(4, 12))
        for title, text in (
            ("功能参考", "HMCL（Hello Minecraft! Launcher）——界面与功能对照见 README"),
            ("项目源码", "C:\\MC —— Python 标准库实现，零第三方运行时依赖"),
            ("文档", "C:\\MC\\README.md —— 安装、命令、P2P、诊断修复说明"),
        ):
            card = RoundedCard(body, padding=(16, 10))
            card.pack(fill="x", pady=(0, 8))
            tk.Label(card.body, text=title, bg=theme.CARD_BG, fg=theme.TEXT,
                     font=theme.font(10, bold=True), anchor="w").pack(fill="x")
            tk.Label(card.body, text=text, bg=theme.CARD_BG, fg=theme.TEXT_SECONDARY,
                     font=theme.font(8), anchor="w", justify="left").pack(fill="x")
