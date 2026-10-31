"""资源包 / 光影 / 存档管理的单元测试（tmp_path 造假实例目录，不联网）。"""

from __future__ import annotations

import gzip
import json
import zipfile
from pathlib import Path

import pytest

from mcdecent import resources, servers


# --------------------------------------------------------------------------- 工具
def make_game(tmp_path, *, options: str | None = None) -> Path:
    game = tmp_path / "game"
    for sub in ("resourcepacks", "shaderpacks", "saves", "config"):
        (game / sub).mkdir(parents=True, exist_ok=True)
    if options is not None:
        (game / "options.txt").write_text(options, encoding="utf-8")
    return game


def make_pack_zip(path, *, description="测试资源包", fmt=15) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("pack.mcmeta", json.dumps(
            {"pack": {"pack_format": fmt, "description": description}}, ensure_ascii=False))
        zf.writestr("assets/minecraft/lang/zh_cn.json", "{}")
    return path


def make_dir_pack(path, *, description="目录资源包", fmt=8) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "pack.mcmeta").write_text(json.dumps(
        {"pack": {"pack_format": fmt, "description": description}}, ensure_ascii=False),
        encoding="utf-8")
    return path


def make_shader_zip(path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("shaders/shader.fsh", "void main() {}")
    return path


def level_dat_bytes(*, level_name="世界", version="1.20.1", last_played=1_700_000_000_000) -> bytes:
    tag = servers.Compound({
        "Data": servers.Compound({
            "LevelName": servers.String(level_name),
            "LastPlayed": servers.Long(last_played),
            "Version": servers.Compound({
                "Id": servers.Int(3465),
                "Name": servers.String(version),
            }),
        })
    })
    return gzip.compress(servers.dumps_nbt(tag))


def make_world(path, **kwargs) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "level.dat").write_bytes(level_dat_bytes(**kwargs))
    (path / "region").mkdir(exist_ok=True)
    (path / "region" / "r.0.0.mca").write_bytes(b"x" * 64)
    return path


# --------------------------------------------------------------------------- 资源包
def test_list_resourcepacks(tmp_path):
    game = make_game(tmp_path, options='resourcePacks:["vanilla","file/张三.zip"]\n')
    make_pack_zip(game / "resourcepacks" / "张三.zip", description="张三的资源包")
    make_pack_zip(game / "resourcepacks" / "text_desc.zip", description={"text": "文本组件描述"})
    make_dir_pack(game / "resourcepacks" / "目录包")
    (game / "resourcepacks" / "readme.txt").write_text("忽略我", encoding="utf-8")
    (game / "resourcepacks" / "bad.zip").write_bytes(b"not a zip")

    items = {item["name"]: item for item in resources.list_resourcepacks(game)}
    assert set(items) == {"张三.zip", "text_desc.zip", "目录包", "bad.zip"}

    good = items["张三.zip"]
    assert good["kind"] == "zip"
    assert good["enabled"] is True
    assert good["format"] == 15
    assert good["description"] == "张三的资源包"
    assert good["size"] > 0
    assert Path(good["path"]).name == "张三.zip"

    assert items["text_desc.zip"]["description"] == "文本组件描述"
    assert items["text_desc.zip"]["enabled"] is False

    folder = items["目录包"]
    assert folder["kind"] == "dir" and folder["format"] == 8
    assert folder["enabled"] is False

    assert items["bad.zip"]["format"] is None
    assert items["bad.zip"]["error"], "坏包应给出 error 说明"

    # 不存在的目录不应抛异常
    assert resources.list_resourcepacks(tmp_path / "empty") == []
    # 非法名称 / 路径穿越
    with pytest.raises(ValueError):
        resources.remove_resourcepack(game, "../options.txt")


def test_set_resourcepack_enabled_keeps_other_lines(tmp_path):
    game = make_game(tmp_path)
    (game / "options.txt").write_text(
        "version:3465\nlang:zh_cn\nresourcePacks:[\"vanilla\"]\nmouseSensitivity:0.5\n",
        encoding="utf-8")
    resources.set_resourcepack_enabled(game, "A.zip", True)
    lines = (game / "options.txt").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "version:3465"
    assert lines[1] == "lang:zh_cn"
    assert lines[3] == "mouseSensitivity:0.5"
    assert json.loads(lines[2].split(":", 1)[1]) == ["vanilla", "file/A.zip"]
    assert resources.enabled_packs(game) == ["vanilla", "file/A.zip"]

    resources.set_resourcepack_enabled(game, "A.zip", False)
    assert resources.enabled_packs(game) == ["vanilla"]
    resources.set_resourcepack_enabled(game, "B.zip", False)  # 禁用不存在的包：不报错
    assert resources.enabled_packs(game) == ["vanilla"]


def test_set_resourcepack_enabled_creates_options(tmp_path):
    game = make_game(tmp_path)
    assert resources.enabled_packs(game) == []
    resources.set_resourcepack_enabled(game, "x.zip", True)
    assert resources.enabled_packs(game) == ["file/x.zip"]
    assert (game / "options.txt").is_file()
    # 带 file/ 前缀也接受
    resources.set_resourcepack_enabled(game, "file/y.zip", True)
    assert resources.enabled_packs(game) == ["file/x.zip", "file/y.zip"]


def test_install_resourcepack_dedup_and_validation(tmp_path):
    game = make_game(tmp_path)
    src = make_pack_zip(tmp_path / "src" / "漂亮的包.zip")

    first = resources.install_resourcepack(game, src)
    assert first["name"] == "漂亮的包.zip"
    assert first["enabled"] is False
    second = resources.install_resourcepack(game, src)
    assert second["name"] == "漂亮的包-2.zip"
    assert (game / "resourcepacks" / "漂亮的包-2.zip").is_file()

    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("hello.txt", "x")
    with pytest.raises(ValueError):
        resources.install_resourcepack(game, bad)
    with pytest.raises(FileNotFoundError):
        resources.install_resourcepack(game, tmp_path / "没有这个.zip")
    assert "bad.zip" not in {item.name for item in (game / "resourcepacks").iterdir()}


def test_install_resourcepack_from_dir(tmp_path):
    game = make_game(tmp_path)
    src = make_dir_pack(tmp_path / "nice_pack", description="来自目录", fmt=22)
    entry = resources.install_resourcepack(game, src)
    assert entry["name"] == "nice_pack"
    assert entry["kind"] == "dir"
    assert entry["format"] == 22
    assert entry["description"] == "来自目录"


def test_remove_resourcepack_disables_entry(tmp_path):
    game = make_game(tmp_path, options='resourcePacks:["vanilla","file/A.zip"]\n')
    make_pack_zip(game / "resourcepacks" / "A.zip")
    assert resources.remove_resourcepack(game, "A.zip") is True
    assert not (game / "resourcepacks" / "A.zip").exists()
    assert resources.enabled_packs(game) == ["vanilla"]
    with pytest.raises(FileNotFoundError):
        resources.remove_resourcepack(game, "A.zip")


# --------------------------------------------------------------------------- 光影
def test_list_shaderpacks_iris_state(tmp_path):
    game = make_game(tmp_path)
    make_shader_zip(game / "shaderpacks" / "BSL.zip")
    make_shader_zip(game / "shaderpacks" / "Complementary.zip")

    # 没有配置文件 → 无法判断（None）
    unsupported = resources.list_shaderpacks(game)
    assert len(unsupported) == 2
    assert all(item["enabled"] is None for item in unsupported)

    (game / "config" / "iris.properties").write_text(
        "# Iris 配置\nshaderPack=BSL.zip\n", encoding="utf-8")
    items = {item["name"]: item for item in resources.list_shaderpacks(game)}
    assert items["BSL.zip"]["enabled"] is True
    assert items["Complementary.zip"]["enabled"] is False


def test_set_shaderpack_enabled_creates_config(tmp_path):
    game = make_game(tmp_path)
    make_shader_zip(game / "shaderpacks" / "BSL.zip")
    value = resources.set_shaderpack_enabled(game, "BSL.zip")
    assert value == "BSL.zip"
    config = game / "config" / "iris.properties"
    assert config.is_file()
    text = config.read_text(encoding="utf-8")
    assert "shaderPack=BSL.zip" in text
    assert "enableShaders=true" in text
    assert resources.shader_pack_enabled(game) == "BSL.zip"

    resources.set_shaderpack_enabled(game, None)
    assert resources.shader_pack_enabled(game) == ""
    assert "enableShaders=false" in config.read_text(encoding="utf-8")
    items = resources.list_shaderpacks(game)
    assert items and items[0]["enabled"] is False


def test_shaderpacks_optifine_and_remove(tmp_path):
    game = make_game(tmp_path)
    make_shader_zip(game / "shaderpacks" / "S.zip")
    (game / "optionsshaders.txt").write_text("shaderPack=S.zip\n", encoding="utf-8")
    assert resources.shader_pack_enabled(game) == "S.zip"
    assert resources.list_shaderpacks(game)[0]["enabled"] is True

    assert resources.remove_shaderpack(game, "S.zip") is True
    assert not (game / "shaderpacks" / "S.zip").exists()
    assert resources.shader_pack_enabled(game) == ""
    with pytest.raises(FileNotFoundError):
        resources.remove_shaderpack(game, "S.zip")


def test_install_shaderpack_validates_shaders_folder(tmp_path):
    game = make_game(tmp_path)
    src = make_shader_zip(tmp_path / "packs" / "BSL.zip")
    entry = resources.install_shaderpack(game, src)
    assert entry["name"] == "BSL.zip"
    assert (game / "shaderpacks" / "BSL.zip").is_file()

    bad = tmp_path / "packs" / "fake.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("readme.txt", "no shaders")
    with pytest.raises(ValueError):
        resources.install_shaderpack(game, bad)


def test_install_shaderpack_from_dir(tmp_path):
    game = make_game(tmp_path)
    src = tmp_path / "shader_dir" / "MyShaders"
    (src / "shaders").mkdir(parents=True)
    (src / "shaders" / "x.fsh").write_text("void main() {}", encoding="utf-8")
    entry = resources.install_shaderpack(game, src)
    assert entry["name"] == "MyShaders"
    assert entry["kind"] == "dir"
    assert (game / "shaderpacks" / "MyShaders" / "shaders" / "x.fsh").is_file()


# --------------------------------------------------------------------------- 存档
def test_list_saves(tmp_path):
    game = make_game(tmp_path)
    make_world(game / "saves" / "我的世界")
    broken = game / "saves" / "坏档"
    broken.mkdir()
    (broken / "level.dat").write_bytes(b"garbage-nbt")
    (game / "saves" / "缺文件").mkdir()

    items = {item["name"]: item for item in resources.list_saves(game)}
    assert set(items) == {"我的世界", "坏档", "缺文件"}

    good = items["我的世界"]
    assert good["level_name"] == "世界"
    assert good["version"] == "1.20.1"
    assert good["broken"] is False
    assert good["size"] > 0
    assert good["last_played"], "LastPlayed 应转成本地时间字符串"
    assert good["path"].endswith("我的世界")

    assert items["坏档"]["broken"] is True
    assert items["缺文件"]["broken"] is True
    assert items["缺文件"]["level_name"] == "缺文件"


def test_backup_save(tmp_path):
    game = make_game(tmp_path)
    world = make_world(game / "saves" / "世界")
    out = resources.backup_save(game, "世界")
    assert out.is_file()
    assert out.parent == tmp_path / "backups" / "saves"
    assert out.name.startswith("世界-") and out.suffix == ".zip"
    with zipfile.ZipFile(out) as zf:
        assert "level.dat" in zf.namelist()
        assert "region/r.0.0.mca" in zf.namelist()

    second = resources.backup_save(game, "世界")
    assert second != out or second.exists()  # 同名时间戳冲突时会加序号

    (game / "saves" / "无档").mkdir()
    with pytest.raises(ValueError):
        resources.backup_save(game, "无档")
    with pytest.raises(FileNotFoundError):
        resources.backup_save(game, "不存在")


def test_delete_save(tmp_path):
    game = make_game(tmp_path)
    world = make_world(game / "saves" / "W")
    assert resources.delete_save(game, "W") is True
    assert not world.exists()
    with pytest.raises(FileNotFoundError):
        resources.delete_save(game, "W")
    with pytest.raises(ValueError):
        resources.delete_save(game, "..")


def test_import_world_with_single_top_dir(tmp_path):
    game = make_game(tmp_path)
    src = tmp_path / "world.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("存档A/level.dat", level_dat_bytes(level_name="导入的世界"))
        zf.writestr("存档A/region/r.0.0.mca", b"x" * 32)

    entry = resources.import_world(game, src)
    target = game / "saves" / "存档A"
    assert entry["name"] == "存档A"
    assert target.is_dir() and (target / "level.dat").is_file()
    assert (target / "region" / "r.0.0.mca").is_file()
    assert entry["broken"] is False
    assert entry["level_name"] == "导入的世界"

    again = resources.import_world(game, src)
    assert again["name"] == "存档A-2"
    assert (game / "saves" / "存档A-2" / "level.dat").is_file()


def test_import_world_flat_zip(tmp_path):
    game = make_game(tmp_path)
    src = tmp_path / "flatworld.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("level.dat", level_dat_bytes(level_name="扁平世界"))
        zf.writestr("region/r.0.0.mca", b"x")

    entry = resources.import_world(game, src)
    assert entry["name"] == "flatworld"
    assert (game / "saves" / "flatworld" / "level.dat").is_file()
    assert entry["level_name"] == "扁平世界"


def test_import_world_rejects_traversal_and_missing_level(tmp_path):
    game = make_game(tmp_path)

    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../evil.txt", b"x")
        zf.writestr("level.dat", level_dat_bytes())
    with pytest.raises(ValueError):
        resources.import_world(game, evil)

    no_level = tmp_path / "nolevel.zip"
    with zipfile.ZipFile(no_level, "w") as zf:
        zf.writestr("world/foo.txt", "no level.dat")
    with pytest.raises(ValueError):
        resources.import_world(game, no_level)

    not_zip = tmp_path / "text.zip"
    not_zip.write_text("我不是 zip", encoding="utf-8")
    with pytest.raises(ValueError):
        resources.import_world(game, not_zip)
    with pytest.raises(FileNotFoundError):
        resources.import_world(game, tmp_path / "missing.zip")

    # 失败后不应在 saves/ 里留下半成品或临时目录
    assert not list((game / "saves").iterdir())


# --------------------------------------------------------------------------- 页签元数据


def test_resources_pages_wired_into_instance_manage():
    """资源/光影/存档现在作为「实例管理」页的子页签提供。"""
    from mcdecent.gui.pages import instance_manage

    assert instance_manage.TITLE == "实例管理"
    keys = [key for key, _text in instance_manage.TABS]
    assert {"resourcepacks", "shaderpacks", "saves"} <= set(keys)
