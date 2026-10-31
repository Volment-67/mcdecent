"""HMCL 风格控件：圆角卡片、按钮、开关、滑块、侧边栏、顶栏、列表。"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable

from . import theme


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius=8, **kwargs):
    """在 canvas 上画圆角矩形。"""
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1,
        x2, y1 + radius, x2, y2 - radius, x2, y2,
        x2 - radius, y2, x1 + radius, y2, x1, y2,
        x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class RoundedCard(tk.Frame):
    """白色圆角卡片，内部放 content 帧。"""

    def __init__(self, parent, *, radius: int = 10, bg: str = theme.CARD_BG,
                 border: str | None = theme.CARD_BORDER, padding=(14, 10)):
        super().__init__(parent, bg=parent["bg"] if "bg" in parent.keys() else theme.WINDOW_BG,
                         highlightthickness=0)
        self._canvas = tk.Canvas(self, bg=self["bg"], highlightthickness=0, height=40)
        self._canvas.pack(fill="x")
        self._radius = radius
        self._card_bg = bg
        self._border = border
        self.body = tk.Frame(self._canvas, bg=bg)
        self._window = self._canvas.create_window(padding[0], padding[1], window=self.body,
                                                  anchor="nw")
        self._padding = padding
        self._canvas.bind("<Configure>", self._redraw)
        self.body.bind("<Configure>", self._sync_height)

    def _sync_height(self, _event=None):
        """卡片高度跟随内容，而不是被父容器拉满。"""
        desired = max(4, self.body.winfo_reqheight() + self._padding[1] * 2)
        try:
            if int(self._canvas.cget("height")) != desired:
                self._canvas.configure(height=desired)
        except tk.TclError:
            pass

    def _redraw(self, _event=None):
        self._canvas.delete("card")
        width = self._canvas.winfo_width()
        height = self._canvas.winfo_height()
        if width < 4 or height < 4:
            return
        round_rect(self._canvas, 1, 1, width - 1, height - 1, self._radius,
                   fill=self._card_bg, outline=self._border or self._card_bg, tags="card")
        self._canvas.tag_lower("card")
        self._canvas.itemconfigure(self._window, width=max(10, width - self._padding[0] * 2))


class HButton(tk.Canvas):
    """HMCL 风格按钮（圆角、主色/次要色）。"""

    def __init__(self, parent, text: str, command: Callable | None = None, *,
                 kind: str = "primary", width: int = 120, height: int = 34,
                 radius: int = 8, icon: str = "", font_size: int = 10):
        bg = parent["bg"] if "bg" in parent.keys() else theme.WINDOW_BG
        super().__init__(parent, width=width, height=height, highlightthickness=0, bg=bg)
        self._command = command
        self._kind = kind
        self._radius = radius
        self._text = text
        self._icon = icon
        self._font_size = font_size
        self._hover = False
        self._draw()
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: self._command and self._command())
        self.configure(cursor="hand2")

    def _colors(self):
        if self._kind == "primary":
            fill = theme.PRIMARY_ACTIVE if self._hover else theme.PRIMARY
            text = theme.WHITE
            outline = ""
        elif self._kind == "secondary":
            fill = theme.PRIMARY_LIGHT if self._hover else "#f1f2f9"
            text = theme.PRIMARY
            outline = ""
        elif self._kind == "danger":
            fill = "#fbe9ea" if self._hover else "#f7f2f2"
            text = theme.RED
            outline = ""
        else:
            fill = "#edeeF4" if self._hover else self["bg"]
            text = theme.TEXT
            outline = theme.DIVIDER
        return fill, text, outline

    def _draw(self):
        self.delete("all")
        width, height = int(self["width"]), int(self["height"])
        fill, text_color, outline = self._colors()
        round_rect(self, 1, 1, width - 1, height - 1, self._radius,
                   fill=fill, outline=outline or fill)
        label = (f"{self._icon}  {self._text}" if self._icon else self._text)
        self.create_text(width / 2, height / 2, text=label, fill=text_color,
                         font=theme.font(self._font_size, bold=self._kind == "primary"))

    def _set_hover(self, value: bool):
        self._hover = value
        self._draw()

    def set_text(self, text: str, icon: str = "") -> None:
        self._text = text
        if icon:
            self._icon = icon
        self._draw()


class Toggle(tk.Canvas):
    """HMCL 风格开关。"""

    def __init__(self, parent, variable: tk.BooleanVar, *, command: Callable | None = None,
                 width: int = 44, height: int = 24):
        bg = parent["bg"] if "bg" in parent.keys() else theme.CARD_BG
        super().__init__(parent, width=width, height=height, highlightthickness=0, bg=bg,
                         cursor="hand2")
        self._var = variable
        self._command = command
        self._cw, self._ch = width, height
        self._draw()
        self.bind("<Button-1>", self._toggle)

    def _draw(self):
        self.delete("all")
        on = bool(self._var.get())
        fill = theme.PRIMARY if on else "#d6d8e0"
        round_rect(self, 1, 1, self._cw - 1, self._ch - 1, self._ch // 2, fill=fill, outline=fill)
        cx = self._cw - self._ch // 2 if on else self._ch // 2
        r = self._ch // 2 - 3
        self.create_oval(cx - r, self._ch // 2 - r, cx + r, self._ch // 2 + r, fill="white", outline="")

    def _toggle(self, _event=None):
        self._var.set(not self._var.get())
        self._draw()
        if self._command:
            self._command()


class HSlider(tk.Canvas):
    """简洁滑块（用于内存分配），返回整数。"""

    def __init__(self, parent, *, minimum: int, maximum: int, value: int,
                 command: Callable[[int], None] | None = None, width: int = 260, height: int = 30):
        bg = parent["bg"] if "bg" in parent.keys() else theme.CARD_BG
        super().__init__(parent, width=width, height=height, highlightthickness=0, bg=bg)
        self._min, self._max, self._value = minimum, maximum, value
        self._command = command
        self._cw, self._ch = width, height
        self._draw()
        self.bind("<Button-1>", self._on_drag)
        self.bind("<B1-Motion>", self._on_drag)

    def _ratio(self) -> float:
        span = max(1, self._max - self._min)
        return min(1.0, max(0.0, (self._value - self._min) / span))

    def _draw(self):
        self.delete("all")
        pad = 10
        y = self._ch // 2
        self.create_line(pad, y, self._cw - pad, y, fill="#e0e2ea", width=4, capstyle="round")
        x = pad + (self._cw - pad * 2) * self._ratio()
        self.create_line(pad, y, x, y, fill=theme.PRIMARY, width=4, capstyle="round")
        self.create_oval(x - 8, y - 8, x + 8, y + 8, fill=theme.PRIMARY, outline="white", width=2)

    def _on_drag(self, event):
        pad = 10
        ratio = min(1.0, max(0.0, (event.x - pad) / max(1, self._cw - pad * 2)))
        value = int(self._min + ratio * (self._max - self._min))
        self._value = value
        self._draw()
        if self._command:
            self._command(value)

    def set_value(self, value: int):
        self._value = value
        self._draw()


class Sidebar(tk.Frame):
    """左侧导航：分组标题 + 条目（图标/名称/副标题）。"""

    def __init__(self, parent, *, width: int = 200, bg: str = theme.SIDEBAR_BG):
        super().__init__(parent, width=width, bg=bg)
        self.pack_propagate(False)
        self._bg = bg
        self._items: dict[str, tk.Frame] = {}
        self._selected: str | None = None
        self._commands: dict[str, Callable] = {}
        self._inner = tk.Frame(self, bg=bg)
        self._inner.pack(fill="both", expand=True, padx=10, pady=8)

    def add_section(self, title: str) -> None:
        frame = tk.Frame(self._inner, bg=self._bg)
        frame.pack(fill="x", pady=(12, 2))
        tk.Label(frame, text=title, bg=self._bg, fg=theme.SIDEBAR_SECTION,
                 font=theme.font(9), anchor="w").pack(fill="x")
        tk.Frame(frame, bg=theme.DIVIDER, height=1).pack(fill="x", pady=(2, 0))

    def add_item(self, key: str, text: str, *, icon: str = "•", subtitle: str = "",
                 command: Callable | None = None) -> tk.Frame:
        row = tk.Frame(self._inner, bg=self._bg, cursor="hand2", padx=8, pady=4)
        row.pack(fill="x", pady=1)
        icon_label = tk.Label(row, text=icon, bg=self._bg, fg=theme.TEXT_SECONDARY,
                              font=theme.font(12), width=2, anchor="w")
        icon_label.pack(side="left")
        text_frame = tk.Frame(row, bg=self._bg)
        text_frame.pack(side="left", fill="x", expand=True)
        name = tk.Label(text_frame, text=text, bg=self._bg, fg=theme.TEXT,
                        font=theme.font(10), anchor="w")
        name.pack(fill="x")
        sub = None
        if subtitle:
            sub = tk.Label(text_frame, text=subtitle, bg=self._bg, fg=theme.TEXT_SECONDARY,
                           font=theme.font(8), anchor="w")
            sub.pack(fill="x")
        widgets = [row, icon_label, text_frame, name] + ([sub] if sub else [])
        for widget in widgets:
            widget.bind("<Button-1>", lambda _e, k=key: self.select(k))
        self._items[key] = row
        self._subs = getattr(self, "_subs", {})
        self._subs[key] = (name, sub, icon_label)
        if command:
            self._commands[key] = command
        return row

    def set_subtitle(self, key: str, text: str) -> None:
        name, sub, _icon = getattr(self, "_subs", {}).get(key, (None, None, None))
        if sub is None and name is not None:
            sub = tk.Label(name.master, bg=self._bg, fg=theme.TEXT_SECONDARY,
                           font=theme.font(8), anchor="w")
            sub.pack(fill="x")
            self._subs[key] = (name, sub, _icon)
        if sub is not None:
            sub.configure(text=text)

    def select(self, key: str) -> None:
        if key not in self._items:
            return
        already = self._selected == key
        for item_key, row in self._items.items():
            color = "#ffffff" if item_key == key else self._bg
            self._paint(row, color)
        self._selected = key
        command = self._commands.get(key)
        if command and not already:
            command()

    def _paint(self, widget: tk.Frame, color: str) -> None:
        widget.configure(bg=color)
        for child in widget.winfo_children():
            try:
                child.configure(bg=color)
            except tk.TclError:
                pass
            for grand in child.winfo_children():
                try:
                    grand.configure(bg=color)
                except tk.TclError:
                    pass
        # 细边框模拟 HMCL 选中卡片的阴影
        widget.configure(highlightthickness=0)

    def select_key(self, key: str) -> None:
        if key in self._items:
            self.select(key)

    def set_painted(self, key: str) -> None:
        """只更新高亮，不触发命令（用于页面内部切换时避免递归）。"""
        if key not in self._items:
            return
        self._selected = key
        for item_key, row in self._items.items():
            self._paint(row, "#ffffff" if item_key == key else self._bg)


class TopBar(tk.Frame):
    """紫色顶栏：返回 / 图标标题 / 帮助 / 最小化 / 关闭，支持拖动窗口。"""

    def __init__(self, parent, app, title: str):
        super().__init__(parent, bg=theme.PRIMARY, height=40)
        self.pack_propagate(False)
        self.app = app
        self._drag_x = 0
        self._drag_y = 0
        self.back_button = tk.Label(self, text="‹", bg=theme.PRIMARY, fg="white",
                                    font=theme.font(20, bold=True), width=2, cursor="hand2")
        self.back_button.pack(side="left", padx=(4, 0))
        self.back_button.bind("<Button-1>", lambda _e: app.go_back())
        # 顶栏不再显示启动器图标（用户要求删掉标题左边的图标）
        self.title_label = tk.Label(self, text=title, bg=theme.PRIMARY, fg="white",
                                    font=theme.font(11, bold=True), anchor="w")
        self.title_label.pack(side="left")
        for widget in (self, self.title_label):
            widget.bind("<Button-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<Double-Button-1>", lambda _e: app.toggle_maximize())
        close = tk.Label(self, text="✕", bg=theme.PRIMARY, fg="white", font=theme.font(11),
                         width=3, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: app.on_close())
        close.bind("<Enter>", lambda _e: close.configure(bg="#d9534f"))
        close.bind("<Leave>", lambda _e: close.configure(bg=theme.PRIMARY))
        minimize = tk.Label(self, text="—", bg=theme.PRIMARY, fg="white", font=theme.font(11),
                            width=3, cursor="hand2")
        minimize.pack(side="right")
        minimize.bind("<Button-1>", lambda _e: app.iconify())
        minimize.bind("<Enter>", lambda _e: minimize.configure(bg=theme.PRIMARY_HOVER))
        minimize.bind("<Leave>", lambda _e: minimize.configure(bg=theme.PRIMARY))
        help_button = tk.Label(self, text="?", bg=theme.PRIMARY, fg="white",
                               font=theme.font(11, bold=True), width=3, cursor="hand2")
        help_button.pack(side="right")
        help_button.bind("<Button-1>", lambda _e: app.show_help())
        help_button.bind("<Enter>", lambda _e: help_button.configure(bg=theme.PRIMARY_HOVER))
        help_button.bind("<Leave>", lambda _e: help_button.configure(bg=theme.PRIMARY))

    def _draw_icon(self):
        self.icon.delete("all")
        self.icon.create_rectangle(2, 2, 20, 20, fill="#7bc043", outline="#5a9130")
        self.icon.create_rectangle(6, 6, 9, 9, fill="#1b1b1b", outline="")
        self.icon.create_rectangle(13, 6, 16, 9, fill="#1b1b1b", outline="")
        self.icon.create_rectangle(9, 13, 13, 17, fill="#1b1b1b", outline="")

    def _start_drag(self, event):
        self._drag_x, self._drag_y = event.x_root, event.y_root

    def _drag(self, event):
        dx = event.x_root - self._drag_x
        dy = event.y_root - self._drag_y
        self._drag_x, self._drag_y = event.x_root, event.y_root
        try:
            x, y = self.app.winfo_x(), self.app.winfo_y()
            self.app.geometry(f"+{x + dx}+{y + dy}")
        except tk.TclError:
            pass


class TabStrip(tk.Frame):
    """HMCL 风格横向页签条。"""

    def __init__(self, parent, on_select: Callable[[str], None]):
        super().__init__(parent, bg=theme.WINDOW_BG)
        self._on_select = on_select
        self._tabs: dict[str, tk.Label] = {}
        self._selected: str | None = None

    def add(self, key: str, text: str) -> None:
        label = tk.Label(self, text=text, bg=theme.WINDOW_BG, fg=theme.TEXT_SECONDARY,
                         font=theme.font(10), padx=14, pady=6, cursor="hand2")
        label.pack(side="left", padx=(0, 4))
        label.bind("<Button-1>", lambda _e, k=key: self.select(k))
        self._tabs[key] = label
        if self._selected is None:
            self.select(key)

    def select(self, key: str) -> None:
        self._selected = key
        for tab_key, label in self._tabs.items():
            if tab_key == key:
                label.configure(bg="#ffffff", fg=theme.PRIMARY, font=theme.font(10, bold=True))
            else:
                label.configure(bg=theme.WINDOW_BG, fg=theme.TEXT_SECONDARY,
                                font=theme.font(10))
        self._on_select(key)


def list_style() -> ttk.Style:
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("HMCL.Treeview", background=theme.CARD_BG, fieldbackground=theme.CARD_BG,
                    foreground=theme.TEXT, rowheight=30, borderwidth=0, font=theme.font(10))
    style.configure("HMCL.Treeview.Heading", background=theme.CARD_BG, foreground=theme.TEXT_SECONDARY,
                    font=theme.font(9), relief="flat", borderwidth=0)
    style.map("HMCL.Treeview", background=[("selected", theme.PRIMARY_LIGHT)],
              foreground=[("selected", theme.PRIMARY)])
    return style


def make_table(parent, columns: list[tuple[str, str, int]], *, height: int = 12) -> ttk.Treeview:
    list_style()
    wrapper = tk.Frame(parent, bg=theme.CARD_BG)
    wrapper.pack(fill="both", expand=True)
    tree = ttk.Treeview(wrapper, columns=[c[0] for c in columns], show="headings",
                        height=height, style="HMCL.Treeview")
    for key, title, width in columns:
        tree.heading(key, text=title, anchor="w")
        tree.column(key, width=width, anchor="w")
    scroll = ttk.Scrollbar(wrapper, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    return tree


def hline(parent, **pack_kwargs) -> tk.Frame:
    line = tk.Frame(parent, bg=theme.DIVIDER, height=1)
    line.pack(fill="x", **pack_kwargs)
    return line


class HCheck(tk.Canvas):
    """HMCL 风格复选框（蓝色圆角方块 + 白色对勾）。"""

    def __init__(self, parent, variable: tk.BooleanVar, text: str = "",
                 command: Callable | None = None):
        bg = parent["bg"] if "bg" in parent.keys() else theme.CARD_BG
        text_font = tkfont.Font(family=theme.FONT_FAMILY, size=9)
        width = 24 + text_font.measure(text) + 4 if text else 22
        super().__init__(parent, width=width, height=24, bg=bg, highlightthickness=0,
                         cursor="hand2")
        self._var = variable
        self._text = text
        self._command = command
        self._draw()
        self.bind("<Button-1>", self._toggle)

    def _draw(self):
        self.delete("all")
        checked = bool(self._var.get())
        fill = theme.PRIMARY if checked else "#ffffff"
        outline = theme.PRIMARY if checked else "#c9ccd6"
        round_rect(self, 1, 3, 19, 21, 5, fill=fill, outline=outline)
        if checked:
            self.create_line(5, 12, 9, 17, 16, 7, fill="white", width=2,
                             capstyle="round", joinstyle="round")
        if self._text:
            self.create_text(26, 12, text=self._text, anchor="w", fill=theme.TEXT,
                             font=theme.font(9))

    def _toggle(self, _event=None):
        self._var.set(not self._var.get())
        self._draw()
        if self._command:
            self._command()
