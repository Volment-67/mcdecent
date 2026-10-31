"""servers.dat 与最小 NBT 编解码的单元测试（不联网、不开窗口）。"""

from __future__ import annotations

import gzip

import pytest

from mcdecent import servers


# --------------------------------------------------------------------------- NBT
def test_nbt_roundtrip_all_types(tmp_path):
    path = tmp_path / "all.dat"
    tag = servers.Compound({
        "byte": servers.Byte(-5),
        "short": servers.Short(300),
        "int": servers.Int(-70000),
        "long": servers.Long(1_700_000_000_123),
        "float": servers.Float(1.5),
        "double": servers.Double(-2.25),
        "string": servers.String("你好 Minecraft"),
        "byteArray": servers.ByteArray(b"\x01\x02\xff"),
        "intArray": servers.IntArray([1, -2, 3]),
        "nested": servers.Compound({"leaf": servers.String("x")}),
        "servers": servers.NbtList(servers.TAG_COMPOUND, [
            servers.Compound({"name": servers.String("a"), "num": servers.Int(1)}),
            servers.Compound({"name": servers.String("b"), "num": servers.Int(2)}),
        ]),
        "emptyList": servers.NbtList(servers.TAG_COMPOUND, []),
    })
    servers.write_nbt(path, tag)
    raw = path.read_bytes()
    assert raw[:2] == b"\x1f\x8b", "写回必须保持 gzip 压缩"

    got = servers.read_nbt(path)
    assert isinstance(got, dict)
    assert type(got["byte"]) is servers.Byte and got["byte"] == -5
    assert type(got["short"]) is servers.Short and got["short"] == 300
    assert type(got["int"]) is servers.Int and got["int"] == -70000
    assert type(got["long"]) is servers.Long and got["long"] == 1_700_000_000_123
    assert type(got["float"]) is servers.Float and got["float"] == 1.5
    assert type(got["double"]) is servers.Double and got["double"] == -2.25
    assert type(got["string"]) is servers.String and got["string"] == "你好 Minecraft"
    assert type(got["byteArray"]) is servers.ByteArray and got["byteArray"] == b"\x01\x02\xff"
    assert type(got["intArray"]) is servers.IntArray and list(got["intArray"]) == [1, -2, 3]
    assert got["nested"]["leaf"] == "x"
    assert [item["name"] for item in got["servers"]] == ["a", "b"]
    assert got["servers"].elem_type == servers.TAG_COMPOUND
    assert got["emptyList"].elem_type == servers.TAG_COMPOUND
    assert len(got["emptyList"]) == 0


def test_read_nbt_accepts_uncompressed(tmp_path):
    path = tmp_path / "raw.dat"
    tag = servers.Compound({"x": servers.Int(7)})
    path.write_bytes(servers.dumps_nbt(tag))  # 未压缩
    assert servers.read_nbt(path)["x"] == 7


def test_read_nbt_missing_and_corrupt(tmp_path):
    with pytest.raises(FileNotFoundError):
        servers.read_nbt(tmp_path / "nope.dat")
    corrupt = tmp_path / "corrupt.dat"
    corrupt.write_bytes(b"this is not nbt at all")
    with pytest.raises(ValueError):
        servers.read_nbt(corrupt)
    truncated = tmp_path / "truncated.dat"
    truncated.write_bytes(gzip.compress(b"\x0a\x00\x00"))  # 根 compound 后直接 EOF
    with pytest.raises(ValueError):
        servers.read_nbt(truncated)


# --------------------------------------------------------------------------- servers.dat
def test_servers_crud(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    assert servers.read_servers(game) == []

    servers.add_server(game, "测试服", "mc.example.com:25565")
    servers.add_server(game, "", "127.0.0.1")  # 名称留空时用地址
    path = game / "servers.dat"
    assert path.is_file()
    assert path.read_bytes()[:2] == b"\x1f\x8b"

    items = servers.read_servers(game)
    assert [item["name"] for item in items] == ["测试服", "127.0.0.1"]
    assert items[0]["ip"] == "mc.example.com:25565"
    assert items[0]["index"] == 0 and items[1]["index"] == 1

    servers.remove_server(game, 0)
    items = servers.read_servers(game)
    assert len(items) == 1 and items[0]["ip"] == "127.0.0.1"

    with pytest.raises(IndexError):
        servers.remove_server(game, 5)
    with pytest.raises(ValueError):
        servers.add_server(game, "坏", "  ")
    assert len(servers.read_servers(game)) == 1


def test_save_servers_preserves_unknown_fields(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    root = servers.Compound({
        "servers": servers.NbtList(servers.TAG_COMPOUND, [
            servers.Compound({
                "name": servers.String("老服务器"),
                "ip": servers.String("old.example.com"),
                "icon": servers.String("data:image/png;base64,AAA"),
                "acceptTextures": servers.Byte(1),
                "modExtra": servers.Int(42),  # 未知字段，必须原样保留
            })
        ]),
        "rootExtra": servers.Long(9),  # 根 compound 里的未知字段
    })
    servers.write_nbt(servers.servers_path(game), root)

    servers.add_server(game, "新服", "new.example.com")
    got = servers.read_servers(game)
    assert [item["name"] for item in got] == ["老服务器", "新服"]
    assert got[0]["icon"].startswith("data:image")
    assert got[0]["_nbt"]["modExtra"] == 42
    assert isinstance(got[0]["_nbt"]["modExtra"], servers.Int)

    reloaded = servers.read_nbt(servers.servers_path(game))
    assert reloaded["rootExtra"] == 9
    assert isinstance(reloaded["rootExtra"], servers.Long)
    assert reloaded["servers"][1]["ip"] == "new.example.com"


def test_save_servers_from_plain_dicts(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    result = servers.save_servers(game, [{"name": "A", "ip": "a.example.com"}])
    assert result[0]["name"] == "A" and result[0]["ip"] == "a.example.com"
    root = servers.read_nbt(servers.servers_path(game))
    assert isinstance(root["servers"], servers.NbtList)
    assert root["servers"].elem_type == servers.TAG_COMPOUND
    assert "acceptTextures" in root["servers"][0]


def test_corrupt_servers_dat_raises_readable_error(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    (game / "servers.dat").write_bytes(b"definitely-not-nbt")
    with pytest.raises(ValueError, match="servers.dat"):
        servers.read_servers(game)


# --------------------------------------------------------------------------- 页签元数据


def test_servers_page_wired_into_instance_manage():
    """服务器列表现在作为「实例管理」页的子页签提供。"""
    from mcdecent.gui.pages import instance_manage

    keys = [key for key, _text in instance_manage.TABS]
    assert "servers" in keys
