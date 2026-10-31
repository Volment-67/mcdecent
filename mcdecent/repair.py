"""错误诊断 + 一键修复。

启动失败、游戏崩溃、Java 缺失、内存/磁盘异常……这些故障都对应着「有明确原因、
有明确修法」的操作。本模块把这类排查经验固化成两部分：

* :func:`diagnose` —— 只读检查，返回 :class:`Issue` 列表（中文原因 + 可执行修复动作）；
* :func:`apply_fix` / :func:`fix_all` —— 按 issue id 执行修复，返回人类可读的 actions。

设计约束：

* **绝不冒泡异常**：诊断/修复里的任何异常都记 ``LOG.warning`` 并转成 Issue 或失败结果，
  界面（Tkinter）不会因为一个坏文件而崩掉。
* **只动该动的东西**：清理磁盘只删 cache 临时文件与 7 天前的 ``game-*.log``；
  禁用模组用 ``.disabled`` 后缀；删文件前先备份（options.txt）。
* **可测试**：所有对 ``installer`` / ``java`` / ``versions`` / ``loaders`` 的调用都通过
  模块属性访问（``java.ensure_java(...)``），测试可以 monkeypatch 掉真实下载。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import installer, java, loaders, util, versions

LOG = util.LOG

LOW_DISK_BYTES = 2 * 1024 ** 3          # 低于 2 GB 视为空间不足
CRASH_RECENT_SECONDS = 24 * 3600        # 最近 24 小时内的崩溃报告
OLD_FILES_SECONDS = 7 * 86400           # 7 天：旧日志 / 缓存
_ALIASES = {"latest", "latest-release", "latest-snapshot"}

_OPTION_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_RENDER_TOKENS = (
    "opengl", "glfw", "sodium", "failed to locate library", "iris", "optifine",
    "embeddium", "shader", "显卡", "渲染",
)
_CONFLICT_TOKENS = (
    "duplicate mods", "modresolutionexception", "mod resolution", "mixin",
    "nosuchmethoderror", "incompatibleclasschangeerror", "incompatible mods",
)
_JAVA_VERSION_TOKENS = ("unsupportedclassversionerror", "class file version")


# --------------------------------------------------------------------------- 数据模型


@dataclass
class Fix:
    """一条用户可点击的修复动作。"""

    label: str
    action: str
    description: str = ""


@dataclass
class Issue:
    """一个被检出的问题。"""

    id: str
    severity: str                                   # error | warning | info
    title: str
    detail: str
    causes: list[str] = field(default_factory=list)
    fixes: list[Fix] = field(default_factory=list)
    auto_fixable: bool = False


# --------------------------------------------------------------------------- 通用辅助


def _emit(progress, message: str) -> None:
    """向可选的进度回调汇报一句话（兼容回调函数或 util.Progress 风格对象）。"""
    if progress is None or not message:
        return
    try:
        if callable(progress):
            progress(message)
        elif hasattr(progress, "add_file"):
            progress.add_file(message)
    except Exception as exc:  # noqa: BLE001 - 进度汇报失败不能影响修复
        LOG.debug("诊断进度回调失败：%r", exc)


def _total_memory_mb() -> int | None:
    """物理内存总量（MB）：Windows 用 GlobalMemoryStatusEx，其它系统回退 os.sysconf。"""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys // (1024 * 1024))
        except Exception as exc:  # noqa: BLE001
            LOG.debug("读取系统内存失败（ctypes）：%r", exc)
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages and page_size:
            return int(pages * page_size // (1024 * 1024))
    except (AttributeError, ValueError, OSError, TypeError):
        pass
    return None


def _current_memory(cfg, instance) -> int:
    """当前生效的内存上限（实例覆盖全局配置）。取不到时返回 0。"""
    value = getattr(instance, "memory_mb", None) if instance is not None else None
    if value is None:
        value = cfg.get("memory_mb")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _recommended_memory(cfg, instance) -> int:
    """推荐内存：过低给 2048；过高给系统内存的一半（按 256 MB 对齐）。"""
    current = _current_memory(cfg, instance)
    total = _total_memory_mb()
    if current < 1024:
        return 2048
    if total and current > total * 0.75:
        return max(1024, int(total * 0.5) // 256 * 256)
    return 2048 if current < 2048 else current


def _set_memory(cfg, instance, value: int) -> int:
    """写入内存设置（实例优先，同时同步全局配置），返回最终值。"""
    value = int(value)
    try:
        cfg.set("memory_mb", value)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("写入内存配置失败：%r", exc)
    if instance is not None:
        try:
            instance.memory_mb = value
        except Exception as exc:  # noqa: BLE001
            LOG.warning("写入实例内存设置失败：%r", exc)
        _persist_instance(cfg, instance)
    try:
        cfg.save()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("保存配置失败：%r", exc)
    return value


def _persist_instance(cfg, instance) -> None:
    """把实例对象的变化写回 instances.json（失败只记日志）。"""
    try:
        from .instances import InstanceManager

        InstanceManager(cfg).update(instance)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("保存实例设置失败：%r", exc)


def _game_dir(cfg, instance) -> Path:
    """当前实例的游戏目录（兼容 cfg 已指向实例目录、以及只有 Instance 的情况）。"""
    if instance is not None:
        try:
            declared = getattr(instance, "game_dir", None)
            if declared:
                return Path(declared).expanduser()
            if getattr(instance, "isolated", False):
                from .instances import InstanceManager

                return Path(cfg.root) / "instances" / InstanceManager.safe_dirname(instance.name)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("计算实例游戏目录失败：%r", exc)
    return Path(cfg.game_dir)


def _installed_ids(cfg) -> set[str]:
    try:
        return {str(item.get("id")) for item in versions.installed_versions(cfg)}
    except Exception as exc:  # noqa: BLE001
        LOG.warning("扫描已安装版本失败：%r", exc)
        return set()


def _target_version(cfg, instance, installed: list[dict] | None = None) -> str:
    """实例/配置指定或最近安装的版本 id。"""
    explicit = ""
    if instance is not None:
        explicit = str(getattr(instance, "version_id", "") or "")
    if not explicit:
        explicit = str(cfg.get("default_version") or "")
    if explicit and explicit not in _ALIASES:
        return explicit
    if installed is None:
        try:
            installed = versions.installed_versions(cfg)
        except Exception:  # noqa: BLE001
            installed = []
    return str(installed[0]["id"]) if installed else ""


def _safe_resolve(cfg, version_id: str):
    if not version_id:
        return None
    try:
        return versions.resolve(cfg, version_id)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("诊断：解析版本 %s 失败：%r", version_id, exc)
        return None


def _pid_alive(pid) -> bool:
    """判断 pid 是否仍存活（不依赖 commands 包，避免额外导入开销）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _open_path(path) -> bool:
    """尝试用系统文件管理器打开目录/文件；失败返回 False（不抛异常）。"""
    target = Path(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]
            return True
        import subprocess

        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(target)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception as exc:  # noqa: BLE001
        LOG.warning("打开目录失败 %s：%r", target, exc)
        return False


def _read_text(path: Path, limit: int = 512 * 1024) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError as exc:
        LOG.warning("读取文本失败 %s：%r", path, exc)
        return ""


# --------------------------------------------------------------------------- 崩溃报告


def _latest_crash_report(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    try:
        for item in directory.iterdir():
            if not item.is_file() or item.name.startswith("."):
                continue
            try:
                candidates.append((item.stat().st_mtime, item))
            except OSError:
                continue
    except OSError as exc:
        LOG.warning("扫描崩溃报告目录失败 %s：%r", directory, exc)
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def _classify_crash(text: str) -> str:
    """把崩溃报告归类：oom / java / graphics / mods / unknown。"""
    low = text.lower()
    if "outofmemoryerror" in low or "java heap space" in low or "out of memory" in low:
        return "oom"
    if any(token in low for token in _JAVA_VERSION_TOKENS):
        return "java"
    if any(token in low for token in _RENDER_TOKENS):
        return "graphics"
    if any(token in low for token in _CONFLICT_TOKENS):
        return "mods"
    return "unknown"


def _crash_excerpt(text: str, kind: str) -> str:
    tokens = {
        "oom": ("outofmemoryerror", "java heap space", "out of memory"),
        "java": _JAVA_VERSION_TOKENS,
        "graphics": _RENDER_TOKENS,
        "mods": _CONFLICT_TOKENS,
        "unknown": tuple(),
    }.get(kind, tuple())
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if not tokens or any(token in low for token in tokens):
            return stripped[:180]
    return ""


_CRASH_CAUSES = {
    "oom": [
        "内存不足（OutOfMemoryError / Java heap space）",
        "游戏分配的内存低于模组、资源包或高画质的实际需求",
        "后台同时开了太多程序，可用内存被挤压",
    ],
    "graphics": [
        "显卡驱动过旧或不受支持（OpenGL / GLFW 初始化失败）",
        "渲染类模组（Sodium / Iris / OptiFine / Embeddium 等）与当前版本不匹配",
        "缺少本地图形库（如 libglfw.so / glfw.dll）",
    ],
    "mods": [
        "模组之间冲突，或缺少前置依赖（Duplicate mods / Mixin / NoSuchMethodError）",
        "模组与当前游戏版本或加载器版本不匹配",
        "同一个模组装了多个版本（重复模组）",
    ],
    "java": [
        "Java 版本低于游戏或模组要求（UnsupportedClassVersionError）",
        "启动器选中了过旧的 Java 运行时",
    ],
    "unknown": [
        "具体原因未知，需要查看完整崩溃报告",
        "可能是硬件、驱动或系统环境问题",
    ],
}

_CRASH_TITLES = {
    "oom": "游戏最近因内存不足崩溃",
    "graphics": "游戏最近因图形/渲染问题崩溃",
    "mods": "游戏最近因模组冲突崩溃",
    "java": "游戏最近因 Java 版本过低崩溃",
    "unknown": "游戏最近崩溃（原因未知）",
}


def _crash_issue(cfg, instance) -> Issue | None:
    directory = _game_dir(cfg, instance) / "crash-reports"
    report = _latest_crash_report(directory)
    if report is None:
        return None
    try:
        age = time.time() - report.stat().st_mtime
    except OSError:
        return None
    if age > CRASH_RECENT_SECONDS:
        return None

    text = _read_text(report)
    kind = _classify_crash(text)
    excerpt = _crash_excerpt(text, kind)
    detail = (
        f"最近 24 小时内有一次崩溃：{report.name}\n"
        f"崩溃报告：{report}"
    )
    if excerpt:
        detail += f"\n关键行：{excerpt}"

    fixes: list[Fix] = []
    auto = True
    if kind == "oom":
        current = _current_memory(cfg, instance)
        fixes = [Fix(
            "增大内存并重试",
            "crash-memory",
            f"把内存上限从 {current} MB 提高 1024 MB；仍不足时再降低渲染距离/画质",
        )]
    elif kind == "graphics":
        fixes = [Fix(
            "禁用图形类模组",
            "crash-graphics",
            "把名字含 sodium / iris / optifine / embeddium 的模组改成 .disabled 后重启游戏",
        )]
    elif kind == "mods":
        fixes = [Fix(
            "禁用全部模组",
            "crash-mods",
            "先全部禁用确认能启动，再逐个启用以定位冲突；原列表备份到 mods.disabled-by-repair.txt",
        )]
    elif kind == "java":
        fixes = [Fix(
            "安装匹配的 Java",
            "crash-java",
            "自动下载并切换到满足游戏版本要求的官方 Java 运行时",
        )]
    else:
        auto = False
        fixes = [Fix(
            "打开崩溃报告与日志目录",
            "crash-open",
            "手动查看完整报告与游戏日志，定位具体报错",
        )]

    return Issue(
        id="game-crash",
        severity="error",
        title=_CRASH_TITLES.get(kind, _CRASH_TITLES["unknown"]),
        detail=detail,
        causes=list(_CRASH_CAUSES.get(kind, _CRASH_CAUSES["unknown"])),
        fixes=fixes,
        auto_fixable=auto,
    )


# --------------------------------------------------------------------------- 各项检查


def _check_version(cfg, instance, installed: list[dict], explicit: str) -> Issue | None:
    installed_ids = {str(item.get("id")) for item in installed}
    if not installed:
        title = "没有安装任何游戏版本"
        detail = "启动器目录里还没有可启动的 Minecraft 版本，启动游戏会直接失败。"
        if explicit:
            title = f"实例指定的版本 {explicit} 未安装"
            detail = f"当前实例指定了 {explicit}，但 versions/ 里找不到它，无法启动。"
        return Issue(
            id="no-version",
            severity="error",
            title=title,
            detail=detail,
            causes=[
                "从未安装过游戏版本（首次使用）",
                "版本目录被手动删除或移动",
                "实例指向的版本在别处安装，本机没有对应文件",
            ],
            fixes=[
                Fix("安装最新正式版", "install-latest",
                    "联网获取 Mojang 最新正式版并下载客户端、库与资源"),
                Fix("安装实例指定版本", "install-version",
                    "重新安装当前实例需要的版本（没有指定时安装最新正式版）"),
            ],
            auto_fixable=True,
        )
    if explicit and explicit not in installed_ids:
        return Issue(
            id="no-version",
            severity="error",
            title=f"实例指定的版本 {explicit} 未安装",
            detail=(
                f"当前实例指向 {explicit}，但本机已安装的版本里没有它。\n"
                f"已安装：{'、'.join(sorted(installed_ids)) or '（无）'}"
            ),
            causes=[
                "切换实例后没有安装对应版本",
                "版本文件被删除或移动到别的启动器目录",
            ],
            fixes=[
                Fix(f"安装 {explicit}", "install-version",
                    "下载并安装该版本（含加载器库、资源与 Java 检查）"),
            ],
            auto_fixable=True,
        )
    return None


def _is_fabric_version(cfg, version_id: str) -> bool:
    if not version_id:
        return False
    try:
        meta = versions.read_loader_meta(cfg, version_id) or {}
    except Exception:  # noqa: BLE001
        meta = {}
    loader_type = str(meta.get("type") or "").lower()
    if loader_type in ("fabric", "quilt"):
        return True
    low = version_id.lower()
    return low.startswith(("fabric-loader-", "quilt-loader-"))


def _check_incomplete(cfg, version_id: str, is_fabric: bool) -> Issue | None:
    if not version_id:
        return None
    try:
        report = installer.installation_report(cfg, version_id)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("诊断：检查 %s 安装完整性失败：%r", version_id, exc)
        return None
    if report.get("ok"):
        return None
    if report.get("error"):
        # Fabric 版本的“基础版本缺失”由独立的 fabric-missing 检查更准确地描述
        if is_fabric:
            return None
        detail = f"无法解析版本 {version_id}：{report.get('error')}"
        causes = ["版本 JSON 缺失或损坏", "基础版本被删除/移动", "从别处复制了不完整的目录"]
    else:
        missing = list(report.get("missing") or [])
        count = report.get("missing_count") or len(missing)
        detail = f"版本 {version_id} 缺少 {count} 个必要文件，启动会失败。"
        if missing:
            detail += "\n例如：" + "、".join(str(item) for item in missing[:5])
        causes = [
            "下载过程被中断或磁盘写入失败",
            "手动删除过 versions / libraries / assets 里的文件",
            "从别处拷贝了不完整的启动器目录",
        ]
    return Issue(
        id="incomplete-install",
        severity="error",
        title=f"版本 {version_id} 安装不完整",
        detail=detail,
        causes=causes,
        fixes=[Fix(
            "重新下载缺失文件",
            "reinstall-version",
            "重新执行安装流程：复用已有缓存，只补齐缺失的客户端/库/资源",
        )],
        auto_fixable=True,
    )


def _check_java(cfg, version_id: str) -> Issue | None:
    resolved = _safe_resolve(cfg, version_id)
    if resolved is None:
        return None
    required = resolved.java_major
    try:
        runtime = java.find_java(cfg, major=required)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("诊断：查找 Java 失败：%r", exc)
        return None
    if runtime is not None and (not required or getattr(runtime, "major", 0) >= required):
        return None

    any_java = None
    if runtime is None:
        try:
            any_java = java.find_java(cfg)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("诊断：查找任意 Java 失败：%r", exc)
            any_java = None

    component = resolved.java_component
    if any_java is not None and required and getattr(any_java, "major", 0) < required:
        title = f"Java 版本过低（当前 {any_java.major}，需要 {required}）"
        detail = (
            f"版本 {version_id} 需要 Java {required} 或更高，但本机找到的是 "
            f"Java {any_java.major}（{getattr(any_java, 'path', '?')}）。"
        )
        causes = [
            f"本机只装了 Java {any_java.major}，低于 {version_id} 的要求",
            "多个 Java 共存时启动器选中了旧的那个",
        ]
    else:
        title = f"缺少 Java 运行时（需要 Java {required}）" if required else "缺少 Java 运行时"
        detail = (
            f"版本 {version_id} 需要 Java {required}，但系统 PATH/JAVA_HOME 和启动器"
            f"托管目录里都没有找到可用运行时。"
            if required else f"没有找到可用的 Java 运行时，{version_id} 无法启动。"
        )
        causes = [
            "系统没有安装 Java，或安装目录不在 PATH / JAVA_HOME 中",
            "启动器托管目录 <home>/java 下没有可用运行时",
        ]
    component_note = f"（Mojang 组件 {component}）" if component else ""
    return Issue(
        id="java-missing",
        severity="error",
        title=title,
        detail=detail + component_note,
        causes=causes,
        fixes=[Fix(
            f"下载并安装 Java {required}" if required else "下载并安装 Java",
            "install-java",
            "自动从 Mojang 官方运行时或 Adoptium 下载，落在启动器目录，不改动系统环境",
        )],
        auto_fixable=True,
    )


def _check_memory(cfg, instance) -> Issue | None:
    current = _current_memory(cfg, instance)
    total = _total_memory_mb()
    if current < 1024:
        title = "内存设置过低"
        detail = (
            f"当前内存上限只有 {current} MB，现代版本（尤其是带模组/光影时）很容易 "
            f"OutOfMemoryError 崩溃。建议至少 2048 MB。"
        )
        causes = [
            "手动把 memory_mb 调到了 1024 以下",
            "旧配置/整合包自带的低内存设置",
        ]
    elif total and current > total * 0.75:
        title = "内存设置过高"
        detail = (
            f"当前内存上限 {current} MB 超过系统物理内存（{total} MB）的 75%，"
            f"可能导致系统整体卡顿甚至崩溃。建议下调到约 {_recommended_memory(cfg, instance)} MB。"
        )
        causes = [
            "照搬了别人的 JVM 参数/内存设置",
            "系统物理内存较小，但配置里写死了大内存",
        ]
    else:
        return None
    recommended = _recommended_memory(cfg, instance)
    return Issue(
        id="memory-config",
        severity="warning",
        title=title,
        detail=detail,
        causes=causes,
        fixes=[Fix(
            f"设为推荐值 {recommended} MB",
            "set-memory",
            "写入内存设置（实例优先，同时同步全局配置）",
        )],
        auto_fixable=True,
    )


def _check_disk(cfg) -> Issue | None:
    try:
        usage = shutil.disk_usage(cfg.root)
    except OSError as exc:
        LOG.warning("诊断：读取磁盘空间失败：%r", exc)
        return None
    if usage.free >= LOW_DISK_BYTES:
        return None
    return Issue(
        id="low-disk",
        severity="warning",
        title="磁盘剩余空间不足",
        detail=(
            f"启动器目录所在磁盘只剩 {util.human_size(usage.free)}，"
            f"安装/更新版本或写存档时可能失败（建议至少保留 2 GB）。"
        ),
        causes=[
            "磁盘被其它程序/下载占满",
            "cache 与旧游戏日志长期未清理",
            "安装的版本、资源与整合包过多",
        ],
        fixes=[Fix(
            "清理缓存与旧日志",
            "clean-disk",
            "删除 cache 里的临时文件与 7 天前的 game-*.log；不会动已安装版本、库与资源",
        )],
        auto_fixable=True,
    )


def _check_stale_pid(cfg) -> Issue | None:
    path = Path(cfg.root) / "game.pid"
    if not path.is_file():
        return None
    data = util.read_json(path, {})
    pid = data.get("pid") if isinstance(data, dict) else None
    if isinstance(pid, int) and pid > 0 and _pid_alive(pid):
        return None
    if isinstance(pid, int) and pid > 0:
        detail = f"记录的游戏进程（PID {pid}）已经不存在，但 game.pid 还留着。"
    else:
        detail = "game.pid 内容不完整（没有有效的 pid），属于上次异常退出留下的垃圾文件。"
    return Issue(
        id="stale-pid",
        severity="warning",
        title="存在残留的游戏进程记录",
        detail=detail + " 一般不影响启动，但「游戏运行中」状态会显示错误。",
        causes=[
            "游戏异常退出/被任务管理器结束后没有清理 PID 文件",
            "启动器在写 PID 时被强制关闭",
        ],
        fixes=[Fix(
            "清理残留的 game.pid",
            "clean-pid",
            "删除 PID 文件；正在运行的游戏进程（若存在）不会受影响",
        )],
        auto_fixable=True,
    )


def _option_line_valid(line: str) -> bool:
    """options.txt 的一行是否可解析（空行视为合法）。"""
    if not line.strip():
        return True
    if "\ufffd" in line or "\x00" in line:
        return False
    if ":" not in line:
        return False
    key, _, value = line.partition(":")
    if not _OPTION_KEY_RE.match(key.strip()):
        return False
    raw = value.strip()
    if not raw:
        return False
    # 以 JSON 结构（数组/对象/字符串）出现的值必须能解析
    if raw[0] in "{[\"":
        try:
            json.loads(raw)
        except (ValueError, TypeError):
            return False
    return True


def _check_options(cfg, instance) -> Issue | None:
    game_dir = _game_dir(cfg, instance)
    path = game_dir / "options.txt"
    if not path.is_file():
        return None
    text = _read_text(path, limit=256 * 1024)
    invalid = [line for line in text.splitlines() if not _option_line_valid(line)]
    if not invalid:
        return None
    return Issue(
        id="options-corrupt",
        severity="warning",
        title="options.txt 存在无法解析的内容",
        detail=(
            f"options.txt 有 {len(invalid)} 行格式非法，可能导致游戏设置混乱或启动报错。\n"
            f"文件：{path}\n例如：{invalid[0].strip()[:120]}"
        ),
        causes=[
            "游戏异常退出时写入了不完整的行",
            "手动编辑 options.txt 时格式写错",
            "磁盘写入中断导致文件损坏",
        ],
        fixes=[Fix(
            "备份并重建 options.txt",
            "rebuild-options",
            "原文件备份为 options.txt.bak-<时间>，只保留能解析的设置行",
        )],
        auto_fixable=True,
    )


def _check_fabric(cfg, version_id: str) -> Issue | None:
    if not version_id or not _is_fabric_version(cfg, version_id):
        return None
    try:
        meta = versions.read_loader_meta(cfg, version_id) or {}
        local = versions.read_local_version(cfg, version_id) or {}
    except Exception as exc:  # noqa: BLE001
        LOG.warning("诊断：读取加载器元信息失败 %s：%r", version_id, exc)
        return None
    base = local.get("inheritsFrom") or meta.get("game")
    problems: list[str] = []
    if not base:
        problems.append("缺少 inheritsFrom，无法确定基础游戏版本")
    else:
        try:
            base_json = versions.version_json_path(cfg, str(base))
        except Exception:  # noqa: BLE001
            base_json = Path(cfg.versions_dir) / str(base) / f"{base}.json"
        if not base_json.is_file():
            problems.append(f"基础版本 {base} 的版本 JSON 不存在（{base_json}）")
    try:
        report = installer.installation_report(cfg, version_id)
    except Exception as exc:  # noqa: BLE001
        report = {"error": repr(exc)}
    if report.get("error"):
        problems.append(f"加载器版本解析失败：{report.get('error')}")
    else:
        missing = [str(item) for item in (report.get("missing") or [])]
        loader_missing = [
            item for item in missing
            if any(token in item.lower() for token in ("fabric", "quilt", "intermediary", "loader"))
        ]
        if loader_missing:
            problems.append("加载器库缺失：" + "、".join(loader_missing[:3]))
    if not problems:
        return None
    loader_type = str(meta.get("type") or "fabric").lower()
    return Issue(
        id="fabric-missing",
        severity="error",
        title=f"{loader_type.capitalize()} 加载器不完整",
        detail=(
            f"版本 {version_id} 是 {loader_type} 加载器版本，但安装不完整：\n- "
            + "\n- ".join(problems)
        ),
        causes=[
            "加载器安装过程中断（下载或写盘失败）",
            "基础版本被删除或移动",
            "手动复制了不完整的版本目录",
        ],
        fixes=[Fix(
            f"重新安装 {loader_type.capitalize()} 加载器",
            "reinstall-fabric",
            "重新拉取加载器 profile，并补齐基础版本与加载器库",
        )],
        auto_fixable=True,
    )


def _probe_network(cfg) -> dict:
    """探测官方源与镜像的连通性（HEAD，超时 8s）。返回结果字典，绝不抛异常。"""
    result: dict[str, Any] = {
        "official_ok": False, "official_detail": "",
        "mirror_ok": False, "mirror_name": "", "mirror_detail": "",
        "error": "",
    }
    try:
        downloader = cfg.make_downloader(quiet=True)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"无法创建下载器：{exc!r}"
        return result
    head_ok = getattr(downloader, "head_ok", None)
    if not callable(head_ok):
        result["error"] = "下载器不支持可用性探测"
        return result
    try:
        ok, detail = head_ok(versions.MANIFEST_URL, timeout=8)
        result["official_ok"] = bool(ok)
        result["official_detail"] = str(detail)
    except Exception as exc:  # noqa: BLE001
        result["official_detail"] = repr(exc)[:160]
    try:
        mirrors = cfg.mirror_objects()
    except Exception:  # noqa: BLE001
        mirrors = []
    for mirror in mirrors:
        try:
            url = mirror.rewrite(versions.MANIFEST_URL)
        except Exception:  # noqa: BLE001
            continue
        if not url:
            continue
        try:
            ok, detail = head_ok(url, timeout=8)
        except Exception as exc:  # noqa: BLE001
            result["mirror_detail"] = repr(exc)[:160]
            continue
        if ok:
            result["mirror_ok"] = True
            result["mirror_name"] = getattr(mirror, "name", "")
            break
        result["mirror_detail"] = str(detail)
    return result


def _check_network(cfg) -> Issue | None:
    probe = _probe_network(cfg)
    if probe.get("error"):
        LOG.warning("诊断：网络探测失败：%s", probe["error"])
        return None
    if probe.get("official_ok") or probe.get("mirror_ok"):
        return None
    detail = (
        "官方源与镜像都无法连接：\n"
        f"- Mojang 官方：{probe.get('official_detail') or '连接失败'}\n"
        f"- 默认镜像：{probe.get('mirror_detail') or '连接失败'}\n"
        "安装/更新版本、下载 Java 都会失败。"
    )
    return Issue(
        id="network-mirror",
        severity="warning",
        title="网络或镜像不可用",
        detail=detail,
        causes=[
            "网络断开、需要代理，或代理配置错误",
            "镜像站维护、限流或域名无法解析",
            "系统根证书过旧导致 HTTPS 证书校验失败",
        ],
        fixes=[
            Fix("切换镜像策略并重试", "fix-network",
                "在镜像 / 官方直连之间切换，并在必要时回退 TLS 校验策略"),
        ],
        auto_fixable=True,
    )


# --------------------------------------------------------------------------- 诊断入口


def diagnose(cfg, instance=None, *, include_network: bool = False) -> list[Issue]:
    """检查当前配置/实例的常见故障，返回按严重程度排列的 Issue 列表。

    本函数保证不抛异常：单个检查失败只记日志，其余检查继续。
    """
    issues: list[Issue] = []
    try:
        installed = versions.installed_versions(cfg)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("诊断：扫描已安装版本失败：%r", exc)
        installed = []

    explicit = ""
    if instance is not None:
        explicit = str(getattr(instance, "version_id", "") or "")
    if not explicit:
        explicit = str(cfg.get("default_version") or "")
    if explicit in _ALIASES:
        explicit = ""
    target = explicit or (str(installed[0]["id"]) if installed else "")
    installed_ids = {str(item.get("id")) for item in installed}
    target_ready = bool(target) and target in installed_ids
    is_fabric = target_ready and _is_fabric_version(cfg, target)

    checks: list[tuple[str, Callable[[], Issue | None]]] = [
        ("version", lambda: _check_version(cfg, instance, installed, explicit)),
        ("incomplete-install", lambda: _check_incomplete(cfg, target, is_fabric) if target_ready else None),
        ("java", lambda: _check_java(cfg, target) if target_ready else None),
        ("fabric", lambda: _check_fabric(cfg, target) if target_ready else None),
        ("memory", lambda: _check_memory(cfg, instance)),
        ("disk", lambda: _check_disk(cfg)),
        ("crash", lambda: _crash_issue(cfg, instance)),
        ("stale-pid", lambda: _check_stale_pid(cfg)),
        ("options", lambda: _check_options(cfg, instance)),
    ]
    if include_network:
        checks.append(("network", lambda: _check_network(cfg)))

    for name, check in checks:
        try:
            issue = check()
        except Exception as exc:  # noqa: BLE001 - 任何单项异常都不能拖垮诊断
            LOG.warning("诊断项 %s 失败：%r", name, exc)
            continue
        if issue is not None:
            issues.append(issue)
    issues.sort(key=lambda item: {"error": 0, "warning": 1, "info": 2}.get(item.severity, 3))
    return issues


# --------------------------------------------------------------------------- 修复动作实现


def _disable_mods_by_tokens(cfg, instance, tokens: tuple[str, ...]) -> list[str]:
    directory = _game_dir(cfg, instance) / "mods"
    if not directory.is_dir():
        return []
    disabled: list[str] = []
    try:
        jars = sorted(p for p in directory.iterdir()
                      if p.is_file() and p.name.lower().endswith(".jar"))
    except OSError as exc:
        LOG.warning("扫描 mods 目录失败 %s：%r", directory, exc)
        return []
    for path in jars:
        low = path.name.lower()
        if not any(token in low for token in tokens):
            continue
        target = path.with_name(path.name + ".disabled")
        try:
            if target.exists():
                LOG.warning("禁用模组失败（目标已存在）：%s", target)
                continue
            path.rename(target)
            disabled.append(path.name)
        except OSError as exc:
            LOG.warning("禁用模组失败 %s：%r", path, exc)
    return disabled


def _disable_all_mods(cfg, instance) -> tuple[list[str], Path | None]:
    directory = _game_dir(cfg, instance) / "mods"
    if not directory.is_dir():
        return [], None
    try:
        jars = sorted(p for p in directory.iterdir()
                      if p.is_file() and p.name.lower().endswith(".jar"))
    except OSError as exc:
        LOG.warning("扫描 mods 目录失败 %s：%r", directory, exc)
        return [], None
    if not jars:
        return [], None
    backup = directory / "mods.disabled-by-repair.txt"
    try:
        util.atomic_write_text(
            backup,
            "\n".join(path.name for path in jars) + "\n",
        )
    except OSError as exc:  # noqa: PERF203
        LOG.warning("写入模组备份列表失败 %s：%r", backup, exc)
        backup = None
    disabled: list[str] = []
    for path in jars:
        target = path.with_name(path.name + ".disabled")
        try:
            if target.exists():
                continue
            path.rename(target)
            disabled.append(path.name)
        except OSError as exc:
            LOG.warning("禁用模组失败 %s：%r", path, exc)
    return disabled, backup


def _fix_no_version(cfg, instance, actions: list[str], progress) -> str:
    version_id = ""
    if instance is not None:
        version_id = str(getattr(instance, "version_id", "") or "")
    if not version_id or version_id in _ALIASES:
        version_id = str(cfg.get("default_version") or "")
    if version_id in _ALIASES:
        version_id = ""
    if not version_id:
        _emit(progress, "正在获取最新正式版…")
        version_id = versions.latest_release(cfg)
        actions.append(f"最新正式版：{version_id}")
    _emit(progress, f"正在安装版本 {version_id}…")
    summary = installer.install_version(cfg, version_id)
    installed_id = str((summary or {}).get("version") or version_id)
    actions.append(f"已安装版本 {installed_id}")
    if instance is not None and not getattr(instance, "version_id", ""):
        instance.version_id = installed_id
        _persist_instance(cfg, instance)
        actions.append(f"已把实例「{getattr(instance, 'name', '')}」绑定到 {installed_id}")
    if cfg.get("default_version") != installed_id:
        try:
            cfg.set("default_version", installed_id)
            cfg.save()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("写入默认版本失败：%r", exc)
    return f"版本 {installed_id} 安装完成"


def _fix_incomplete(cfg, instance, actions: list[str], progress) -> str:
    version_id = _target_version(cfg, instance)
    if not version_id:
        raise RuntimeError("没有可修复的版本，请先在版本页选择一个版本")
    _emit(progress, f"正在补齐 {version_id} 的安装文件…")
    summary = installer.install_version(cfg, version_id)
    actions.append(f"已补全版本 {version_id} 的缺失文件")
    java_info = (summary or {}).get("java")
    if isinstance(java_info, dict):
        actions.append(f"Java：{java_info.get('major', '?')}（{java_info.get('path', '?')}）")
    return f"版本 {version_id} 安装已补全"


def _fix_java(cfg, instance, actions: list[str], progress) -> str:
    version_id = _target_version(cfg, instance)
    resolved = _safe_resolve(cfg, version_id)
    major = getattr(resolved, "java_major", None) if resolved is not None else None
    component = getattr(resolved, "java_component", None) if resolved is not None else None
    _emit(progress, f"正在准备 Java {major or ''} 运行时…")
    runtime = java.ensure_java(cfg, major=major, component=component)
    actions.append(
        f"已准备 Java {getattr(runtime, 'major', major or '?')}"
        f"（{getattr(runtime, 'source', 'auto')}）"
    )
    runtime_path = getattr(runtime, "path", None)
    if runtime_path:
        actions.append(f"路径：{runtime_path}")
    return "Java 运行时已就绪"


def _fix_memory(cfg, instance, actions: list[str], progress) -> str:
    current = _current_memory(cfg, instance)
    recommended = _recommended_memory(cfg, instance)
    final = _set_memory(cfg, instance, recommended)
    actions.append(f"内存上限：{current} MB → {final} MB")
    return f"内存已调整为 {final} MB"


def _fix_disk(cfg, instance, actions: list[str], progress) -> str:
    now = time.time()
    freed_files = 0
    freed_bytes = 0
    cache_dir = Path(cfg.cache_dir)
    _emit(progress, "正在清理缓存临时文件…")
    if cache_dir.is_dir():
        try:
            candidates = list(cache_dir.rglob("*"))
        except OSError as exc:
            LOG.warning("扫描缓存目录失败 %s：%r", cache_dir, exc)
            candidates = []
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            name = path.name.lower()
            is_temp = (
                name.startswith(".") or name.endswith(".part") or name.endswith(".lzma")
                or name.startswith("tmp") or ".tmp-" in name
            )
            if not (is_temp or now - stat.st_mtime > OLD_FILES_SECONDS):
                continue
            try:
                path.unlink()
                freed_files += 1
                freed_bytes += stat.st_size
            except OSError as exc:
                LOG.warning("删除缓存文件失败 %s：%r", path, exc)
    actions.append(f"清理缓存临时文件 {freed_files} 个（{util.human_size(freed_bytes)}）")

    removed_logs = 0
    logs_dir = Path(cfg.logs_dir)
    if logs_dir.is_dir():
        for path in logs_dir.glob("game-*.log"):
            try:
                if now - path.stat().st_mtime <= OLD_FILES_SECONDS:
                    continue
                path.unlink()
                removed_logs += 1
            except OSError as exc:
                LOG.warning("删除旧日志失败 %s：%r", path, exc)
    actions.append(f"删除 7 天前的游戏日志 {removed_logs} 个")

    if _open_path(cfg.root):
        actions.append(f"已打开启动器目录：{cfg.root}")
    else:
        actions.append(f"启动器目录：{cfg.root}")
    return "已清理缓存与旧日志"


def _fix_stale_pid(cfg, instance, actions: list[str], progress) -> str:
    path = Path(cfg.root) / "game.pid"
    if not path.is_file():
        actions.append("没有残留的 game.pid，无需清理")
        return "没有需要清理的进程记录"
    try:
        path.unlink()
    except OSError as exc:
        raise RuntimeError(f"无法删除 {path}：{exc}") from exc
    actions.append("已删除残留的 game.pid")
    return "已清理残留进程记录"


def _fix_options(cfg, instance, actions: list[str], progress) -> str:
    game_dir = _game_dir(cfg, instance)
    path = game_dir / "options.txt"
    if not path.is_file():
        raise RuntimeError(f"找不到 {path}，无需修复")
    text = _read_text(path, limit=1024 * 1024)
    valid_lines = [line for line in text.splitlines() if line.strip() and _option_line_valid(line)]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"options.txt.bak-{stamp}")
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        LOG.warning("备份 options.txt 失败，改为直接写入备份文本：%r", exc)
        try:
            util.atomic_write_text(backup, text)
        except OSError as exc2:
            raise RuntimeError(f"无法备份 options.txt：{exc2}") from exc2
    payload = "\n".join(valid_lines) + ("\n" if valid_lines else "")
    util.atomic_write_text(path, payload)
    actions.append(f"原文件已备份：{backup.name}")
    actions.append(f"已重建 options.txt（保留 {len(valid_lines)} 行有效设置）")
    return "options.txt 已重建"


def _fix_network(cfg, instance, actions: list[str], progress) -> str:
    _emit(progress, "正在重新探测网络与镜像…")
    probe = _probe_network(cfg)
    mode = str(cfg.get("mirror_mode") or "auto")
    if probe.get("mirror_ok") and not probe.get("official_ok"):
        new_mode = "auto"
    elif probe.get("official_ok") and not probe.get("mirror_ok"):
        new_mode = "off"
    else:
        new_mode = "off" if mode != "off" else "auto"
    if new_mode != mode:
        cfg.set("mirror_mode", new_mode)
        actions.append(f"镜像策略：{mode} → {new_mode}")
    else:
        actions.append(f"镜像策略保持 {mode}")
    if str(cfg.get("tls_verify") or "fallback") == "strict":
        cfg.set("tls_verify", "fallback")
        actions.append("TLS 校验：strict → fallback（内容仍按 SHA 摘要校验）")
    try:
        cfg.save()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("保存网络设置失败：%r", exc)
    actions.append("请重新诊断确认网络是否恢复")
    return "已调整网络/镜像策略"


def _fix_crash(cfg, instance, actions: list[str], progress) -> str:
    game_dir = _game_dir(cfg, instance)
    report = _latest_crash_report(game_dir / "crash-reports")
    if report is None:
        raise RuntimeError("没有找到崩溃报告，可能已被清理")
    text = _read_text(report)
    kind = _classify_crash(text)
    actions.append(f"崩溃报告：{report}")

    if kind == "oom":
        current = _current_memory(cfg, instance)
        target = current + 1024
        final = _set_memory(cfg, instance, target)
        actions.append(f"内存上限：{current} MB → {final} MB")
        actions.append("提示：若仍 OOM，请降低渲染距离、关闭光影或减少模组")
        return "已增大内存，请重启游戏重试"
    if kind == "graphics":
        disabled = _disable_mods_by_tokens(
            cfg, instance,
            ("sodium", "iris", "optifine", "embeddium", "oculus", "shader"),
        )
        if disabled:
            actions.append("已禁用图形类模组：" + "、".join(disabled))
        else:
            actions.append("mods 里没有找到 sodium/iris/optifine/embeddium 类模组")
        actions.append("建议同时更新显卡驱动；若装了光影/资源包可先移除")
        return "已禁用图形类模组，请重启游戏"
    if kind == "mods":
        disabled, backup = _disable_all_mods(cfg, instance)
        if backup is not None:
            actions.append(f"原模组列表已备份：{backup.name}")
        actions.append(f"已禁用 {len(disabled)} 个模组")
        if not disabled:
            actions.append("mods 目录里没有启用的模组")
        actions.append("逐个重新启用模组即可定位冲突项")
        return "已禁用全部模组，请重启游戏确认"
    if kind == "java":
        version_id = _target_version(cfg, instance)
        resolved = _safe_resolve(cfg, version_id)
        major = getattr(resolved, "java_major", None) if resolved is not None else None
        component = getattr(resolved, "java_component", None) if resolved is not None else None
        _emit(progress, f"正在准备 Java {major or ''}…")
        runtime = java.ensure_java(cfg, major=major, component=component)
        actions.append(
            f"已准备 Java {getattr(runtime, 'major', major or '?')}"
            f"（{getattr(runtime, 'source', 'auto')}）"
        )
        return "已准备匹配的 Java 运行时"

    # unknown：打开报告与日志目录，交给用户判断
    actions.append(f"日志目录：{cfg.logs_dir}")
    opened = _open_path(report.parent)
    opened = _open_path(cfg.logs_dir) or opened
    actions.append("已尝试打开崩溃报告与日志目录" if opened else "请手动打开上面的目录")
    return "已定位崩溃报告，请根据完整日志排查"


def _fix_fabric(cfg, instance, actions: list[str], progress) -> str:
    version_id = _target_version(cfg, instance)
    if not version_id:
        raise RuntimeError("没有可修复的版本，请先在版本页选择一个版本")
    try:
        meta = versions.read_loader_meta(cfg, version_id) or {}
        local = versions.read_local_version(cfg, version_id) or {}
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"读取加载器信息失败：{exc}") from exc
    loader_type = str(meta.get("type") or "").lower()
    if loader_type not in ("fabric", "quilt"):
        loader_type = "fabric" if version_id.lower().startswith("fabric") else "quilt"
    loader_version = meta.get("version")
    base = local.get("inheritsFrom") or meta.get("game") or ""
    if not base:
        raise RuntimeError("无法确定基础游戏版本，请手动重装该加载器版本")
    actions.append(f"重新安装 {loader_type} 加载器（基础版本 {base}）")
    loader_id = version_id
    if loader_version:
        _emit(progress, f"正在重新写入 {loader_type} {loader_version} 的 profile…")
        try:
            new_id = loaders.install_loader(cfg, base, loader_type, loader_version)
            if new_id:
                loader_id = str(new_id)
            actions.append(f"加载器 profile 已就绪：{loader_id}")
        except Exception as exc:  # noqa: BLE001
            actions.append(f"重新获取加载器 profile 失败，继续尝试补齐本地文件：{exc}")
    _emit(progress, "正在补齐加载器库与基础版本文件…")
    installer.install_version(cfg, loader_id, skip_java=True)
    actions.append(f"已补全版本 {loader_id} 的加载器库")
    if loader_id != version_id and instance is not None:
        instance.version_id = loader_id
        _persist_instance(cfg, instance)
        actions.append(f"已把实例「{getattr(instance, 'name', '')}」切换到 {loader_id}")
    return f"{loader_type} 加载器已重新安装"


_HANDLERS: dict[str, Callable[[Any, Any, list[str], Any], str]] = {
    "no-version": _fix_no_version,
    "incomplete-install": _fix_incomplete,
    "java-missing": _fix_java,
    "memory-config": _fix_memory,
    "low-disk": _fix_disk,
    "game-crash": _fix_crash,
    "network-mirror": _fix_network,
    "stale-pid": _fix_stale_pid,
    "options-corrupt": _fix_options,
    "fabric-missing": _fix_fabric,
}


# --------------------------------------------------------------------------- 修复入口


def apply_fix(cfg, issue_id, *, instance=None, progress=None) -> dict:
    """按 issue id 执行修复，返回 ``{ok, issue_id, message, actions}``。

    不支持的 issue 返回 ``ok=False`` 与 ``message="该问题需要手动处理"``；
    修复过程中的异常一律转成 ``ok=False``（不冒泡）。
    """
    issue_key = str(issue_id or "")
    actions: list[str] = []
    result = {"ok": False, "issue_id": issue_key, "message": "", "actions": actions}
    handler = _HANDLERS.get(issue_key)
    if handler is None:
        result["message"] = "该问题需要手动处理"
        return result
    _emit(progress, f"开始修复：{issue_key}")
    try:
        message = handler(cfg, instance, actions, progress)
    except Exception as exc:  # noqa: BLE001 - 修复失败不能把界面带崩
        LOG.warning("修复 %s 失败：%r", issue_key, exc)
        result["message"] = f"修复失败：{exc}"
        return result
    _emit(progress, "修复完成")
    result["ok"] = True
    result["message"] = str(message or "修复完成")
    return result


def fix_all(cfg, instance=None, *, progress=None) -> list[dict]:
    """对当前所有 auto_fixable 的问题逐个修复，单个失败不影响其它。"""
    results: list[dict] = []
    try:
        issues = diagnose(cfg, instance)
    except Exception as exc:  # noqa: BLE001 - diagnose 理论上不抛，兜底
        LOG.warning("批量修复：诊断失败：%r", exc)
        return results
    for issue in issues:
        if not getattr(issue, "auto_fixable", False):
            continue
        try:
            results.append(apply_fix(cfg, issue.id, instance=instance, progress=progress))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("批量修复 %s 失败：%r", issue.id, exc)
            results.append({
                "ok": False,
                "issue_id": issue.id,
                "message": f"修复失败：{exc}",
                "actions": [],
            })
    return results


__all__ = ["Fix", "Issue", "diagnose", "apply_fix", "fix_all"]
