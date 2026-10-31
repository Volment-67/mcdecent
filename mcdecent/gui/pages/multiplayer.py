"""多人联机页（HMCL 对应页的占位实现）。"""

from __future__ import annotations

import tkinter as tk

from .. import theme
from ..widgets import HButton, RoundedCard
from . import PageBase

TITLE = "多人联机"


class Page(PageBase):
    def _build(self) -> None:
        body = tk.Frame(self, bg=theme.WINDOW_BG)
        body.pack(fill="both", expand=True, padx=18, pady=14)
        tk.Label(body, text="多人联机", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(12, bold=True), anchor="w").pack(fill="x")
        tk.Label(body, text="HMCL 使用 Terracotta 提供无需公网 IP 的联机房间；mcdecent 暂未接入该服务。",
                 bg=theme.WINDOW_BG, fg=theme.TEXT_SECONDARY, font=theme.font(9),
                 anchor="w", justify="left").pack(fill="x", pady=(4, 12))
        card = RoundedCard(body, padding=(16, 12))
        card.pack(fill="x", pady=(0, 8))
        tk.Label(card.body, text="创建联机房间", bg=theme.CARD_BG, fg=theme.TEXT,
                 font=theme.font(10, bold=True), anchor="w").pack(fill="x")
        tk.Label(card.body, text="需要 Terracotta 中继服务（暂未接入）", bg=theme.CARD_BG,
                 fg=theme.TEXT_SECONDARY, font=theme.font(8), anchor="w").pack(fill="x")
        HButton(card.body, "创建房间（未接入）", kind="secondary", width=150, height=30,
                font_size=9, command=lambda: self.app.show_error(
                    "多人联机", "Terracotta 联机服务暂未接入。\n可使用局域网联机，或在「服务器」页添加服务器。")).pack(anchor="w", pady=(8, 0))
        card2 = RoundedCard(body, padding=(16, 12))
        card2.pack(fill="x")
        tk.Label(card2.body, text="通过服务器联机", bg=theme.CARD_BG, fg=theme.TEXT,
                 font=theme.font(10, bold=True), anchor="w").pack(fill="x")
        tk.Label(card2.body, text="在实例管理的「服务器」页添加服务器地址，启动后即可直接进入。",
                 bg=theme.CARD_BG, fg=theme.TEXT_SECONDARY, font=theme.font(8), anchor="w").pack(fill="x")
        HButton(card2.body, "打开服务器列表", kind="secondary", width=150, height=30,
                font_size=9, command=lambda: self.app.show_page("instance_manage")).pack(anchor="w", pady=(8, 0))
