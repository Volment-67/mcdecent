"""catalog.py：facets、主文件选择、路径安全、安装路由（离线）与一个真实搜索用例。"""

from __future__ import annotations

import io
import json
import urllib.parse
import zipfile
from pathlib import Path

import pytest

from mcdecent import catalog, net
from mcdecent.config import Config


# --------------------------------------------------------------------------- 测试数据


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _jar_bytes(marker: bytes = b"payload") -> bytes:
    return _zip_bytes(
        {
            "META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\n",
            "fabric.mod.json": b'{"schemaVersion":1,"id":"demo","version":"1.0.0"}',
            "data.bin": marker,
        }
    )


def _mrpack_bytes(files: list[dict] | None = None, *, overrides: dict[str, bytes] | None = None) -> bytes:
    index = {
        "formatVersion": 1,
        "game": "minecraft:1.21.4",
        "versionId": "1.2.3",
        "name": "测试整合包",
        "files": files or [],
    }
    entries: dict[str, bytes] = {"modrinth.index.json": json.dumps(index, ensure_ascii=False).encode("utf-8")}
    entries.update(overrides or {})
    return _zip_bytes(entries)


def _file(url: str, filename: str, *, size: int = 16, sha1: str = "a" * 40,
          sha512: str | None = None, primary: bool = True) -> dict:
    hashes = {"sha1": sha1, "sha512": sha512} if sha512 else {"sha1": sha1}
    return {"url": url, "filename": filename, "size": size, "primary": primary, "hashes": hashes}


def _version(files: list[dict], *, version_number: str = "1.0", vid: str = "ver-1") -> dict:
    return {
        "id": vid,
        "project_id": "proj-1",
        "version_number": version_number,
        "game_versions": ["1.21.4"],
        "loaders": ["fabric"],
        "files": files,
    }


def _patch_downloads(monkeypatch, payloads: dict[str, bytes], record: list | None = None) -> None:
    """把 Downloader.download_one 换成"按文件名/URL 直接写本地假文件"。"""

    def fake(self, task):
        if record is not None:
            record.append(task)
        dest = Path(task.dest)
        data = payloads.get(dest.name) or payloads.get(task.url)
        if data is None:
            return net.DownloadResult(task, "failed", 0, 0.0, error="测试没有预置该文件")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return net.DownloadResult(task, "downloaded", len(data), 0.01)

    monkeypatch.setattr(net.Downloader, "download_one", fake)


@pytest.fixture
def cfg(tmp_path):
    c = Config(home=tmp_path / "home", load=False)
    c.ensure_layout()
    return c


# --------------------------------------------------------------------------- facets / 搜索


def test_build_facets_matches_modrinth_format():
    assert catalog.PROJECT_TYPES == {"mod", "modpack", "resourcepack", "shader", "world"}
    assert catalog.build_facets("mod", loader="fabric", game_version="26.3") == [
        ["project_type:mod"],
        ["categories:fabric"],
        ["versions:26.3"],
    ]
    assert catalog.build_facets("mod") == [["project_type:mod"]]
    # any / 空加载器不加 facet
    assert catalog.build_facets("world", loader="any", game_version=None) == [["project_type:world"]]
    with pytest.raises(ValueError):
        catalog.build_facets("not-a-type")


def test_search_builds_url_and_normalizes_hits(tmp_path):
    class FakeFetcher:
        def __init__(self):
            self.cache_dir = tmp_path / "dl-cache"
            self.urls: list[str] = []

        def fetch_json(self, url, *, cache_path=None, max_age=None, desc=""):
            self.urls.append(url)
            return {
                "hits": [
                    {
                        "project_id": "AANobbMI",
                        "slug": "sodium",
                        "title": "Sodium",
                        "description": "极速渲染",
                        "downloads": 123456,
                        "icon_url": "https://example.invalid/icon.png",
                        "author": "jellysquid3",
                        "versions": ["1.21.4"],
                    }
                ],
                "total_hits": 42,
            }

    fake = FakeFetcher()
    data = catalog.search(
        "mod", "sodium", loader="fabric", game_version="1.21.4",
        limit=3, offset=0, sort="downloads", downloader=fake,
    )
    url = fake.urls[0]
    assert "https://api.modrinth.com/v2/search" in url
    assert "project_type%3Amod" in url
    assert "categories%3Afabric" in url
    assert "versions%3A1.21.4" in url
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["index"] == ["downloads"]
    assert query["limit"] == ["3"]
    assert json.loads(query["facets"][0]) == [
        ["project_type:mod"], ["categories:fabric"], ["versions:1.21.4"]
    ]
    assert data["total_hits"] == 42
    hit = data["hits"][0]
    assert hit["slug"] == "sodium" and hit["downloads"] == 123456 and hit["author"] == "jellysquid3"


def test_search_rejects_malformed_payload(tmp_path):
    class FakeFetcher:
        cache_dir = tmp_path / "cache"

        def fetch_json(self, url, *, cache_path=None, max_age=None, desc=""):
            return {"nope": True}

    with pytest.raises(RuntimeError, match="格式异常"):
        catalog.search("mod", "sodium", downloader=FakeFetcher())


def test_versions_passes_filters_and_validates(tmp_path):
    class FakeFetcher:
        def __init__(self, data):
            self.cache_dir = tmp_path / "cache"
            self.data = data
            self.urls: list[str] = []

        def fetch_json(self, url, *, cache_path=None, max_age=None, desc=""):
            self.urls.append(url)
            return self.data

    fake = FakeFetcher([_version([_file("https://cdn.example.invalid/x.jar", "x.jar")])])
    found = catalog.versions("demo", loader="fabric", game_version="1.21.4", downloader=fake)
    assert found and found[0]["version_number"] == "1.0"
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.urls[0]).query)
    assert query["loaders"] == ['["fabric"]']
    assert query["game_versions"] == ['["1.21.4"]']

    with pytest.raises(RuntimeError, match="格式异常"):
        catalog.versions("demo", downloader=FakeFetcher({"nope": 1}))
    with pytest.raises(RuntimeError, match="必须提供"):
        catalog.versions("  ")


# --------------------------------------------------------------------------- 主文件 / 格式化


def test_pick_file_prefers_primary():
    secondary = _file("https://cdn.example.invalid/secondary.jar", "secondary.jar", primary=False)
    primary = _file(
        "https://cdn.example.invalid/main.jar", "main-1.0.jar", size=321,
        sha1="b" * 40, sha512="c" * 128, primary=True,
    )
    picked = catalog.pick_file(_version([secondary, primary]))
    assert picked["url"] == primary["url"]
    assert picked["filename"] == "main-1.0.jar"
    assert picked["size"] == 321
    assert picked["sha1"] == "b" * 40
    assert picked["sha512"] == "c" * 128
    assert picked["hashes"]["sha1"] == "b" * 40


def test_pick_file_falls_back_to_first_and_rejects_empty():
    only = _file("https://cdn.example.invalid/only.jar", "only.jar", primary=False)
    assert catalog.pick_file(_version([only]))["filename"] == "only.jar"

    with pytest.raises(RuntimeError, match="建议"):
        catalog.pick_file(_version([]))

    broken = _file("", "broken.jar")
    with pytest.raises(RuntimeError, match="建议"):
        catalog.pick_file(_version([broken]))


def test_format_downloads():
    assert catalog.format_downloads(999) == "999"
    assert catalog.format_downloads(12345) == "1.2 万"
    assert catalog.format_downloads(123_456_789) == "1.2 亿"
    assert catalog.format_downloads(0) == "0"
    assert catalog.format_downloads(None) == "0"
    assert catalog.format_downloads("bad") == "?"


# --------------------------------------------------------------------------- 路径安全


def test_safe_relative_accepts_normal_paths():
    rel = catalog._safe_relative("mods/sodium.jar")
    assert Path(*rel.parts) == Path("mods") / "sodium.jar"
    assert Path(*catalog._safe_relative("config/x.json").parts) == Path("config") / "x.json"


@pytest.mark.parametrize(
    "bad",
    ["../evil.jar", "a/../../evil.jar", "/etc/passwd", "C:/Windows/evil.dll",
     "C:\\Windows\\evil.dll", "..", "", "   "],
)
def test_safe_relative_rejects_unsafe_paths(bad):
    with pytest.raises(RuntimeError, match="建议"):
        catalog._safe_relative(bad)


# --------------------------------------------------------------------------- 安装路由


def test_install_mod_lands_in_instance_mods(cfg, tmp_path, monkeypatch):
    game_dir = tmp_path / "game"
    jar = _jar_bytes()
    version = _version([_file("https://cdn.modrinth.com/data/x/demo-1.0.jar", "demo-1.0.jar", size=len(jar))])
    monkeypatch.setattr(catalog, "versions", lambda slug, **kwargs: [version])
    tasks: list = []
    _patch_downloads(monkeypatch, {"demo-1.0.jar": jar}, record=tasks)

    result = catalog.install(cfg, game_dir, "mod", "demo", loader="fabric", game_version="1.21.4")

    target = game_dir / "mods" / "demo-1.0.jar"
    assert target.is_file()
    assert result["project_type"] == "mod"
    assert result["version_number"] == "1.0"
    assert Path(result["target"]) == target
    # DownloadTask 带 sha1 与大小（校验信息来自 pick_file）
    assert tasks and tasks[0].sha1 == "a" * 40 and tasks[0].size == len(jar)
    # 安装不该污染传入的 cfg
    assert cfg._game_dir_override is None


@pytest.mark.parametrize(
    "ptype,sub",
    [("resourcepack", "resourcepacks"), ("shader", "shaderpacks")],
)
def test_install_pack_like_types(cfg, tmp_path, monkeypatch, ptype, sub):
    game_dir = tmp_path / "game"
    payload = b"pack-bytes"
    filename = f"{ptype}.zip"
    version = _version([_file(f"https://cdn.modrinth.com/data/x/{filename}", filename, size=len(payload))])
    monkeypatch.setattr(catalog, "versions", lambda slug, **kwargs: [version])
    _patch_downloads(monkeypatch, {filename: payload})

    result = catalog.install(cfg, game_dir, ptype, "demo", loader=None)

    target = game_dir / sub / filename
    assert target.is_file() and target.read_bytes() == payload
    assert Path(result["target"]) == target
    assert result["project_type"] == ptype


def test_install_world_extracts_to_saves(cfg, tmp_path, monkeypatch):
    game_dir = tmp_path / "game"
    payload = _zip_bytes({"level.dat": b"level", "region/r.0.0.mca": b"region"})
    version = _version([_file("https://cdn.modrinth.com/data/x/myworld.zip", "myworld.zip", size=len(payload))])
    monkeypatch.setattr(catalog, "versions", lambda slug, **kwargs: [version])
    _patch_downloads(monkeypatch, {"myworld.zip": payload})

    result = catalog.install(cfg, game_dir, "world", "myworld")

    target = game_dir / "saves" / "myworld"
    assert (target / "level.dat").is_file()
    assert (target / "region" / "r.0.0.mca").is_file()
    assert result["path"] == str(target)


def test_install_world_promotes_single_nested_folder(cfg, tmp_path, monkeypatch):
    game_dir = tmp_path / "game"
    payload = _zip_bytes({"packed/level.dat": b"level"})
    version = _version([_file("https://cdn.modrinth.com/data/x/packed.zip", "packed.zip", size=len(payload))])
    monkeypatch.setattr(catalog, "versions", lambda slug, **kwargs: [version])
    _patch_downloads(monkeypatch, {"packed.zip": payload})

    catalog.install(cfg, game_dir, "world", "packed")

    target = game_dir / "saves" / "packed"
    assert (target / "level.dat").is_file()
    assert not (target / "packed").exists()


def test_install_world_requires_level_dat(cfg, tmp_path, monkeypatch):
    game_dir = tmp_path / "game"
    payload = _zip_bytes({"readme.txt": b"not a world"})
    version = _version([_file("https://cdn.modrinth.com/data/x/broken.zip", "broken.zip", size=len(payload))])
    monkeypatch.setattr(catalog, "versions", lambda slug, **kwargs: [version])
    _patch_downloads(monkeypatch, {"broken.zip": payload})

    with pytest.raises(RuntimeError, match="level.dat"):
        catalog.install(cfg, game_dir, "world", "broken")
    assert not (game_dir / "saves" / "broken").exists()


def test_install_world_skips_traversal_members(cfg, tmp_path, monkeypatch):
    game_dir = tmp_path / "game"
    payload = _zip_bytes({"level.dat": b"level", "../escaped.txt": b"evil"})
    version = _version([_file("https://cdn.modrinth.com/data/x/evilworld.zip", "evilworld.zip", size=len(payload))])
    monkeypatch.setattr(catalog, "versions", lambda slug, **kwargs: [version])
    _patch_downloads(monkeypatch, {"evilworld.zip": payload})

    catalog.install(cfg, game_dir, "world", "evilworld")

    target = game_dir / "saves" / "evilworld"
    assert (target / "level.dat").is_file()
    assert not (target.parent / "escaped.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_install_modpack_downloads_files_and_overrides(cfg, tmp_path, monkeypatch):
    game_dir = tmp_path / "game"
    mod_payload = _jar_bytes(b"mod")
    cfg_payload = b"config-bytes"
    files = [
        {
            "path": "mods/example.jar",
            "hashes": {"sha1": "d" * 40},
            "fileSize": len(mod_payload),
            "downloads": ["https://cdn.modrinth.com/data/x/mods/example.jar"],
        },
        {
            "path": "config/extra.txt",
            "hashes": {"sha1": "e" * 40},
            "fileSize": len(cfg_payload),
            "downloads": ["https://cdn.modrinth.com/data/x/config/extra.txt"],
        },
    ]
    bundle = _mrpack_bytes(
        files,
        overrides={
            "overrides/config/from-pack.txt": b"from-pack",
            "client-overrides/options.txt": b"options",
        },
    )
    version = _version([_file("https://cdn.modrinth.com/data/x/pack.mrpack", "pack.mrpack", size=len(bundle))])
    monkeypatch.setattr(catalog, "versions", lambda slug, **kwargs: [version])
    _patch_downloads(
        monkeypatch,
        {"pack.mrpack": bundle, "example.jar": mod_payload, "extra.txt": cfg_payload},
    )

    result = catalog.install(cfg, game_dir, "modpack", "pack", loader=None, game_version="1.21.4")

    assert (game_dir / "mods" / "example.jar").read_bytes() == mod_payload
    assert (game_dir / "config" / "extra.txt").read_bytes() == cfg_payload
    assert (game_dir / "config" / "from-pack.txt").read_bytes() == b"from-pack"
    assert (game_dir / "options.txt").read_bytes() == b"options"
    assert result["project_type"] == "modpack"
    assert result["name"] == "测试整合包"
    assert result["version"] == "1.2.3"
    assert result["files"] == 2 and result["written"] == 2
    assert result["overrides"] == 2


def test_install_modpack_rejects_traversal_path(cfg, tmp_path, monkeypatch):
    game_dir = tmp_path / "game"
    files = [{
        "path": "../escaped.txt",
        "hashes": {"sha1": "f" * 40},
        "fileSize": 4,
        "downloads": ["https://cdn.modrinth.com/data/x/escaped.txt"],
    }]
    bundle = _mrpack_bytes(files)
    version = _version([_file("https://cdn.modrinth.com/data/x/pack.mrpack", "pack.mrpack", size=len(bundle))])
    monkeypatch.setattr(catalog, "versions", lambda slug, **kwargs: [version])
    _patch_downloads(monkeypatch, {"pack.mrpack": bundle, "escaped.txt": b"evil"})

    with pytest.raises(RuntimeError, match="路径"):
        catalog.install(cfg, game_dir, "modpack", "pack")
    assert not (game_dir / "escaped.txt").exists()


def test_install_modpack_reports_download_failure(cfg, tmp_path, monkeypatch):
    game_dir = tmp_path / "game"
    files = [{
        "path": "mods/example.jar",
        "hashes": {"sha1": "1" * 40},
        "fileSize": 4,
        "downloads": ["https://cdn.modrinth.com/data/x/example.jar"],
    }]
    bundle = _mrpack_bytes(files)
    version = _version([_file("https://cdn.modrinth.com/data/x/pack.mrpack", "pack.mrpack", size=len(bundle))])
    monkeypatch.setattr(catalog, "versions", lambda slug, **kwargs: [version])
    _patch_downloads(monkeypatch, {"pack.mrpack": bundle})  # 清单里的文件全部下载失败

    with pytest.raises(RuntimeError, match="整合包文件下载失败"):
        catalog.install(cfg, game_dir, "modpack", "pack")


def test_install_rejects_unknown_type_and_missing_version(cfg, tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="不支持的下载类型"):
        catalog.install(cfg, tmp_path / "game", "bad-type", "demo")

    monkeypatch.setattr(catalog, "versions", lambda slug, **kwargs: [])
    with pytest.raises(RuntimeError, match="建议"):
        catalog.install(cfg, tmp_path / "game", "mod", "ghost", loader="fabric", game_version="1.21.4")


# --------------------------------------------------------------------------- 联网（标签 network）


@pytest.mark.network
def test_search_real_modrinth(monkeypatch, tmp_path):
    """真实访问 Modrinth：只发一次小请求（limit=3）。"""
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path / "net-home"))
    catalog.reset_default_downloader()
    try:
        data = catalog.search("mod", "sodium", limit=3)
    finally:
        catalog.reset_default_downloader()
    assert 1 <= len(data["hits"]) <= 3
    assert data["total_hits"] >= len(data["hits"])
    hit = data["hits"][0]
    assert hit["slug"] and hit["title"]
    assert isinstance(hit["downloads"], int)
