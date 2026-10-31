"""设置页：HMCL 布局（左侧分组导航 + 右侧白色卡片列表）。"""

from __future__ import annotations

import ctypes
import os
import sys
import tkinter as tk
from tkinter import ttk

from ... import java as java_module, util
from .. import theme
from ..widgets import HButton, HCheck, HSlider, RoundedCard, Sidebar, Toggle, hline, make_table
from . import PageBase

TITLE = "设置"

VISIBILITY = {
    "keep": "保持启动器可见",
    "minimize": "游戏启动后最小化启动器",
    "hide": "游戏启动后隐藏启动器",
    "hide_and_reopen": "隐藏启动器并在游戏结束后重新打开",
    "exit": "游戏启动后结束启动器",
}
PRIORITY = {"low": "低", "below_normal": "较低", "normal": "中", "above_normal": "较高", "high": "高"}
RESOLUTIONS = ["854x480", "1280x720", "1366x768", "1600x900", "1920x1080"]


def physical_memory_bytes() -> int:
    if sys.platform.startswith("win"):
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return int(status.ullTotalPhys)
        except Exception:  # noqa: BLE001
            pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return 8 * 1024 ** 3


def available_memory_bytes() -> int:
    if sys.platform.startswith("win"):
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return int(status.ullAvailPhys)
        except Exception:  # noqa: BLE001
            pass
    return physical_memory_bytes() // 2


class Page(PageBase):
    def _build(self) -> None:
        self.sidebar = Sidebar(self, width=188)
        self.sidebar.pack(side="left", fill="y")
        self.content = tk.Frame(self, bg=theme.WINDOW_BG)
        self.content.pack(side="left", fill="both", expand=True)
        self.sidebar.add_item("global", "全局游戏设置", icon="🎮", command=lambda: self.select("global"))
        self.sidebar.add_item("java", "Java 管理", icon="☕", command=lambda: self.select("java"))
        self.sidebar.add_section("启动器")
        self.sidebar.add_item("general", "通用", icon="⚙", command=lambda: self.select("general"))
        self.sidebar.add_item("appearance", "外观", icon="🎨", command=lambda: self.select("appearance"))
        self.sidebar.add_item("download", "下载", icon="⬇", command=lambda: self.select("download"))
        self.sidebar.add_section("帮助")
        self.sidebar.add_item("help", "帮助", icon="❓", command=lambda: self.select("help"))
        self.sidebar.add_item("feedback", "反馈", icon="✉", command=lambda: self.select("feedback"))
        self.sidebar.add_item("about", "关于", icon="ℹ", command=lambda: self.select("about"))
        self._views: dict[str, tk.Widget] = {}
        self._current = None
        self.select("global")

    # ------------------------------------------------------------ 视图切换
    def select(self, key: str) -> None:
        self.sidebar.set_painted(key)
        for widget in self.content.winfo_children():
            widget.destroy()
        builder = getattr(self, f"_view_{key}", None)
        if builder is None:
            return
        self._current = key
        builder()

    def _scroll(self) -> tk.Frame:
        canvas = tk.Canvas(self.content, bg=theme.WINDOW_BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.content, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=theme.WINDOW_BG)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        body = tk.Frame(inner, bg=theme.WINDOW_BG)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        return body

    # ------------------------------------------------------------ 卡片/行
    def _row_card(self, parent, title: str, subtitle: str = "", *, right=None, command=None,
                  chevron: bool = False):
        card = RoundedCard(parent, padding=(16, 10))
        card.pack(fill="x", pady=(0, 8))
        left = tk.Frame(card.body, bg=theme.CARD_BG)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=title, bg=theme.CARD_BG, fg=theme.TEXT, font=theme.font(10, bold=True),
                 anchor="w").pack(fill="x")
        if subtitle:
            tk.Label(left, text=subtitle, bg=theme.CARD_BG, fg=theme.TEXT_SECONDARY,
                     font=theme.font(8), anchor="w", justify="left").pack(fill="x")
        holder = tk.Frame(card.body, bg=theme.CARD_BG)
        holder.pack(side="right")
        if right is not None:
            right(holder)
        elif chevron:
            tk.Label(holder, text="›", bg=theme.CARD_BG, fg=theme.TEXT_SECONDARY,
                     font=theme.font(14)).pack(padx=6)
        if command:
            for widget in (card, card.body, left):
                widget.bind("<Button-1>", lambda _e: command())
        return card

    def _view_global(self) -> None:
        body = self._scroll()
        cfg = self.app.cfg
        java_path = cfg.get("java.path") or ""
        java_text = f"自动选择合适的 Java（当前：{java_path}）" if java_path else "自动选择合适的 Java"

        def java_action():
            self.select("java")

        self._row_card(body, "游戏 Java", java_text, command=java_action, chevron=True)

        def isolation_action():
            self.select("general")

        self._row_card(body, "版本隔离",
                       "建议使用模组时选择「各实例独立」。改后需移动世界、模组等相关游戏文件",
                       command=isolation_action, chevron=True)

        # 游戏内存
        card = RoundedCard(body, padding=(16, 12))
        card.pack(fill="x", pady=(0, 8))
        tk.Label(card.body, text="游戏内存", bg=theme.CARD_BG, fg=theme.TEXT,
                 font=theme.font(10, bold=True), anchor="w").pack(fill="x")
        auto = tk.BooleanVar(value=bool(cfg.get("memory_auto", True)))
        auto_row = tk.Frame(card.body, bg=theme.CARD_BG)
        auto_row.pack(fill="x", pady=(8, 4))
        HCheck(auto_row, auto, "自动分配内存",
               command=lambda: self._set("memory_auto", bool(auto.get()))).pack(side="left")
        slider_row = tk.Frame(card.body, bg=theme.CARD_BG)
        slider_row.pack(fill="x", pady=(4, 2))
        tk.Label(slider_row, text="最低内存分配", bg=theme.CARD_BG, fg=theme.TEXT,
                 font=theme.font(9)).pack(side="left")
        total_mb = int(physical_memory_bytes() / 1024 / 1024)
        maximum = max(1024, min(16384, int(total_mb * 0.75)))
        current = max(512, min(maximum, int(cfg.get("memory_mb") or 2048)))
        right_holder = tk.Frame(slider_row, bg=theme.CARD_BG)
        right_holder.pack(side="right")
        tk.Label(right_holder, text="MiB", bg=theme.CARD_BG, fg=theme.TEXT_SECONDARY,
                 font=theme.font(9)).pack(side="right", padx=(4, 2))
        value_label = tk.Label(right_holder, text=str(current), width=6, bg=theme.INPUT_BG,
                               fg=theme.TEXT, font=theme.font(9), anchor="e")
        value_label.pack(side="right")

        def on_slide(value: int):
            value_label.configure(text=str(value))
            self._set("memory_mb", value)

        slider = HSlider(slider_row, minimum=512, maximum=maximum, value=current,
                         command=on_slide, width=210)
        slider.pack(side="left", padx=8)

        used_gb = (physical_memory_bytes() - available_memory_bytes()) / 1024 ** 3
        total_gb = physical_memory_bytes() / 1024 ** 3
        bar = tk.Canvas(card.body, height=6, bg=theme.CARD_BG, highlightthickness=0)
        bar.pack(fill="x", pady=(8, 2))
        balance_row = tk.Frame(card.body, bg=theme.CARD_BG)
        balance_row.pack(fill="x")
        tk.Label(balance_row, text=f"已使用 {used_gb:.1f} GiB / 总内存 {total_gb:.1f} GiB",
                 bg=theme.CARD_BG, fg=theme.TEXT_SECONDARY, font=theme.font(8)).pack(side="left")
        tk.Label(balance_row,
                 text=f"最低分配 {current / 1024:.1f} GiB / 实际分配 {current / 1024:.1f} GiB",
                 bg=theme.CARD_BG, fg=theme.TEXT_SECONDARY, font=theme.font(8)).pack(side="right")

        def draw_bar(_event=None):
            bar.delete("all")
            width = bar.winfo_width()
            if width < 4:
                return
            bar.create_rectangle(0, 0, width, 6, fill="#e8e9f2", outline="")
            ratio = min(1.0, current / max(1, total_mb))
            bar.create_rectangle(0, 0, int(width * ratio), 6, fill=theme.PRIMARY, outline="")

        bar.bind("<Configure>", draw_bar)

        # 启动器可见性
        def visibility_holder(holder):
            var = tk.StringVar(value=VISIBILITY.get(str(cfg.get("launcher_visibility") or "keep"),
                                                    VISIBILITY["keep"]))
            combo = ttk.Combobox(holder, textvariable=var, values=list(VISIBILITY.values()),
                                 width=24, state="readonly")
            combo.pack()
            combo.bind("<<ComboboxSelected>>", lambda _e: self._set(
                "launcher_visibility", {v: k for k, v in VISIBILITY.items()}[var.get()]))

        self._row_card(body, "启动器可见性", right=visibility_holder)

        # 游戏窗口分辨率
        def resolution_holder(holder):
            frame = tk.Frame(holder, bg=theme.CARD_BG)
            frame.pack()
            resolution = str(cfg.get("resolution") or "854x480")
            var = tk.StringVar(value=resolution)
            combo = ttk.Combobox(frame, textvariable=var, values=RESOLUTIONS, width=12,
                                 state="readonly")
            combo.pack(side="left")
            fullscreen = tk.BooleanVar(value=cfg.get("window_type") == "fullscreen")
            HCheck(frame, fullscreen, "全屏",
                   command=lambda: self._set("window_type", "fullscreen" if fullscreen.get() else "windowed")
                   ).pack(side="left", padx=(8, 0))
            combo.bind("<<ComboboxSelected>>", lambda _e: self._set("resolution", var.get()))

        self._row_card(body, "游戏窗口分辨率", right=resolution_holder)

        # 日志开关
        def toggle_row(title: str, key: str, default=False):
            var = tk.BooleanVar(value=bool(cfg.get(key, default)))
            self._row_card(body, title, right=lambda holder: Toggle(holder, var, command=lambda: self._set(key, bool(var.get()))))

        toggle_row("查看日志", "show_game_log", True)
        toggle_row("输出调试日志", "debug_log", False)

        # 进程优先级
        def priority_holder(holder):
            var = tk.StringVar(value=PRIORITY.get(str(cfg.get("process_priority") or "normal"), "中"))
            combo = ttk.Combobox(holder, textvariable=var, values=list(PRIORITY.values()),
                                 width=8, state="readonly")
            combo.pack()
            combo.bind("<<ComboboxSelected>>", lambda _e: self._set(
                "process_priority", {v: k for k, v in PRIORITY.items()}[var.get()]))

        self._row_card(body, "进程优先级", right=priority_holder)

        # 服务器地址
        def server_holder(holder):
            entry = ttk.Entry(holder, width=34)
            entry.insert(0, str(cfg.get("quick_play_value") or ""))
            entry.pack()
            entry.bind("<FocusOut>", lambda _e: self._set("quick_play_value", entry.get().strip()))

        self._row_card(body, "服务器地址", right=server_holder)

        # 高级设置
        self._row_card(body, "高级设置", "JVM 参数、环境变量、启动前/后命令、包装命令、完整性检查",
                       command=lambda: self.select("general"), chevron=True)

    def _view_java(self) -> None:
        body = self._scroll()
        cfg = self.app.cfg

        def refresh_runtimes():
            for widget in list_frame.winfo_children():
                widget.destroy()
            runtimes = java_module.list_installed_runtimes(cfg)
            current = java_module.find_java(cfg)
            if current is not None:
                self._row_card(list_frame, "当前 Java", f"{current.major} · {current.path}")
            for runtime in runtimes:
                self._row_card(list_frame, f"Java {runtime.major}", str(runtime.path))
            if not runtimes:
                self._row_card(list_frame, "还没有由启动器托管的 Java", "点击下方按钮下载，或直接启动游戏自动下载")

        tk.Label(body, text="Java 管理", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(12, bold=True), anchor="w").pack(fill="x", pady=(0, 8))
        buttons = tk.Frame(body, bg=theme.WINDOW_BG)
        buttons.pack(fill="x", pady=(0, 8))
        HButton(buttons, "下载 Java 21", command=lambda: self.app.run_cli(
            ["java", "install", "--major", "21"], use_instance=False), width=140).pack(side="left", padx=(0, 8))
        HButton(buttons, "检测本机 Java", command=lambda: self.app.run_task(
            lambda: java_module.find_java(cfg),
            on_done=lambda ok, runtime: self.app.log(f"[Java] {runtime}\n" if ok and runtime else "[Java] 未找到\n"),
            label="检测 Java"), kind="secondary", width=140).pack(side="left")
        list_frame = tk.Frame(body, bg=theme.WINDOW_BG)
        list_frame.pack(fill="both", expand=True)
        refresh_runtimes()

        def custom_holder(holder):
            entry = ttk.Entry(holder, width=30)
            entry.insert(0, str(cfg.get("java.path") or ""))
            entry.pack(side="left")
            ttk.Button(holder, text="浏览", width=6, command=lambda: self._pick_java(entry)).pack(side="left", padx=4)
            entry.bind("<FocusOut>", lambda _e: self._set("java.path", entry.get().strip() or None))

        self._row_card(body, "自定义 Java 路径", right=custom_holder)

    def _pick_java(self, entry) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(title="选择 java 可执行文件",
                                          filetypes=[("java", "java.exe"), ("所有文件", "*.*")])
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)
            self._set("java.path", path)

    def _view_general(self) -> None:
        body = self._scroll()
        cfg = self.app.cfg
        tk.Label(body, text="高级设置", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(12, bold=True), anchor="w").pack(fill="x", pady=(0, 8))

        def entry_row(title: str, subtitle: str, key: str, initial: str, width=36):
            def holder(h):
                entry = ttk.Entry(h, width=width)
                entry.insert(0, initial or "")
                entry.pack()
                entry.bind("<FocusOut>", lambda _e: self._set(key, entry.get().strip() or None))
            return self._row_card(body, title, subtitle, right=holder)

        entry_row("全局 JVM 参数", "额外附加到所有游戏的 JVM 参数", "jvm_args_text", " ".join(cfg.get("jvm_args") or []))
        entry_row("全局游戏参数", "额外附加的游戏参数", "game_args_text", " ".join(cfg.get("game_args") or []))
        entry_row("启动前命令", "游戏启动前执行的 shell 命令", "pre_call", str(cfg.get("pre_call") or ""))
        entry_row("游戏结束后命令", "游戏退出后执行的 shell 命令", "post_exit", str(cfg.get("post_exit") or ""))
        entry_row("包装命令", "如 optirun", "wrapper", str(cfg.get("wrapper") or ""))

        def isolation_holder(holder):
            var = tk.StringVar(value={"always": "隔离所有实例", "modded": "只隔离模组实例",
                                      "never": "不隔离实例"}.get(
                str(cfg.get("default_isolation_policy") or "always"), "隔离所有实例"))
            combo = ttk.Combobox(holder, textvariable=var, width=16, state="readonly",
                                 values=["隔离所有实例", "只隔离模组实例", "不隔离实例"])
            combo.pack()
            combo.bind("<<ComboboxSelected>>", lambda _e: self._set(
                "default_isolation_policy",
                {"隔离所有实例": "always", "只隔离模组实例": "modded", "不隔离实例": "never"}[var.get()]))

        self._row_card(body, "默认版本隔离策略", right=isolation_holder)

        completeness = tk.BooleanVar(value=bool(cfg.get("check_game_completeness", True)))
        self._row_card(body, "启动前检查游戏文件完整性",
                       "缺失文件会阻止启动并提示一键修复",
                       right=lambda h: Toggle(h, completeness,
                                              command=lambda: self._set("check_game_completeness",
                                                                        bool(completeness.get()))))

        # P2P / 诊断 / 数据目录
        tk.Label(body, text="本启动器附加功能", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(12, bold=True), anchor="w").pack(fill="x", pady=(12, 8))
        p2p_row = tk.Frame(body, bg=theme.WINDOW_BG)
        p2p_row.pack(fill="x", pady=(0, 8))
        HButton(p2p_row, "启动 P2P 节点", command=self._p2p_start, kind="secondary", width=130).pack(side="left", padx=(0, 6))
        HButton(p2p_row, "停止节点", command=self._p2p_stop, kind="secondary", width=90).pack(side="left", padx=(0, 6))
        HButton(p2p_row, "发现局域网节点", command=self._p2p_discover, kind="secondary", width=140).pack(side="left")
        self._p2p_status = tk.Label(body, text=self._p2p_text(), bg=theme.WINDOW_BG,
                                    fg=theme.TEXT_SECONDARY, font=theme.font(8), anchor="w")
        self._p2p_status.pack(fill="x", pady=(0, 8))

        diag_row = tk.Frame(body, bg=theme.WINDOW_BG)
        diag_row.pack(fill="x", pady=(0, 8))
        HButton(diag_row, "诊断并一键修复", command=self._diagnose, width=150).pack(side="left", padx=(0, 6))
        HButton(diag_row, "打开数据目录", command=lambda: self.app.open_path(self.app.cfg.root),
                kind="secondary", width=130).pack(side="left")

    def _view_appearance(self) -> None:
        body = self._scroll()
        cfg = self.app.cfg
        tk.Label(body, text="外观", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(12, bold=True), anchor="w").pack(fill="x", pady=(0, 8))

        def theme_holder(holder):
            var = tk.StringVar(value=str(cfg.get("theme") or "light"))
            combo = ttk.Combobox(holder, textvariable=var, values=["light", "dark"], width=10,
                                 state="readonly")
            combo.pack()
            combo.bind("<<ComboboxSelected>>", lambda _e: (self._set("theme", var.get()),
                                                          self.app._apply_theme(var.get())))

        self._row_card(body, "主题", right=theme_holder)

    def _view_download(self) -> None:
        body = self._scroll()
        cfg = self.app.cfg
        tk.Label(body, text="下载", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(12, bold=True), anchor="w").pack(fill="x", pady=(0, 8))

        def mirror_holder(holder):
            labels = {"auto": "自动选择", "off": "仅官方源", "only": "仅镜像"}
            var = tk.StringVar(value=labels.get(str(cfg.get("mirror_mode") or "auto"), "自动选择"))
            combo = ttk.Combobox(holder, textvariable=var, values=list(labels.values()), width=12,
                                 state="readonly")
            combo.pack()
            combo.bind("<<ComboboxSelected>>", lambda _e: self._set(
                "mirror_mode", {v: k for k, v in labels.items()}[var.get()]))

        self._row_card(body, "下载源", "自动：镜像优先、官方兜底", right=mirror_holder)

        def workers_holder(holder):
            spin = ttk.Spinbox(holder, from_=1, to=32, width=6)
            spin.insert(0, str(cfg.get("download_workers") or 16))
            spin.pack()
            spin.bind("<FocusOut>", lambda _e: self._set("download_workers", int(spin.get() or 16)))

        self._row_card(body, "下载并发数", right=workers_holder)

        def tls_holder(holder):
            var = tk.StringVar(value="严格" if cfg.get("tls_verify") == "strict" else "兼容（证书链失败时降级）")
            combo = ttk.Combobox(holder, textvariable=var,
                                 values=["严格", "兼容（证书链失败时降级）"], width=24, state="readonly")
            combo.pack()
            combo.bind("<<ComboboxSelected>>", lambda _e: self._set(
                "tls_verify", "strict" if var.get() == "严格" else "fallback"))

        self._row_card(body, "TLS 校验", right=tls_holder)

    def _view_help(self) -> None:
        body = self._scroll()
        tk.Label(body, text="帮助 / 诊断", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(12, bold=True), anchor="w").pack(fill="x", pady=(0, 8))
        row = tk.Frame(body, bg=theme.WINDOW_BG)
        row.pack(fill="x", pady=(0, 8))
        HButton(row, "运行诊断", command=self._diagnose, width=110).pack(side="left", padx=(0, 6))
        HButton(row, "查看游戏日志", command=self._show_log, kind="secondary", width=120).pack(side="left", padx=(0, 6))
        HButton(row, "打开崩溃报告目录", command=self._open_crashes, kind="secondary", width=150).pack(side="left")
        self._log_text = tk.Text(body, height=14, font=(theme.MONO_FAMILY, 8), bg="#ffffff",
                                 fg=theme.TEXT, relief="flat", highlightthickness=1,
                                 highlightbackground=theme.CARD_BORDER, state="disabled")
        self._log_text.pack(fill="both", expand=True)
        self.append_log(self.app.log_text())

    def append_log(self, text: str) -> None:
        widget = getattr(self, "_log_text", None)
        if widget is None:
            return
        try:
            widget.configure(state="normal")
            widget.insert("end", text)
            widget.see("end")
            widget.configure(state="disabled")
        except tk.TclError:
            pass

    def _view_feedback(self) -> None:
        body = self._scroll()
        tk.Label(body, text="反馈", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(12, bold=True), anchor="w").pack(fill="x", pady=(0, 8))
        self._row_card(body, "GitHub 仓库", "https://github.com/HMCL-dev/HMCL（功能参考）")
        self._row_card(body, "项目目录", "C:\\MC（源码与打包产物）", command=lambda: self.app.open_path("C:\\MC"))
        self._row_card(body, "README", "完整功能与命令说明：C:\\MC\\README.md")

    def _view_about(self) -> None:
        body = self._scroll()
        from ... import __version__

        tk.Label(body, text="关于", bg=theme.WINDOW_BG, fg=theme.TEXT,
                 font=theme.font(12, bold=True), anchor="w").pack(fill="x", pady=(0, 8))
        self._row_card(body, f"mcdecent 启动器 v{__version__}",
                       "去中心化 Minecraft 启动器：内容寻址 + P2P 分发 + 多镜像回退")
        self._row_card(body, "数据目录", str(self.app.cfg.root))
        self._row_card(body, "Python", sys.version.split()[0])

    # ------------------------------------------------------------ 设置写入
    def _set(self, key: str, value) -> None:
        cfg = self.app.cfg
        if key == "jvm_args_text":
            cfg.set("jvm_args", str(value).split())
        elif key == "game_args_text":
            cfg.set("game_args", str(value).split())
        else:
            cfg.set(key, value)
        cfg.save()
        self.app.log(f"[设置] {key} = {value}\n")

    # ------------------------------------------------------------ 附加功能
    def _p2p_text(self) -> str:
        from ...p2p import known_peers, peer_id

        try:
            return f"P2P 节点 ID：{peer_id(self.app.cfg)}，已知节点 {len(known_peers(self.app.cfg))} 个"
        except Exception:  # noqa: BLE001
            return "P2P 未初始化"

    def _p2p_start(self) -> None:
        try:
            from ...p2p import PeerServer

            if getattr(self.app, "_peer_server", None) is None:
                server = PeerServer(self.app.cfg)
                server.start()
                self.app._peer_server = server
                self.app.log(f"[P2P] 节点已启动：{server.url}\n")
            self._p2p_status.configure(text=self._p2p_text())
        except Exception as exc:  # noqa: BLE001
            self.app.show_error("P2P", f"启动失败：{exc}")

    def _p2p_stop(self) -> None:
        server = getattr(self.app, "_peer_server", None)
        if server is not None:
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass
            self.app._peer_server = None
            self.app.log("[P2P] 节点已停止\n")
        self._p2p_status.configure(text=self._p2p_text())

    def _p2p_discover(self) -> None:
        from ...p2p import PeerDiscovery

        self.app.run_task(lambda: PeerDiscovery(self.app.cfg).discover(timeout=5.0),
                          on_done=lambda ok, peers: self.app.log(
                              f"[P2P] 发现 {len(peers) if ok and isinstance(peers, list) else 0} 个节点\n"),
                          label="发现节点")

    def _diagnose(self) -> None:
        try:
            from ... import repair
        except Exception as exc:  # noqa: BLE001
            self.app.show_error("诊断", f"修复模块不可用：{exc}")
            return
        self.app.run_task(lambda: repair.diagnose(self.app.instance_cfg(), self.app.current_instance()),
                          on_done=lambda _ok, issues: self.app.show_diagnosis(issues if isinstance(issues, list) else []),
                          label="诊断")

    def _show_log(self) -> None:
        from ...commands.cmds_game import latest_game_log

        path = latest_game_log(self.app.cfg)
        if path is None:
            self.app.show_error("日志", "还没有游戏日志。")
            return
        self.app.open_path(path.parent)

    def _open_crashes(self) -> None:
        self.app.open_path(self.app.instance_cfg().game_dir / "crash-reports")

    def refresh(self) -> None:
        self.select(self._current or "global")
