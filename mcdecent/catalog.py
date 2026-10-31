"""Modrinth 下载中心：搜索、版本解析与一键安装。

支持 5 类内容：``mod`` / ``modpack`` / ``resourcepack`` / ``shader`` / ``world``。
网络访问统一走 :mod:`mcdecent.net`（多镜像回退、重试、SHA 校验、JSON 缓存）；
所有失败都抛 ``RuntimeError``，消息里带「建议：…」说明下一步该做什么。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

from . import util
from .config import Config
from .net import DownloadError, DownloadTask, Downloader

LOG = util.LOG

DEFAULT_API = "https://api.modrinth.com/v2"
PROJECT_TYPES = {"mod", "modpack", "resourcepack", "shader", "world"}
SORTS = ("relevance", "downloads", "newest", "updated", "follows")
SEARCH_CACHE_SECONDS = 300.0
METADATA_CACHE_SECONDS = 600.0
INDEX_FILE = "modrinth.index.json"
INVALID_NAME_CHARS = '<>:"/\\|?*'

NET_ADVICE = "检查网络连接；若使用代理/镜像，可在「设置」页把 mirror_mode 改成 auto 或 off 后重试。"
SECURITY_ADVICE = "这可能是损坏或恶意的文件，已中止安装；请到 modrinth.com 重新下载。"

_DEFAULT_LOCK = threading.Lock()
_DEFAULT_DOWNLOADER: Downloader | None = None


# --------------------------------------------------------------------------- 内部工具


def _fail(message: str, advice: str = "") -> RuntimeError:
    """构造带「下一步建议」的中文 RuntimeError。"""
    text = str(message).strip()
    if advice:
        text += f"\n建议：{advice}"
    return RuntimeError(text)


def _api_base() -> str:
    return (os.environ.get("MCDECENT_MODRINTH_API") or DEFAULT_API).rstrip("/")


def _api_base_of(cfg) -> str:
    if cfg is not None:
        try:
            base = cfg.get("modrinth_api")
        except Exception:  # noqa: BLE001 - 配置对象可能是测试替身
            base = None
        if base:
            return str(base).rstrip("/")
    return _api_base()


def default_downloader() -> Downloader:
    """无 cfg 场景（``search``/``versions``）复用的默认 Downloader。"""
    global _DEFAULT_DOWNLOADER
    with _DEFAULT_LOCK:
        if _DEFAULT_DOWNLOADER is None:
            _DEFAULT_DOWNLOADER = Config().make_downloader(quiet=True)
        return _DEFAULT_DOWNLOADER


def reset_default_downloader() -> None:
    """丢弃缓存的默认 Downloader（测试里切换 MCDECENT_HOME 后调用）。"""
    global _DEFAULT_DOWNLOADER
    with _DEFAULT_LOCK:
        _DEFAULT_DOWNLOADER = None


def _cache_file(downloader, prefix: str, key: dict) -> Path:
    raw = json.dumps(key, sort_keys=True, ensure_ascii=False).encode("utf-8")
    name = f"{prefix}-{util.sha256_bytes(raw)[:16]}.json"
    return Path(downloader.cache_dir) / "catalog" / name


def _request_json(
    path: str,
    *,
    params: dict | None = None,
    downloader: Downloader | None = None,
    max_age: float | None = SEARCH_CACHE_SECONDS,
    prefix: str = "api",
    cache_key: dict | None = None,
    api_base: str | None = None,
) -> object:
    """GET 一个 Modrinth API 路径，结果按参数哈希缓存到 cache_dir/catalog。"""
    dl = downloader if downloader is not None else default_downloader()
    url = (api_base or _api_base()) + path
    if params:
        url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    cache = _cache_file(dl, prefix, cache_key if cache_key is not None else (params or {}))
    try:
        return dl.fetch_json(url, cache_path=cache, max_age=max_age, desc=f"Modrinth {path}")
    except DownloadError as exc:
        raise _fail(f"访问 Modrinth 失败：{path}（{exc}）", NET_ADVICE) from exc
    except (OSError, ValueError) as exc:  # 缓存损坏 / 响应不是 JSON
        raise _fail(f"解析 Modrinth 响应失败：{path}（{exc!r}）", NET_ADVICE) from exc


def _normalize_hit(hit: dict) -> dict:
    return {
        "project_id": str(hit.get("project_id") or ""),
        "slug": str(hit.get("slug") or ""),
        "title": str(hit.get("title") or ""),
        "description": str(hit.get("description") or ""),
        "downloads": int(hit.get("downloads") or 0),
        "icon_url": hit.get("icon_url"),
        "author": str(hit.get("author") or ""),
        "versions": list(hit.get("versions") or []),
    }


def _version_meta(version: dict) -> dict:
    return {
        "project_id": version.get("project_id"),
        "version_id": version.get("id"),
        "version_number": version.get("version_number"),
        "game_versions": list(version.get("game_versions") or []),
        "loaders": list(version.get("loaders") or []),
    }


# --------------------------------------------------------------------------- 搜索 / 版本


def build_facets(project_type: str, *, loader: str | None = None,
                 game_version: str | None = None) -> list[list[str]]:
    """构造 Modrinth facets（调用方负责 ``json.dumps`` 后放进 facets 参数）。"""
    ptype = str(project_type or "").strip().lower()
    if ptype not in PROJECT_TYPES:
        raise ValueError(f"不支持的下载类型：{project_type!r}（可选：{'、'.join(sorted(PROJECT_TYPES))}）")
    facets: list[list[str]] = [[f"project_type:{ptype}"]]
    ld = str(loader or "").strip().lower()
    if ld and ld not in ("any", "all", "*"):
        facets.append([f"categories:{ld}"])
    gv = str(game_version or "").strip()
    if gv:
        facets.append([f"versions:{gv}"])
    return facets


def search(
    project_type: str,
    query: str = "",
    *,
    loader: str | None = None,
    game_version: str | None = None,
    limit: int = 20,
    offset: int = 0,
    sort: str = "relevance",
    downloader: Downloader | None = None,
) -> dict:
    """在 Modrinth 搜索项目，返回 ``{"hits": [...], "total_hits": N}``。"""
    facets = build_facets(project_type, loader=loader, game_version=game_version)
    index = str(sort or "relevance").strip().lower()
    if index not in SORTS:
        index = "relevance"
    params = {
        "query": str(query or "").strip(),
        "facets": json.dumps(facets, ensure_ascii=False, separators=(",", ":")),
        "limit": max(1, min(int(limit or 20), 100)),
        "offset": max(0, int(offset or 0)),
        "index": index,
    }
    data = _request_json("/search", params=params, downloader=downloader, prefix="search")
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        raise _fail("Modrinth 搜索返回格式异常（缺少 hits 列表）", NET_ADVICE)
    out = [_normalize_hit(hit) for hit in hits if isinstance(hit, dict)]
    total = data.get("total_hits")
    total_hits = int(total) if isinstance(total, (int, float)) else len(out)
    return {"hits": out, "total_hits": total_hits}


def project(slug: str, *, downloader: Downloader | None = None) -> dict:
    """取单个项目详情（图标、作者、简介、分类等）。"""
    key = str(slug or "").strip()
    if not key:
        raise _fail("必须提供 Modrinth 项目 slug 或 ID", "在搜索框里搜索并选择项目，或复制 modrinth.com 项目地址里的 slug。")
    quoted = urllib.parse.quote(key, safe="")
    data = _request_json(
        f"/project/{quoted}", downloader=downloader, prefix="project",
        max_age=METADATA_CACHE_SECONDS, cache_key={"slug": key},
    )
    if not isinstance(data, dict):
        raise _fail(f"Modrinth 返回的项目信息格式异常：{slug}", NET_ADVICE)
    return data


def versions(
    slug: str,
    *,
    loader: str | None = None,
    game_version: str | None = None,
    downloader: Downloader | None = None,
) -> list[dict]:
    """取项目版本列表（新版本在前）；可按加载器/游戏版本过滤。"""
    key = str(slug or "").strip()
    if not key:
        raise _fail("必须提供 Modrinth 项目 slug 或 ID", "在搜索框里搜索并选择项目，或复制 modrinth.com 项目地址里的 slug。")
    params: dict = {}
    ld = str(loader or "").strip()
    if ld and ld.lower() not in ("any", "all", "*"):
        params["loaders"] = json.dumps([ld], separators=(",", ":"))
    gv = str(game_version or "").strip()
    if gv:
        params["game_versions"] = json.dumps([gv], separators=(",", ":"))
    quoted = urllib.parse.quote(key, safe="")
    data = _request_json(
        f"/project/{quoted}/version",
        params=params or None,
        downloader=downloader,
        prefix="versions",
        cache_key={"slug": key, **params},
    )
    if not isinstance(data, list):
        raise _fail(f"Modrinth 返回的版本列表格式异常：{slug}", NET_ADVICE)
    return [item for item in data if isinstance(item, dict)]


def _fetch_version(version_id: str, *, downloader: Downloader | None = None,
                   api_base: str | None = None) -> dict:
    key = str(version_id or "").strip()
    quoted = urllib.parse.quote(key, safe="")
    data = _request_json(
        f"/version/{quoted}",
        downloader=downloader,
        prefix="version",
        max_age=METADATA_CACHE_SECONDS,
        cache_key={"id": key},
        api_base=api_base,
    )
    if not isinstance(data, dict):
        raise _fail(f"Modrinth 返回的版本信息格式异常：{version_id}", NET_ADVICE)
    return data


def pick_file(version: dict) -> dict:
    """从版本里挑出主文件，返回 {url, filename, size, sha1, sha512, hashes}。"""
    if not isinstance(version, dict):
        raise _fail("版本信息不合法，无法选择下载文件", "重新搜索并选择项目后重试。")
    files = [item for item in (version.get("files") or []) if isinstance(item, dict)]
    if not files:
        label = version.get("version_number") or version.get("id") or ""
        raise _fail(
            f"版本 {label} 没有可下载文件",
            "该版本可能被作者撤下或只能到官网手动下载；请换一个版本，或到 modrinth.com 手动安装。",
        )
    primary = next((item for item in files if item.get("primary")), files[0])
    url = str(primary.get("url") or "").strip()
    if not url:
        raise _fail("主文件缺少下载地址（url 为空）", "换一个版本重试；若仍失败请到 modrinth.com 手动下载。")
    raw_hashes = primary.get("hashes") if isinstance(primary.get("hashes"), dict) else {}
    hashes = {str(k).lower(): str(v).lower() for k, v in raw_hashes.items() if isinstance(v, str)}
    filename = Path(str(primary.get("filename") or url.rsplit("/", 1)[-1])).name
    if not filename or filename in (".", ".."):
        filename = "download.bin"
    size = primary.get("size")
    return {
        "url": url,
        "filename": filename,
        "size": int(size) if isinstance(size, (int, float)) and size > 0 else None,
        "sha1": hashes.get("sha1"),
        "sha512": hashes.get("sha512"),
        "hashes": hashes,
    }


def format_downloads(num) -> str:
    """把下载量格式化成人类可读（万/亿）。"""
    try:
        n = int(num or 0)
    except (TypeError, ValueError):
        return "?"
    if n < 0:
        return "0"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f} 亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f} 万"
    return f"{n:,}"


# --------------------------------------------------------------------------- 路径安全


def _safe_relative(rel) -> PurePosixPath:
    """把相对路径规范化为 PurePosixPath；拒绝绝对路径/盘符/``..``。"""
    text = str(rel or "").replace("\\", "/").strip()
    if not text:
        raise _fail("文件路径为空（整合包清单可能已损坏）", SECURITY_ADVICE)
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise _fail(f"拒绝绝对路径：{rel}", SECURITY_ADVICE)
    parts = [part for part in PurePosixPath(text).parts if part not in ("", ".")]
    if not parts:
        raise _fail(f"拒绝可疑路径：{rel}", SECURITY_ADVICE)
    if any(part == ".." for part in parts):
        raise _fail(f"拒绝路径穿越：{rel}", SECURITY_ADVICE)
    return PurePosixPath(*parts)


def _safe_join(base, rel) -> Path:
    return Path(base).joinpath(*_safe_relative(rel).parts)


def _safe_component(name: str, fallback: str = "world") -> str:
    cleaned = "".join(ch for ch in str(name or "") if ch not in INVALID_NAME_CHARS and ch not in "\r\n\t")
    cleaned = cleaned.strip().strip(".")
    return cleaned or fallback


def _unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.name}-{index}")
        if not candidate.exists():
            return candidate
    raise _fail(f"目标目录已存在且无法生成新名字：{path}", "先清理 saves 目录里同名世界后重试。")


# --------------------------------------------------------------------------- 安装路由


def install(
    cfg,
    game_dir,
    project_type: str,
    slug: str,
    *,
    loader: str = "fabric",
    game_version: str | None = None,
    version_id: str | None = None,
    progress=None,
) -> dict:
    """下载并安装一个 Modrinth 项目到 ``game_dir``（当前实例目录）。

    ``progress`` 会透传给 Downloader（``util.Progress`` 或 None）。
    """
    ptype = str(project_type or "").strip().lower()
    if ptype not in PROJECT_TYPES:
        raise _fail(
            f"不支持的下载类型：{project_type!r}",
            f"可选类型：{'、'.join(sorted(PROJECT_TYPES))}（模组/整合包/资源包/光影/世界）。",
        )
    key = str(slug or "").strip()
    if not key:
        raise _fail("必须提供 Modrinth 项目 slug 或 ID", "在搜索结果里选择一个项目后再点「安装」。")

    if cfg is None:
        cfg = Config()
    target_dir = Path(game_dir).expanduser()
    dl = cfg.make_downloader(progress=progress, quiet=True)
    ld = str(loader or "").strip() or None
    gv = str(game_version or "").strip() or None

    # 1) 解析版本与主文件
    if version_id:
        version = _fetch_version(str(version_id), downloader=dl, api_base=_api_base_of(cfg))
    else:
        found = versions(key, loader=ld, game_version=gv)
        if not found:
            raise _fail(
                f"Modrinth 上没有找到 {key} 兼容「{ld or '任意加载器'} / {gv or '任意游戏版本'}」的版本",
                "换一个游戏版本或加载器再试；也可以到 modrinth.com 确认项目支持的版本。",
            )
        version = found[0]
    meta = _version_meta(version)
    file = pick_file(version)

    # 2) 下载到缓存（DownloadTask 带 sha1 + 大小校验）
    cache_dir = util.ensure_dir(Path(cfg.cache_dir) / "catalog")
    dest = cache_dir / file["filename"]
    task = DownloadTask(
        url=file["url"],
        dest=dest,
        sha1=file.get("sha1"),
        size=file.get("size"),
        desc=f"{key} {version.get('version_number') or ''}".strip(),
    )
    result = dl.download_one(task)
    if result.status not in ("cached", "downloaded"):
        raise _fail(
            f"下载 {file['filename']} 失败：{result.error or file['url']}",
            NET_ADVICE + " 如果一直失败，也可以到 modrinth.com 手动下载后放进对应目录。",
        )
    if file.get("sha512") and not file.get("sha1"):
        actual = util.sha512_file(dest)
        if actual.lower() != str(file["sha512"]).lower():
            raise _fail(
                f"文件校验失败（sha512 不符）：{file['filename']}",
                "镜像可能返回了错误内容，删除缓存目录后重试，或到 modrinth.com 手动下载。",
            )

    # 3) 按类型落盘
    if ptype == "mod":
        inst_cfg = copy.copy(cfg)
        inst_cfg.set_game_dir(target_dir)
        try:
            from .mods import install_mod_file

            info = install_mod_file(inst_cfg, dest, source=f"modrinth:{key}")
        except (ValueError, OSError) as exc:
            raise _fail(
                f"安装模组失败：{exc}",
                "确认下载类型选的是「模组」且文件是 .jar；若文件损坏可清空缓存后重试。",
            ) from exc
        return {
            **info,
            "project_type": ptype,
            "slug": key,
            "target": info.get("path"),
            "file": file["filename"],
            "source": f"modrinth:{key}",
            **meta,
        }

    if ptype in ("resourcepack", "shader"):
        sub = "resourcepacks" if ptype == "resourcepack" else "shaderpacks"
        directory = util.ensure_dir(target_dir / sub)
        target = directory / dest.name
        try:
            shutil.copyfile(dest, target)
        except OSError as exc:
            raise _fail(
                f"写入 {sub} 目录失败：{exc}",
                "检查实例目录是否只读/被占用，或关闭游戏后重试。",
            ) from exc
        LOG.info("已安装%s：%s", "资源包" if ptype == "resourcepack" else "光影", target)
        return {
            "name": target.name,
            "path": str(target),
            "target": str(target),
            "project_type": ptype,
            "slug": key,
            "file": file["filename"],
            "size": target.stat().st_size if target.is_file() else None,
            **meta,
        }

    if ptype == "world":
        return _install_world(target_dir, dest, key, meta)

    return _install_modpack(cfg, target_dir, dest, key, meta, dl)


def _install_world(target_dir: Path, bundle: Path, slug: str, meta: dict) -> dict:
    """世界存档：解压到 saves/<名称>/，必须含 level.dat。"""
    if not zipfile.is_zipfile(bundle):
        raise _fail(
            f"{bundle.name} 不是有效的世界存档（应为 zip）",
            "确认下载类型选的是「世界」；也可以手动解压后放进 saves 目录。",
        )
    saves = util.ensure_dir(target_dir / "saves")
    name = _safe_component(Path(bundle.name).stem)
    target = _unique_dir(saves / name)
    written = util.safe_extract_zip(bundle, target, skip_meta=False)
    root = _world_root(target)
    if root is None:
        util.remove_tree(target)
        raise _fail(
            f"{bundle.name} 里没有 level.dat，不是有效的世界存档",
            "确认下载类型选的是「世界」；也可以手动解压后把含 level.dat 的文件夹放进 saves 目录。",
        )
    if root != target:
        _promote_dir(root, target)
    return {
        "name": target.name,
        "path": str(target),
        "target": str(target),
        "project_type": "world",
        "slug": slug,
        "file": bundle.name,
        "written": len(written),
        "files": len(written),
        **meta,
    }


def _world_root(target: Path) -> Path | None:
    if (target / "level.dat").is_file():
        return target
    for child in sorted(target.iterdir()):
        if child.is_dir() and (child / "level.dat").is_file():
            return child
    return None


def _promote_dir(root: Path, target: Path) -> None:
    """把单层嵌套目录（saves/x/x/level.dat）的内容提升到 saves/x/。"""
    try:
        for item in list(root.iterdir()):
            dest = target / item.name
            if dest.exists():
                util.remove_tree(dest)
            shutil.move(str(item), str(dest))
    finally:
        util.remove_tree(root)


def _extract_prefixed(zf: zipfile.ZipFile, prefix: str, dest: Path) -> int:
    """把 zip 内 ``prefix/`` 下的文件（去掉前缀）解压到 ``dest``，拒绝路径穿越。"""
    written = 0
    for info in zf.infolist():
        name = info.filename
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if not rel or rel.endswith("/"):
            continue
        if (info.external_attr >> 16) & 0o170000 == 0o120000:  # 符号链接
            LOG.warning("跳过符号链接：%s", name)
            continue
        target = _safe_join(dest, rel)
        util.ensure_dir(target.parent)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out, util.CHUNK)
        written += 1
    return written


def _install_modpack(cfg, target_dir: Path, bundle: Path, slug: str, meta: dict, dl: Downloader) -> dict:
    """整合包（.mrpack）：解析 modrinth.index.json，下载 files[] 并解压 overrides。"""
    if not zipfile.is_zipfile(bundle):
        raise _fail(
            f"{bundle.name} 不是有效的整合包（.mrpack 应为 zip）",
            "确认下载类型选的是「整合包」；也可以到 modrinth.com 手动下载 .mrpack 后用「整合包」页导入。",
        )
    index: dict | None = None
    try:
        with zipfile.ZipFile(bundle) as zf:
            names = set(zf.namelist())
            if INDEX_FILE in names:
                index = json.loads(zf.read(INDEX_FILE).decode("utf-8"))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise _fail(f"读取整合包失败：{exc!r}", "重新下载整合包；若仍失败请到 modrinth.com 手动下载。") from exc
    if not isinstance(index, dict):
        raise _fail(
            f"{bundle.name} 里缺少 {INDEX_FILE}",
            "确认下载类型选的是「整合包」；.mrpack 是 Modrinth 专用格式，其他整合包请用「整合包」页导入。",
        )

    entries = index.get("files")
    if not isinstance(entries, list):
        raise _fail("整合包清单里的 files 字段不合法", "重新下载整合包，或到 modrinth.com 找其他版本。")

    util.ensure_dir(target_dir)
    tasks: list[tuple[DownloadTask, str | None]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
        if str(env.get("client") or "").lower() == "unsupported":
            continue
        rel = entry.get("path")
        if not rel:
            continue
        target = _safe_join(target_dir, rel)
        downloads = [url for url in (entry.get("downloads") or []) if isinstance(url, str) and url.strip()]
        if not downloads:
            LOG.warning("整合包文件缺少下载地址，跳过：%s", rel)
            continue
        raw_hashes = entry.get("hashes") if isinstance(entry.get("hashes"), dict) else {}
        sha1 = str(raw_hashes.get("sha1") or "").lower() or None
        sha512 = str(raw_hashes.get("sha512") or "").lower() or None
        size = entry.get("fileSize") or entry.get("size")
        tasks.append(
            (
                DownloadTask(
                    url=downloads[0].strip(),
                    dest=target,
                    sha1=sha1,
                    size=int(size) if isinstance(size, (int, float)) and size > 0 else None,
                    desc=str(rel),
                ),
                sha512,
            )
        )
    if not tasks:
        raise _fail(
            "整合包清单里没有可下载文件",
            "该整合包可能只提供外部下载；请到 modrinth.com 手动下载或换一个整合包。",
        )

    try:
        results = dl.download_all([task for task, _sha in tasks], desc=f"整合包 {slug}")
    except DownloadError as exc:
        raise _fail(f"整合包文件下载失败：{exc}", NET_ADVICE) from exc
    failed = [item for item in results if item.status not in ("cached", "downloaded")]
    if failed:
        detail = "；".join(item.error for item in failed[:3] if item.error)
        raise _fail(
            f"{len(failed)} 个整合包文件下载失败" + (f"：{detail}" if detail else ""),
            NET_ADVICE + " 若反复失败，可到 modrinth.com 手动下载 .mrpack 后用「整合包」页导入。",
        )
    # 只有 sha512 时在落盘后补一次校验（DownloadTask 只支持 sha1/sha256）
    for task, sha512 in tasks:
        if sha512 and not task.sha1:
            if util.sha512_file(task.dest).lower() != sha512:
                raise _fail(
                    f"整合包文件校验失败（sha512 不符）：{task.desc}",
                    "镜像可能返回了错误内容，删除缓存后重试。",
                )

    overrides = 0
    try:
        with zipfile.ZipFile(bundle) as zf:
            for prefix in ("overrides/", "client-overrides/"):
                overrides += _extract_prefixed(zf, prefix, target_dir)
    except RuntimeError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise _fail(f"解压整合包 overrides 失败：{exc!r}", "重新下载整合包后重试。") from exc

    name = str(index.get("name") or bundle.stem)
    version = str(index.get("versionId") or meta.get("version_number") or "")
    LOG.info("整合包「%s」%s 安装完成：%d 个文件，%d 个覆盖文件", name, version, len(tasks), overrides)
    return {
        "name": name,
        "version": version,
        "project_type": "modpack",
        "slug": slug,
        "files": len(tasks),
        "written": len(tasks),
        "overrides": overrides,
        "path": str(target_dir),
        "target": str(target_dir),
        "file": bundle.name,
        **meta,
    }
