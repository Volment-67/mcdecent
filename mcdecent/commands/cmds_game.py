"""游戏进程控制：后台启动、停止、状态、日志。

与 `launch` 的区别：`launch` 前台运行并把游戏输出打到当前控制台；`game start` 后台启动，
立刻返回提示符（适合在交互式 shell 里用），并记录 PID 到 <home>/game.pid，
之后可以用 `game status` / `game stop` / `game log` 管理。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .. import __version__, auth, launch, util, versions
from . import Context, make_parser
from .cmds_core import _parse_args_list, _pick_version


# --------------------------------------------------------------------------- PID 管理


def pid_file(ctx: Context) -> Path:
    return ctx.cfg.root / "game.pid"


def read_pid(ctx: Context) -> dict | None:
    data = util.read_json(pid_file(ctx))
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        return None
    return data


def clear_pid(ctx: Context) -> None:
    try:
        pid_file(ctx).unlink()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def kill_pid_tree(pid: int) -> bool:
    """结束进程及其子进程（Windows 用 taskkill /T）。"""
    if not pid_alive(pid):
        return True
    if sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        for _ in range(20):
            if not pid_alive(pid):
                return True
            time.sleep(0.25)
        return not pid_alive(pid)
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return True
    for _ in range(20):
        if not pid_alive(pid):
            return True
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return not pid_alive(pid)


def latest_game_log(cfg) -> Path | None:
    logs = sorted(cfg.logs_dir.glob("game-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _tail(path: Path, lines: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return text[-max(1, lines):]


# --------------------------------------------------------------------------- game start


def cmd_game_start(args) -> int:
    ctx = Context(args)
    running = read_pid(ctx)
    if running and pid_alive(running["pid"]):
        if not args.restart:
            ctx.error(f"游戏已在运行（PID {running['pid']}，版本 {running.get('version')}）。"
                      f"用 `game stop` 停止，或 `game start --restart` 重启。")
            return 1
        ctx.info(f"停止旧进程 PID {running['pid']}…")
        kill_pid_tree(running["pid"])
        clear_pid(ctx)
    inst = getattr(ctx, "instance", None)
    version = args.version or (inst.version_id if inst and inst.version_id else None) or _pick_version(ctx, None)
    username = args.username or ctx.cfg.get("player.name") or "Player"
    profile = auth.offline_profile(username)
    memory = args.memory if args.memory is not None else (inst.memory_mb if inst else None)
    jvm_args = list(inst.jvm_args if inst else []) + _parse_args_list(args.jvm_arg)
    game_args = list(inst.game_args if inst else []) + _parse_args_list(args.game_arg)
    if inst and inst.pre_launch:
        ctx.info(f"执行实例启动前命令：{inst.pre_launch}")
        try:
            subprocess.run(inst.pre_launch, shell=True, cwd=str(ctx.cfg.game_dir), timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            ctx.warn(f"启动前命令失败：{exc}")
    env_extra: dict[str, str] = {}
    for item in args.env or []:
        if "=" in str(item):
            key, value = str(item).split("=", 1)
            env_extra[key.strip()] = value
    try:
        plan = launch.build_plan(
            ctx.cfg, version, profile,
            memory_mb=memory, jvm_args=jvm_args,
            game_args=game_args,
            width=args.width, height=args.height, demo=args.demo,
            window_type=args.window_type, quick_play=args.quick_play,
            quick_play_value=args.quick_play_value,
            pre_call=args.pre_call, post_exit=args.post_exit,
            wrapper=args.wrapper, priority=args.priority, env_extra=env_extra,
            auto_java=not args.no_auto_java, downloader=ctx.downloader, progress=ctx.progress,
        )
    except versions.VersionNotInstalled as exc:
        ctx.error(str(exc))
        return 1
    if getattr(args, "no_check", False) or not ctx.cfg.get("check_game_completeness", True):
        problems: list[str] = []
    else:
        problems = launch.check_plan(plan)
    if problems:
        ctx.error("安装不完整，无法启动：")
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"修复：mcdecent install {version}", file=sys.stderr)
        return 1
    if args.dry_run:
        payload = plan.summary()
        payload["command"] = plan.command_line(max_len=20000)
        ctx.emit(payload, "命令：" + plan.command_line())
        return 0

    launch.run_hook(plan.pre_call, plan.cwd, "启动前")
    util.ensure_dir(plan.log_file.parent)
    log_handle = open(plan.log_file, "wb")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    env = os.environ.copy()
    env.pop("_JAVA_OPTIONS", None)
    env["MCDECENT"] = __version__
    env.update({str(k): str(v) for k, v in (plan.env or {}).items()})
    command = list(plan.argv)
    if plan.wrapper:
        try:
            command = shlex.split(plan.wrapper) + command
        except ValueError:
            ctx.warn(f"包装命令无法解析：{plan.wrapper}")
    try:
        proc = subprocess.Popen(
            command, cwd=str(plan.cwd), env=env,
            stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    finally:
        log_handle.close()
    launch.apply_priority(proc.pid, plan.priority)
    pid_data = {
        "pid": proc.pid,
        "version": plan.resolved.id,
        "instance": inst.name if inst else None,
        "started_at": util.now_iso(),
        "log": str(plan.log_file),
        "post_exit": plan.post_exit,
        "game_dir": str(plan.cwd),
    }
    util.write_json(pid_file(ctx), pid_data)
    # 快速失败检测：启动脚本/Java 立刻退出时把日志尾巴给用户看
    time.sleep(1.5)
    if proc.poll() is not None:
        clear_pid(ctx)
        ctx.error(f"游戏启动后立刻退出（退出码 {proc.returncode}），日志末尾：")
        for line in _tail(plan.log_file, 15):
            print("  " + line, file=sys.stderr)
        return 1
    payload = dict(pid_data, java=str(plan.java.path), log=str(plan.log_file))
    ctx.emit(payload, "\n".join([
        f"已启动 {plan.resolved.id}（PID {proc.pid}，Java {plan.java.major}）",
        f"日志：{plan.log_file}",
        "等待游戏窗口出现（这台机器约 30–70 秒）…",
        "查看状态：game status    停止：game stop    日志：game log -f",
    ]))
    return 0


# --------------------------------------------------------------------------- game stop / status / log


def cmd_game_stop(args) -> int:
    ctx = Context(args)
    data = read_pid(ctx)
    if not data:
        ctx.info("游戏未在运行（没有 game.pid）。")
        return 0
    pid = data["pid"]
    alive = pid_alive(pid)
    if alive:
        ctx.info(f"正在停止 PID {pid}（版本 {data.get('version')}）…")
        ok = kill_pid_tree(pid)
    else:
        ok = True
        ctx.info("记录里的进程已不存在，清理 PID 文件。")
    clear_pid(ctx)
    if not ok:
        ctx.error(f"无法结束 PID {pid}，请手动结束该进程。")
        return 1
    payload = {"stopped": True, "pid": pid, "version": data.get("version")}
    ctx.emit(payload, "已停止。" if alive else "已清理。")
    return 0


def cmd_game_status(args) -> int:
    ctx = Context(args)
    data = read_pid(ctx)
    if data and pid_alive(data["pid"]):
        started = data.get("started_at", "")
        payload = dict(data, running=True)
        text = "\n".join([
            f"运行中：PID {data['pid']}",
            f"版本：{data.get('version')}",
            f"启动时间：{started}",
            f"日志：{data.get('log')}",
        ])
        ctx.emit(payload, text)
        return 0
    if data:
        clear_pid(ctx)
        ctx.info("上次运行已结束（PID 文件已清理）。")
        return 0
    ctx.emit({"running": False}, "游戏未在运行。")
    return 0


def cmd_game_log(args) -> int:
    ctx = Context(args)
    data = read_pid(ctx)
    path = Path(data["log"]) if data and data.get("log") else None
    if not path or not path.is_file():
        path = latest_game_log(ctx.cfg)
    if not path or not path.is_file():
        ctx.error("还没有游戏日志。先 `game start` 启动一次。")
        return 1
    for line in _tail(path, args.lines):
        print(line)
    if not args.follow:
        return 0
    util.LOG.info("跟踪日志 %s（Ctrl+C 停止）", path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            while True:
                where = fh.tell()
                line = fh.readline()
                if not line:
                    if not (read_pid(ctx) and pid_alive(read_pid(ctx)["pid"])):
                        # 进程结束且没有新内容
                        fh.seek(where)
                        time.sleep(0.5)
                        if not fh.readline():
                            break
                    fh.seek(where)
                    time.sleep(0.5)
                    continue
                print(line, end="")
    except KeyboardInterrupt:
        print()
    return 0


# --------------------------------------------------------------------------- 注册


def register(subparsers, parent) -> None:
    p = make_parser(subparsers, parent, "game", "后台启动/停止/查看游戏进程（适合交互式使用）")
    game_sub = p.add_subparsers(dest="game_action", metavar="<操作>")

    sp = game_sub.add_parser("start", parents=[parent], help="后台启动游戏，立刻返回")
    sp.add_argument("version", nargs="?", help="版本号，默认最近安装/配置的版本")
    sp.add_argument("--username", default=None, help="玩家名")
    sp.add_argument("--memory", type=int, default=None, help="最大内存 MB")
    sp.add_argument("--jvm-arg", action="append", default=[], help="追加 JVM 参数")
    sp.add_argument("--game-arg", action="append", default=[], help="追加游戏参数")
    sp.add_argument("--width", type=int, default=None)
    sp.add_argument("--height", type=int, default=None)
    sp.add_argument("--demo", action="store_true")
    sp.add_argument("--window-type", default=None, choices=["windowed", "fullscreen", "maximized"],
                    help="窗口类型：窗口化/全屏/最大化（写 options.txt）")
    sp.add_argument("--quick-play", default=None, choices=["none", "singleplayer", "multiplayer", "realms"],
                    help="快速游戏：启动后直接进入存档/服务器")
    sp.add_argument("--quick-play-value", default=None, help="存档目录名或服务器地址")
    sp.add_argument("--env", action="append", default=[], help="附加环境变量 KEY=VALUE，可重复")
    sp.add_argument("--wrapper", default=None, help="包装命令（如 optirun）")
    sp.add_argument("--priority", default=None,
                    choices=["low", "below_normal", "normal", "above_normal", "high"], help="进程优先级")
    sp.add_argument("--pre-call", default=None, help="启动前执行的命令")
    sp.add_argument("--post-exit", default=None, help="游戏结束后执行的命令")
    sp.add_argument("--no-check", action="store_true", help="跳过安装完整性检查")
    sp.add_argument("--restart", action="store_true", help="已在运行时先停止再启动")
    sp.add_argument("--dry-run", action="store_true", help="只打印启动命令")
    sp.add_argument("--no-auto-java", action="store_true")
    sp.set_defaults(func=cmd_game_start)

    sp = game_sub.add_parser("stop", parents=[parent], help="停止游戏进程")
    sp.set_defaults(func=cmd_game_stop)

    sp = game_sub.add_parser("status", parents=[parent], help="查看游戏是否在运行")
    sp.set_defaults(func=cmd_game_status)

    sp = game_sub.add_parser("log", parents=[parent], help="查看游戏日志")
    sp.add_argument("--lines", type=int, default=40, help="显示末尾行数")
    sp.add_argument("-f", "--follow", action="store_true", help="持续跟踪新日志")
    sp.set_defaults(func=cmd_game_log)
