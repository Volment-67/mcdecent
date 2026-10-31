"""loaders.py：加载器版本归一化、profile 落盘、Quilt 兼容。

除标记 ``network`` 的用例外，全部用假 Downloader 隔离网络。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcdecent import loaders
from mcdecent.config import Config


class FakeDownloader:
    """按 URL 子串返回预置数据；顺序敏感（先匹配的优先）。"""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    def fetch_json(self, url, *, cache_path=None, max_age=None, desc=""):
        self.calls.append(url)
        for needle, payload in self.responses.items():
            if needle in url:
                return payload
        raise RuntimeError(f"未预置的 URL：{url}")


FABRIC_VERSIONS = [
    {"loader": {"version": "0.16.14", "stable": True, "maven": "net.fabricmc:fabric-loader:0.16.14"}, "intermediary": {"version": "1.21.4"}, "launcherMeta": {}},
    {"loader": {"version": "0.17.0-beta.1", "stable": False}, "intermediary": {"version": "1.21.4"}, "launcherMeta": {}},
]

QUILT_VERSIONS = [
    {"version": "0.27.1", "maven": "org.quiltmc:quilt-loader:0.27.1", "build": 1},
]

FABRIC_PROFILE = {
    "id": "fabric-loader-0.16.14-1.21.4",
    "inheritsFrom": "1.21.4",
    "type": "release",
    "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
    "libraries": [{"name": "net.fabricmc:fabric-loader:0.16.14"}],
    "arguments": {"game": [], "jvm": []},
}


@pytest.fixture
def cfg(tmp_path):
    c = Config(home=tmp_path / "home")
    c.ensure_layout()
    return c


# ---------------------------------------------------------------- 版本列表


def test_fetch_fabric_loader_versions_normalized(cfg):
    dl = FakeDownloader({"meta.fabricmc.net/v2/versions/loader/1.21.4": FABRIC_VERSIONS})
    entries = loaders.fetch_fabric_loader_versions(cfg, "1.21.4", downloader=dl)

    assert len(entries) == 2
    assert entries[0]["loader"]["version"] == "0.16.14"
    assert entries[0]["loader"]["stable"] is True
    # 提供扁平字段方便使用
    assert entries[1]["version"] == "0.17.0-beta.1"
    assert entries[1]["stable"] is False
    assert "meta.fabricmc.net" in dl.calls[0]


def test_fetch_quilt_versions_flattens_entries(cfg):
    dl = FakeDownloader({"meta.quiltmc.org/v3/versions/loader/1.21.4": QUILT_VERSIONS})
    entries = loaders.fetch_quilt_loader_versions(cfg, "1.21.4", downloader=dl)

    assert entries[0]["loader"]["version"] == "0.27.1"
    assert entries[0]["version"] == "0.27.1"
    assert "meta.quiltmc.org" in dl.calls[0]


def test_unknown_loader_type_rejected(cfg):
    with pytest.raises(ValueError, match="不支持的加载器类型"):
        loaders.fetch_loader_versions(cfg, "1.21.4", loader_type="neoforge", downloader=FakeDownloader({}))


def test_pick_loader_version_prefers_stable():
    entries = loaders._normalize_entries(FABRIC_VERSIONS)
    assert loaders.pick_loader_version(entries) == "0.16.14"
    assert loaders.pick_loader_version(entries, "0.17.0-beta.1") == "0.17.0-beta.1"
    with pytest.raises(ValueError, match="不存在"):
        loaders.pick_loader_version(entries, "9.9.9")


# ---------------------------------------------------------------- 安装 profile


def test_install_loader_writes_profile_and_meta(cfg):
    dl = FakeDownloader({"/profile/json": FABRIC_PROFILE})
    loader_id = loaders.install_loader(cfg, "1.21.4", loader_version="0.16.14", downloader=dl)

    assert loader_id == "fabric-loader-0.16.14-1.21.4"
    version_dir = Path(cfg.versions_dir) / loader_id
    profile = json.loads((version_dir / f"{loader_id}.json").read_text(encoding="utf-8"))
    assert profile["id"] == loader_id
    assert profile["inheritsFrom"] == "1.21.4"
    assert profile["mainClass"] == FABRIC_PROFILE["mainClass"]

    meta = json.loads((version_dir / "mcdecent.meta.json").read_text(encoding="utf-8"))
    assert meta["loader"] == {"type": "fabric", "version": "0.16.14", "game": "1.21.4"}
    assert meta.get("installed_at")

    installed = loaders.installed_loaders(cfg)
    assert [item["id"] for item in installed] == [loader_id]
    assert loaders.detect_loader(cfg)["version"] == "0.16.14"


def test_install_loader_auto_picks_latest_stable(cfg):
    dl = FakeDownloader(
        {
            "/profile/json": FABRIC_PROFILE,
            "versions/loader/1.21.4": FABRIC_VERSIONS,
        }
    )
    loader_id = loaders.install_loader(cfg, "1.21.4", downloader=dl)
    assert loader_id == "fabric-loader-0.16.14-1.21.4"
    # 先查版本列表，再取 profile
    assert any("versions/loader/1.21.4" in url for url in dl.calls)
    assert any("/profile/json" in url for url in dl.calls)


def test_quilt_profile_without_id_gets_fallback_name(cfg):
    profile = dict(FABRIC_PROFILE)
    profile.pop("id")
    dl = FakeDownloader({"/profile/json": profile})
    loader_id = loaders.install_loader(
        cfg, "1.21.4", loader_type="quilt", loader_version="0.27.1", downloader=dl
    )
    assert loader_id == "quilt-loader-0.27.1-1.21.4"
    assert "meta.quiltmc.org" in dl.calls[0]


def test_installed_loaders_empty_when_no_versions(cfg):
    assert loaders.installed_loaders(cfg) == []
    assert loaders.detect_loader(cfg) is None


# ---------------------------------------------------------------- 联网（标注 network）


@pytest.mark.network
def test_fabric_meta_real(cfg):
    entries = loaders.fetch_fabric_loader_versions(cfg, "1.21.4")
    assert entries
    assert any(entry["loader"]["stable"] for entry in entries)


@pytest.mark.network
def test_fabric_profile_real(cfg):
    entries = loaders.fetch_fabric_loader_versions(cfg, "1.21.4")
    version = loaders.pick_loader_version(entries)
    profile = loaders.fetch_loader_profile(cfg, "1.21.4", version)
    assert profile.get("mainClass")
    assert profile.get("libraries")
