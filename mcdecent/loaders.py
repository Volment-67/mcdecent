"""Fabric / Quilt 加载器安装。

加载器本质上只是一份"继承"基础版本的 profile JSON：

    versions/<loader_id>/<loader_id>.json   # inheritsFrom 指向基础版本，补充 mainClass 与 libraries

Mojang 官方启动器与 mcdecent 都通过 ``inheritsFrom`` 把它和原版合并。本模块只负责
取回 meta 元数据、把 profile 落盘，并把加载器自身依赖的库交给 ``installer`` 下载。

约定：所有对 ``installer`` / ``versions`` 的导入都延迟到函数内部，这样核心模块尚未
就绪时本模块依然可以 import 并跑单元测试。
"""

from __future__ import annotations

from pathlib import Path

from . import util

LOG = util.LOG

FABRIC_META = "https://meta.fabricmc.net/v2"
QUILT_META = "https://meta.quiltmc.org/v3"
LOADER_META = {"fabric": FABRIC_META, "quilt": QUILT_META}
LOADER_TYPES = tuple(LOADER_META)

# 元数据本地缓存的有效期（秒）
VERSIONS_MAX_AGE = 3600
PROFILE_MAX_AGE = 24 * 3600


def _norm_type(loader_type: str | None) -> str:
    lt = (loader_type or "fabric").strip().lower()
    if lt not in LOADER_META:
        raise ValueError(f"不支持的加载器类型：{loader_type!r}（可选：{', '.join(LOADER_TYPES)}）")
    return lt


def _downloader(cfg, downloader=None):
    """允许注入 Downloader（测试/复用连接），否则按需创建。"""
    return downloader if downloader is not None else cfg.make_downloader(quiet=True)


def _normalize_entries(data) -> list[dict]:
    """把 Fabric(v2) 与 Quilt(v3) 两种 meta 结构归一化。

    每个元素形如 ``{"loader": {"version": ..., "stable": ...}, ...}``，
    同时提供扁平的 ``version`` / ``stable`` 方便调用方使用。
    """
    if isinstance(data, dict):
        data = data.get("values") or data.get("versions") or []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        loader = item.get("loader")
        if not isinstance(loader, dict):
            # Quilt v3 的条目把 loader 字段直接摊平在顶层
            loader = item
        version = loader.get("version")
        if not version:
            continue
        stable = bool(loader.get("stable", False))
        out.append(
            {
                "loader": {
                    "version": str(version),
                    "stable": stable,
                    "maven": loader.get("maven"),
                },
                "intermediary": item.get("intermediary") or {},
                "launcherMeta": item.get("launcherMeta") or {},
                "raw": item,
                "version": str(version),
                "stable": stable,
            }
        )
    return out


def fetch_loader_versions(cfg, game_version: str, *, loader_type: str = "fabric", downloader=None) -> list[dict]:
    """取某个游戏版本可用的加载器版本列表（带 1 小时本地缓存）。"""
    lt = _norm_type(loader_type)
    url = f"{LOADER_META[lt]}/versions/loader/{game_version}"
    cache = Path(cfg.cache_dir) / f"{lt}-loaders-{game_version}.json"
    dl = _downloader(cfg, downloader)
    try:
        data = dl.fetch_json(url, cache_path=cache, max_age=VERSIONS_MAX_AGE, desc=f"{lt} 加载器版本列表")
    except Exception as exc:  # noqa: BLE001 - 网络错误种类繁多，统一给出可读提示
        raise RuntimeError(
            f"无法获取 {lt} 加载器版本列表（{url}）：{exc}。"
            "请检查网络或稍后重试；也可手动指定 --loader-version 跳过该请求。"
        ) from exc
    entries = _normalize_entries(data)
    if not entries:
        raise RuntimeError(f"{lt} 元数据里没有适用于 {game_version} 的加载器版本（{url}）")
    LOG.debug("%s 可用加载器版本 %d 个（游戏 %s）", lt, len(entries), game_version)
    return entries


def fetch_fabric_loader_versions(cfg, game_version: str, *, downloader=None) -> list[dict]:
    """Fabric 加载器版本列表（meta.fabricmc.net/v2）。"""
    return fetch_loader_versions(cfg, game_version, loader_type="fabric", downloader=downloader)


def fetch_quilt_loader_versions(cfg, game_version: str, *, downloader=None) -> list[dict]:
    """Quilt 加载器版本列表（meta.quiltmc.org/v3）。"""
    return fetch_loader_versions(cfg, game_version, loader_type="quilt", downloader=downloader)


def pick_loader_version(entries: list[dict], loader_version: str | None = None) -> str:
    """从版本列表里挑一个：显式指定优先；否则最新的 stable，再否则第一个。"""
    if loader_version:
        wanted = str(loader_version).strip()
        for entry in entries:
            if entry["loader"]["version"] == wanted:
                return wanted
        raise ValueError(f"指定的加载器版本 {wanted} 不存在；可先运行 loader versions 查看可用版本")
    for entry in entries:
        if entry["loader"]["stable"]:
            return entry["loader"]["version"]
    return entries[0]["loader"]["version"]


def fetch_loader_profile(
    cfg,
    base_version: str,
    loader_version: str,
    *,
    loader_type: str = "fabric",
    downloader=None,
) -> dict:
    """取加载器的 profile JSON（含 mainClass/libraries/inheritsFrom）。"""
    lt = _norm_type(loader_type)
    url = f"{LOADER_META[lt]}/versions/loader/{base_version}/{loader_version}/profile/json"
    cache = Path(cfg.cache_dir) / f"{lt}-profile-{base_version}-{loader_version}.json"
    dl = _downloader(cfg, downloader)
    try:
        data = dl.fetch_json(url, cache_path=cache, max_age=PROFILE_MAX_AGE, desc=f"{lt} {loader_version} profile")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"无法获取 {lt} {loader_version} 的 profile（{url}）：{exc}。"
            "请确认游戏版本与加载器版本组合是否存在。"
        ) from exc
    if not isinstance(data, dict) or not data:
        raise RuntimeError(f"{lt} profile 返回内容不合法（{url}）")
    return data


def install_loader(
    cfg,
    base_version: str,
    loader_type: str = "fabric",
    loader_version: str | None = None,
    *,
    downloader=None,
) -> str:
    """把加载器 profile 安装为一个"继承"基础版本的版本目录，返回 loader_id。

    只写元数据，不下载库；库由 ``installer.install_version(cfg, loader_id)`` 完成。
    """
    lt = _norm_type(loader_type)
    dl = _downloader(cfg, downloader)
    if not loader_version:
        entries = fetch_loader_versions(cfg, base_version, loader_type=lt, downloader=dl)
        loader_version = pick_loader_version(entries)
    profile = fetch_loader_profile(cfg, base_version, loader_version, loader_type=lt, downloader=dl)

    profile = dict(profile)
    loader_id = str(profile.get("id") or "").strip()
    if not loader_id:
        # 兜底命名：与官方启动器风格保持一致
        loader_id = f"{lt}-loader-{loader_version}-{base_version}"
    # 注入关键字段：id 决定版本目录名，inheritsFrom 决定与哪个原版合并
    profile["id"] = loader_id
    profile["inheritsFrom"] = base_version
    profile.setdefault("type", "release")

    target_dir = util.ensure_dir(Path(cfg.versions_dir) / loader_id)
    util.write_json(target_dir / f"{loader_id}.json", profile)
    util.write_json(
        target_dir / "mcdecent.meta.json",
        {
            "loader": {"type": lt, "version": loader_version, "game": base_version},
            "installed_at": util.now_iso(),
        },
    )
    LOG.info("已写入 %s 加载器 profile：%s（继承自 %s）", lt, loader_id, base_version)
    return loader_id


def installed_loaders(cfg) -> list[dict]:
    """列出由本启动器安装过的加载器（读取版本目录里的 mcdecent.meta.json）。"""
    out: list[dict] = []
    versions = Path(cfg.versions_dir)
    if not versions.is_dir():
        return out
    for child in sorted(versions.iterdir()):
        if not child.is_dir():
            continue
        meta = util.read_json(child / "mcdecent.meta.json")
        if not isinstance(meta, dict):
            continue
        loader = meta.get("loader") or {}
        out.append(
            {
                "id": child.name,
                "type": loader.get("type"),
                "version": loader.get("version"),
                "game_version": loader.get("game"),
                "installed_at": meta.get("installed_at"),
                "path": str(child),
            }
        )
    return out


def detect_loader(cfg) -> dict | None:
    """返回最近安装（按目录名排序取最后一个）的加载器信息，供整合包导出等使用。"""
    loaders = installed_loaders(cfg)
    return loaders[-1] if loaders else None
