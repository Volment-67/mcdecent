"""核心层单元测试：版本合并、规则求值、镜像重写、CAS、配置、参数展开。

不联网、不下载，全部使用临时目录与构造数据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcdecent import auth, cas, config, launch, net, util, versions


@pytest.fixture()
def cfg(tmp_path: Path) -> config.Config:
    c = config.Config(home=tmp_path / "home", load=False)
    c.ensure_layout()
    return c


# --------------------------------------------------------------------------- 规则


def test_rule_allows_windows_only():
    rules = [{"action": "allow", "os": {"name": "windows"}}]
    assert versions.rule_allows(rules) is True
    rules = [{"action": "allow", "os": {"name": "linux"}}]
    assert versions.rule_allows(rules) is False


def test_rule_allows_disallow_wins():
    rules = [
        {"action": "allow"},
        {"action": "disallow", "os": {"name": "windows"}},
    ]
    assert versions.rule_allows(rules) is False


def test_rule_features():
    rules = [{"action": "allow", "features": {"is_demo_user": True}}]
    assert versions.rule_allows(rules, {"is_demo_user": True}) is True
    assert versions.rule_allows(rules, {}) is False


def test_rule_without_rules_is_allowed():
    assert versions.rule_allows(None) is True
    assert versions.rule_allows([]) is True


# --------------------------------------------------------------------------- Maven


def test_maven_path_basic():
    assert versions.maven_path("org.ow2.asm:asm:9.10.1") == "org/ow2/asm/asm/9.10.1/asm-9.10.1.jar"


def test_maven_path_classifier():
    assert versions.maven_path("net.java.jinput:jinput:2.0.5:natives-windows") == (
        "net/java/jinput/jinput/2.0.5/jinput-2.0.5-natives-windows.jar"
    )


def test_maven_path_invalid():
    with pytest.raises(ValueError):
        versions.maven_path("broken")


# --------------------------------------------------------------------------- 镜像


def _bmclapi() -> net.Mirror:
    return net.Mirror.from_dict(config.DEFAULT_MIRRORS[0])


def test_mirror_rewrites_libraries_to_maven():
    mirror = _bmclapi()
    url = "https://libraries.minecraft.net/at/yawk/lz4/lz4-java/1.10.1/lz4-java-1.10.1.jar"
    assert mirror.rewrite(url) == (
        "https://bmclapi2.bangbang93.com/maven/at/yawk/lz4/lz4-java/1.10.1/lz4-java-1.10.1.jar"
    )


def test_mirror_rewrites_assets():
    mirror = _bmclapi()
    url = "https://resources.download.minecraft.net/ab/cdef"
    assert mirror.rewrite(url) == "https://bmclapi2.bangbang93.com/assets/ab/cdef"


def test_mirror_ignores_unknown_host():
    assert _bmclapi().rewrite("https://example.com/file") is None


def test_downloader_resolve_order(mirror_mode="auto"):
    dl = net.Downloader(cache_dir=".", mirrors=[_bmclapi()], mirror_mode="auto")
    urls = dl.resolve_urls("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
    assert urls[0].startswith("https://bmclapi2.bangbang93.com")
    assert urls[-1].startswith("https://piston-meta.mojang.com")
    dl_off = net.Downloader(cache_dir=".", mirrors=[_bmclapi()], mirror_mode="off")
    assert dl_off.resolve_urls("https://piston-meta.mojang.com/x") == ["https://piston-meta.mojang.com/x"]
    dl_only = net.Downloader(cache_dir=".", mirrors=[_bmclapi()], mirror_mode="only")
    assert dl_only.resolve_urls("https://piston-meta.mojang.com/x") == ["https://bmclapi2.bangbang93.com/x"]


# --------------------------------------------------------------------------- 版本合并


def test_merge_chain_child_wins_and_libraries_dedup():
    parent = {
        "id": "1.20.1",
        "mainClass": "net.minecraft.client.main.Main",
        "libraries": [
            {"name": "com.a:same:1.0"},
            {"name": "com.b:only-parent:2.0"},
        ],
        "arguments": {"game": ["--username", "${auth_player_name}"], "jvm": ["-cp", "${classpath}"]},
        "downloads": {"client": {"sha1": "aa", "size": 1, "url": "http://x/client.jar"}},
        "assetIndex": {"id": "5", "url": "http://x/5.json"},
        "javaVersion": {"majorVersion": 17},
    }
    child = {
        "id": "fabric-loader-0.15-1.20.1",
        "inheritsFrom": "1.20.1",
        "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
        "libraries": [
            {"name": "com.a:same:2.0"},
            {"name": "net.fabricmc:loader:0.15"},
        ],
        "arguments": {"game": [], "jvm": ["-DFabricMcEmu= net.minecraft.client.main.Main "]},
    }
    merged = versions._merge_chain([child, parent])
    assert merged["mainClass"].startswith("net.fabricmc")
    names = [lib["name"] for lib in merged["libraries"]]
    assert "com.a:same:2.0" in names
    assert "com.a:same:1.0" not in names
    assert "com.b:only-parent:2.0" in names
    assert merged["downloads"]["client"]["sha1"] == "aa"
    assert merged["javaVersion"]["majorVersion"] == 17
    assert any("FabricMcEmu" in arg for arg in merged["arguments"]["jvm"])
    assert merged["arguments"]["game"] == ["--username", "${auth_player_name}"]


def test_merge_chain_keeps_native_classifier_libraries():
    """回归测试：classifier 不同的条目不能被 group:artifact 去重掉。"""
    parent = {
        "id": "26.3",
        "mainClass": "net.minecraft.client.main.Main",
        "libraries": [
            {"name": "org.lwjgl:lwjgl:3.4.3"},
            {"name": "org.lwjgl:lwjgl:3.4.3:natives-windows",
             "rules": [{"action": "allow", "os": {"name": "windows"}}]},
            {"name": "org.lwjgl:lwjgl:3.4.3:natives-linux",
             "rules": [{"action": "allow", "os": {"name": "linux"}}]},
        ],
    }
    merged = versions._merge_chain([parent])
    names = [lib["name"] for lib in merged["libraries"]]
    assert "org.lwjgl:lwjgl:3.4.3" in names
    assert "org.lwjgl:lwjgl:3.4.3:natives-windows" in names
    assert "org.lwjgl:lwjgl:3.4.3:natives-linux" in names


def test_library_url_without_downloads_uses_maven_coordinates(cfg):
    merged = {
        "libraries": [
            {"name": "net.fabricmc:fabric-loader:0.15.0", "url": "https://maven.fabricmc.net/"},
        ]
    }
    libs = versions._classpath_libraries(cfg, merged)
    assert libs[0]["rel_path"] == "net/fabricmc/fabric-loader/0.15.0/fabric-loader-0.15.0.jar"
    assert libs[0]["url"] == "https://maven.fabricmc.net/net/fabricmc/fabric-loader/0.15.0/fabric-loader-0.15.0.jar"


# --------------------------------------------------------------------------- CAS


def test_cas_put_get_dedup(tmp_path):
    store = cas.ContentStore(tmp_path / "cas")
    file_a = tmp_path / "a.bin"
    file_a.write_bytes(b"hello mcdecent")
    digest = store.put_file(file_a)
    assert store.has(digest)
    assert store.get(digest).read_bytes() == b"hello mcdecent"
    # 同一内容重复放入应复用对象
    file_b = tmp_path / "b.bin"
    file_b.write_bytes(b"hello mcdecent")
    assert store.put_file(file_b) == digest
    assert store.index() == {digest: 14}
    # put_bytes
    assert store.put_bytes(b"hello mcdecent") == digest


def test_cas_verify_all_detects_corruption(tmp_path):
    store = cas.ContentStore(tmp_path / "cas")
    digest = store.put_bytes(b"content")
    ok, bad = store.verify_all()
    assert (ok, bad) == (1, [])
    path = store.path_for(digest)
    path.write_bytes(b"tampered")
    ok, bad = store.verify_all()
    assert ok == 0 and bad == [digest]


# --------------------------------------------------------------------------- 身份


def test_offline_uuid_is_stable_and_v3():
    a = auth.offline_uuid("Steve")
    b = auth.offline_uuid("Steve")
    assert a == b
    assert a[14] == "3"  # UUID 版本位
    assert auth.offline_uuid("Alex") != a


def test_offline_profile_truncates_long_name():
    profile = auth.offline_profile("x" * 40)
    assert len(profile.name) == 16


def test_online_profile_rejects_bad_uuid():
    with pytest.raises(ValueError):
        auth.online_profile("Steve", "not-a-uuid", "token")


# --------------------------------------------------------------------------- 参数展开


def test_expand_args_substitutes_and_filters_rules():
    mapping = {"auth_player_name": "Steve", "classpath": "a:b"}
    args = [
        "--username", "${auth_player_name}",
        {"rules": [{"action": "allow", "features": {"is_demo_user": True}}], "value": "--demo"},
        {"rules": [{"action": "allow", "os": {"name": "linux"}}], "value": "--linux-only"},
        {"rules": [{"action": "allow", "os": {"name": "windows"}}], "value": "--win-only"},
    ]
    out = launch._expand_args(args, mapping, {"is_demo_user": False})
    assert "Steve" in out
    assert "--demo" not in out
    assert "--linux-only" not in out
    assert "--win-only" in out


def test_expand_args_legacy_string():
    out = launch._expand_args(["--uuid", "${auth_uuid}"], {"auth_uuid": "abc"})
    assert out == ["--uuid", "abc"]


# --------------------------------------------------------------------------- 配置


def test_config_roundtrip(tmp_path):
    cfg = config.Config(home=tmp_path / "h", load=False)
    cfg.set("memory_mb", 4096)
    cfg.set("player.name", "Notch")
    cfg.save()
    reloaded = config.Config(home=tmp_path / "h")
    assert reloaded.get("memory_mb") == 4096
    assert reloaded.get("player.name") == "Notch"
    assert reloaded.game_dir == (tmp_path / "h" / "game").resolve()


def test_installed_versions_empty(cfg):
    assert versions.installed_versions(cfg) == []


def test_installation_report_missing_version(cfg):
    report = __import__("mcdecent.installer", fromlist=["installation_report"]).installation_report(cfg, "9.9.9")
    assert report["ok"] is False
