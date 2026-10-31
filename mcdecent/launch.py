"""启动：把解析后的版本 + Java + 身份组装成真正的命令行并执行。"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, util, versions
from . import java as java_module
from .auth import Profile
from .config import Config


@dataclass
class LaunchPlan:
    argv: list[str]
    cwd: Path
    resolved: versions.ResolvedVersion
    java: java_module.JavaRuntime
    classpath: list[str]
    native_dir: Path
    log_file: Path | None = None
    features: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    wrapper: str | None = None
    pre_call: str | None = None
    post_exit: str | None = None
    priority: str | None = None

    def command_line(self, max_len: int = 4000) -> str:
        try:
            text = shlex.join(self.argv)
        except (TypeError, ValueError):
            text = " ".join(self.argv)
        if self.wrapper:
            text = self.wrapper + " " + text
        if len(text) > max_len:
            text = text[:max_len] + f" …（共 {len(text)} 字符）"
        return text

    def summary(self) -> dict:
        return {
            "version": self.resolved.id,
            "main_class": self.resolved.main_class,
            "java": str(self.java.path),
            "java_major": self.java.major,
            "cwd": str(self.cwd),
            "argv": self.argv,
            "classpath_entries": len(self.classpath),
            "wrapper": self.wrapper,
            "pre_call": self.pre_call,
            "post_exit": self.post_exit,
            "env_keys": sorted(self.env),
        }


def _expand(value, mapping: dict) -> str:
    if not isinstance(value, str):
        return str(value)
    out = value
    for key, replacement in mapping.items():
        token = "${" + key + "}"
        if token in out:
            out = out.replace(token, str(replacement))
    return out


def _expand_args(items: list, mapping: dict, features: dict | None = None) -> list[str]:
    out: list[str] = []
    for item in items or []:
        if isinstance(item, str):            out.append(_expand(item, mapping))
        elif isinstance(item, dict):
            if not versions.rule_allows(item.get("rules"), features):
                continue
            value = item.get("value", [])
            if isinstance(value, str):
                out.append(_expand(value, mapping))
            else:
                out.extend(_expand(v, mapping) for v in value)
    return out


# --------------------------------------------------------------------------- 窗口 / 快速游戏 / 进程选项


def screen_size() -> tuple[int, int]:
    if sys.platform.startswith("win"):
        try:
            import ctypes

            return int(ctypes.windll.user32.GetSystemMetrics(0)), int(ctypes.windll.user32.GetSystemMetrics(1))
        except Exception:  # noqa: BLE001
            pass
    return 1280, 720


def patch_options(game_dir, updates: dict[str, str]) -> Path:
    """按行修改 options.txt，保留其它设置；不存在则创建。"""
    path = Path(game_dir) / "options.txt"
    lines: list[str] = []
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if ":" in line:
            key = line.split(":", 1)[0]
            if key in updates:
                out.append(f"{key}:{updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}:{value}")
    util.ensure_dir(path.parent)
    path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    return path


def apply_window_options(game_dir, window_type: str | None) -> tuple[int | None, int | None]:
    """窗口类型（对齐 HMCL）：全屏/窗口化写入 options.txt，最大化返回屏幕尺寸。"""
    kind = (window_type or "").strip().lower()
    if kind in ("fullscreen", "全屏"):
        patch_options(game_dir, {"fullscreen": "true"})
        return None, None
    if kind in ("windowed", "窗口化"):
        patch_options(game_dir, {"fullscreen": "false"})
        return None, None
    if kind in ("maximized", "最大化"):
        width, height = screen_size()
        patch_options(game_dir, {
            "fullscreen": "false",
            "overrideWidth": str(width),
            "overrideHeight": str(height),
        })
        return width, height
    return None, None


def quick_play_args(kind: str | None, value: str | None) -> list[str]:
    """生成快速游戏参数（1.20+ 支持 --quickPlay*）。"""
    text = (value or "").strip()
    if not text:
        return []
    kind = (kind or "").strip().lower()
    if kind in ("singleplayer", "单人游戏", "单人"):
        return ["--quickPlaySingleplayer", text]
    if kind in ("multiplayer", "多人游戏", "多人", "服务器"):
        return ["--quickPlayMultiplayer", text]
    if kind in ("realms", "领域"):
        return ["--quickPlayRealms", text]
    return []


def apply_priority(pid: int, priority: str | None) -> None:
    """设置游戏进程优先级（best-effort，失败不影响启动）。"""
    level = (priority or "").strip().lower()
    if not level or pid <= 0:
        return
    if sys.platform.startswith("win"):
        classes = {
            "low": 0x00000040,
            "below_normal": 0x00004000,
            "normal": 0x00000020,
            "above_normal": 0x00008000,
            "high": 0x00000080,
        }
        if level not in classes:
            return
        try:
            import ctypes

            PROCESS_SET_INFORMATION = 0x0200
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, int(pid))
            if handle:
                try:
                    ctypes.windll.kernel32.SetPriorityClass(handle, classes[level])
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        nice_value = {"low": 10, "below_normal": 5, "normal": 0, "above_normal": -5, "high": -10}.get(level)
        if nice_value is not None:
            os.nice(nice_value)
    except (OSError, AttributeError):
        pass


def run_hook(command: str | None, cwd, label: str = "") -> int:
    """执行启动前/结束后的钩子命令（shell 语义），返回退出码。"""
    if not command:
        return 0
    try:
        proc = subprocess.run(command, shell=True, cwd=str(cwd), timeout=180,
                              capture_output=True, text=True, errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        util.LOG.warning("%s命令执行失败：%r", label, exc)
        return 1
    if proc.stdout:
        util.LOG.info("%s命令输出：%s", label, proc.stdout.strip()[-2000:])
    if proc.stderr:
        util.LOG.warning("%s命令错误：%s", label, proc.stderr.strip()[-2000:])
    return int(proc.returncode or 0)


def build_plan(
    cfg: Config,
    version_id: str,
    profile: Profile,
    *,
    java_runtime: java_module.JavaRuntime | None = None,
    memory_mb: int | None = None,
    min_memory_mb: int | None = None,
    jvm_args: list[str] | None = None,
    game_args: list[str] | None = None,
    width: int | None = None,
    height: int | None = None,
    demo: bool = False,
    auto_java: bool = True,
    downloader=None,
    progress=None,
    dry_run: bool = False,
    window_type: str | None = None,
    quick_play: str | None = None,
    quick_play_value: str | None = None,
    pre_call: str | None = None,
    post_exit: str | None = None,
    env_extra: dict | None = None,
    wrapper: str | None = None,
    priority: str | None = None,
) -> LaunchPlan:
    resolved = versions.resolve(cfg, version_id)
    if java_runtime is None:
        java_runtime = java_module.ensure_java(
            cfg, major=resolved.java_major, component=resolved.java_component,
            downloader=downloader, progress=progress, auto_install=auto_java,
        )
    # 窗口类型（全屏/窗口化写 options.txt；最大化取屏幕尺寸）
    effective_window = window_type if window_type is not None else str(cfg.get("window_type") or "windowed")
    if not dry_run:
        extra_width, extra_height = apply_window_options(cfg.game_dir, effective_window)
        if width is None and extra_width:
            width = extra_width
        if height is None and extra_height:
            height = extra_height
    # 快速游戏（启动后直接进入存档/服务器）
    effective_quick_play = quick_play if quick_play is not None else str(cfg.get("quick_play") or "none")
    effective_quick_value = quick_play_value if quick_play_value is not None else str(cfg.get("quick_play_value") or "")
    quick_enabled = str(effective_quick_play).strip().lower() not in ("", "none", "无")
    features = {
        "is_demo_user": bool(demo),
        "has_custom_resolution": bool(width and height),
        "has_quick_plays_support": quick_enabled,
        "is_quick_play_singleplayer": str(effective_quick_play).lower() in ("singleplayer", "单人游戏", "单人"),
        "is_quick_play_multiplayer": str(effective_quick_play).lower() in ("multiplayer", "多人游戏", "多人", "服务器"),
        "is_quick_play_realms": str(effective_quick_play).lower() in ("realms", "领域"),
    }
    classpath = versions.classpath_entries(cfg, resolved)
    native_dir = resolved.native_dir or (cfg.root / "natives" / version_id)
    util.ensure_dir(native_dir)
    util.ensure_dir(cfg.game_dir)
    logging_file = None
    if resolved.logging_client:
        file_info = resolved.logging_client.get("file") or {}
        name = file_info.get("id")
        if name:
            candidate = cfg.assets_dir / "log_configs" / name
            if candidate.is_file():
                logging_file = candidate
    assets_index = resolved.assets_index_id or "legacy"
    mapping = {
        "auth_player_name": profile.name,
        "version_name": resolved.id,
        "game_directory": str(cfg.game_dir),
        "assets_root": str(cfg.assets_dir),
        "game_assets": str(cfg.assets_dir / "virtual" / assets_index),
        "assets_index_name": assets_index,
        "auth_uuid": profile.uuid.replace("-", ""),
        "auth_access_token": profile.access_token,
        "auth_session": profile.access_token,
        "user_type": profile.user_type,
        "user_properties": "{}",
        "clientid": profile.client_id,
        "auth_xuid": profile.xuid,
        "version_type": resolved.type,
        "launcher_name": "mcdecent",
        "launcher_version": __version__,
        "classpath": os.pathsep.join(classpath),
        "classpath_separator": os.pathsep,
        "natives_directory": str(native_dir),
        "library_directory": str(cfg.libraries_dir),
        "resolution_width": width or 854,
        "resolution_height": height or 480,
        "path": str(logging_file) if logging_file else "",
        "quickPlayPath": str(cfg.logs_dir / "quickplay.log"),
        "quickPlaySingleplayer": effective_quick_value if str(effective_quick_play).lower() in ("singleplayer", "单人游戏", "单人") else "",
        "quickPlayMultiplayer": effective_quick_value if str(effective_quick_play).lower() in ("multiplayer", "多人游戏", "多人", "服务器") else "",
        "quickPlayRealms": effective_quick_value if str(effective_quick_play).lower() in ("realms", "领域") else "",
    }

    jvm: list[str] = []
    if resolved.jvm_args:
        jvm.extend(_expand_args(resolved.jvm_args, mapping, features))
    else:
        # 老版本（1.12 及以前）没有 arguments 字段
        jvm.extend([
            "-Djava.library.path=${natives_directory}",
            "-Dminecraft.launcher.brand=${launcher_name}",
            "-Dminecraft.launcher.version=${launcher_version}",
            "-cp", "${classpath}",
        ])
        jvm = [_expand(item, mapping) for item in jvm]
    if logging_file and not any("log4j.configurationFile" in a for a in jvm):
        jvm.insert(0, f"-Dlog4j.configurationFile={logging_file}")
    # 内存与编码：放在 -cp 之前，兼容任何版本
    effective_memory = int(memory_mb or cfg.get("memory_mb") or 2048)
    effective_min = int(min_memory_mb if min_memory_mb is not None else cfg.get("min_memory_mb") or 512)
    extra_jvm: list[str] = [
        f"-Xms{effective_min}M",
        f"-Xmx{effective_memory}M",
        "-Dfile.encoding=UTF-8",
        "-Dstdout.encoding=UTF-8",
        "-Dstderr.encoding=UTF-8",
    ]
    if not any(arg.startswith("-Djava.library.path") for arg in jvm):
        extra_jvm.append(f"-Djava.library.path={native_dir}")
    if cfg.get("jvm_args"):
        extra_jvm.extend([_expand(a, mapping) for a in cfg.get("jvm_args") or []])
    if jvm_args:
        extra_jvm.extend(jvm_args)

    game = _expand_args(resolved.game_args, mapping, features)
    if width and height and "--width" not in game:
        game.extend(["--width", str(width), "--height", str(height)])
    if demo and "--demo" not in game:
        game.append("--demo")
    quick_args = quick_play_args(effective_quick_play, effective_quick_value)
    if quick_args and quick_args[0] not in game:
        game.extend(quick_args)
    if game_args:
        game.extend(game_args)

    argv = [str(java_runtime.path)] + extra_jvm + jvm + [resolved.main_class] + game
    log_file = cfg.logs_dir / f"game-{resolved.id}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    plan_env = dict(cfg.get("environment") or {})
    plan_env.update(env_extra or {})
    return LaunchPlan(
        argv=argv, cwd=cfg.game_dir, resolved=resolved, java=java_runtime,
        classpath=classpath, native_dir=native_dir, log_file=log_file, features=features,
        env={str(k): str(v) for k, v in plan_env.items()},
        wrapper=wrapper if wrapper is not None else (cfg.get("wrapper") or None),
        pre_call=pre_call if pre_call is not None else (cfg.get("pre_call") or None),
        post_exit=post_exit if post_exit is not None else (cfg.get("post_exit") or None),
        priority=priority if priority is not None else str(cfg.get("process_priority") or "normal"),
    )


def check_plan(plan: LaunchPlan) -> list[str]:
    """启动前检查：缺失文件列表。"""
    problems: list[str] = []
    if not plan.java.path.is_file():
        problems.append(f"Java 不存在：{plan.java.path}")
    missing_cp = [p for p in plan.classpath if not Path(p).is_file()]
    if missing_cp:
        problems.append(f"{len(missing_cp)} 个 classpath 文件缺失（例：{missing_cp[0]}）")
    if plan.resolved.client_jar and not plan.resolved.client_jar.is_file():
        problems.append(f"客户端 jar 缺失：{plan.resolved.client_jar}")
    return problems


def run_plan(plan: LaunchPlan, *, detach: bool = False, quiet: bool = False, echo: bool = True) -> int:
    util.ensure_dir(plan.cwd)
    if echo:
        print(f"启动 {plan.resolved.id}（{plan.resolved.java_requirement}，{plan.java.version or plan.java.source}）")
        print(f"工作目录：{plan.cwd}")
        print(f"日志文件：{plan.log_file}")
    env = os.environ.copy()
    env.pop("_JAVA_OPTIONS", None)
    env["MCDECENT"] = __version__
    env.update({str(k): str(v) for k, v in (plan.env or {}).items()})
    command = list(plan.argv)
    if plan.wrapper:
        try:
            command = shlex.split(plan.wrapper) + command
        except ValueError:
            util.LOG.warning("包装命令无法解析：%s", plan.wrapper)
    if plan.pre_call:
        run_hook(plan.pre_call, plan.cwd, "启动前")
    if detach:
        proc = subprocess.Popen(command, cwd=str(plan.cwd), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        apply_priority(proc.pid, plan.priority)
        print(f"已后台启动，PID={proc.pid}")
        return 0
    util.ensure_dir(plan.log_file.parent)
    with open(plan.log_file, "w", encoding="utf-8", errors="replace") as log_fh:
        proc = subprocess.Popen(
            command, cwd=str(plan.cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1,
        )
        apply_priority(proc.pid, plan.priority)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                log_fh.write(line)
                log_fh.flush()
                if not quiet:
                    sys.stdout.write(line)
                    sys.stdout.flush()
        except KeyboardInterrupt:
            proc.terminate()
            raise
        finally:
            proc.wait()
    if plan.post_exit:
        run_hook(plan.post_exit, plan.cwd, "游戏结束后")
    return int(proc.returncode or 0)
