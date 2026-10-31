"""下载页：按 HMCL 3.15.2 实际界面复刻。

左侧：新游戏〔游戏 / 整合包〕、游戏内容〔模组 / 资源包 / 光影 / 世界〕
右侧：
  * 游戏：名称搜索 / 版本类型 / 刷新 + 版本卡片（草方块图标、正式版标签、发布时间、下载/详情）
  * 整合包/模组/资源包/光影/世界：游戏 / 下载源 / 名称 / 游戏版本 / 类别 / 排序
    + 第一页/上一页/页码/下一页/最后一页 + 搜索（整合包另有「安装整合包」）
    + 结果卡片（自动加载热门，不会空白）
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import filedialog, ttk

from ... import catalog, versions
from .. import assets, theme
from ..widgets import HButton, RoundedCard, Sidebar
from . import PageBase

TITLE = "下载"

try:
    from PIL import Image, ImageTk
except Exception:  # noqa: BLE001
    Image = ImageTk = None  # type: ignore[assignment]

CATEGORIES = [
    ("section", "新游戏"),
    ("game", "游戏", "🎮"),
    ("modpack", "整合包", "📦"),
    ("section", "游戏内容"),
    ("mod", "模组", "⭐"),
    ("resourcepack", "资源包", "▨"),
    ("shader", "光影", "☀"),
    ("world", "世界", "🌍"),
]
TYPE_LABELS = {"release": "正式版", "snapshot": "快照", "old_beta": "远古版", "old_alpha": "远古版"}
CATEGORY_LABELS = {key: text for key, text, *_rest in CATEGORIES if key != "section"}
SORTS = {"热度": "downloads", "相关度": "relevance", "最新": "newest", "关注": "follows"}
CATEGORIES_BY_TYPE = {
    "mod": ["全部", "优化", "装饰", "科技", "冒险", "工具", "魔法", "世界生成"],
    "modpack": ["全部", "优化", "冒险", "科技", "魔法", "空岛", "整合"],
    "resourcepack": ["全部", "写实", "卡通", "简约", "中世纪", "原版风格"],
    "shader": ["全部", "写实", "卡通", "光影"],
    "world": ["全部", "冒险", "生存", "建筑", "小游戏"],
}
PAGE_SIZE = 20


def format_release_time(text: str) -> str:
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        return moment.strftime("%Y 年 %m 月 %d 日 %H:%M:%S")
    except (ValueError, TypeError):
        return str(text)


class Page(PageBase):
    def _build(self) -> None:
        self._background_photo = None
        self._icon_photo = None
        self._category = "game"
        self._rows: list[dict] = []
        self._hits: list[dict] = []
        self._offset = 0
        self._total = 0
        self._query = ""
        self._sort = "热度"
        self._project_type = "game"

        self.canvas = tk.Canvas(self, bg=theme.WINDOW_BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._draw_background())

        self.sidebar = Sidebar(self.canvas, width=200)
        self._sidebar_window = self.canvas.create_window(0, 0, window=self.sidebar, anchor="nw")
        self.sidebar.add_section("新游戏")
        self.sidebar.add_item("game", "游戏", icon="🎮", command=lambda: self.select("game"))
        self.sidebar.add_item("modpack", "整合包", icon="📦", command=lambda: self.select("modpack"))
        self.sidebar.add_section("游戏内容")
        self.sidebar.add_item("mod", "模组", icon="⭐", command=lambda: self.select("mod"))
        self.sidebar.add_item("resourcepack", "资源包", icon="▨", command=lambda: self.select("resourcepack"))
        self.sidebar.add_item("shader", "光影", icon="☀", command=lambda: self.select("shader"))
        self.sidebar.add_item("world", "世界", icon="🌍", command=lambda: self.select("world"))

        self.content = tk.Frame(self.canvas, bg=theme.WINDOW_BG)
        self._content_window = self.canvas.create_window(200, 0, window=self.content, anchor="nw")
        self.select("game")

    # ------------------------------------------------------------ 背景
    def _draw_background(self) -> None:
        width = max(self.canvas.winfo_width(), 400)
        height = max(self.canvas.winfo_height(), 300)
        self.canvas.delete("pagebg")
        shown = False
        path = assets.asset("background.png")
        if path is not None and Image is not None:
            try:
                image = Image.open(path).convert("RGB")
                scale = max(width / image.width, height / image.height)
                resized = image.resize((int(image.width * scale) + 1, int(image.height * scale) + 1),
                                       Image.LANCZOS)
                left = (resized.width - width) // 2
                top = (resized.height - height) // 2
                resized = resized.crop((left, top, left + width, top + height))
                self._background_photo = ImageTk.PhotoImage(resized)
                self.canvas.create_image(0, 0, anchor="nw", image=self._background_photo, tags="pagebg")
                self.canvas.tag_lower("pagebg")
                shown = True
            except Exception:  # noqa: BLE001
                shown = False
        if not shown:
            self.canvas.create_rectangle(0, 0, width, height, fill=theme.WINDOW_BG, tags="pagebg")
        self.canvas.coords(self._sidebar_window, 0, 0)
        self.canvas.itemconfigure(self._sidebar_window, height=height)
        self.canvas.coords(self._content_window, 200, 0)
        self.canvas.itemconfigure(self._content_window, width=max(300, width - 200), height=height)

    # ------------------------------------------------------------ 分类切换
    def select(self, key: str) -> None:
        self._category = key
        self.sidebar.set_painted(key)
        for widget in self.content.winfo_children():
            widget.destroy()
        if key == "game":
            self._project_type = "game"
            self._build_game_tab()
        else:
            self._build_resource_tab(key)

    def refresh(self) -> None:
        self.select(self._category)

    # ------------------------------------------------------------ 游戏版本
    def _build_game_tab(self) -> None:
        self._toolbar_game()
        self.list_area = tk.Frame(self.content, bg=theme.WINDOW_BG)
        self.list_area.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self._refresh_game_list()

    def _toolbar_game(self) -> None:
        bar = RoundedCard(self.content, padding=(12, 8))
        bar.pack(fill="x", padx=14, pady=(12, 6))
        row = tk.Frame(bar.body, bg=theme.CARD_BG)
        row.pack(fill="x")
        tk.Label(row, text="名称", bg=theme.CARD_BG, fg=theme.TEXT, font=theme.font(9)).pack(side="left", padx=(0, 8))
        self.game_search = ttk.Entry(row, width=30)
        self.game_search.pack(side="left")
        self.game_search.insert(0, "输入版本名称进行搜索")
        self.game_search.configure(foreground="#999999")
        self.game_search.bind("<FocusIn>", lambda _e: self._clear_entry(self.game_search, "输入版本名称进行搜索"))
        self.game_search.bind("<Return>", lambda _e: self._refresh_game_list(self.game_search.get().strip()))
        tk.Label(row, text="版本类型", bg=theme.CARD_BG, fg=theme.TEXT,
                 font=theme.font(9)).pack(side="left", padx=(16, 8))
        self.type_var = tk.StringVar(value="正式版")
        combo = ttk.Combobox(row, textvariable=self.type_var, width=8, state="readonly",
                             values=["正式版", "快照", "全部"])
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_game_list(self.game_search.get().strip()))
        HButton(row, "刷新", command=lambda: self._refresh_game_list(self.game_search.get().strip()),
                width=64, height=28, font_size=9).pack(side="right")

    def _refresh_game_list(self, query: str = "") -> None:
        if not hasattr(self, "list_area"):
            return
        for widget in self.list_area.winfo_children():
            widget.destroy()
        try:
            manifest = versions.get_manifest(self.app.cfg, self.app.cfg.make_downloader(quiet=True))
        except Exception as exc:  # noqa: BLE001
            tk.Label(self.list_area, text=f"获取版本清单失败：{exc}", bg=theme.WINDOW_BG,
                     fg=theme.RED, font=theme.font(9)).pack(anchor="w", pady=20)
            return
        desired = self.type_var.get() if hasattr(self, "type_var") else "正式版"
        query = "" if query in ("输入版本名称进行搜索", "输入名称搜索") else query
        rows = []
        for item in manifest.get("versions", []):
            version_type = item.get("type", "release")
            if desired == "正式版" and version_type != "release":
                continue
            if desired == "快照" and version_type != "snapshot":
                continue
            if query and query.lower() not in str(item.get("id", "")).lower():
                continue
            rows.append(item)
        if not rows:
            tk.Label(self.list_area, text="没有匹配的版本", bg=theme.WINDOW_BG,
                     fg=theme.TEXT_SECONDARY, font=theme.font(9)).pack(anchor="w", pady=20)
            return
        self._render_rows(rows[:200], kind="game")

    # ------------------------------------------------------------ 资源下载（整合包/模组/资源包/光影/世界）
    def _build_resource_tab(self, project_type: str) -> None:
        self._project_type = project_type
        self._offset = 0
        self._query = ""
        self._hits = []
        form = RoundedCard(self.content, padding=(12, 8))
        form.pack(fill="x", padx=14, pady=(12, 6))
        body = form.body
        first = tk.Frame(body, bg=theme.CARD_BG)
        first.pack(fill="x", pady=(0, 4))
        if project_type == "mod":
            self._label_combo(first, "游戏", ["(当前实例)", "全部版本"], width=12)
        self._label_combo(first, "下载源", ["Modrinth"], width=14)
        second = tk.Frame(body, bg=theme.CARD_BG)
        second.pack(fill="x", pady=(0, 4))
        tk.Label(second, text="名称", bg=theme.CARD_BG, fg=theme.TEXT, font=theme.font(9)).pack(
            side="left", padx=(0, 8))
        self.query_entry = ttk.Entry(second, width=28)
        self.query_entry.insert(0, "支持中英文搜索")
        self.query_entry.configure(foreground="#999999")
        self.query_entry.pack(side="left")
        self.query_entry.bind("<FocusIn>", lambda _e: self._clear_entry(self.query_entry, "支持中英文搜索"))
        self.query_entry.bind("<Return>", lambda _e: self._search(offset=0))
        tk.Label(second, text="游戏版本", bg=theme.CARD_BG, fg=theme.TEXT,
                 font=theme.font(9)).pack(side="left", padx=(16, 8))
        self.game_version_var = tk.StringVar(value=self._current_game_version())
        versions_combo = ttk.Combobox(second, textvariable=self.game_version_var, width=12,
                                      values=self._game_version_choices())
        versions_combo.pack(side="left")
        third = tk.Frame(body, bg=theme.CARD_BG)
        third.pack(fill="x", pady=(0, 4))
        self._label_combo(third, "类别", CATEGORIES_BY_TYPE.get(project_type, ["全部"]), width=12)
        self.sort_var = tk.StringVar(value="热度")
        tk.Label(third, text="排序", bg=theme.CARD_BG, fg=theme.TEXT,
                 font=theme.font(9)).pack(side="left", padx=(16, 8))
        sort_combo = ttk.Combobox(third, textvariable=self.sort_var, values=list(SORTS), width=8,
                                  state="readonly")
        sort_combo.pack(side="left")

        page_row = tk.Frame(body, bg=theme.CARD_BG)
        page_row.pack(fill="x", pady=(4, 0))
        HButton(page_row, "第一页", command=lambda: self._search(offset=0), kind="secondary",
                width=64, height=26, font_size=8).pack(side="left", padx=(0, 4))
        HButton(page_row, "上一页", command=lambda: self._search(offset=max(0, self._offset - PAGE_SIZE)),
                kind="secondary", width=64, height=26, font_size=8).pack(side="left", padx=(0, 4))
        self.page_label = tk.Label(page_row, text="1 / ?", bg=theme.CARD_BG, fg=theme.TEXT,
                                   font=theme.font(9), width=8)
        self.page_label.pack(side="left", padx=4)
        HButton(page_row, "下一页", command=lambda: self._search(offset=self._offset + PAGE_SIZE),
                kind="secondary", width=64, height=26, font_size=8).pack(side="left", padx=(0, 4))
        HButton(page_row, "最后一页",
                command=lambda: self._search(offset=max(0, ((max(1, self._total) - 1) // PAGE_SIZE) * PAGE_SIZE)),
                kind="secondary", width=70, height=26, font_size=8).pack(side="left")
        HButton(page_row, "搜索", command=lambda: self._search(offset=0), width=70, height=26,
                font_size=8).pack(side="left", padx=(12, 4))
        if project_type == "modpack":
            HButton(page_row, "安装整合包", command=self._install_local_modpack, width=100, height=26,
                    font_size=8).pack(side="left")

        self.list_area = tk.Frame(self.content, bg=theme.WINDOW_BG)
        self.list_area.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self._search(offset=0)

    def _clear_entry(self, entry, placeholder: str) -> None:
        if entry.get() == placeholder:
            entry.delete(0, "end")
            entry.configure(foreground=theme.TEXT)

    def _label_combo(self, parent, label: str, values: list[str], *, width: int) -> ttk.Combobox:
        tk.Label(parent, text=label, bg=theme.CARD_BG, fg=theme.TEXT,
                 font=theme.font(9)).pack(side="left", padx=(0, 8))
        combo = ttk.Combobox(parent, values=values, width=width, state="readonly")
        combo.current(0)
        combo.pack(side="left")
        return combo

    def _current_game_version(self) -> str:
        instance = self.app.current_instance()
        if instance is None or not instance.version_id:
            return "全部版本"
        try:
            return versions.resolve(self.app.cfg, instance.version_id).base_id or instance.version_id
        except Exception:  # noqa: BLE001
            return instance.version_id

    def _game_version_choices(self) -> list[str]:
        choices = ["全部版本"]
        current = self._current_game_version()
        if current and current != "全部版本":
            choices.append(current)
        for item in versions.installed_versions(self.app.cfg):
            value = item.get("base") or item["id"]
            if value not in choices:
                choices.append(value)
        return choices

    def _search(self, *, offset: int) -> None:
        placeholder = ("支持中英文搜索", "输入名称搜索")
        query = self.query_entry.get().strip()
        if query in placeholder:
            query = ""
        self._offset = max(0, offset)
        for widget in self.list_area.winfo_children():
            widget.destroy()
        tk.Label(self.list_area, text="加载中…", bg=theme.WINDOW_BG, fg=theme.TEXT_SECONDARY,
                 font=theme.font(9)).pack(anchor="w", pady=12)
        project_type = self._project_type
        game_version = self.game_version_var.get()
        game_version = None if game_version in ("", "全部版本") else game_version
        sort = SORTS.get(self.sort_var.get(), "downloads")
        offset = self._offset

        def work():
            return catalog.search(project_type, query,
                                  loader="fabric" if project_type in ("mod", "modpack") else None,
                                  game_version=game_version, limit=PAGE_SIZE, offset=offset, sort=sort)

        def done(ok, result):
            for widget in self.list_area.winfo_children():
                widget.destroy()
            if not ok or not isinstance(result, dict):
                tk.Label(self.list_area, text=f"加载失败：{result}", bg=theme.WINDOW_BG,
                         fg=theme.RED, font=theme.font(9)).pack(anchor="w", pady=12)
                return
            self._hits = result.get("hits") or []
            self._total = int(result.get("total_hits") or 0)
            page = self._offset // PAGE_SIZE + 1
            total_pages = max(1, (self._total + PAGE_SIZE - 1) // PAGE_SIZE)
            self.page_label.configure(text=f"{min(page, total_pages)} / {total_pages}")
            if not self._hits:
                tk.Label(self.list_area, text="没有找到结果", bg=theme.WINDOW_BG,
                         fg=theme.TEXT_SECONDARY, font=theme.font(9)).pack(anchor="w", pady=12)
                return
            self._render_hits(self._hits, project_type)

        self.app.run_task(work, on_done=done, label="搜索下载源")

    # ------------------------------------------------------------ 结果卡片
    def _render_hits(self, hits: list[dict], project_type: str) -> None:
        parent = self.list_area
        scroll = tk.Canvas(parent, bg=theme.WINDOW_BG, highlightthickness=0)
        bar = ttk.Scrollbar(parent, orient="vertical", command=scroll.yview)
        inner = tk.Frame(scroll, bg=theme.WINDOW_BG)
        window = scroll.create_window((0, 0), window=inner, anchor="nw")
        scroll.configure(yscrollcommand=bar.set)
        inner.bind("<Configure>", lambda _e: scroll.configure(scrollregion=scroll.bbox("all")))
        scroll.bind("<Configure>", lambda e: scroll.itemconfigure(window, width=e.width))
        scroll.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        for hit in hits:
            self._hit_card(inner, hit, project_type)
        scroll.bind_all("<MouseWheel>", lambda e: scroll.yview_scroll(int(-e.delta / 120), "units"))

    def _hit_card(self, parent, hit: dict, project_type: str) -> None:
        card = tk.Frame(parent, bg=theme.CARD_BG, padx=12, pady=8,
                        highlightthickness=1, highlightbackground=theme.CARD_BORDER)
        card.pack(fill="x", pady=(0, 4))
        left = tk.Frame(card, bg=theme.CARD_BG)
        left.pack(side="left", fill="x", expand=True)
        title_row = tk.Frame(left, bg=theme.CARD_BG)
        title_row.pack(fill="x")
        tk.Label(title_row, text=str(hit.get("title") or hit.get("slug") or ""), bg=theme.CARD_BG,
                 fg=theme.TEXT, font=theme.font(10, bold=True)).pack(side="left")
        for tag in (hit.get("categories") or [])[:4]:
            tk.Label(title_row, text=str(tag), bg="#eef0ff", fg=theme.PRIMARY,
                     font=theme.font(8), padx=5).pack(side="left", padx=(6, 0))
        tk.Label(left, text=(hit.get("description") or "")[:110], bg=theme.CARD_BG,
                 fg=theme.TEXT_SECONDARY, font=theme.font(8), anchor="w").pack(fill="x")
        right = tk.Frame(card, bg=theme.CARD_BG)
        right.pack(side="right")
        HButton(right, "安装", command=lambda h=hit: self._install_hit(h, project_type),
                width=64, height=26, font_size=8).pack(side="right", padx=(8, 0))
        tk.Label(right, text=CATEGORY_LABELS.get(project_type, ""), bg=theme.CARD_BG,
                 fg=theme.TEXT_SECONDARY, font=theme.font(8)).pack(side="right", padx=8)

    def _install_hit(self, hit: dict, project_type: str) -> None:
        instance = self.app.current_instance()
        if instance is None or not instance.version_id:
            self.app.show_error("下载", "当前没有可用实例，请先在实例列表新建实例。")
            return
        game_version = self._current_game_version()
        if game_version == "全部版本":
            game_version = instance.version_id
        game_dir = self.app.instances.game_dir(instance)
        slug = hit.get("slug") or hit.get("project_id")
        self.app.log(f"[下载] 安装 {hit.get('title')} …\n")
        self.app.run_task(
            lambda: catalog.install(self.app.instance_cfg(), game_dir, project_type, slug,
                                    loader="fabric", game_version=game_version),
            on_done=lambda ok, result: self.app.log(f"[下载] {'完成' if ok else '失败'}：{result}\n"),
            label="安装资源",
        )

    def _install_local_modpack(self) -> None:
        path = filedialog.askopenfilename(title="选择整合包",
                                          filetypes=[("整合包", "*.zip *.mrpack"), ("所有文件", "*.*")])
        if not path:
            return
        instance = self.app.current_instance()
        if instance is None:
            self.app.show_error("安装整合包", "当前没有可用实例。")
            return
        game_dir = self.app.instances.game_dir(instance)
        self.app.log(f"[下载] 导入整合包 {path} …\n")
        self.app.run_task(
            lambda: catalog.install(self.app.instance_cfg(), game_dir, "modpack", path,
                                    loader="fabric", game_version=self._current_game_version()),
            on_done=lambda ok, result: self.app.log(f"[下载] {'完成' if ok else '失败'}：{result}\n"),
            label="导入整合包",
        )

    # ------------------------------------------------------------ 游戏版本卡片
    def _render_rows(self, rows: list[dict], *, kind: str) -> None:
        parent = self.list_area
        scroll = tk.Canvas(parent, bg=theme.WINDOW_BG, highlightthickness=0)
        bar = ttk.Scrollbar(parent, orient="vertical", command=scroll.yview)
        inner = tk.Frame(scroll, bg=theme.WINDOW_BG)
        window = scroll.create_window((0, 0), window=inner, anchor="nw")
        scroll.configure(yscrollcommand=bar.set)
        inner.bind("<Configure>", lambda _e: scroll.configure(scrollregion=scroll.bbox("all")))
        scroll.bind("<Configure>", lambda e: scroll.itemconfigure(window, width=e.width))
        scroll.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        for item in rows:
            if kind == "game":
                version_id = str(item.get("id"))
                self._row(
                    title=version_id,
                    tag=TYPE_LABELS.get(str(item.get("type")), str(item.get("type"))),
                    subtitle=format_release_time(item.get("releaseTime")),
                    icon=True,
                    on_download=lambda v=version_id: self._download_version(v),
                    on_detail=lambda v=version_id: self._show_version(v),
                    parent=inner,
                )
        scroll.bind_all("<MouseWheel>", lambda e: scroll.yview_scroll(int(-e.delta / 120), "units"))

    def _row(self, *, title: str, tag: str, subtitle: str, on_download,
             on_detail=None, icon: bool = False, parent=None) -> None:
        parent = parent or self.list_area
        card = tk.Frame(parent, bg=theme.CARD_BG, padx=12, pady=8,
                        highlightthickness=1, highlightbackground=theme.CARD_BORDER)
        card.pack(fill="x", pady=(0, 4))
        left = tk.Frame(card, bg=theme.CARD_BG)
        left.pack(side="left", fill="x", expand=True)
        if icon:
            photo = self._get_icon_photo()
            if photo is not None:
                tk.Label(left, bg=theme.CARD_BG, bd=0, image=photo).pack(side="left", padx=(0, 10))
        text_frame = tk.Frame(left, bg=theme.CARD_BG)
        text_frame.pack(side="left", fill="x", expand=True)
        title_row = tk.Frame(text_frame, bg=theme.CARD_BG)
        title_row.pack(fill="x")
        tk.Label(title_row, text=title, bg=theme.CARD_BG, fg=theme.TEXT,
                 font=theme.font(10, bold=True)).pack(side="left")
        if tag:
            tk.Label(title_row, text=tag, bg="#eef0ff", fg=theme.PRIMARY,
                     font=theme.font(8), padx=6).pack(side="left", padx=(8, 0))
        tk.Label(text_frame, text=subtitle, bg=theme.CARD_BG, fg=theme.TEXT_SECONDARY,
                 font=theme.font(8), anchor="w").pack(fill="x")
        right = tk.Frame(card, bg=theme.CARD_BG)
        right.pack(side="right")
        arrow = tk.Label(right, text="→", bg=theme.CARD_BG, fg=theme.TEXT,
                         font=theme.font(13), cursor="hand2", padx=8)
        arrow.pack(side="right")
        arrow.bind("<Button-1>", lambda _e: (on_detail or on_download)())
        download = tk.Label(right, text="⤓", bg=theme.CARD_BG, fg=theme.TEXT,
                            font=theme.font(14), cursor="hand2", padx=10)
        download.pack(side="right")
        download.bind("<Button-1>", lambda _e: on_download())

    def _get_icon_photo(self):
        if self._icon_photo is not None:
            return self._icon_photo
        path = assets.asset("account.png")
        if path is None:
            return None
        try:
            photo = tk.PhotoImage(file=str(path))
            step = max(1, photo.width() // 32)
            self._icon_photo = photo.subsample(step, step)
        except tk.TclError:
            return None
        return self._icon_photo

    # ------------------------------------------------------------ 操作
    def _download_version(self, version_id: str) -> None:
        self.app.log(f"[下载] 开始安装 {version_id} …\n")
        self.app.run_cli(["install", version_id],
                         on_done=lambda rc: self.app.log(f"[下载] {version_id} {'完成' if rc == 0 else '失败'}\n"))

    def _show_version(self, version_id: str) -> None:
        try:
            resolved = versions.resolve(self.app.cfg, version_id)
            detail = (f"版本：{resolved.id}\n类型：{resolved.type}\n"
                      f"Java：{resolved.java_requirement}\n主类：{resolved.main_class}\n"
                      f"资源索引：{resolved.assets_index_id}\n库数量：{len(resolved.libraries)}")
        except Exception:  # noqa: BLE001
            detail = f"版本：{version_id}\n（尚未安装，安装后可查看详情）"
        self.app.show_error(f"版本详情 · {version_id}", detail,
                            actions=[("安装该版本", lambda: self._download_version(version_id))])
