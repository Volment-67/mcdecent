"""命令模块：自动发现 + 共享上下文。

约定（各命令模块必须遵守）：
    * 文件名 ``cmds_*.py``，且不以 ``_`` 开头；
    * 暴露 ``register(subparsers, parent)``；
    * 在解析器上 ``set_defaults(func=handler)``，handler(args) 返回 int。

这样新增命令只需新增一个文件，``cli.py`` 无需改动。
"""

from __future__ import annotations

import importlib
import json as _json
import pkgutil
import sys
from pathlib import Path

from .. import cas, config, net, util
from ..config import Config


# 打包成单文件/目录程序（PyInstaller 等）时，pkgutil 扫不到源码目录，
# 这里保留静态清单作为兜底，保证命令在冻结环境里也能注册。
_KNOWN_COMMANDS = (
    "cmds_core",
    "cmds_mods",
    "cmds_peer",
    "cmds_game",
    "cmds_shell",
    "cmds_gui",
    "cmds_instance",
    "cmds_repair",
)


def discover() -> list:
    """发现并导入所有命令模块。"""
    modules = []
    for info in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if info.name.startswith("_") or info.name == "__init__":
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as exc:  # noqa: BLE001
            util.LOG.debug("无法导入命令模块 %s：%r", info.name, exc)
            continue
        if hasattr(module, "register"):
            modules.append(module)
    if not modules:
        # 冻结环境（PyInstaller 等）或目录不可枚举时走静态清单
        for name in _KNOWN_COMMANDS:
            if any(getattr(m, "__name__", "").endswith("." + name) for m in modules):
                continue
            try:
                module = importlib.import_module(f"{__name__}.{name}")
            except Exception as exc:  # noqa: BLE001
                util.LOG.debug("无法导入内置命令模块 %s：%r", name, exc)
                continue
            if hasattr(module, "register"):
                modules.append(module)
    return modules


class Context:
    """一次命令调用的上下文：配置、进度、下载器、CAS、输出方式。"""

    def __init__(self, args):
        self.args = args
        self.cfg = Config(getattr(args, "home", None))
        self.cfg.ensure_layout()
        # 实例：显式 --instance 优先；否则用 instances.json 里的默认实例
        # （旧数据第一次会生成一个共享目录的「默认」实例，行为与之前一致）
        self.instance = None
        try:
            from ..instances import InstanceManager

            manager = InstanceManager(self.cfg)
            explicit = getattr(args, "instance", None)
            inst = manager.get(explicit) if explicit else manager.ensure_default()
            if explicit and inst is None:
                raise SystemExit(f"实例不存在：{explicit}")
            if inst is not None:
                self.instance = inst
                self.cfg.set_game_dir(manager.game_dir(inst))
                manager.ensure_dirs(inst)
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001 - 实例层故障不应阻断命令
            self.instance = None
        util.setup_logging(
            self.cfg.logs_file,
            verbose=bool(getattr(args, "verbose", False)),
            quiet=bool(getattr(args, "quiet", False)),
        )
        self.json_mode = bool(getattr(args, "json", False))
        self.quiet = bool(getattr(args, "quiet", False))
        self._progress: util.Progress | None = None
        self._downloader: net.Downloader | None = None
        self._cas: cas.ContentStore | None = None

    # ------------------------------------------------------------ 组件
    @property
    def progress(self) -> util.Progress:
        if self._progress is None:
            enabled = not self.quiet and not bool(getattr(self.args, "no_progress", False))
            self._progress = util.Progress(enabled=enabled)
        return self._progress

    def start_progress(self, total_files: int = 0, total_bytes: int | None = None) -> util.Progress:
        p = self.progress
        p.total_files = total_files
        p.total_bytes = total_bytes
        return p

    @property
    def downloader(self) -> net.Downloader:
        if self._downloader is None:
            self._downloader = self.cfg.make_downloader(progress=self.progress, quiet=self.quiet)
        return self._downloader

    @property
    def cas(self) -> cas.ContentStore:
        if self._cas is None:
            self._cas = cas.ContentStore(self.cfg.cas_dir)
        return self._cas

    # ------------------------------------------------------------ 输出
    def emit(self, payload, text: str = "") -> None:
        if self.json_mode:
            print(_json.dumps(payload, ensure_ascii=False, indent=2))
        elif text:
            print(text)

    def info(self, message: str) -> None:
        if not self.quiet:
            print(message)

    def warn(self, message: str) -> None:
        print(f"[警告] {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        print(f"[错误] {message}", file=sys.stderr)


def make_parser(subparsers, parent, name: str, help_text: str):
    """创建子命令解析器的便捷函数（统一继承全局参数）。"""
    return subparsers.add_parser(name, parents=[parent], help=help_text, description=help_text)


def context_from(args) -> Context:
    return Context(args)
