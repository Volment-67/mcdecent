"""账户页：HMCL「账户列表」布局（添加账户 + 已有账户）。"""

from __future__ import annotations

import tkinter as tk

from ... import auth
from .. import theme
from ..widgets import HButton
from . import PageBase

TITLE = "账户列表"


class Page(PageBase):
    def _build(self) -> None:
        self.panel = tk.Frame(self, width=200, bg=theme.SIDEBAR_BG)
        self.panel.pack(side="left", fill="y")
        self.panel.pack_propagate(False)
        self.list_frame = tk.Frame(self.panel, bg=theme.SIDEBAR_BG)
        self.list_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.content = tk.Frame(self, bg=theme.WINDOW_BG)
        self.content.pack(side="left", fill="both", expand=True)

    def _section(self, text: str) -> None:
        frame = tk.Frame(self.list_frame, bg=theme.SIDEBAR_BG)
        frame.pack(fill="x", pady=(10, 2))
        tk.Label(frame, text=text, bg=theme.SIDEBAR_BG, fg=theme.SIDEBAR_SECTION,
                 font=theme.font(9), anchor="w").pack(fill="x")
        tk.Frame(frame, bg=theme.DIVIDER, height=1).pack(fill="x", pady=(2, 0))

    def _row(self, text: str, subtitle: str, icon: str, command, *, remove=None) -> None:
        row = tk.Frame(self.list_frame, bg=theme.SIDEBAR_BG, cursor="hand2", padx=6, pady=5)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=icon, bg=theme.SIDEBAR_BG, fg=theme.TEXT_SECONDARY,
                 font=theme.font(12), width=2, anchor="w").pack(side="left")
        texts = tk.Frame(row, bg=theme.SIDEBAR_BG)
        texts.pack(side="left", fill="x", expand=True)
        name = tk.Label(texts, text=text, bg=theme.SIDEBAR_BG, fg=theme.TEXT,
                        font=theme.font(9, bold=True), anchor="w")
        name.pack(fill="x")
        if subtitle:
            tk.Label(texts, text=subtitle, bg=theme.SIDEBAR_BG, fg=theme.TEXT_SECONDARY,
                     font=theme.font(8), anchor="w").pack(fill="x")
        if remove:
            close = tk.Label(row, text="✕", bg=theme.SIDEBAR_BG, fg=theme.TEXT_SECONDARY,
                             font=theme.font(9), cursor="hand2")
            close.pack(side="right")
            close.bind("<Button-1>", lambda _e: remove())
        for widget in (row, texts, name):
            widget.bind("<Button-1>", lambda _e: command())

    def refresh(self) -> None:
        for widget in self.list_frame.winfo_children():
            widget.destroy()
        self._section("添加账户")
        self._row("微软账户", "官方登录（暂未接入）", "▦", lambda: self._unsupported("微软账户"))
        self._row("离线模式", "使用离线 UUID 进入游戏", "👤", self.add_offline)
        self._row("外置登录", "authlib-injector 认证服务器（暂未接入）", "添",
                  lambda: self._unsupported("外置登录"))
        self._section("已添加账户")
        accounts = self._accounts()
        if not accounts:
            tk.Label(self.list_frame, text="还没有账户，点击上方「离线模式」添加",
                     bg=theme.SIDEBAR_BG, fg=theme.TEXT_SECONDARY, font=theme.font(8),
                     wraplength=230, justify="left").pack(anchor="w", pady=8)
        current = self.app.cfg.get("player.name")
        for account in accounts:
            name = str(account.get("name"))
            suffix = "（当前）" if name == current else account.get("uuid", "")[:8]
            self._row(name, suffix, "👤", lambda n=name: self.use_account(n),
                      remove=lambda n=name: self.remove_account(n))
        bottom = tk.Frame(self.panel, bg=theme.SIDEBAR_BG)
        bottom.pack(side="bottom", fill="x", padx=10, pady=10)
        tk.Label(bottom, text="+ 添加认证服务器", bg=theme.SIDEBAR_BG, fg=theme.TEXT,
                 font=theme.font(9), anchor="w", cursor="hand2").pack(fill="x")
        tk.Label(bottom, text="外置登录", bg=theme.SIDEBAR_BG, fg=theme.TEXT_SECONDARY,
                 font=theme.font(8), anchor="w").pack(fill="x")
        # 右侧空内容区显示提示
        for widget in self.content.winfo_children():
            widget.destroy()
        tk.Label(self.content, text="账户列表", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(12, bold=True)).pack(anchor="nw", padx=20, pady=20)
        tk.Label(self.content,
                 text="选择左侧账户即可切换；离线账户 UUID 由玩家名按官方离线算法生成，\n"
                      "不依赖任何认证服务器。",
                 bg=theme.WINDOW_BG, fg=theme.TEXT_SECONDARY, font=theme.font(9),
                 justify="left").pack(anchor="nw", padx=20)

    # ------------------------------------------------------------ 账户操作
    def _accounts(self) -> list[dict]:
        items = self.app.cfg.get("accounts")
        if not isinstance(items, list):
            items = []
        return [dict(item) for item in items if isinstance(item, dict) and item.get("name")]

    def add_offline(self) -> None:
        name = self.app.ask_string("添加离线账户", "玩家名（1–16 字符）：", "Steve")
        if not name:
            return
        name = name.strip()[:16]
        accounts = self._accounts()
        if not any(item.get("name") == name for item in accounts):
            accounts.append({"name": name, "uuid": auth.offline_uuid(name)})
        self.app.cfg.set("accounts", accounts)
        self.app.cfg.set("player.name", name)
        self.app.cfg.save()
        self.app.log(f"[账户] 已添加离线账户 {name}（{auth.offline_uuid(name)}）\n")
        self.refresh()

    def use_account(self, name: str) -> None:
        self.app.cfg.set("player.name", name)
        self.app.cfg.save()
        self.app.log(f"[账户] 当前账户：{name}\n")
        self.refresh()

    def remove_account(self, name: str) -> None:
        if not self.app.ask_yesno("删除账户", f"确定删除账户「{name}」？"):
            return
        accounts = [item for item in self._accounts() if item.get("name") != name]
        self.app.cfg.set("accounts", accounts)
        if self.app.cfg.get("player.name") == name:
            self.app.cfg.set("player.name", accounts[0]["name"] if accounts else "Steve")
        self.app.cfg.save()
        self.refresh()

    def _unsupported(self, what: str) -> None:
        self.app.show_error(what, f"{what} 暂未接入。\n当前版本支持离线账户；微软登录与外置登录在 Roadmap 中。")
