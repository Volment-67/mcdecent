"""交互式 Shell：在 cmd / PowerShell 里打开后直接敲命令操作游戏。

    C:\\MC> mcdecent
    mcdecent[空闲]> play                 # 后台启动游戏
    mcdecent[运行中]> status             # 看 PID / 版本 / 日志位置
    mcdecent[运行中]> log -f             # 跟踪游戏日志（Ctrl+C 返回）
    mcdecent[运行中]> stop               # 停止游戏
    mcdecent[空闲]> mods install sodium --loader fabric --game-version 26.3
    mcdecent[空闲]> exit

只依赖 argparse 与标准库 input()；在管道/脚本里也能用（`echo "list" | mcdecent shell`）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

from . import __version__, util
from .cli import build_parser

# 命令别名：把常用操作缩短成动词
ALIASES: dict[str, list[str]] = {
    "ls": ["list"],
    "ll": ["list", "--available"],
    "play": ["game", "start"],
    "run": ["game", "start"],
    "start": ["game", "start"],
    "stop": ["game", "stop"],
    "status": ["game", "status"],
    "log": ["game", "log"],
    "packs": ["pack", "list"],
    "peers": ["peer", "peers"],
    "doctor!": ["doctor"],
}

SHELL_HELP = """\
交互式命令（直接输入即可，和命令行子命令完全一致，另加别名）：

  游戏控制
    play [--memory 1536] [--width 1280 --height 720]   后台启动游戏（= game start）
    status                                              查看游戏进程
    log [-f] [--lines 80]                               查看/跟踪游戏日志
    stop                                                停止游戏

  版本与模组
    list [--available]          已安装/可安装版本
    install <版本> [--loader fabric] [--loader-version X]
    info [版本]                 解析结果与安装完整性
    mods list | mods install <slug> [--loader fabric --game-version 26.3]
    mods add <jar> | mods rm <名字> | mods enable/disable <名字>
    loader versions <版本> | loader install <版本> --type fabric

  分发与网络
    pack export <名字> --game-version 26.3 --include mods [--sign-key <hex>]
    pack import <mcdecent://pack/<sha256>|路径|sha256> [--peer host:port]
    pack list
    peer serve [--port 25577]    启动只读 CAS 节点
    peer discover | peer peers | peer pull <sha256> [--peer host:port]

  环境
    doctor | java list | mirror test | config list
    config set memory_mb 1536

  Shell 内建
    help / ?        显示帮助        history       命令历史
    !n / !!         重跑第 n 条/上一条
    home            数据目录        clear/cls     清屏
    exit / quit / q 退出
"""


def split_command(line: str) -> list[str]:
    """按空白拆分，支持单/双引号，且**不**把反斜杠当转义（Windows 路径友好）。"""
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in ("'", '"'):
            quote = ch
        elif ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


def apply_alias(tokens: list[str]) -> list[str]:
    if not tokens:
        return tokens
    alias = ALIASES.get(tokens[0].lower())
    if alias:
        return list(alias) + tokens[1:]
    return tokens


class Shell:
    def __init__(self, home: str | os.PathLike | None = None, *, quiet: bool = False):
        if home:
            os.environ["MCDECENT_HOME"] = str(Path(home).expanduser())
        self.parser = build_parser()
        self.home = Path(home).expanduser() if home else None
        self.history: list[str] = []
        self.quiet = quiet
        self._tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
        self._running_cache = {"at": 0.0, "value": False}

    # ------------------------------------------------------------ 状态
    @property
    def root(self) -> Path:
        from .config import Config

        return Config(getattr(self, "home", None), load=False).root

    def game_running(self) -> bool:
        import time

        now = time.time()
        if now - self._running_cache["at"] < 2.0:
            return self._running_cache["value"]
        running = False
        try:
            from .commands.cmds_game import pid_alive

            data = util.read_json(self.root / "game.pid", {}) or {}
            if isinstance(data, dict) and isinstance(data.get("pid"), int):
                running = pid_alive(data["pid"])
        except Exception:  # noqa: BLE001 - 状态栏绝不能影响主流程
            running = False
        self._running_cache = {"at": now, "value": running}
        return running

    def prompt(self) -> str:
        return f"mcdecent[{'运行中' if self.game_running() else '空闲'}]> "

    # ------------------------------------------------------------ 主循环
    def run(self) -> int:
        if not self.quiet:
            self.print_banner()
        while True:
            try:
                line = input(self.prompt() if self._tty else "")
            except EOFError:
                if self._tty:
                    print()
                break
            except KeyboardInterrupt:
                print("\n（Ctrl+C：输入 exit 退出；Ctrl+D / Ctrl+Z 回车亦可）")
                continue
            text = line.strip()
            if not text:
                continue
            if text.lower() in ("exit", "quit", "q", ":q"):
                break
            self.execute(text)
        if not self.quiet:
            print("再见。")
        return 0

    def print_banner(self) -> None:
        print(f"mcdecent {__version__} 交互式启动器")
        print(f"数据目录：{self.root}")
        print("输入 help 查看命令，exit 退出。常用：play / status / log -f / stop / mods install <slug>")
        print()

    # ------------------------------------------------------------ 执行
    def execute(self, line: str) -> int | None:
        text = self._history_expand(line.strip())
        if not text:
            return None
        self.history.append(text)
        tokens = apply_alias(split_command(text))
        if not tokens:
            return None
        cmd = tokens[0].lower()
        if cmd in ("help", "?", "h"):
            print(SHELL_HELP)
            print("—— 完整命令行帮助 ——")
            print(self.parser.format_help())
            return 0
        if cmd in ("exit", "quit", "q"):
            return None
        if cmd in ("clear", "cls"):
            os.system("cls" if os.name == "nt" else "clear")
            return 0
        if cmd == "history":
            for index, item in enumerate(self.history[:-1], 1):
                print(f"{index:4d}  {item}")
            return 0
        if cmd == "home":
            print(self.root)
            return 0
        if tokens[0].startswith("!"):
            return None  # 已在 _history_expand 里替换
        try:
            args = self.parser.parse_args(tokens)
        except SystemExit:
            return 2  # argparse 已打印帮助/错误
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] 参数解析失败：{exc}", file=sys.stderr)
            return 2
        if not getattr(args, "func", None):
            print(SHELL_HELP)
            return 2
        try:
            return args.func(args)
        except KeyboardInterrupt:
            print("\n已中断。")
            return 130
        except SystemExit:
            return 0
        except Exception as exc:  # noqa: BLE001 - shell 不应因为单条命令崩溃
            print(f"[错误] {exc}", file=sys.stderr)
            if getattr(args, "verbose", False):
                import traceback

                traceback.print_exc()
            return 1

    def _history_expand(self, text: str) -> str:
        if not text.startswith("!"):
            return text
        if text == "!!":
            if not self.history:
                print("[错误] 没有历史命令", file=sys.stderr)
                return ""
            return self.history[-1]
        if text.startswith("!-") and text[2:].isdigit():
            index = len(self.history) - int(text[2:])
            if 0 <= index < len(self.history):
                return self.history[index]
            print(f"[错误] 没有第 {text} 条历史命令", file=sys.stderr)
            return ""
        if text[1:].isdigit():
            index = int(text[1:]) - 1
            if 0 <= index < len(self.history):
                return self.history[index]
            print(f"[错误] 没有第 {text} 条历史命令", file=sys.stderr)
            return ""
        return text


def run(home: str | os.PathLike | None = None, *, quiet: bool = False) -> int:
    return Shell(home=home, quiet=quiet).run()
