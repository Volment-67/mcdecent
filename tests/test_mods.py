"""mods.py：本地 jar 安装/禁用/删除、CAS 去重、Loader 推断、Modrinth 接口。"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

import pytest

from mcdecent import mods
from mcdecent.cas import ContentStore
from mcdecent.config import Config


def make_jar(path: Path, *, marker: bytes = b"v1", fabric: bool = False) -> Path:
    """造一个最小合法 jar（META-INF/MANIFEST.MF 是 zipfile 也能通过校验）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nCreated-By: test\n")
        if fabric:
            zf.writestr("fabric.mod.json", '{"schemaVersion":1,"id":"demo","version":"1.0.0"}')
        zf.writestr("data.bin", marker)
    return path


@pytest.fixture
def cfg(tmp_path):
    c = Config(home=tmp_path / "home")
    c.ensure_layout()
    return c


# ---------------------------------------------------------------- 安装 / 列举


def test_install_mod_file_puts_in_cas_and_mods(cfg, tmp_path):
    jar = make_jar(tmp_path / "src" / "demo-1.0.jar", fabric=True)
    info = mods.install_mod_file(cfg, jar)

    assert info["name"] == "demo-1.0.jar"
    assert info["enabled"] is True
    assert info["loader_hint"] == "fabric"
    target = Path(info["path"])
    assert target.is_file()
    assert target.parent == Path(cfg.mods_dir)
    assert ContentStore(cfg.cas_dir).has(info["sha256"])
    assert info["size"] == jar.stat().st_size

    listed = mods.list_mods(cfg)
    assert len(listed) == 1
    assert listed[0]["sha256"] == info["sha256"]
    assert listed[0]["enabled"] is True


def test_install_same_file_twice_is_idempotent(cfg, tmp_path):
    jar = make_jar(tmp_path / "src" / "demo.jar")
    first = mods.install_mod_file(cfg, jar)
    second = mods.install_mod_file(cfg, jar)
    assert first["path"] == second["path"]
    assert len(mods.list_mods(cfg)) == 1


def test_same_name_different_content_gets_sha_suffix(cfg, tmp_path):
    one = mods.install_mod_file(cfg, make_jar(tmp_path / "a" / "demo.jar", marker=b"one"))
    two = mods.install_mod_file(cfg, make_jar(tmp_path / "b" / "demo.jar", marker=b"two"))

    assert one["name"] == "demo.jar"
    assert two["name"] != "demo.jar"
    assert two["name"].startswith("demo-")
    assert two["name"].endswith(".jar")
    assert one["sha256"] != two["sha256"]
    assert len(mods.list_mods(cfg)) == 2


def test_install_with_enable_false_uses_disabled_suffix(cfg, tmp_path):
    info = mods.install_mod_file(cfg, make_jar(tmp_path / "demo.jar"), enable=False)
    assert info["name"] == "demo.jar.disabled"
    assert info["enabled"] is False
    assert len(mods.list_mods(cfg)) == 1
    assert mods.list_mods(cfg)[0]["enabled"] is False


def test_install_rejects_non_zip(cfg, tmp_path):
    bad = tmp_path / "bad.jar"
    bad.write_bytes(b"not a zip at all")
    with pytest.raises(ValueError, match="不是合法的"):
        mods.install_mod_file(cfg, bad)


def test_install_missing_file(cfg, tmp_path):
    with pytest.raises(FileNotFoundError):
        mods.install_mod_file(cfg, tmp_path / "nope.jar")


def test_loader_hint_reads_jar_metadata(cfg, tmp_path):
    jar = make_jar(tmp_path / "src" / "coolmod.jar", fabric=True)
    info = mods.install_mod_file(cfg, jar)
    assert info["loader_hint"] == "fabric"

    forge_jar = make_jar(tmp_path / "src" / "othermod.jar")
    with zipfile.ZipFile(tmp_path / "src" / "forgemod.jar", "w") as zf:
        zf.writestr("META-INF/mods.toml", "modLoader=\"javafml\"\n")
    info = mods.install_mod_file(cfg, tmp_path / "src" / "forgemod.jar")
    assert info["loader_hint"] == "forge"


# ---------------------------------------------------------------- 禁用 / 删除


def test_set_enabled_roundtrip(cfg, tmp_path):
    info = mods.install_mod_file(cfg, make_jar(tmp_path / "demo.jar"))
    disabled = mods.set_enabled(cfg, "demo.jar", False)
    assert disabled.name == "demo.jar.disabled"
    assert disabled.is_file()
    assert [m["enabled"] for m in mods.list_mods(cfg)] == [False]

    enabled = mods.set_enabled(cfg, "demo", True)  # 支持去后缀名匹配
    assert enabled.name == "demo.jar"
    assert enabled.is_file()
    assert [m["enabled"] for m in mods.list_mods(cfg)] == [True]

    with pytest.raises(FileNotFoundError):
        mods.set_enabled(cfg, "not-there.jar", False)


def test_remove_mod_by_name_and_glob(cfg, tmp_path):
    mods.install_mod_file(cfg, make_jar(tmp_path / "a" / "alpha.jar"))
    mods.install_mod_file(cfg, make_jar(tmp_path / "b" / "beta.jar"))
    assert len(mods.list_mods(cfg)) == 2

    assert mods.remove_mod(cfg, "alpha.jar") is True
    assert len(mods.list_mods(cfg)) == 1

    assert mods.remove_mod(cfg, "be*.jar") is True
    assert mods.list_mods(cfg) == []
    assert mods.remove_mod(cfg, "ghost.jar") is False


# ---------------------------------------------------------------- Modrinth


def test_search_modrinth_normalizes_hits(cfg, monkeypatch):
    hits = {
        "hits": [
            {
                "slug": "sodium",
                "project_id": "AANobbMI",
                "title": "Sodium",
                "description": "极速渲染",
                "downloads": 123456,
                "author": "jellysquid3",
                "icon_url": "https://example.invalid/icon.png",
                "categories": ["fabric", "optimization"],
                "versions": ["1.21.4"],
            }
        ]
    }
    seen: dict = {}

    class FakeDownloader:
        def fetch_json(self, url, *, cache_path=None, max_age=None, desc=""):
            seen["url"] = url
            return hits

    monkeypatch.setattr(cfg, "make_downloader", lambda **kwargs: FakeDownloader(), raising=False)
    results = mods.search_modrinth(cfg, "sodium", loader="fabric", game_version="1.21.4", limit=5)

    assert results[0]["slug"] == "sodium"
    assert results[0]["downloads"] == 123456
    assert results[0]["url"] == "https://modrinth.com/mod/sodium"
    assert "api.modrinth.com/v2/search" in seen["url"]
    assert "project_type%3Amod" in seen["url"]  # facets 被正确编码
    assert "versions%3A1.21.4" in seen["url"]


def test_install_modrinth_downloads_and_installs(cfg, tmp_path, monkeypatch):
    version = {
        "id": "ver123",
        "project_id": "proj123",
        "version_number": "1.2.3",
        "game_versions": ["1.21.4"],
        "loaders": ["fabric"],
        "files": [
            {
                "url": "https://cdn.example.invalid/sodium.jar",
                "filename": "sodium-1.2.3.jar",
                "primary": True,
                "size": 123,
                "hashes": {"sha1": "0" * 40, "sha512": "0" * 128},
            }
        ],
    }
    jar = make_jar(tmp_path / "cache.jar", marker=b"payload")

    class FakeDownloader:
        def fetch_json(self, url, *, cache_path=None, max_age=None, desc=""):
            return [version]

        def download_one(self, task):
            # 模拟"下载完成"：直接把假 jar 放到目标位置
            task.dest.parent.mkdir(parents=True, exist_ok=True)
            task.dest.write_bytes(jar.read_bytes())
            from mcdecent.net import DownloadResult

            return DownloadResult(task, "downloaded", task.dest.stat().st_size, 0.01)

    info = mods.install_modrinth(
        cfg,
        "sodium",
        loader="fabric",
        game_version="1.21.4",
        downloader=FakeDownloader(),
    )

    assert info["name"] == "sodium-1.2.3.jar"
    assert info["source"] == "modrinth:sodium"
    assert info["version_number"] == "1.2.3"
    assert Path(info["path"]).is_file()
    assert ContentStore(cfg.cas_dir).has(info["sha256"])


def test_install_modrinth_without_compatible_version_raises(cfg):
    class FakeDownloader:
        def fetch_json(self, url, *, cache_path=None, max_age=None, desc=""):
            return []

    with pytest.raises(RuntimeError, match="找不到"):
        mods.install_modrinth(
            cfg, "ghost", loader="fabric", game_version="1.21.4", downloader=FakeDownloader()
        )


# ---------------------------------------------------------------- 命令注册


def test_cli_parses_mods_commands():
    from mcdecent.commands import cmds_mods

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--home", default=argparse.SUPPRESS)
    parent.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    cmds_mods.register(sub, parent)

    assert parser.parse_args(["mods", "list"]).func is cmds_mods.cmd_mods_list
    add = parser.parse_args(["mods", "add", "a.jar", "b.jar", "--disable"])
    assert add.files == ["a.jar", "b.jar"] and add.disable is True
    search = parser.parse_args(["mods", "search", "sodium", "--game-version", "1.21.4"])
    assert search.query == "sodium" and search.loader == "fabric"
    install = parser.parse_args(["loader", "install", "1.21.4", "--type", "quilt", "--skip-assets"])
    assert install.loader_type == "quilt" and install.skip_assets is True
    export = parser.parse_args(
        ["pack", "export", "demo", "--game-version", "1.21.4", "--include", "mods", "config"]
    )
    assert export.name == "demo" and export.include == ["mods", "config"]


# ---------------------------------------------------------------- 联网（标注 network）


@pytest.mark.network
def test_modrinth_search_real(cfg):
    results = mods.search_modrinth(cfg, "sodium", loader="fabric", game_version="1.21.4", limit=5)
    assert results
    assert any("sodium" in (item["slug"] + item["title"]).lower() for item in results)


@pytest.mark.network
def test_modrinth_project_versions_real(cfg):
    versions = mods.fetch_project_versions(cfg, "sodium", loader="fabric", game_version="1.21.4")
    assert versions
    assert versions[0].get("files")
    assert "sha1" in (versions[0]["files"][0].get("hashes") or {})
