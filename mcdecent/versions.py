"""版本元数据：清单解析、版本 JSON 继承链合并、规则求值、库路径推导、资源索引。

这里只处理"是什么 / 从哪来"，实际下载在 installer.py，启动参数拼装在 launch.py。
"""

from __future__ import annotations

import json
import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import util
from .config import Config
from .net import Downloader, DownloadTask

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RESOURCES_URL = "https://resources.download.minecraft.net"
LIBRARIES_URL = "https://libraries.minecraft.net"
MANIFEST_CACHE_MAX_AGE = 600  # 秒

_OS_MAP = {"Windows": "windows", "Linux": "linux", "Darwin": "osx"}
_ARCH_MAP = {
    "amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64",
    "arm64": "arm64", "aarch64": "arm64",
    "x86": "x86", "i386": "x86", "i686": "x86",
}


def system_os() -> str:
    return _OS_MAP.get(platform.system(), "linux")


def system_arch() -> str:
    return _ARCH_MAP.get(platform.machine().lower(), platform.machine().lower())


class VersionNotFound(RuntimeError):
    pass


class VersionNotInstalled(RuntimeError):
    pass


# --------------------------------------------------------------------------- 规则求值


def rule_matches(rule: dict, features: dict | None = None) -> bool:
    """判断单条规则的条件是否命中（不含 action）。"""
    features = features or {}
    os_rule = rule.get("os") or {}
    if os_rule:
        name = os_rule.get("name")
        if name and name != system_os():
            return False
        arch = os_rule.get("arch")
        if arch:
            arch = str(arch).lower()
            if arch == "x86" and system_arch() not in ("x86",):
                return False
            if arch != "x86" and arch != system_arch():
                # Mojang 的 arch 只在少数规则里出现，宽松匹配 x86_64
                if not (arch in ("x86_64", "amd64") and system_arch() == "x86_64"):
                    return False
        version = os_rule.get("version")
        if version:
            ver = platform.version() or platform.release() or ""
            try:
                if not re.search(version, ver):
                    return False
            except re.error:
                return False
    feature_rule = rule.get("features") or {}
    for key, expected in feature_rule.items():
        if bool(features.get(key, False)) != bool(expected):
            return False
    return True


def rule_allows(rules: list | None, features: dict | None = None) -> bool:
    """按官方语义求值：无 rules 视为允许；有 rules 时默认拒绝，逐条命中覆盖。"""
    if not rules:
        return True
    allowed = False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule_matches(rule, features):
            allowed = rule.get("action", "allow") == "allow"
    return allowed


# --------------------------------------------------------------------------- Maven 路径


def maven_path(name: str) -> str:
    """group:artifact:version[:classifier] -> 相对路径。"""
    parts = name.split(":")
    if len(parts) < 3:
        raise ValueError(f"非法 Maven 坐标：{name!r}")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else None
    filename = f"{artifact}-{version}" + (f"-{classifier}" if classifier else "") + ".jar"
    return "/".join(group.split(".") + [artifact, version, filename])


def maven_url(base: str, name: str) -> str:
    return base.rstrip("/") + "/" + maven_path(name)


# --------------------------------------------------------------------------- 版本 JSON 读写


def version_dir(cfg: Config, version_id: str) -> Path:
    return cfg.versions_dir / version_id


def version_json_path(cfg: Config, version_id: str) -> Path:
    return version_dir(cfg, version_id) / f"{version_id}.json"


def read_local_version(cfg: Config, version_id: str) -> dict | None:
    data = util.read_json(version_json_path(cfg, version_id))
    return data if isinstance(data, dict) else None


def get_manifest(cfg: Config, downloader: Downloader | None = None, *, refresh: bool = False) -> dict:
    """获取 Mojang 版本清单（带本地缓存与离线降级）。"""
    cache = cfg.cache_dir / "version_manifest_v2.json"
    if refresh:
        try:
            cache.unlink()
        except OSError:
            pass
    if downloader is None:
        downloader = cfg.make_downloader(quiet=True)
    try:
        return downloader.fetch_json(MANIFEST_URL, cache_path=cache, max_age=MANIFEST_CACHE_MAX_AGE)
    except Exception:
        cached = util.read_json(cache)
        if isinstance(cached, dict):
            util.LOG.warning("无法刷新版本清单，使用本地缓存")
            return cached
        raise


def manifest_entry(cfg: Config, version_id: str, downloader: Downloader | None = None) -> dict:
    manifest = get_manifest(cfg, downloader)
    for entry in manifest.get("versions", []):
        if entry.get("id") == version_id:
            return entry
    aliases = {"latest": manifest.get("latest", {}).get("release"), "latest-release": manifest.get("latest", {}).get("release"),
               "latest-snapshot": manifest.get("latest", {}).get("snapshot")}
    alias = aliases.get(version_id)
    if alias:
        return manifest_entry(cfg, alias, downloader)
    raise VersionNotFound(f"版本清单中不存在：{version_id}")


def fetch_version_json(cfg: Config, version_id: str, downloader: Downloader) -> dict:
    """确保本地有该版本 JSON，返回内容（已安装则直接读本地）。"""
    local = read_local_version(cfg, version_id)
    if local:
        return local
    entry = manifest_entry(cfg, version_id, downloader)
    data = downloader.fetch_json(entry["url"])
    util.ensure_dir(version_dir(cfg, version_id))
    util.write_json(version_json_path(cfg, version_id), data)
    return data


# --------------------------------------------------------------------------- 继承链合并


def _library_key(lib: dict) -> str:
    """去重键：group:artifact[:classifier]。

    必须带上 classifier：现代版本把原生库拆成独立条目
    （如 ``org.lwjgl:lwjgl:3.4.3`` 与 ``org.lwjgl:lwjgl:3.4.3:natives-windows``），
    只按 group:artifact 去重会把原生库误删，导致启动时 LWJGL 找不到本地库。
    """
    name = lib.get("name") or ""
    parts = name.split(":")
    if len(parts) >= 4 and parts[3]:
        return ":".join(parts[:2]) + ":" + parts[3]
    return ":".join(parts[:2])


def _merge_chain(chain: list[dict]) -> dict:
    """chain[0] 是最具体（子版本），依次向上。合并成一份等效版本描述。"""
    merged: dict = {}
    for data in chain:
        for key, value in data.items():
            if key in ("libraries", "arguments", "id", "inheritsFrom", "downloads", "assetIndex", "javaVersion"):
                continue
            if key not in merged:
                merged[key] = value
    # 标量/字典类字段：子优先
    for key in ("downloads", "assetIndex", "javaVersion", "logging", "assets", "mainClass", "type",
                "releaseTime", "time", "complianceLevel", "minimumLauncherVersion", "javaVersion"):
        for data in chain:
            if data.get(key) is not None:
                merged[key] = data[key]
                break
    # 库：子在前，按 group:artifact 去重（子版本优先）
    libraries: list[dict] = []
    seen: set[str] = set()
    for data in chain:
        for lib in data.get("libraries") or []:
            key = _library_key(lib)
            if key in seen:
                continue
            seen.add(key)
            libraries.append(lib)
    merged["libraries"] = libraries
    # 参数：父在前、子在后（子可追加自己的 JVM 参数，如 Fabric 的 -DFabricMcEmu）
    args = {"game": [], "jvm": []}
    for data in reversed(chain):
        data_args = data.get("arguments") or {}
        args["game"].extend(data_args.get("game") or [])
        args["jvm"].extend(data_args.get("jvm") or [])
    merged["arguments"] = args
    # 老的 minecraftArguments 格式
    for data in chain:
        if data.get("minecraftArguments"):
            merged["minecraftArguments"] = data["minecraftArguments"]
            break
    merged["id"] = chain[0].get("id") or merged.get("id")
    merged["inheritsFrom"] = chain[-1].get("inheritsFrom") if len(chain) > 1 else chain[0].get("inheritsFrom")
    return merged


@dataclass
class ResolvedLibrary:
    name: str
    rel_path: str
    url: str
    sha1: str | None
    size: int | None
    native: bool = False
    native_classifier: str | None = None
    extract_exclude: list[str] = field(default_factory=list)

    @property
    def dest(self) -> Path:  # 由 installer/launch 注入 libraries_dir 后使用
        raise RuntimeError("请使用绝对路径字段")


@dataclass
class ResolvedVersion:
    id: str
    type: str
    json: dict
    client_jar: Path | None
    libraries: list[dict]
    assets_index_id: str | None
    main_class: str
    java_major: int | None
    java_component: str | None
    game_args: list
    jvm_args: list
    logging_client: dict | None
    loader: dict | None = None
    base_id: str | None = None
    native_dir: Path | None = None
    classpath_separator: str = os.pathsep

    @property
    def java_requirement(self) -> str:
        if self.java_major:
            return f"Java {self.java_major}+"
        return "Java 未知"


def _resolve_chain(cfg: Config, version_id: str, *, fetch: bool = False, downloader: Downloader | None = None) -> list[dict]:
    """向上解析 inheritsFrom 链，返回 [子, ..., 根]。"""
    chain: list[dict] = []
    current = version_id
    seen: set[str] = set()
    while current:
        if current in seen:
            raise VersionNotFound(f"版本继承链存在循环：{current}")
        seen.add(current)
        data = read_local_version(cfg, current)
        if data is None:
            if not fetch:
                raise VersionNotInstalled(
                    f"版本 {current} 未安装（缺少 {version_json_path(cfg, current)}）。"
                    f"请先运行：mcdecent install {current}"
                )
            if downloader is None:
                downloader = cfg.make_downloader(quiet=True)
            data = fetch_version_json(cfg, current, downloader)
        chain.append(data)
        parent = data.get("inheritsFrom")
        current = parent if parent else None
    return chain


def _classpath_libraries(cfg: Config, merged: dict) -> list[dict]:
    """把库条目解析为 {name, rel_path, url, sha1, size, native, ...}，并过滤规则不匹配的。"""
    out: list[dict] = []
    for lib in merged.get("libraries") or []:
        if not rule_allows(lib.get("rules")):
            continue
        name = lib.get("name") or ""
        downloads = lib.get("downloads") or {}
        natives = lib.get("natives") or {}
        osname = system_os()
        native_classifier = None
        if natives:
            if osname not in natives:
                continue
            native_classifier = natives[osname]
            if isinstance(native_classifier, dict):
                # 老格式 {"windows": {"name": "natives-windows"}}
                native_classifier = native_classifier.get("name") or native_classifier.get("classifier")
            native_classifier = str(native_classifier).replace("${arch}", "64" if system_arch() == "x86_64" else "32")
        extract = lib.get("extract") or {}
        exclude = extract.get("exclude") or []
        if native_classifier:
            classifier_key = native_classifier
            artifact = (downloads.get("classifiers") or {}).get(classifier_key)
            entry_name = f"{name}:{classifier_key}" if not name.endswith(f":{classifier_key}") else name
            rel = maven_path(entry_name)
            url = (artifact or {}).get("url") or maven_url(lib.get("url") or LIBRARIES_URL, entry_name)
            out.append({
                "name": entry_name, "rel_path": rel, "url": url,
                "sha1": (artifact or {}).get("sha1"), "size": (artifact or {}).get("size"),
                "native": True, "native_classifier": classifier_key, "extract_exclude": exclude,
            })
            continue
        artifact = downloads.get("artifact")
        if artifact:
            rel = artifact.get("path") or maven_path(name)
            url = artifact.get("url") or maven_url(lib.get("url") or LIBRARIES_URL, name)
            out.append({
                "name": name, "rel_path": rel, "url": url,
                "sha1": artifact.get("sha1"), "size": artifact.get("size"),
                "native": False, "native_classifier": None, "extract_exclude": [],
            })
        else:
            # 无 downloads 字段：按 Maven 坐标拼 URL（Fabric 库常见）
            rel = maven_path(name)
            url = maven_url(lib.get("url") or "https://maven.fabricmc.net/", name)
            out.append({
                "name": name, "rel_path": rel, "url": url,
                "sha1": lib.get("sha1"), "size": lib.get("size"),
                "native": False, "native_classifier": None, "extract_exclude": [],
            })
    return out


def read_loader_meta(cfg: Config, version_id: str) -> dict | None:
    data = util.read_json(version_dir(cfg, version_id) / "mcdecent.meta.json")
    return data.get("loader") if isinstance(data, dict) else None


def resolve(cfg: Config, version_id: str, *, downloader: Downloader | None = None, fetch: bool = False) -> ResolvedVersion:
    """解析出可启动的完整版本描述（本地优先；fetch=True 时会补下载缺失的版本 JSON）。"""
    chain = _resolve_chain(cfg, version_id, fetch=fetch, downloader=downloader)
    merged = _merge_chain(chain)
    resolved = ResolvedVersion(
        id=version_id,
        type=merged.get("type", "release"),
        json=merged,
        client_jar=None,
        libraries=_classpath_libraries(cfg, merged),
        assets_index_id=(merged.get("assetIndex") or {}).get("id") or merged.get("assets"),
        main_class=merged.get("mainClass", "net.minecraft.client.main.Main"),
        java_major=(merged.get("javaVersion") or {}).get("majorVersion"),
        java_component=(merged.get("javaVersion") or {}).get("component"),
        game_args=merged.get("arguments", {}).get("game") or [],
        jvm_args=merged.get("arguments", {}).get("jvm") or [],
        logging_client=(merged.get("logging") or {}).get("client"),
        loader=read_loader_meta(cfg, version_id),
        base_id=None,
        native_dir=cfg.root / "natives" / version_id,
        classpath_separator=os.pathsep,
    )
    # 基础版本的客户端 jar 位置
    downloads = merged.get("downloads") or {}
    client = downloads.get("client")
    if client:
        base = chain[-1].get("id") if len(chain) > 1 else version_id
        resolved.base_id = base
        resolved.client_jar = cfg.versions_dir / base / f"{base}.jar"
    else:
        resolved.base_id = version_id
    # 旧的 minecraftArguments 转成 game_args 占位符列表
    if not resolved.game_args and merged.get("minecraftArguments"):
        resolved.game_args = str(merged["minecraftArguments"]).split()
    return resolved


def classpath_entries(cfg: Config, resolved: ResolvedVersion) -> list[str]:
    entries = []
    for lib in resolved.libraries:
        if lib.get("native"):
            continue
        entries.append(str(cfg.libraries_dir / lib["rel_path"]))
    if resolved.client_jar:
        entries.append(str(resolved.client_jar))
    return entries


def installed_versions(cfg: Config) -> list[dict]:
    """扫描 versions/ 目录，返回已安装版本列表。"""
    out = []
    if not cfg.versions_dir.is_dir():
        return out
    for folder in sorted(cfg.versions_dir.iterdir()):
        if not folder.is_dir():
            continue
        jf = folder / f"{folder.name}.json"
        if not jf.is_file():
            continue
        data = util.read_json(jf, {}) or {}
        loader = read_loader_meta(cfg, folder.name)
        base = data.get("inheritsFrom")
        java_major = (data.get("javaVersion") or {}).get("majorVersion")
        if java_major is None and base:
            parent = read_local_version(cfg, base) or {}
            java_major = (parent.get("javaVersion") or {}).get("majorVersion")
        client_ok = True
        if not base:
            client_ok = (folder / f"{folder.name}.jar").is_file() or (data.get("downloads") or {}).get("client") is None
        out.append({
            "id": folder.name,
            "type": data.get("type", "release"),
            "base": base,
            "loader": loader,
            "java_major": java_major,
            "client_ready": client_ok,
            "path": str(folder),
        })
    return out


def latest_release(cfg: Config, downloader: Downloader | None = None) -> str:
    manifest = get_manifest(cfg, downloader)
    return manifest.get("latest", {}).get("release")


def available_versions(cfg: Config, downloader: Downloader | None = None, *, limit: int = 0) -> list[dict]:
    manifest = get_manifest(cfg, downloader)
    versions = manifest.get("versions", [])
    if limit:
        versions = versions[:limit]
    latest = manifest.get("latest", {})
    out = []
    for v in versions:
        out.append({
            "id": v.get("id"), "type": v.get("type"),
            "release_time": v.get("releaseTime"),
            "latest": v.get("id") == latest.get("release"),
            "snapshot": v.get("id") == latest.get("snapshot"),
        })
    return out
