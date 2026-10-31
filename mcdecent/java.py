"""Java 运行时：探测本机、按版本需求自动获取。

按优先级：
  1. 配置里显式指定的 java；
  2. 本机已安装的 Java（PATH / JAVA_HOME / 常见安装目录）；
  3. 启动器自己接管过的运行时（<home>/java/*）；
  4. Mojang 官方运行时清单（与游戏版本 JSON 的 javaVersion.component 对应，最稳）；
  5. Adoptium Temurin JRE 兜底。

下载的运行时全部落在 <home>/java 下，可离线复用，不污染系统环境。
"""

from __future__ import annotations

import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import util
from .config import Config
from .net import DownloadTask, Downloader

JAVA_RUNTIME_MANIFEST_URL = (
    "https://launchermeta.mojang.com/v1/products/java-runtime/2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"
)
ADOPTIUM_ASSETS_API = "https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
JAVA_MANIFEST_CACHE_MAX_AGE = 86400


@dataclass
class JavaRuntime:
    path: Path
    home: Path
    major: int
    source: str = "system"
    version: str = ""

    def describe(self) -> dict:
        return {
            "path": str(self.path),
            "home": str(self.home),
            "major": self.major,
            "source": self.source,
            "version": self.version,
        }

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"Java {self.major} ({self.source}) {self.path}"


class JavaNotFound(RuntimeError):
    pass


# --------------------------------------------------------------------------- 平台标识


def platform_runtime_key() -> str:
    """Mojang 运行时清单里的平台键。"""
    if sys.platform.startswith("win"):
        arch = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower() or "amd64"
        if "arm" in arch:
            return "windows-arm64"
        if arch in ("x86",):
            return "windows-x86"
        return "windows-x64"
    if sys.platform == "darwin":
        return "mac-os-arm64" if os.uname().machine == "arm64" else "mac-os"
    machine = os.uname().machine
    if machine in ("aarch64", "arm64"):
        return "linux-aarch64" if "linux-aarch64" else "linux"
    if machine in ("i386", "i686"):
        return "linux-i386"
    return "linux"


def executable_name() -> str:
    return "java.exe" if sys.platform.startswith("win") else "java"


# --------------------------------------------------------------------------- 版本探测


_VERSION_RE = re.compile(r'version\s+"([^"]+)"')
_OPENJDK_RE = re.compile(r'openjdk\s+version\s+"([^"]+)"', re.IGNORECASE)


def parse_java_version(text: str) -> tuple[int | None, str]:
    """从 java -version 输出里解析主版本号。"""
    match = _VERSION_RE.search(text)
    if not match:
        return None, ""
    raw = match.group(1)
    if raw.startswith("1."):
        try:
            return int(raw.split(".")[1]), raw
        except (IndexError, ValueError):
            return None, raw
    try:
        return int(re.split(r"[.+-]", raw)[0]), raw
    except ValueError:
        return None, raw


def probe_java(path: str | os.PathLike) -> JavaRuntime | None:
    exe = Path(path)
    if not exe.is_file():
        return None
    try:
        proc = subprocess.run(
            [str(exe), "-version"],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (proc.stderr or "") + (proc.stdout or "")
    major, raw = parse_java_version(output)
    if not major:
        return None
    home = exe.parent.parent if (exe.parent / "bin").name == "bin" else exe.parent
    return JavaRuntime(path=exe, home=home, major=major, source="system", version=raw)


def _candidate_system_javas() -> list[Path]:
    out: list[Path] = []
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        out.append(Path(java_home) / "bin" / executable_name())
    which = shutil.which("java")
    if which:
        out.append(Path(which))
    roots = []
    if sys.platform.startswith("win"):
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            roots += [
                Path(base) / "Java",
                Path(base) / "Eclipse Adoptium",
                Path(base) / "Microsoft" / "jdk",
                Path(base) / "Zulu",
                Path(base) / "BellSoft",
                Path(base) / "Programs" / "Eclipse Adoptium",
            ]
    else:
        roots += [Path("/usr/lib/jvm"), Path("/Library/Java/JavaVirtualMachines"),
                  Path.home() / ".sdkman" / "candidates" / "java"]
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            for sub in (child / "bin", child / "Contents" / "Home" / "bin"):
                exe = sub / executable_name()
                if exe.is_file():
                    out.append(exe)
    return out


def list_installed_runtimes(cfg: Config) -> list[JavaRuntime]:
    """扫描 <home>/java 下由启动器管理的运行时。"""
    found: list[JavaRuntime] = []
    base = cfg.java_dir
    if not base.is_dir():
        return found
    for manifest in sorted(base.rglob("release")):
        home = manifest.parent
        exe = home / "bin" / executable_name()
        if not exe.is_file():
            continue
        runtime = probe_java(exe)
        if runtime:
            runtime.source = "managed"
            found.append(runtime)
    return found


def find_java(cfg: Config, *, major: int | None = None, allow_system: bool = True) -> JavaRuntime | None:
    """按配置/系统/托管运行时顺序找一个满足 major 的 Java。"""
    explicit = cfg.get("java.path")
    if explicit:
        runtime = probe_java(explicit)
        if runtime and (major is None or runtime.major >= major):
            try:
                # 区分"启动器托管"与自己装的：方便展示来源
                if runtime.path.resolve().is_relative_to(cfg.java_dir.resolve()):
                    runtime.source = "managed"
            except (OSError, ValueError):
                pass
            return runtime
    if allow_system:
        for exe in _candidate_system_javas():
            runtime = probe_java(exe)
            if runtime and (major is None or runtime.major >= major):
                return runtime
    for runtime in list_installed_runtimes(cfg):
        if major is None or runtime.major >= major:
            return runtime
    return None


# --------------------------------------------------------------------------- Mojang 运行时


def runtime_manifest(cfg: Config, downloader: Downloader) -> dict:
    cache = cfg.cache_dir / "java_runtime_manifest_all.json"
    return downloader.fetch_json(JAVA_RUNTIME_MANIFEST_URL, cache_path=cache, max_age=JAVA_MANIFEST_CACHE_MAX_AGE)


def _safe_relpath(raw: str) -> str | None:
    rel = raw.replace("\\", "/")
    if rel.startswith("/") or rel.startswith("../") or "/../" in rel or rel in ("", "."):
        return None
    return rel


def _runtime_executable(home: Path) -> Path | None:
    for rel in (f"bin/{executable_name()}", "bin/javaw.exe"):
        exe = home / rel
        if exe.is_file():
            return exe
    return None


def install_mojang_runtime(
    cfg: Config,
    component: str,
    *,
    major: int | None = None,
    downloader: Downloader | None = None,
    progress: util.Progress | None = None,
) -> JavaRuntime:
    """按 Mojang 官方运行时清单安装一个 JRE（自带 Java 25 等最新组件）。"""
    downloader = downloader or cfg.make_downloader(progress=progress)
    manifest = runtime_manifest(cfg, downloader)
    key = platform_runtime_key()
    platform_entries = manifest.get(key) or {}
    candidates = platform_entries.get(component) or []
    if not candidates:
        raise JavaNotFound(f"Mojang 运行时清单里没有 {component}（平台 {key}）")
    entry = candidates[0]
    version_name = (entry.get("version") or {}).get("name") or component
    home = cfg.java_dir / component / version_name
    existing = _runtime_executable(home)
    if existing is not None:
        runtime = probe_java(existing)
        if runtime:
            return runtime
    util.LOG.info("获取官方 Java 运行时 %s %s（%s）", component, version_name, key)
    file_manifest = downloader.fetch_json(entry["manifest"]["url"])
    files = file_manifest.get("files") or {}
    # 官方运行时同时提供 raw 与 lzma（压缩后约 1/3 体积），优先 lzma 以省带宽
    lzma_items: list[tuple[str, DownloadTask, dict]] = []
    raw_tasks: list[DownloadTask] = []
    for rel_raw, meta in files.items():
        rel = _safe_relpath(str(rel_raw))
        if rel is None:
            util.LOG.warning("跳过可疑运行时文件路径：%s", rel_raw)
            continue
        if (meta.get("type") or "file") != "file":
            util.ensure_dir(home / rel)
            continue
        downloads = meta.get("downloads") or {}
        raw = downloads.get("raw")
        compressed = downloads.get("lzma")
        if compressed and raw:
            lzma_items.append((rel, DownloadTask(
                url=compressed["url"], dest=home / f"{rel}.lzma",
                sha1=compressed.get("sha1"), size=compressed.get("size"), desc=f"jre/{rel}.lzma",
            ), raw))
        elif raw:
            raw_tasks.append(DownloadTask(
                url=raw["url"], dest=home / rel, sha1=raw.get("sha1"),
                size=raw.get("size"), desc=f"jre/{rel}",
            ))
    if lzma_items:
        downloader.download_all([t for _r, t, _x in lzma_items], desc=f"Java 运行时 {component}")
        fallback: list[DownloadTask] = []
        for rel, task, raw in lzma_items:
            src = task.dest
            try:
                payload = lzma.decompress(src.read_bytes(), format=lzma.FORMAT_AUTO)
                if raw.get("size") and len(payload) != raw["size"]:
                    raise ValueError(f"长度不符：{len(payload)} != {raw['size']}")
                if raw.get("sha1") and util.sha1_bytes(payload) != raw["sha1"]:
                    raise ValueError("sha1 不符")
                util.atomic_write_bytes(home / rel, payload)
            except Exception as exc:  # noqa: BLE001
                util.LOG.warning("解压失败，改用原始文件：%s（%s）", rel, exc)
                fallback.append(DownloadTask(
                    url=raw["url"], dest=home / rel, sha1=raw.get("sha1"),
                    size=raw.get("size"), desc=f"jre/{rel}",
                ))
            finally:
                try:
                    src.unlink()
                except OSError:
                    pass
        raw_tasks.extend(fallback)
    if raw_tasks:
        downloader.download_all(raw_tasks, desc=f"Java 运行时 {component}（原始文件）")
    exe = _runtime_executable(home)
    if exe is None:
        raise JavaNotFound(f"运行时安装完成但找不到 java 可执行文件：{home}")
    runtime = probe_java(exe)
    if runtime is None:
        raise JavaNotFound(f"运行时无法执行：{exe}")
    runtime.source = f"mojang:{component}"
    if major is not None and runtime.major < major:
        raise JavaNotFound(f"官方运行时 {component} 版本 {runtime.major} 低于要求 {major}")
    cfg.set("java.path", str(exe))
    cfg.save()
    util.LOG.info("Java 运行时就绪：%s", runtime.version)
    return runtime


# --------------------------------------------------------------------------- Adoptium 兜底


def install_adoptium_runtime(
    cfg: Config,
    major: int,
    *,
    downloader: Downloader | None = None,
    progress: util.Progress | None = None,
) -> JavaRuntime:
    downloader = downloader or cfg.make_downloader(progress=progress)
    os_name = {"windows": "windows", "linux": "linux", "osx": "mac"}.get(
        {"Windows": "windows", "Linux": "linux", "Darwin": "osx"}.get(__import__("platform").system(), "linux")
    )
    arch = "x64" if sys.maxsize > 2**32 else "x86"
    if sys.platform.startswith("win"):
        os_name = "windows"
    url = ADOPTIUM_ASSETS_API.format(major=major) + f"?architecture={arch}&image_type=jre&os={os_name}&vendor=eclipse"
    util.LOG.info("从 Adoptium 获取 Java %s", major)
    data = json.loads(downloader.fetch_bytes(url).decode("utf-8"))
    if not data:
        raise JavaNotFound(f"Adoptium 没有 Java {major} 的 JRE")
    binary = data[0]["binary"]
    package = binary.get("package") or {}
    link = package.get("link")
    if not link:
        raise JavaNotFound("Adoptium 返回数据缺少下载地址")
    filename = package.get("name") or f"temurin-{major}.zip"
    archive = cfg.cache_dir / filename
    task = DownloadTask(url=link, dest=archive, sha256=package.get("checksum"), size=package.get("size"), desc=filename)
    downloader.download_one(task)
    home = cfg.java_dir / f"adoptium-{major}" / filename.replace(".zip", "")
    if not (home / "bin" / executable_name()).is_file():
        util.ensure_dir(home)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(home)
    exe = _runtime_executable(home)
    if exe is None:
        # 压缩包可能带一层顶层目录
        for sub in home.iterdir():
            if sub.is_dir() and (sub / "bin" / executable_name()).is_file():
                exe = sub / "bin" / executable_name()
                home = sub
                break
    if exe is None:
        raise JavaNotFound(f"Adoptium 运行时解压后找不到 java：{archive}")
    runtime = probe_java(exe)
    if runtime is None:
        raise JavaNotFound(f"Adoptium 运行时无法执行：{exe}")
    runtime.source = "adoptium"
    cfg.set("java.path", str(exe))
    cfg.save()
    return runtime


# --------------------------------------------------------------------------- 统一入口


def ensure_java(
    cfg: Config,
    *,
    major: int | None = None,
    component: str | None = None,
    downloader: Downloader | None = None,
    progress: util.Progress | None = None,
    auto_install: bool = True,
) -> JavaRuntime:
    runtime = find_java(cfg, major=major)
    if runtime:
        util.LOG.info("使用本机 Java %s（%s）", runtime.major, runtime.path)
        return runtime
    if not auto_install:
        raise JavaNotFound(f"未找到满足要求的 Java {major}，已禁用自动安装")
    errors: list[str] = []
    if component:
        try:
            return install_mojang_runtime(cfg, component, major=major, downloader=downloader, progress=progress)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Mojang 运行时：{exc!r}")
            util.LOG.warning("官方运行时安装失败，尝试 Adoptium：%r", exc)
    if major:
        try:
            return install_adoptium_runtime(cfg, major, downloader=downloader, progress=progress)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Adoptium：{exc!r}")
    raise JavaNotFound(
        "找不到可用的 Java 运行时。"
        + (f"需要 Java {major}。" if major else "")
        + " 请安装 JRE 或允许联网自动下载。详情：" + " | ".join(errors)
    )
