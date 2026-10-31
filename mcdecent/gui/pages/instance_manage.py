"""实例管理页：实例头部 + 游戏设置/模组/资源包/光影/存档/服务器/整合包 子页。"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, ttk

from ... import mods as mods_module, pack as pack_module, resources, servers, util, versions
from .. import theme
from ..widgets import HButton, RoundedCard, TabStrip, make_table
from . import PageBase

TITLE = "实例管理"
TABS = [("settings", "游戏设置"), ("mods", "模组管理"), ("resourcepacks", "资源包"),
        ("shaderpacks", "光影"), ("saves", "存档"), ("servers", "服务器"), ("packs", "整合包")]


class Page(PageBase):
    def _build(self) -> None:
        self.header = tk.Frame(self, bg=theme.WINDOW_BG)
        self.header.pack(fill="x", padx=16, pady=(10, 2))
        self.tabstrip = TabStrip(self, self._on_tab)
        self.tabstrip.pack(fill="x", padx=16)
        self.content = tk.Frame(self, bg=theme.WINDOW_BG)
        self.content.pack(fill="both", expand=True, padx=16, pady=(4, 10))
        for key, text in TABS:
            self.tabstrip.add(key, text)
        self._current = "settings"
        self.tabstrip.select("settings")

    def refresh(self) -> None:
        for widget in self.header.winfo_children():
            widget.destroy()
        inst = self.app.current_instance()
        left = tk.Frame(self.header, bg=theme.WINDOW_BG)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=inst.name if inst else "没有游戏实例", bg=theme.WINDOW_BG,
                 fg=theme.TEXT, font=theme.font(12, bold=True)).pack(side="left")
        if inst and inst.version_id:
            loader = versions.read_loader_meta(self.app.cfg, inst.version_id) or {}
            text = f"  {inst.version_id}" + (f" · {loader.get('type')} {loader.get('version')}" if loader else "")
            tk.Label(left, text=text, bg=theme.WINDOW_BG, fg=theme.TEXT_SECONDARY,
                     font=theme.font(9)).pack(side="left")
        actions = tk.Frame(self.header, bg=theme.WINDOW_BG)
        actions.pack(side="right")
        HButton(actions, "▶ 启动", command=self.start, width=76, height=28, font_size=9).pack(side="left", padx=3)
        HButton(actions, "打开目录", command=self.open_dir, kind="secondary", width=84, height=28, font_size=9).pack(side="left", padx=3)
        HButton(actions, "设为默认", command=self.set_default, kind="secondary", width=84, height=28, font_size=9).pack(side="left", padx=3)
        self._render_tab(self._current)

    # ------------------------------------------------------------ 页签
    def _on_tab(self, key: str) -> None:
        self._current = key
        self._render_tab(key)

    def _render_tab(self, key: str) -> None:
        for widget in self.content.winfo_children():
            widget.destroy()
        inst = self.app.current_instance()
        if inst is None or not inst.version_id:
            tk.Label(self.content, text="还没有可管理的游戏实例。\n请到「下载」安装游戏版本，或在「实例列表」新建实例。",
                     bg=theme.WINDOW_BG, fg=theme.TEXT_SECONDARY, font=theme.font(10),
                     justify="left").pack(anchor="w", pady=30)
            return
        renderer = getattr(self, f"_tab_{key}", None)
        if renderer:
            renderer(inst)

    def _card(self, parent) -> tk.Frame:
        card = RoundedCard(parent, padding=(12, 10))
        card.pack(fill="both", expand=True)
        return card.body

    def _table(self, parent, columns, height=8) -> ttk.Treeview:
        return make_table(parent, columns, height=height)

    def _hint(self, parent, text: str) -> None:
        tk.Label(parent, text=text, bg=theme.CARD_BG, fg=theme.TEXT_SECONDARY,
                 font=theme.font(8), anchor="w").pack(fill="x", pady=(4, 0))

    # ------------------------------------------------------------ 游戏设置
    def _tab_settings(self, inst) -> None:
        body = self._card(self.content)
        frame = tk.Frame(body, bg=theme.CARD_BG)
        frame.pack(fill="x")
        fields = [
            ("最大内存 (MB)", str(inst.memory_mb or self.app.cfg.get("memory_mb") or 2048)),
            ("JVM 参数", " ".join(inst.jvm_args)),
            ("游戏参数", " ".join(inst.game_args)),
            ("Java 路径", inst.java_path or ""),
            ("启动前命令", inst.pre_launch or ""),
        ]
        entries = {}
        for index, (label, value) in enumerate(fields):
            tk.Label(frame, text=label, bg=theme.CARD_BG, fg=theme.TEXT, font=theme.font(9)).grid(
                row=index, column=0, sticky="w", pady=5, padx=(0, 10))
            entry = ttk.Entry(frame, width=56)
            entry.insert(0, value)
            entry.grid(row=index, column=1, sticky="w", pady=5)
            entries[label] = entry
        row = ttk.Frame(frame)
        row.grid(row=len(fields), column=1, sticky="w", pady=(10, 0))
        ttk.Button(row, text="保存实例设置", command=lambda: self._save_settings(inst, entries)).pack(side="left")
        self._hint(body, "窗口类型/快速游戏等全局选项在「设置」页；实例级设置优先于全局。")

    def _save_settings(self, inst, entries) -> None:
        try:
            inst.memory_mb = int(entries["最大内存 (MB)"].get()) or None
        except ValueError:
            inst.memory_mb = None
        inst.jvm_args = entries["JVM 参数"].get().split()
        inst.game_args = entries["游戏参数"].get().split()
        inst.java_path = entries["Java 路径"].get().strip() or None
        inst.pre_launch = entries["启动前命令"].get().strip() or None
        self.app.instances.update(inst)
        self.app.log(f"[实例] 已保存 {inst.name} 的设置\n")

    # ------------------------------------------------------------ 模组
    def _tab_mods(self, inst) -> None:
        body = self._card(self.content)
        table = self._table(body, [("state", "状态", 60), ("name", "模组", 420),
                                   ("size", "大小", 90), ("loader", "类型", 80)])
        for item in mods_module.list_mods(self.app.instance_cfg()):
            table.insert("", "end", values=("启用" if item.get("enabled") else "禁用",
                                            item.get("name"), util.human_size(item.get("size")),
                                            item.get("loader_hint") or "-"))
        row = tk.Frame(body, bg=theme.CARD_BG)
        row.pack(fill="x", pady=(8, 0))
        HButton(row, "添加本地 jar…", command=self._add_mod, width=120, height=28, font_size=9).pack(side="left", padx=3)
        HButton(row, "从 Modrinth 安装…", command=self._install_modrinth, kind="secondary", width=140, height=28, font_size=9).pack(side="left", padx=3)
        for label, command in (
            ("启用", lambda: self._toggle_mod(table, True)),
            ("禁用", lambda: self._toggle_mod(table, False)),
            ("删除", lambda: self._remove_mod(table)),
        ):
            HButton(row, label, command=command, kind="secondary", width=64, height=28, font_size=9).pack(side="left", padx=3)

    def _selected_name(self, table) -> str | None:
        item = table.selection()
        return table.item(item[0], "values")[1] if item else None

    def _add_mod(self) -> None:
        paths = filedialog.askopenfilenames(title="选择模组 jar",
                                           filetypes=[("Minecraft 模组", "*.jar"), ("所有文件", "*.*")])
        for path in paths or []:
            self.app.run_cli(["mods", "add", path], on_done=lambda _rc: self._render_tab("mods"))

    def _install_modrinth(self) -> None:
        slug = self.app.ask_string("从 Modrinth 安装", "模组 slug 或项目 ID：")
        if not slug:
            return
        version = self.app.current_instance().version_id
        game_version = versions.resolve(self.app.cfg, version).base_id or version
        self.app.run_cli(["mods", "install", slug.strip(), "--loader", "fabric",
                          "--game-version", game_version],
                         on_done=lambda _rc: self._render_tab("mods"))

    def _toggle_mod(self, table, enabled: bool) -> None:
        name = self._selected_name(table)
        if not name:
            return
        self.app.run_cli(["mods", "enable" if enabled else "disable", name],
                         on_done=lambda _rc: self._render_tab("mods"))

    def _remove_mod(self, table) -> None:
        name = self._selected_name(table)
        if not name:
            return
        if self.app.ask_yesno("删除模组", f"确定删除 {name}？"):
            self.app.run_cli(["mods", "rm", name], on_done=lambda _rc: self._render_tab("mods"))

    # ------------------------------------------------------------ 资源包 / 光影 / 存档
    def _tab_resourcepacks(self, inst) -> None:
        body = self._card(self.content)
        game_dir = self.app.instances.game_dir(inst)
        table = self._table(body, [("state", "状态", 60), ("name", "资源包", 360),
                                   ("format", "格式", 80), ("size", "大小", 90),
                                   ("desc", "描述", 240)])
        for item in resources.list_resourcepacks(game_dir):
            table.insert("", "end", values=("启用" if item.get("enabled") else "禁用",
                                            item.get("name"), item.get("format"),
                                            util.human_size(item.get("size")), item.get("description", "")))
        row = tk.Frame(body, bg=theme.CARD_BG)
        row.pack(fill="x", pady=(8, 0))
        HButton(row, "导入资源包…", command=lambda: self._import_resourcepack(game_dir), width=120, height=28, font_size=9).pack(side="left", padx=3)
        for label, enabled in (("启用", True), ("禁用", False)):
            HButton(row, label, command=lambda e=enabled: self._toggle_resourcepack(game_dir, table, e),
                    kind="secondary", width=64, height=28, font_size=9).pack(side="left", padx=3)

    def _import_resourcepack(self, game_dir) -> None:
        path = filedialog.askopenfilename(title="选择资源包", filetypes=[("资源包", "*.zip"), ("所有文件", "*.*")])
        if path:
            try:
                resources.install_resourcepack(game_dir, path)
            except Exception as exc:  # noqa: BLE001
                self.app.show_error("导入资源包失败", repr(exc))
            self._render_tab("resourcepacks")

    def _toggle_resourcepack(self, game_dir, table, enabled: bool) -> None:
        item = table.selection()
        if not item:
            return
        name = table.item(item[0], "values")[1]
        try:
            resources.set_resourcepack_enabled(game_dir, name, enabled)
        except Exception as exc:  # noqa: BLE001
            self.app.show_error("设置资源包", repr(exc))
        self._render_tab("resourcepacks")

    def _tab_shaderpacks(self, inst) -> None:
        body = self._card(self.content)
        game_dir = self.app.instances.game_dir(inst)
        table = self._table(body, [("state", "状态", 60), ("name", "光影", 420), ("size", "大小", 90)])
        for item in resources.list_shaderpacks(game_dir):
            state = "启用" if item.get("enabled") else ("禁用" if item.get("enabled") is False else "未知")
            table.insert("", "end", values=(state, item.get("name"), util.human_size(item.get("size"))))
        row = tk.Frame(body, bg=theme.CARD_BG)
        row.pack(fill="x", pady=(8, 0))
        HButton(row, "导入光影…", command=lambda: self._import_shader(game_dir), width=110, height=28, font_size=9).pack(side="left", padx=3)
        HButton(row, "启用选中", command=lambda: self._toggle_shader(game_dir, table), kind="secondary",
                width=90, height=28, font_size=9).pack(side="left", padx=3)

    def _import_shader(self, game_dir) -> None:
        path = filedialog.askopenfilename(title="选择光影", filetypes=[("光影", "*.zip"), ("所有文件", "*.*")])
        if path:
            try:
                resources.install_shaderpack(game_dir, path)
            except Exception as exc:  # noqa: BLE001
                self.app.show_error("导入光影失败", repr(exc))
            self._render_tab("shaderpacks")

    def _toggle_shader(self, game_dir, table) -> None:
        item = table.selection()
        if not item:
            return
        name = table.item(item[0], "values")[1]
        try:
            resources.set_shaderpack_enabled(game_dir, name)
        except Exception as exc:  # noqa: BLE001
            self.app.show_error("设置光影", repr(exc))
        self._render_tab("shaderpacks")

    def _tab_saves(self, inst) -> None:
        body = self._card(self.content)
        game_dir = self.app.instances.game_dir(inst)
        table = self._table(body, [("name", "存档", 260), ("level", "世界名", 220),
                                   ("version", "版本", 90), ("played", "最后游玩", 150)])
        for item in resources.list_saves(game_dir):
            table.insert("", "end", values=(item.get("name"), item.get("level_name"),
                                            item.get("version"), item.get("last_played")))
        row = tk.Frame(body, bg=theme.CARD_BG)
        row.pack(fill="x", pady=(8, 0))
        HButton(row, "备份存档", command=lambda: self._backup_save(game_dir, table), width=100, height=28, font_size=9).pack(side="left", padx=3)
        HButton(row, "删除存档", command=lambda: self._delete_save(game_dir, table), kind="danger", width=100, height=28, font_size=9).pack(side="left", padx=3)
        HButton(row, "打开目录", command=lambda: self.app.open_path(game_dir / "saves"), kind="secondary",
                width=100, height=28, font_size=9).pack(side="left", padx=3)

    def _backup_save(self, game_dir, table) -> None:
        item = table.selection()
        if not item:
            return
        try:
            path = resources.backup_save(game_dir, table.item(item[0], "values")[0])
            self.app.log(f"[存档] 已备份到 {path}\n")
        except Exception as exc:  # noqa: BLE001
            self.app.show_error("备份存档失败", repr(exc))

    def _delete_save(self, game_dir, table) -> None:
        item = table.selection()
        if not item:
            return
        name = table.item(item[0], "values")[0]
        if self.app.ask_yesno("删除存档", f"确定删除存档「{name}」？"):
            try:
                resources.delete_save(game_dir, name)
            except Exception as exc:  # noqa: BLE001
                self.app.show_error("删除存档失败", repr(exc))
            self._render_tab("saves")

    # ------------------------------------------------------------ 服务器
    def _tab_servers(self, inst) -> None:
        body = self._card(self.content)
        game_dir = self.app.instances.game_dir(inst)
        table = self._table(body, [("name", "名称", 300), ("ip", "地址", 420)])
        for item in servers.read_servers(game_dir):
            table.insert("", "end", values=(item.get("name"), item.get("ip")))
        row = tk.Frame(body, bg=theme.CARD_BG)
        row.pack(fill="x", pady=(8, 0))
        HButton(row, "添加服务器…", command=lambda: self._add_server(game_dir), width=120, height=28, font_size=9).pack(side="left", padx=3)
        HButton(row, "删除选中", command=lambda: self._remove_server(game_dir, table), kind="danger",
                width=100, height=28, font_size=9).pack(side="left", padx=3)

    def _add_server(self, game_dir) -> None:
        name = self.app.ask_string("添加服务器", "服务器名称：", "新服务器")
        if not name:
            return
        ip = self.app.ask_string("添加服务器", "服务器地址（IP:端口）：", "mc.example.com")
        if not ip:
            return
        try:
            servers.add_server(game_dir, name.strip(), ip.strip())
        except Exception as exc:  # noqa: BLE001
            self.app.show_error("添加服务器失败", repr(exc))
        self._render_tab("servers")

    def _remove_server(self, game_dir, table) -> None:
        item = table.selection()
        if not item:
            return
        try:
            servers.remove_server(game_dir, table.index(item[0]))
        except Exception as exc:  # noqa: BLE001
            self.app.show_error("删除服务器失败", repr(exc))
        self._render_tab("servers")

    # ------------------------------------------------------------ 整合包
    def _tab_packs(self, inst) -> None:
        body = self._card(self.content)
        table = self._table(body, [("name", "名称", 160), ("version", "版本", 80),
                                   ("game", "游戏版本", 100), ("files", "文件数", 70),
                                   ("signed", "签名", 70), ("digest", "内容地址", 300)])
        for item in pack_module.list_packs(self.app.cfg):
            table.insert("", "end", values=(item.get("name"), item.get("version"),
                                            item.get("game_version"), item.get("files"),
                                            "已签名" if item.get("signed") else "未签名",
                                            item.get("digest") or ""))
        row = tk.Frame(body, bg=theme.CARD_BG)
        row.pack(fill="x", pady=(8, 0))
        HButton(row, "导出当前实例…", command=self._export_pack, width=130, height=28, font_size=9).pack(side="left", padx=3)
        HButton(row, "导入整合包…", command=self._import_pack, kind="secondary", width=120, height=28, font_size=9).pack(side="left", padx=3)
        self._hint(body, "整合包按 SHA-256 内容寻址，支持 Ed25519 签名与 P2P 分发（见设置 → 通用）。")

    def _game_version(self) -> str:
        inst = self.app.current_instance()
        try:
            return versions.resolve(self.app.cfg, inst.version_id).base_id or inst.version_id
        except Exception:  # noqa: BLE001
            return inst.version_id if inst else ""

    def _export_pack(self) -> None:
        name = self.app.ask_string("导出整合包", "整合包名称：", f"{self.app.current_instance().name} 整合包")
        if not name:
            return
        self.app.run_cli(["pack", "export", name.strip(), "--game-version", self._game_version(),
                          "--include", "mods", "config"],
                         on_done=lambda _rc: self._render_tab("packs"))

    def _import_pack(self) -> None:
        ref = self.app.ask_string("导入整合包", "清单路径 / SHA-256 / mcdecent://pack/<sha256>：")
        if not ref:
            return
        self.app.run_cli(["pack", "import", ref.strip()], on_done=lambda _rc: self._render_tab("packs"))

    # ------------------------------------------------------------ 通用
    def start(self) -> None:
        inst = self.app.current_instance()
        if inst is None or not inst.version_id:
            return
        self.app.run_cli(["game", "start", inst.version_id])

    def open_dir(self) -> None:
        inst = self.app.current_instance()
        if inst is not None:
            self.app.open_path(self.app.instances.game_dir(inst))

    def set_default(self) -> None:
        inst = self.app.current_instance()
        if inst is not None:
            self.app.instances.set_default(inst.name)
            self.refresh()
