"""首页：左侧分组导航 + 背景 + 右下角「启动游戏」按钮（对齐 HMCL 3.15）。"""

from __future__ import annotations

import tkinter as tk

from ... import auth, launch, util, versions
from ...instances import InstanceManager
from .. import theme
from ..widgets import Sidebar, round_rect
from . import PageBase
from .. import assets
from ... import __version__

TITLE = f"mcdecent 启动器 v{__version__}"


class Page(PageBase):
    def _build(self) -> None:
        self.sidebar = Sidebar(self, width=188)
        self.sidebar.pack(side="left", fill="y")
        self.content = tk.Frame(self, bg="#8fb8de")
        self.content.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(self.content, bg="#8fb8de", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._draw_background())
        self._launch_canvas = tk.Canvas(self.canvas, width=228, height=64, bg="#8fb8de",
                                        highlightthickness=0, cursor="hand2")
        self._launch_window = self.canvas.create_window(0, 0, window=self._launch_canvas,
                                                       anchor="se")
        self._build_sidebar()
        self._draw_background()

    # ------------------------------------------------------------ 侧边栏
    def _build_sidebar(self) -> None:
        self.sidebar.add_section("账户")
        self._account_row = tk.Frame(self.sidebar._inner, bg=theme.SIDEBAR_BG, cursor="hand2",
                                     padx=8, pady=5)
        self._account_row.pack(fill="x", pady=1)
        self._avatar_photo = None
        avatar_path = assets.asset("account.png")
        self._avatar = None
        if avatar_path is not None:
            try:
                photo = tk.PhotoImage(file=str(avatar_path))
                step = max(1, photo.width() // 32)
                self._avatar_photo = photo.subsample(step, step)
                self._avatar = tk.Label(self._account_row, bg=theme.SIDEBAR_BG, bd=0,
                                        image=self._avatar_photo)
            except tk.TclError:
                self._avatar = None
        if self._avatar is None:
            self._avatar = tk.Canvas(self._account_row, width=32, height=32,
                                     bg=theme.SIDEBAR_BG, highlightthickness=0)
        self._avatar.pack(side="left", padx=(0, 8))
        account_text = tk.Frame(self._account_row, bg=theme.SIDEBAR_BG)
        account_text.pack(side="left", fill="x", expand=True)
        self._account_name = tk.Label(account_text, text="Steve", bg=theme.SIDEBAR_BG,
                                      fg=theme.TEXT, font=theme.font(10), anchor="w")
        self._account_name.pack(fill="x")
        self._account_type = tk.Label(account_text, text="离线模式", bg=theme.SIDEBAR_BG,
                                      fg=theme.TEXT_SECONDARY, font=theme.font(8), anchor="w")
        self._account_type.pack(fill="x")
        for widget in (self._account_row, self._avatar, account_text, self._account_name, self._account_type):
            widget.bind("<Button-1>", lambda _e: self.app.show_page("accounts"))

        self.sidebar.add_section("游戏")
        self.sidebar.add_item("instance_manage", "实例管理", icon="◆",
                              subtitle="", command=lambda: self.app.show_page("instance_manage"))
        self.sidebar.add_item("instance_list", "实例列表", icon="☰",
                              command=lambda: self.app.show_page("instance_list"))
        self.sidebar.add_item("download", "下载", icon="⬇",
                              command=lambda: self.app.show_page("download"))

        self.sidebar.add_section("通用")
        self.sidebar.add_item("settings", "设置", icon="⚙",
                              command=lambda: self.app.show_page("settings"))
        self.sidebar.add_item("multiplayer", "多人联机", icon="👥",
                              command=lambda: self.app.show_page("multiplayer"))
        self.sidebar.add_item("group", "官方群组", icon="💬",
                              command=lambda: self.app.show_page("group"))

    def _draw_avatar(self) -> None:
        if self._avatar_photo is not None:
            return
        canvas = self._avatar
        canvas.delete("all")
        skin = "#c8946a"
        canvas.create_rectangle(4, 2, 28, 18, fill="#3b2f24", outline="")
        canvas.create_rectangle(6, 6, 26, 28, fill=skin, outline="")
        canvas.create_rectangle(10, 12, 14, 16, fill="#1b1b1b", outline="")
        canvas.create_rectangle(18, 12, 22, 16, fill="#1b1b1b", outline="")
        canvas.create_rectangle(13, 20, 19, 23, fill="#8a5a3a", outline="")
        canvas.create_rectangle(6, 28, 26, 32, fill="#3b6ea5", outline="")

    # ------------------------------------------------------------ 背景
    def _draw_background(self) -> None:
        canvas = self.canvas
        canvas.delete("bg")
        width = max(canvas.winfo_width(), 400)
        height = max(canvas.winfo_height(), 300)
        if self._draw_photo_background(width, height):
            pass
        else:
            self._draw_procedural_background(width, height)
        canvas.coords(self._launch_window, width - 16, height - 16)
        self._draw_launch_button()

    def _draw_photo_background(self, width: int, height: int) -> bool:
        """用桌面 2.webp 生成的背景图铺满内容区（保持比例、居中裁切）。"""
        path = assets.asset("background.png")
        if path is None:
            return False
        try:
            from PIL import Image, ImageTk
        except Exception:  # noqa: BLE001
            return False
        try:
            image = Image.open(path).convert("RGB")
            scale = max(width / image.width, height / image.height)
            resized = image.resize((max(1, int(image.width * scale) + 1),
                                    max(1, int(image.height * scale) + 1)), Image.LANCZOS)
            left = (resized.width - width) // 2
            top = (resized.height - height) // 2
            resized = resized.crop((left, top, left + width, top + height))
            self._bg_photo = ImageTk.PhotoImage(resized)
            self.canvas.create_image(0, 0, anchor="nw", image=self._bg_photo, tags="bg")
            return True
        except Exception:  # noqa: BLE001
            return False

    def _draw_procedural_background(self, width: int, height: int) -> None:
        canvas = self.canvas
        horizon = int(height * 0.64)

        # 天空渐变（上深下浅）
        for index in range(horizon):
            ratio = index / max(1, horizon)
            color = "#%02x%02x%02x" % (
                int(96 + 100 * ratio), int(150 + 80 * ratio), int(208 + 40 * ratio))
            canvas.create_line(0, index, width, index, fill=color, tags="bg")
        # 太阳光晕
        canvas.create_oval(width * 0.72 - 90, -60, width * 0.72 + 90, 120,
                           fill="#dcecff", outline="", tags="bg")
        canvas.create_oval(width * 0.72 - 46, -20, width * 0.72 + 46, 72,
                           fill="#f4f9ff", outline="", tags="bg")
        # 云：多椭圆叠加，边缘柔和
        for cx, cy, scale in ((width * 0.18, height * 0.18, 1.0),
                              (width * 0.58, height * 0.10, 0.72),
                              (width * 0.80, height * 0.30, 0.58),
                              (width * 0.36, height * 0.34, 0.45)):
            for dx, dy, rw, rh in ((0, 0, 54, 20), (34, -10, 40, 16), (-32, -6, 34, 14),
                                   (12, 10, 44, 16), (-12, 8, 36, 14)):
                canvas.create_oval(cx + dx * scale - rw * scale, cy + dy * scale - rh * scale,
                                   cx + dx * scale + rw * scale, cy + dy * scale + rh * scale,
                                   fill="#ffffff", outline="", tags="bg")
        # 水面渐变
        for index in range(horizon, height):
            ratio = (index - horizon) / max(1, height - horizon)
            color = "#%02x%02x%02x" % (
                int(150 - 40 * ratio), int(196 - 30 * ratio), int(228 - 10 * ratio))
            canvas.create_line(0, index, width, index, fill=color, tags="bg")
        # 水面倒影
        for index in range(0, 7):
            y = horizon + 10 + index * max(8, int((height - horizon) / 8))
            canvas.create_line(width * 0.55, y, width * 0.85, y + 3, fill="#cfe6f7",
                               width=2, tags="bg")
        # 草地小岛
        island_left, island_right = int(width * 0.58), width + 40
        canvas.create_rectangle(island_left, horizon - 6, island_right, horizon + 26,
                                fill="#6faa45", outline="", tags="bg")
        canvas.create_rectangle(island_left + 18, horizon - 18, island_right, horizon - 4,
                                fill="#7fbd4f", outline="", tags="bg")
        canvas.create_rectangle(island_left + 40, horizon - 30, island_right, horizon - 16,
                                fill="#8fd05c", outline="", tags="bg")
        # 树（分层方块 + 树干）
        for ratio in (0.68, 0.80, 0.92):
            x = width * ratio
            canvas.create_rectangle(x, horizon - 84, x + 12, horizon - 4, fill="#6b4a2b",
                                    outline="", tags="bg")
            canvas.create_rectangle(x - 30, horizon - 118, x + 44, horizon - 92,
                                    fill="#3f8f3a", outline="", tags="bg")
            canvas.create_rectangle(x - 20, horizon - 136, x + 34, horizon - 112,
                                    fill="#4aa144", outline="", tags="bg")
            canvas.create_rectangle(x - 8, horizon - 150, x + 22, horizon - 130,
                                    fill="#57b34d", outline="", tags="bg")


    # ------------------------------------------------------------ 启动按钮
    def _draw_launch_button(self) -> None:
        canvas = self._launch_canvas
        canvas.delete("all")
        width, height = 228, 64
        round_rect(canvas, 1, 1, width - 38, height - 1, 8, fill=theme.PRIMARY,
                   outline=theme.PRIMARY)
        round_rect(canvas, width - 35, 1, width - 1, height - 1, 8, fill=theme.PRIMARY,
                   outline=theme.PRIMARY)
        instance = self.app.current_instance()
        title = "开始游戏" if instance and instance.version_id else "没有游戏实例"
        canvas.create_text((width - 38) / 2, 24, text=title, fill="white",
                           font=theme.font(12, bold=True), tags="launch")
        if instance and instance.version_id:
            canvas.create_text((width - 38) / 2, 45, text=self._version_text(), fill="#e6e6ff",
                               font=theme.font(9), tags="launch")
        canvas.create_text(width - 18, height / 2, text="▲", fill="white",
                           font=theme.font(9), tags="arrow")
        canvas.bind("<Button-1>", self._on_launch_click)
        canvas.bind("<Enter>", lambda _e: self._set_button_hover(True))
        canvas.bind("<Leave>", lambda _e: self._set_button_hover(False))
        self._hover = getattr(self, "_hover", False)

    def _set_button_hover(self, value: bool) -> None:
        self._hover = value
        color = theme.PRIMARY_HOVER if value else theme.PRIMARY
        self._launch_canvas.itemconfigure("launch", fill="white")
        for item in self._launch_canvas.find_all():
            if self._launch_canvas.type(item) == "polygon":
                self._launch_canvas.itemconfigure(item, fill=color, outline=color)

    def _on_launch_click(self, event) -> None:
        # 点右侧箭头区域 → 切换实例菜单
        if event.x > self._launch_canvas.winfo_width() - 40:
            self._show_instance_menu(event)
            return
        self.start_game()

    def _show_instance_menu(self, event) -> None:
        menu = tk.Menu(self, tearoff=0)
        current = self.app.current_instance()
        for inst in self.app.instances.list():
            label = f"● {inst.name}（{inst.version_id}）" if current and inst.name == current.name else f"   {inst.name}（{inst.version_id}）"
            menu.add_command(label=label, command=lambda n=inst.name: self._switch_instance(n))
        menu.add_separator()
        menu.add_command(label="实例列表…", command=lambda: self.app.show_page("instance_list"))
        menu.tk_popup(event.x_root, event.y_root)

    def _switch_instance(self, name: str) -> None:
        self.app.set_instance(name)
        self.refresh()
        self._draw_launch_button()

    # ------------------------------------------------------------ 数据
    def _version_text(self) -> str:
        inst = self.app.current_instance()
        if inst is None or not inst.version_id:
            return "未安装版本"
        try:
            resolved = versions.resolve(self.app.cfg, inst.version_id)
            return resolved.base_id or inst.version_id
        except Exception:  # noqa: BLE001
            return inst.version_id

    def refresh(self) -> None:
        self._draw_avatar()
        inst = self.app.current_instance()
        accounts = self.app.cfg.get("accounts") or []
        if accounts:
            self._account_name.configure(text=self.app.cfg.get("player.name") or "Steve")
            self._account_type.configure(text="离线模式")
        else:
            self._account_name.configure(text="没有游戏账户")
            self._account_type.configure(text="点击此处添加账户")
        subs = getattr(self.sidebar, "_subs", {}).get("instance_manage")
        if subs:
            name_label, _sub_label, _icon = subs
            if inst is not None and inst.version_id:
                name_label.configure(text=inst.name)
                self.sidebar.set_subtitle("instance_manage", self._version_text())
            else:
                name_label.configure(text="没有游戏实例")
                self.sidebar.set_subtitle("instance_manage", "进入下载页安装游戏")
        self._draw_launch_button()
        self.app.set_title(TITLE)

    def _title_text(self) -> str:
        return TITLE

    def on_resize(self) -> None:
        self._draw_background()

    # ------------------------------------------------------------ 启动
    def start_game(self) -> None:
        inst = self.app.current_instance()
        if inst is None or not inst.version_id:
            self.app.show_error("无法启动", "还没有可用的游戏实例/版本。\n请到「实例列表」创建实例，或在「下载」安装游戏版本。")
            return
        self.app.run_cli(["game", "start", inst.version_id], on_done=self._after_start)

    def _after_start(self, rc: int) -> None:
        if rc != 0:
            try:
                from ... import repair

                self.app.run_task(lambda: repair.diagnose(self.app.instance_cfg(), self.app.current_instance()),
                                  on_done=lambda _ok, issues: self.app.show_diagnosis(issues if isinstance(issues, list) else []),
                                  label="启动失败诊断")
            except Exception:  # noqa: BLE001
                pass
