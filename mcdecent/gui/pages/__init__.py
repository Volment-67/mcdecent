"""页面注册表。"""

from __future__ import annotations

import importlib

import tkinter as tk

from .. import theme


class PageBase(tk.Frame):
    title = ""

    def __init__(self, parent, app):
        super().__init__(parent, bg=theme.WINDOW_BG)
        self.app = app
        self._build()

    def _build(self) -> None:  # pragma: no cover - 子类实现
        pass

    def refresh(self) -> None:
        pass

    def on_resize(self) -> None:
        pass

    def on_game_state(self, running: bool) -> None:
        pass


_PAGES = ("home", "accounts", "instance_list", "instance_manage", "download",
          "settings", "multiplayer", "group")
_ALIASES = {"instances": "instance_list", "launch": "home", "packs": "instance_manage"}


def create(name: str, parent, app):
    target = _ALIASES.get(name, name)
    if target not in _PAGES:
        return None
    module = importlib.import_module(f"{__name__}.{target}")
    page = module.Page(parent, app)
    page.title = getattr(module, "TITLE", target)
    return page
