"""HMCL 风格主窗口：无边框顶栏 + 页面路由 + 左侧导航（在首页/设置页内）。

对外仍然暴露原来的 App 接口（current_instance / instance_cfg / run_cli / run_task /
log / refresh_all / ask_* / show_error / show_diagnosis / open_path），页面直接复用。
"""

from __future__ import annotations

import ctypes
import io
import os
import queue
import sys
import threading
import time
import traceback
from typing import Callable

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .. import __version__, cli, config, util
from ..instances import InstanceManager
from . import assets, theme
from .widgets import TopBar

if sys.stdout is None:  # pragma: no cover
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:  # pragma: no cover
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

WINDOW_TITLE = f"mcdecent 启动器 v{__version__}"
DEFAULT_SIZE = (854, 480)
MIN_SIZE = (720, 420)


def workarea() -> tuple[int, int]:
    if sys.platform.startswith("win"):
        try:
            rect = ctypes.wintypes.RECT()  # type: ignore[attr-defined]
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
            return rect.right - rect.left, rect.bottom - rect.top
        except Exception:  # noqa: BLE001
            pass
    return 1280, 720


class App(tk.Tk):  # type: ignore[misc]
    def __init__(self, home: str | None = None):
        super().__init__()
        self.home = home
        self.cfg = config.Config(home)
        self.cfg.ensure_layout()
        self.instances = InstanceManager(self.cfg)
        self.instances.ensure_default()

        self.q: queue.Queue = queue.Queue()
        self._busy = 0
        self._pages: dict[str, object] = {}
        self._stack: list[str] = []
        self._closed = False
        self._log_lines: list[str] = []
        self._maximized = False
        self._restore_geometry = ""

        self.overrideredirect(True)
        self.configure(bg=theme.WINDOW_BG)
        self.title(WINDOW_TITLE)
        width, height = DEFAULT_SIZE
        self.geometry(f"{width}x{height}+{(workarea()[0] - width) // 2}+{(workarea()[1] - height) // 2}")
        self.minsize(*MIN_SIZE)
        self._apply_theme(str(self.cfg.get("theme") or "light"))

        self._setup_window_icon()
        self.topbar = TopBar(self, self, WINDOW_TITLE)
        self.topbar.pack(fill="x")
        self.body = tk.Frame(self, bg=theme.WINDOW_BG)
        self.body.pack(fill="both", expand=True)
        self._fix_taskbar()
        self._make_grip()
        self.bind("<Escape>", lambda _e: self.go_back())
        self.bind("<F11>", lambda _e: self.toggle_maximize())

        self.show_page("home")
        self.after(120, self._drain_queue)
        self._tick()

    # ------------------------------------------------------------ 窗口
    def _fix_taskbar(self) -> None:
        """无边框窗口仍然出现在任务栏。"""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x00040000)
        except Exception:  # noqa: BLE001
            pass

    def _make_grip(self) -> None:
        grip = tk.Canvas(self, width=14, height=14, bg=theme.WINDOW_BG,
                         highlightthickness=0, cursor="bottom_right_corner")
        grip.place(relx=1.0, rely=1.0, anchor="se")
        for offset in (3, 7, 11):
            grip.create_line(14 - offset, 13, 13, 14 - offset, fill="#b9bcc4")
        grip.bind("<Button-1>", lambda e: setattr(
            self, "_grip_start", (e.x_root, e.y_root, self.winfo_width(), self.winfo_height())))
        grip.bind("<B1-Motion>", self._on_grip_drag)
        self._resize_grip = grip

    def _on_grip_drag(self, event) -> None:
        start = getattr(self, "_grip_start", None)
        if not start:
            return
        x0, y0, w0, h0 = start
        width = max(MIN_SIZE[0], w0 + (event.x_root - x0))
        height = max(MIN_SIZE[1], h0 + (event.y_root - y0))
        self.geometry(f"{width}x{height}")

    def _setup_window_icon(self) -> None:
        try:
            ico = assets.asset("app.ico")
            if ico is not None and sys.platform.startswith("win"):
                self.iconbitmap(str(ico))
            png = assets.asset("app.png")
            if png is not None:
                self._window_icon = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._window_icon)
        except Exception:  # noqa: BLE001
            pass

    def toggle_maximize(self) -> None:
        if self._maximized:
            self.geometry(self._restore_geometry or f"{DEFAULT_SIZE[0]}x{DEFAULT_SIZE[1]}")
            self._maximized = False
        else:
            self._restore_geometry = self.geometry()
            width, height = workarea()
            self.geometry(f"{width}x{height}+0+0")
            self._maximized = True
        self.update_idletasks()
        page = self._current_page()
        if page is not None and hasattr(page, "on_resize"):
            page.on_resize()

    def on_close(self) -> None:
        self._closed = True
        try:
            self.destroy()
        except tk.TclError:
            pass

    def set_title(self, title: str) -> None:
        self.topbar.title_label.configure(text=title)

    def show_help(self) -> None:
        messagebox.showinfo("帮助", "mcdecent 去中心化 Minecraft 启动器\n\n"
                                   "常用：首页点击「启动游戏」；左侧导航进入 实例列表 / 下载 / 设置。\n"
                                   "详细说明见 C:\\MC\\README.md", parent=self)

    # ------------------------------------------------------------ 页面路由
    def show_page(self, name: str, *, replace: bool = False, title: str | None = None) -> None:
        self.set_busy(1)
        try:
            page = self._pages.get(name)
            if page is None:
                page = self._create_page(name)
                if page is None:
                    return
                self._pages[name] = page
            if replace and self._stack:
                self._stack[-1] = name
            elif not self._stack or self._stack[-1] != name:
                self._stack.append(name)
            for other in self._pages.values():
                try:
                    other.pack_forget()
                except tk.TclError:
                    pass
            page.pack(fill="both", expand=True)
            page_title = title or getattr(page, "title", "") or name
            self.set_title(page_title)
            back = len(self._stack) > 1
            self.topbar.back_button.configure(text="‹" if back else "")
            self.topbar.back_button.configure(cursor="hand2" if back else "arrow")
            refresh = getattr(page, "refresh", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:  # noqa: BLE001
                    util.LOG.exception("页面 %s 刷新失败", name)
        finally:
            self.set_busy(-1)

    def _create_page(self, name: str):
        from . import pages

        return pages.create(name, self.body, self)

    def go_back(self) -> None:
        if len(self._stack) > 1:
            self._stack.pop()
            previous = self._stack[-1]
            page = self._pages.get(previous)
            if page is not None:
                for other in self._pages.values():
                    other.pack_forget()
                page.pack(fill="both", expand=True)
                self.set_title(getattr(page, "title", "") or previous)
                self.topbar.back_button.configure(text="‹" if len(self._stack) > 1 else "")
                refresh = getattr(page, "refresh", None)
                if callable(refresh):
                    refresh()

    def _current_page(self):
        return self._pages.get(self._stack[-1]) if self._stack else None

    def refresh_all(self) -> None:
        page = self._current_page()
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()

    # ------------------------------------------------------------ 实例
    def current_instance(self):
        return self.instances.default()

    def instance_cfg(self) -> config.Config:
        return self.instances.config_for(self.current_instance())

    def set_instance(self, name: str) -> None:
        inst = self.instances.get(name)
        if inst is None:
            return
        self.instances.set_default(inst.name)
        self.refresh_all()

    # ------------------------------------------------------------ 后台任务
    def set_busy(self, delta: int) -> None:
        self._busy = max(0, self._busy + delta)

    def run_cli(self, argv: list, *, on_done: Callable | None = None, use_instance: bool = True) -> None:
        argv = [str(a) for a in argv]
        if use_instance:
            inst = self.current_instance()
            if inst is not None:
                argv = argv + ["--instance", inst.name]
        self.set_busy(1)

        def worker() -> None:
            buf = io.StringIO()
            buf.write("> mcdecent " + " ".join(argv) + "\n")
            rc = 1
            try:
                with _redirect(buf):
                    rc = cli.main(argv)
            except SystemExit as exc:
                rc = int(exc.code or 0)
            except Exception:  # noqa: BLE001
                buf.write("\n[内部异常]\n" + traceback.format_exc())
            finally:
                util.setup_logging(None, quiet=True)
            buf.write(f"\n[退出码 {rc}]\n")
            self.q.put(("log", buf.getvalue()))
            self.q.put(("done", (on_done, rc)))

        threading.Thread(target=worker, name="mcdecent-gui-cmd", daemon=True).start()

    def run_task(self, fn: Callable, *, on_done: Callable | None = None, label: str = "") -> None:
        self.set_busy(1)

        def worker() -> None:
            try:
                result = fn()
                ok = True
            except Exception as exc:  # noqa: BLE001
                result = exc
                ok = False
                self.q.put(("log", f"[任务失败] {label or ''}\n{traceback.format_exc()}\n"))
            if on_done:
                self.q.put(("done", (lambda _rc: on_done(ok, result), 0 if ok else 1)))

        threading.Thread(target=worker, name="mcdecent-gui-task", daemon=True).start()

    def _drain_queue(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "done":
                    callback, rc = payload
                    self.set_busy(-1)
                    if callback:
                        try:
                            callback(rc)
                        except Exception:  # noqa: BLE001
                            util.LOG.exception("回调失败")
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if not self._closed:
            try:
                self.after(120, self._drain_queue)
            except tk.TclError:
                pass

    def _tick(self) -> None:
        if self._closed:
            return
        try:
            self._refresh_game_state()
        except tk.TclError:
            return
        except Exception:  # noqa: BLE001
            pass
        if not self._closed:
            try:
                self.after(3000, self._tick)
            except tk.TclError:
                pass

    def _refresh_game_state(self) -> None:
        from ..commands.cmds_game import pid_alive

        data = util.read_json(self.cfg.root / "game.pid", {}) or {}
        running = bool(isinstance(data, dict) and isinstance(data.get("pid"), int) and pid_alive(data["pid"]))
        if running != getattr(self, "_game_running", None):
            self._game_running = running
            page = self._current_page()
            if page is not None and hasattr(page, "on_game_state"):
                page.on_game_state(running)
        self._apply_visibility(running)

    def _apply_visibility(self, running: bool) -> None:
        mode = str(self.cfg.get("launcher_visibility") or "keep")
        if mode == "keep":
            return
        if not hasattr(self, "_visibility_applied"):
            self._visibility_applied = False
        if running and not self._visibility_applied:
            self._visibility_applied = True
            try:
                if mode == "minimize":
                    self.iconify()
                elif mode in ("hide", "hide_and_reopen"):
                    self.withdraw()
                elif mode == "exit":
                    self.on_close()
            except tk.TclError:
                pass
        elif not running and self._visibility_applied:
            self._visibility_applied = False
            if mode in ("hide", "hide_and_reopen", "minimize"):
                try:
                    self.deiconify()
                    self.lift()
                except tk.TclError:
                    pass

    # ------------------------------------------------------------ 日志 / 交互
    def log(self, text: str) -> None:
        if not text:
            return
        self._log_lines.append(text.replace("\r\n", "\n").replace("\r", "\n"))
        if len(self._log_lines) > 2000:
            del self._log_lines[: len(self._log_lines) - 2000]
        page = self._pages.get("settings")
        if page is not None and hasattr(page, "append_log"):
            page.append_log(text)

    def log_text(self) -> str:
        return "".join(self._log_lines)

    def _apply_theme(self, name: str) -> None:
        self.configure(bg=theme.WINDOW_BG)

    def open_path(self, path) -> None:
        util.ensure_dir(path)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            messagebox.showerror("打开目录", f"无法打开：{path}", parent=self)

    def ask_string(self, title: str, prompt: str, initial: str = "") -> str | None:
        return simpledialog.askstring(title, prompt, initialvalue=initial, parent=self)

    def ask_yesno(self, title: str, message: str) -> bool:
        return bool(messagebox.askyesno(title, message, parent=self))

    def show_error(self, title: str, detail: str, actions: list[tuple[str, Callable]] | None = None) -> None:
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("680x360")
        win.transient(self)
        text = tk.Text(win, wrap="word", font=(theme.MONO_FAMILY, 9))
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.insert("1.0", detail)
        text.configure(state="disabled")
        row = ttk.Frame(win)
        row.pack(fill="x", padx=10, pady=(0, 10))
        for label, command in actions or []:
            ttk.Button(row, text=label, command=lambda c=command: (win.destroy(), c())).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="关闭", command=win.destroy).pack(side="right")

    def show_diagnosis(self, issues: list, *, on_fixed: Callable | None = None) -> None:
        if not issues:
            messagebox.showinfo("诊断", "没有发现问题。", parent=self)
            return
        try:
            from .. import repair
        except Exception:  # noqa: BLE001
            repair = None
        win = tk.Toplevel(self)
        win.title("检测到问题")
        win.geometry("760x480")
        win.transient(self)
        text = tk.Text(win, wrap="word")
        text.pack(fill="both", expand=True, padx=10, pady=10)
        for issue in issues:
            text.insert("end", f"【{getattr(issue, 'severity', 'warning')}】{issue.title}\n")
            text.insert("end", f"  原因：{issue.detail}\n")
            for cause in getattr(issue, "causes", []):
                text.insert("end", f"  · {cause}\n")
            for fix in getattr(issue, "fixes", []):
                text.insert("end", f"  修复（{fix.action}）：{fix.label} {getattr(fix, 'description', '')}\n")
            text.insert("end", "\n")
        text.configure(state="disabled")
        row = ttk.Frame(win)
        row.pack(fill="x", padx=10, pady=(0, 10))
        if repair is not None:
            for issue in issues:
                if not getattr(issue, "auto_fixable", False):
                    continue
                def do_fix(issue=issue):
                    win.destroy()
                    self.run_task(lambda: repair.apply_fix(self.instance_cfg(), issue.id),
                                  on_done=lambda ok, result: self._after_fix(ok, result, on_fixed),
                                  label=f"修复 {issue.id}")
                ttk.Button(row, text=f"一键修复：{issue.title[:16]}", command=do_fix).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="关闭", command=win.destroy).pack(side="right")

    def _after_fix(self, ok: bool, result, on_fixed: Callable | None) -> None:
        message = getattr(result, "message", str(result))
        self.log(f"[修复] {'成功' if ok else '失败'}：{message}\n")
        if on_fixed:
            on_fixed()
        self.refresh_all()


class _redirect:
    def __init__(self, buffer: io.StringIO):
        self.buffer = buffer

    def __enter__(self):
        self._old_out, self._old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = self.buffer
        return self.buffer

    def __exit__(self, *_exc):
        sys.stdout, sys.stderr = self._old_out, self._old_err
        return False


def main(home: str | None = None) -> int:
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    try:
        app = App(home=home)
    except Exception:  # noqa: BLE001
        messagebox.showerror("mcdecent", "界面初始化失败：\n" + traceback.format_exc())
        return 1
    app.mainloop()
    return 0
