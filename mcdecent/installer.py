"""安装流水线：把一个版本从"只有元数据"变成"可以启动"。

覆盖：客户端 jar、库（含原生库解压）、资源索引与资源对象、日志配置、Java 运行时。
所有下载都走 Downloader（多镜像 + 校验 + 并发 + 连接复用）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import java as java_module
from . import util, versions
from .config import Config
from .net import DownloadTask, Downloader

RESOURCES_URL = versions.RESOURCES_URL


def _jre_progress_note() -> str:  # pragma: no cover - 仅用于日志
    return ""


def _library_tasks(cfg: Config, resolved: versions.ResolvedVersion) -> list[DownloadTask]:
    tasks: list[DownloadTask] = []
    for lib in resolved.libraries:
        if lib.get("native"):
            continue
        tasks.append(DownloadTask(
            url=lib["url"],
            dest=cfg.libraries_dir / lib["rel_path"],
            sha1=lib.get("sha1"),
            size=lib.get("size"),
            desc=os.path.basename(lib["rel_path"]),
        ))
    return tasks


def _native_tasks(cfg: Config, resolved: versions.ResolvedVersion) -> list[DownloadTask]:
    tasks: list[DownloadTask] = []
    for lib in resolved.libraries:
        if not lib.get("native"):
            continue
        key = (lib.get("sha1") or lib["rel_path"]).replace("/", "_")
        tasks.append(DownloadTask(
            url=lib["url"],
            dest=cfg.cache_dir / "natives" / f"{key}.jar",
            sha1=lib.get("sha1"),
            size=lib.get("size"),
            desc=f"native:{os.path.basename(lib['rel_path'])}",
        ))
    return tasks


def extract_natives(cfg: Config, resolved: versions.ResolvedVersion, downloader: Downloader) -> int:
    """下载原生库 jar 并解压到 <root>/natives/<version>。"""
    tasks = _native_tasks(cfg, resolved)
    if not tasks:
        util.ensure_dir(resolved.native_dir)
        return 0
    downloader.download_all(tasks, desc="原生库")
    count = 0
    util.ensure_dir(resolved.native_dir)
    for lib in resolved.libraries:
        if not lib.get("native"):
            continue
        key = (lib.get("sha1") or lib["rel_path"]).replace("/", "_")
        jar = cfg.cache_dir / "natives" / f"{key}.jar"
        exclude = lib.get("extract_exclude") or ["META-INF/"]
        try:
            written = util.safe_extract_zip(jar, resolved.native_dir, skip_meta=True)
            count += len(written)
        except Exception as exc:  # noqa: BLE001
            util.LOG.warning("原生库解压失败 %s：%r", jar, exc)
    return count


def ensure_client(cfg: Config, resolved: versions.ResolvedVersion, downloader: Downloader) -> Path | None:
    client = (resolved.json.get("downloads") or {}).get("client")
    if not client or not resolved.client_jar:
        return None
    task = DownloadTask(
        url=client["url"],
        dest=resolved.client_jar,
        sha1=client.get("sha1"),
        size=client.get("size"),
        desc=f"{resolved.base_id or resolved.id}.jar（客户端）",
    )
    downloader.download_one(task)
    return resolved.client_jar


def ensure_libraries(cfg: Config, resolved: versions.ResolvedVersion, downloader: Downloader) -> int:
    tasks = _library_tasks(cfg, resolved)
    downloader.download_all(tasks, desc="库文件")
    return len(tasks)


def _assets_index_task(cfg: Config, resolved: versions.ResolvedVersion) -> DownloadTask | None:
    index = resolved.json.get("assetIndex") or {}
    if not index.get("url"):
        return None
    index_id = index.get("id") or resolved.assets_index_id
    if not index_id:
        return None
    return DownloadTask(
        url=index["url"],
        dest=cfg.assets_dir / "indexes" / f"{index_id}.json",
        sha1=index.get("sha1"),
        size=index.get("size"),
        desc=f"assets/{index_id}.json",
    )


def ensure_assets(cfg: Config, resolved: versions.ResolvedVersion, downloader: Downloader) -> dict:
    """下载资源索引与全部资源对象；返回统计信息。"""
    index_task = _assets_index_task(cfg, resolved)
    if index_task is None:
        util.LOG.info("该版本没有资源索引，跳过资源下载")
        return {"index": None, "objects": 0, "bytes": 0, "skipped": True}
    downloader.download_one(index_task)
    index_data = util.read_json(index_task.dest, {}) or {}
    objects = index_data.get("objects") or {}
    # 同一 hash 可能出现多次，按目标路径去重
    tasks: list[DownloadTask] = []
    seen: set[str] = set()
    total = 0
    for _name, entry in objects.items():
        digest = (entry or {}).get("hash")
        if not digest or digest in seen:
            continue
        seen.add(digest)
        size = (entry or {}).get("size") or 0
        total += size
        tasks.append(DownloadTask(
            url=f"{RESOURCES_URL}/{digest[:2]}/{digest}",
            dest=cfg.assets_dir / "objects" / digest[:2] / digest,
            sha1=digest,
            size=size,
            desc=f"asset:{digest[:8]}",
        ))
    util.LOG.info("资源对象：%d 个（约 %s）", len(tasks), util.human_size(total))
    downloader.download_all(tasks, desc="资源文件")
    return {"index": index_task.dest.name, "objects": len(tasks), "bytes": total, "skipped": False}


def ensure_logging_config(cfg: Config, resolved: versions.ResolvedVersion, downloader: Downloader) -> Path | None:
    client = resolved.logging_client
    if not client:
        return None
    file_info = client.get("file") or {}
    if not file_info.get("url"):
        return None
    name = file_info.get("id") or "client-log4j.xml"
    dest = cfg.assets_dir / "log_configs" / name
    downloader.download_one(DownloadTask(
        url=file_info["url"], dest=dest, sha1=file_info.get("sha1"),
        size=file_info.get("size"), desc=f"日志配置 {name}",
    ))
    return dest


def install_version(
    cfg: Config,
    version_id: str,
    *,
    downloader: Downloader | None = None,
    progress: util.Progress | None = None,
    skip_assets: bool = False,
    skip_java: bool = False,
    auto_java: bool = True,
    refresh: bool = False,
) -> dict:
    """安装（或修复）一个版本，返回摘要。"""
    cfg.ensure_layout()
    downloader = downloader or cfg.make_downloader(progress=progress, quiet=progress is None)
    progress = progress or util.Progress()
    resolved = versions.resolve(cfg, version_id, downloader=downloader, fetch=True)
    util.LOG.info("解析版本 %s（基础版本 %s，类型 %s，%s）",
                  resolved.id, resolved.base_id or resolved.id, resolved.type, resolved.java_requirement)

    summary: dict = {
        "version": resolved.id,
        "base": resolved.base_id or resolved.id,
        "loader": resolved.loader,
        "java": None,
        "client": None,
        "libraries": 0,
        "natives": 0,
        "assets": None,
        "home": str(cfg.root),
    }

    summary["client"] = str(ensure_client(cfg, resolved, downloader)) if resolved.client_jar else None
    summary["libraries"] = ensure_libraries(cfg, resolved, downloader)
    summary["natives"] = extract_natives(cfg, resolved, downloader)
    ensure_logging_config(cfg, resolved, downloader)
    if skip_assets:
        util.LOG.info("按参数跳过资源下载")
        summary["assets"] = {"skipped": True}
    else:
        summary["assets"] = ensure_assets(cfg, resolved, downloader)

    if not skip_java:
        runtime = java_module.ensure_java(
            cfg, major=resolved.java_major, component=resolved.java_component,
            downloader=downloader, progress=progress, auto_install=auto_java,
        )
        summary["java"] = runtime.describe()
        summary["java_requirement"] = f"{resolved.java_major}+" if resolved.java_major else "any"

    # 记录安装时间与加载器信息，便于 list/launch 使用
    meta_file = versions.version_dir(cfg, version_id) / "mcdecent.meta.json"
    meta = util.read_json(meta_file, {}) or {}
    meta.setdefault("loader", resolved.loader)
    meta["installed_at"] = util.now_iso()
    meta["base"] = resolved.base_id or version_id
    util.write_json(meta_file, meta)
    util.ensure_dir(cfg.game_dir)
    if resolved.loader:
        util.ensure_dir(cfg.mods_dir)
    util.LOG.info("版本 %s 安装完成", resolved.id)
    return summary


def installation_report(cfg: Config, version_id: str) -> dict:
    """检查一个版本是否完整（用于 doctor / list --verify）。"""
    try:
        resolved = versions.resolve(cfg, version_id)
    except Exception as exc:  # noqa: BLE001
        return {"version": version_id, "ok": False, "error": str(exc)}
    missing: list[str] = []
    if resolved.client_jar and not resolved.client_jar.is_file():
        missing.append(str(resolved.client_jar))
    for lib in resolved.libraries:
        if lib.get("native"):
            continue
        dest = cfg.libraries_dir / lib["rel_path"]
        if not dest.is_file():
            missing.append(str(dest))
    index_id = resolved.assets_index_id
    if index_id and not (cfg.assets_dir / "indexes" / f"{index_id}.json").is_file():
        missing.append(f"assets/indexes/{index_id}.json")
    natives_ok = resolved.native_dir.is_dir() and any(resolved.native_dir.iterdir()) if resolved.native_dir else False
    return {
        "version": version_id,
        "ok": not missing,
        "missing": missing[:10],
        "missing_count": len(missing),
        "natives": natives_ok,
        "java_requirement": resolved.java_requirement,
        "loader": resolved.loader,
    }
