"""mods 目录管理 + Modrinth 检索/下载安装。

本地模组只是 ``<game>/mods/*.jar``；禁用状态用 ``.jar.disabled`` 后缀表示（与主流
启动器一致，游戏/加载器不会加载它）。每个模组同时按 SHA-256 存入 CAS，于是整合包
导出、P2P 分发、去重都可以复用同一份内容。
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import urllib.parse
import zipfile
from pathlib import Path

from . import util
from .cas import ContentStore
from .net import DownloadTask

LOG = util.LOG

MODRINTH_DEFAULT = "https://api.modrinth.com/v2"
SEARCH_MAX_AGE = 600  # 搜索结果缓存 10 分钟


# --------------------------------------------------------------------------- 目录与列举


def _store(cfg) -> ContentStore:
    return ContentStore(cfg.cas_dir)


def mods_dir(cfg) -> Path:
    """返回 game/mods 目录（不存在则创建）。"""
    return util.ensure_dir(cfg.mods_dir)


def _iter_jar_files(directory: Path):
    if not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.endswith(".jar") or name.endswith(".jar.disabled"):
            yield path


def loader_hint(path: str | Path) -> str:
    """推断模组适用的加载器：先看文件名，再看 jar 内的元数据文件。"""
    name = Path(path).name.lower()
    for hint in ("neoforge", "forge", "quilt", "fabric"):
        if hint in name:
            return hint
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return "unknown"
    joined = "\n".join(names).lower()
    if "fabric.mod.json" in joined:
        return "fabric"
    if "quilt.mod.json" in joined:
        return "quilt"
    if "neoforge.mods.toml" in joined:
        return "neoforge"
    if "mods.toml" in joined:
        return "forge"
    return "unknown"


def list_mods(cfg) -> list[dict]:
    """扫描 mods/，返回 [{name, path, size, sha256, enabled, loader_hint}]。"""
    directory = mods_dir(cfg)
    out: list[dict] = []
    for path in _iter_jar_files(directory):
        disabled = path.name.lower().endswith(".disabled")
        try:
            size = path.stat().st_size
            digest = util.sha256_file(path)
        except OSError as exc:  # 文件被占用/权限问题：不静默跳过
            LOG.warning("读取模组信息失败 %s：%r", path, exc)
            continue
        out.append(
            {
                "name": path.name,
                "path": str(path),
                "size": size,
                "sha256": digest,
                "enabled": not disabled,
                "loader_hint": loader_hint(path),
            }
        )
    out.sort(key=lambda item: item["name"].lower())
    return out


# --------------------------------------------------------------------------- 安装/删除/启停


def _safe_digest(path: Path) -> str | None:
    try:
        return util.sha256_file(path) if path.is_file() else None
    except OSError:
        return None


def _mod_info(path: Path, digest: str, source: str) -> dict:
    return {
        "name": path.name,
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": digest,
        "enabled": not path.name.lower().endswith(".disabled"),
        "loader_hint": loader_hint(path),
        "source": source,
    }


def install_mod_file(cfg, src: str | Path, *, enable: bool = True, source: str = "local") -> dict:
    """校验并安装一个本地 jar：入 CAS、复制进 mods/（按需加 .disabled）。

    同名同内容直接复用（必要时在启用/禁用两种形态间改名）；同名不同内容则加
    ``-<sha8>`` 后缀，绝不悄悄覆盖已有模组。
    """
    src_path = Path(src)
    if not src_path.is_file():
        raise FileNotFoundError(f"找不到模组文件：{src_path}")
    if not zipfile.is_zipfile(src_path):
        raise ValueError(f"不是合法的 jar/zip 模组文件：{src_path}")

    digest = util.sha256_file(src_path)
    _store(cfg).put_file(src_path)  # 入 CAS（去重 + 供整合包/P2P 使用）
    directory = mods_dir(cfg)

    core = src_path.name
    if core.lower().endswith(".disabled"):
        core = core[: -len(".disabled")]
    base = directory / core
    want = base if enable else base.with_name(base.name + ".disabled")
    other = base.with_name(base.name + ".disabled") if enable else base

    # 同内容已存在（任一种启用形态）→ 需要时改名，避免重复文件
    for candidate in (want, other):
        if _safe_digest(candidate) == digest:
            if candidate != want:
                if want.exists():
                    raise RuntimeError(f"无法切换启用状态：目标已存在 {want}")
                candidate.rename(want)
            return _mod_info(want, digest, source)

    # 没有同内容副本 → 复制；同名不同内容则加 -<sha8> 后缀
    target = want
    if target.exists():
        stem, suffix = Path(base.name).stem, Path(base.name).suffix
        target = directory / f"{stem}-{digest[:8]}{suffix}"
        if not enable:
            target = target.with_name(target.name + ".disabled")
    if target.exists() and _safe_digest(target) != digest:
        raise RuntimeError(f"目标文件已存在且内容不同：{target}")
    if not (target.exists() and src_path.resolve() == target.resolve()):
        shutil.copyfile(src_path, target)

    info = _mod_info(target, digest, source)
    LOG.info("已安装模组 %s（%s，%s）", info["name"], util.human_size(info["size"]), source)
    return info


def _match_mod(mod_name: str, pattern: str) -> bool:
    """支持文件名、去后缀名、通配符三种匹配方式（Windows 下大小写不敏感）。"""
    p = pattern.strip()
    if not p:
        return False
    name = mod_name
    no_disabled = name[: -len(".disabled")] if name.lower().endswith(".disabled") else name
    stem = Path(no_disabled).stem
    pl = p.lower()
    candidates = {name.lower(), no_disabled.lower(), stem.lower()}
    if pl in candidates:
        return True
    for candidate in (name.lower(), no_disabled.lower(), stem.lower()):
        if fnmatch.fnmatch(candidate, pl):
            return True
    return False


def _match_patterns(cfg, pattern: str) -> list[dict]:
    return [mod for mod in list_mods(cfg) if _match_mod(mod["name"], pattern)]


def remove_mod(cfg, name: str) -> bool:
    """删除模组文件（支持文件名/去后缀名/通配符）。返回是否删除了至少一个文件。"""
    matches = _match_patterns(cfg, name)
    if not matches:
        LOG.warning("没有找到匹配的模组：%s", name)
        return False
    removed = False
    for mod in matches:
        try:
            Path(mod["path"]).unlink()
            removed = True
            LOG.info("已删除模组：%s", mod["name"])
        except OSError as exc:
            LOG.warning("删除模组失败 %s：%r", mod["path"], exc)
    return removed


def set_enabled(cfg, name: str, enabled: bool) -> Path:
    """启用/禁用模组：通过增删 ``.disabled`` 后缀实现。返回操作后的路径。"""
    matches = _match_patterns(cfg, name)
    if not matches:
        raise FileNotFoundError(f"没有找到匹配的模组：{name}")
    if len(matches) > 1:
        exact = [m for m in matches if m["name"].lower() == name.strip().lower()]
        if len(exact) == 1:
            matches = exact
        else:
            names = "、".join(m["name"] for m in matches[:5])
            raise ValueError(f"名称 {name!r} 匹配到多个模组（{names}…），请写得更精确或加通配符")
    mod = matches[0]
    path = Path(mod["path"])
    if bool(mod["enabled"]) == bool(enabled):
        return path
    target = path.with_name(path.name[: -len(".disabled")] if enabled else path.name + ".disabled")
    if target.exists():  # 极少数情况下两种形态并存，直接报错而不是覆盖
        raise RuntimeError(f"目标文件已存在，无法切换状态：{target}")
    path.rename(target)
    LOG.info("%s模组：%s", "已启用" if enabled else "已禁用", target.name)
    return target


# --------------------------------------------------------------------------- Modrinth


def _modrinth_base(cfg) -> str:
    return str(cfg.get("modrinth_api", MODRINTH_DEFAULT) or MODRINTH_DEFAULT).rstrip("/")


def _cache_name(prefix: str, params: dict) -> str:
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return f"{prefix}-{util.sha256_bytes(raw)[:16]}.json"


def _modrinth_get(cfg, path: str, *, params: dict | None = None, downloader=None, max_age: float | None = SEARCH_MAX_AGE, cache_prefix: str = "modrinth"):
    """调用 Modrinth API。URL 里拼上查询参数；结果按参数哈希缓存到 cache_dir。

    User-Agent 由 Downloader 统一设置（Modrinth 要求标识客户端）。
    """
    url = _modrinth_base(cfg) + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    cache = Path(cfg.cache_dir) / "modrinth" / _cache_name(cache_prefix, params or {})
    dl = downloader if downloader is not None else cfg.make_downloader(quiet=True)
    return dl.fetch_json(url, cache_path=cache, max_age=max_age, desc=f"Modrinth {path}")


def search_modrinth(
    cfg,
    query: str,
    *,
    loader: str = "fabric",
    game_version: str | None = None,
    limit: int = 10,
    downloader=None,
) -> list[dict]:
    """在 Modrinth 上按关键词检索模组（project_type:mod）。"""
    facets = [["project_type:mod"]]
    if loader:
        facets.append([f"categories:{loader}"])
    if game_version:
        facets.append([f"versions:{game_version}"])
    params = {
        "query": query or "",
        "limit": max(1, min(int(limit), 100)),
        "index": "relevance",
        "facets": json.dumps(facets, separators=(",", ":")),
    }
    data = _modrinth_get(cfg, "/search", params=params, downloader=downloader, cache_prefix="search")
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        LOG.warning("Modrinth 搜索结果格式异常：%r", type(data))
        return []
    out: list[dict] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        out.append(
            {
                "slug": hit.get("slug") or "",
                "project_id": hit.get("project_id") or "",
                "title": hit.get("title") or "",
                "description": hit.get("description") or "",
                "downloads": int(hit.get("downloads") or 0),
                "author": hit.get("author") or "",
                "icon_url": hit.get("icon_url"),
                "categories": hit.get("categories") or [],
                "versions": hit.get("versions") or [],
                "url": f"https://modrinth.com/mod/{hit.get('slug')}" if hit.get("slug") else "",
            }
        )
    return out


def fetch_project_versions(
    cfg,
    slug_or_id: str,
    *,
    loader: str | None = "fabric",
    game_version: str | None = None,
    downloader=None,
) -> list[dict]:
    """取某个工程的版本列表（按 Modrinth 返回顺序，通常新版本在前）。"""
    if not slug_or_id:
        raise ValueError("必须提供 Modrinth 工程 slug 或 id")
    params: dict = {}
    if loader:
        params["loaders"] = json.dumps([loader], separators=(",", ":"))
    if game_version:
        params["game_versions"] = json.dumps([game_version], separators=(",", ":"))
    project = urllib.parse.quote(str(slug_or_id), safe="")
    data = _modrinth_get(cfg, f"/project/{project}/version", params=params, downloader=downloader, cache_prefix="versions")
    if isinstance(data, list):
        return [v for v in data if isinstance(v, dict)]
    LOG.warning("Modrinth 版本列表格式异常：%r", type(data))
    return []


def _pick_primary_file(version: dict) -> dict | None:
    files = [f for f in (version.get("files") or []) if isinstance(f, dict)]
    if not files:
        return None
    for item in files:
        if item.get("primary"):
            return item
    return files[0]


def install_modrinth(
    cfg,
    slug_or_id: str,
    *,
    loader: str = "fabric",
    game_version: str | None = None,
    version_id: str | None = None,
    downloader=None,
) -> dict:
    """从 Modrinth 下载并安装一个模组：sha1 校验 → CAS → mods/。"""
    dl = downloader if downloader is not None else cfg.make_downloader(quiet=True)
    if version_id:
        version = _modrinth_get(
            cfg,
            f"/version/{urllib.parse.quote(str(version_id), safe='')}",
            downloader=dl,
            cache_prefix="version",
            max_age=300,
        )
        if not isinstance(version, dict):
            raise RuntimeError(f"Modrinth 返回的版本信息不合法：{version_id}")
    else:
        versions = fetch_project_versions(cfg, slug_or_id, loader=loader, game_version=game_version, downloader=dl)
        if not versions:
            gv = game_version or "任意版本"
            raise RuntimeError(f"Modrinth 上找不到 {slug_or_id} 兼容 {loader} / {gv} 的版本")
        version = versions[0]

    file = _pick_primary_file(version)
    if not file or not file.get("url"):
        raise RuntimeError(f"Modrinth 版本 {version.get('id') or version.get('version_number')} 没有可下载文件")
    hashes = file.get("hashes") or {}
    filename = Path(str(file.get("filename") or f"{slug_or_id}.jar")).name
    cache_dir = util.ensure_dir(Path(cfg.cache_dir) / "mods")
    dest = cache_dir / filename
    task = DownloadTask(
        url=str(file["url"]),
        dest=dest,
        sha1=hashes.get("sha1"),
        size=int(file["size"]) if file.get("size") else None,
        desc=f"模组 {slug_or_id} {version.get('version_number') or ''}".strip(),
    )
    result = dl.download_one(task)
    if result.status not in ("cached", "downloaded"):
        raise RuntimeError(f"下载模组失败：{result.error or file['url']}")

    info = install_mod_file(cfg, dest, source=f"modrinth:{slug_or_id}")
    info.update(
        {
            "project_id": version.get("project_id"),
            "version_id": version.get("id"),
            "version_number": version.get("version_number"),
            "game_versions": version.get("game_versions") or [],
            "loaders": version.get("loaders") or [],
            "download_url": file["url"],
        }
    )
    return info
