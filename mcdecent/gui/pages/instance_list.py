"""实例列表页（HMCL「实例列表」布局）。"""

from __future__ import annotations

import tkinter as tk

from ... import util, versions
from ...instances import default_isolation, format_play_time
from .. import theme
from ..widgets import HButton, RoundedCard
from . import PageBase

TITLE = "实例列表"


class Page(PageBase):
    def _build(self) -> None:
        header = tk.Frame(self, bg=theme.WINDOW_BG)
        header.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(header, text="实例列表", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(12, bold=True)).pack(side="left")
        HButton(header, "+ 新建实例", command=self.create_instance, width=110).pack(side="right")

        self.list_frame = tk.Frame(self, bg=theme.WINDOW_BG)
        self.list_frame.pack(fill="both", expand=True, padx=16, pady=(4, 12))

    def refresh(self) -> None:
        for widget in self.list_frame.winfo_children():
            widget.destroy()
        items = self.app.instances.list()
        if not items:
            tk.Label(self.list_frame, text="还没有游戏实例。点击右上角「新建实例」，或到下载页安装游戏。",
                     bg=theme.WINDOW_BG, fg=theme.TEXT_SECONDARY, font=theme.font(9)).pack(anchor="w", pady=20)
            return
        for inst in items:
            self._instance_row(inst)

    def _instance_row(self, inst) -> None:
        card = RoundedCard(self.list_frame, padding=(14, 8))
        card.pack(fill="x", pady=(0, 6))
        left = tk.Frame(card.body, bg=theme.CARD_BG)
        left.pack(side="left", fill="x", expand=True)
        name_row = tk.Frame(left, bg=theme.CARD_BG)
        name_row.pack(fill="x")
        is_default = self.app.current_instance() is not None and self.app.current_instance().name == inst.name
        tk.Label(name_row, text=("● " if is_default else "") + inst.name, bg=theme.CARD_BG,
                 fg=theme.TEXT, font=theme.font(10, bold=True)).pack(side="left")
        tk.Label(name_row, text=f"  {inst.version_id or '未选择版本'}", bg=theme.CARD_BG,
                 fg=theme.TEXT_SECONDARY, font=theme.font(8)).pack(side="left")
        loader = ""
        try:
            meta = versions.read_loader_meta(self.app.cfg, inst.version_id) or {}
            if meta:
                loader = f" · {meta.get('type')} {meta.get('version')}"
        except Exception:  # noqa: BLE001
            pass
        tk.Label(left, text=f"{'隔离目录' if inst.isolated else '共享目录'}{loader} · 累计 {format_play_time(inst.play_time_seconds)}",
                 bg=theme.CARD_BG, fg=theme.TEXT_SECONDARY, font=theme.font(8), anchor="w").pack(fill="x")

        actions = tk.Frame(card.body, bg=theme.CARD_BG)
        actions.pack(side="right")
        HButton(actions, "▶ 启动", command=lambda n=inst.name: self.start(n), width=76, height=28, font_size=9).pack(side="left", padx=3)
        HButton(actions, "编辑", command=lambda n=inst.name: self.edit(n), kind="secondary", width=60, height=28, font_size=9).pack(side="left", padx=3)
        HButton(actions, "目录", command=lambda n=inst.name: self.open_dir(n), kind="secondary", width=60, height=28, font_size=9).pack(side="left", padx=3)
        if not is_default:
            HButton(actions, "设为默认", command=lambda n=inst.name: self.set_default(n), kind="secondary", width=84, height=28, font_size=9).pack(side="left", padx=3)
        HButton(actions, "删除", command=lambda n=inst.name: self.delete(n), kind="danger", width=60, height=28, font_size=9).pack(side="left", padx=3)

    # ------------------------------------------------------------ 操作
    def create_instance(self) -> None:
        dialog = _NewInstanceDialog(self, self.app)
        self.wait_window(dialog)
        if dialog.created:
            self.refresh()

    def start(self, name: str) -> None:
        inst = self.app.instances.get(name)
        if inst is None:
            return
        if not inst.version_id:
            self.app.show_error("无法启动", "该实例还没有选择游戏版本。\n请到「下载」安装版本，或编辑实例选择版本。")
            return
        self.app.set_instance(name)
        self.app.run_cli(["game", "start", inst.version_id])

    def open_dir(self, name: str) -> None:
        inst = self.app.instances.get(name)
        if inst is not None:
            self.app.open_path(self.app.instances.game_dir(inst))

    def set_default(self, name: str) -> None:
        self.app.set_instance(name)
        self.app.log(f"[实例] 默认实例：{name}\n")
        self.refresh()

    def delete(self, name: str) -> None:
        if not self.app.ask_yesno("删除实例", f"确定删除实例「{name}」？\n（默认保留游戏目录文件）"):
            return
        self.app.instances.delete(name, delete_files=False)
        self.refresh()

    def edit(self, name: str) -> None:
        self.app.set_instance(name)
        self.app.show_page("instance_manage")


class _NewInstanceDialog(tk.Toplevel):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.created = False
        self.title("新建实例")
        self.geometry("420x260+{}+{}".format(parent.winfo_rootx() + 180, parent.winfo_rooty() + 120))
        self.transient(parent)
        self.configure(bg=theme.WINDOW_BG)
        frame = tk.Frame(self, bg=theme.WINDOW_BG, padx=16, pady=14)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="实例名称", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(9)).pack(anchor="w")
        self.name_entry = tk.Entry(frame, font=theme.font(10))
        self.name_entry.insert(0, "新实例")
        self.name_entry.pack(fill="x", pady=(2, 10))
        tk.Label(frame, text="游戏版本", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(9)).pack(anchor="w")
        installed = versions.installed_versions(app.cfg)
        self.version_var = tk.StringVar(value=installed[0]["id"] if installed else "")
        combo = tk.Combobox(frame, textvariable=self.version_var, state="readonly",
                            values=[item["id"] for item in installed])
        combo.pack(fill="x", pady=(2, 10))
        self.isolated = tk.BooleanVar(value=default_isolation(app.cfg, self.version_var.get()))
        tk.Checkbutton(frame, text="版本隔离（独立 mods/存档/配置）", variable=self.isolated,
                       bg=theme.WINDOW_BG, fg=theme.TEXT, font=theme.font(9),
                       activebackground=theme.WINDOW_BG).pack(anchor="w", pady=(0, 12))
        row = tk.Frame(frame, bg=theme.WINDOW_BG)
        row.pack(fill="x")
        HButton(row, "创建", command=self._create, width=90).pack(side="right", padx=(6, 0))
        HButton(row, "取消", command=self.destroy, kind="secondary", width=90).pack(side="right")
        self.grab_set()

    def _create(self) -> None:
        name = self.name_entry.get().strip() or "新实例"
        version = self.version_var.get().strip()
        if not version:
            self.app.show_error("新建实例", "还没有已安装的游戏版本。\n请先到「下载」安装一个版本。")
            return
        try:
            inst = self.app.instances.create(name, version, isolated=bool(self.isolated.get()))
        except Exception as exc:  # noqa: BLE001
            self.app.show_error("新建实例失败", repr(exc))
            return
        self.created = True
        self.app.log(f"[实例] 已创建 {inst.name}（{inst.version_id}）\n")
        self.destroy()
