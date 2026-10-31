"""servers.dat 与最小 NBT 读写（纯标准库，gzip 压缩）。

Minecraft 的 ``servers.dat`` 与存档的 ``level.dat`` 都是「GZip 压缩的 NBT」。
这里实现一个够用的 NBT 编解码器，支持 compound / list / string / byte /
short / int / long / float / double / byte_array / int_array，读写保持大端
字节序与 gzip 压缩。

为了「读出来再写回去」不丢字段、不改类型，数值一律用 int/float 的子类包装
（``Byte``/``Short``/``Int``/``Long``/``Float``/``Double``），列表用
``NbtList`` 记录元素类型，compound 用 ``Compound``。这样即使遇到未知字段
（例如新版游戏新增的 ``acceptTextures``、模组加的键），也能原样保留。
"""

from __future__ import annotations

import gzip
import struct
import zlib
from pathlib import Path

from . import util

# --------------------------------------------------------------------------- 标签类型
TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11

TAG_NAMES = {
    TAG_END: "end", TAG_BYTE: "byte", TAG_SHORT: "short", TAG_INT: "int",
    TAG_LONG: "long", TAG_FLOAT: "float", TAG_DOUBLE: "double",
    TAG_BYTE_ARRAY: "byte_array", TAG_STRING: "string", TAG_LIST: "list",
    TAG_COMPOUND: "compound", TAG_INT_ARRAY: "int_array",
}

_SERVERS_FILE = "servers.dat"


class Tag:
    """显式带 NBT 类型的值基类。"""

    TAG_ID = -1


class Byte(int, Tag):
    TAG_ID = TAG_BYTE


class Short(int, Tag):
    TAG_ID = TAG_SHORT


class Int(int, Tag):
    TAG_ID = TAG_INT


class Long(int, Tag):
    TAG_ID = TAG_LONG


class Float(float, Tag):
    TAG_ID = TAG_FLOAT


class Double(float, Tag):
    TAG_ID = TAG_DOUBLE


class String(str, Tag):
    TAG_ID = TAG_STRING


class ByteArray(bytes, Tag):
    TAG_ID = TAG_BYTE_ARRAY


class IntArray(list, Tag):
    TAG_ID = TAG_INT_ARRAY


class Compound(dict, Tag):
    TAG_ID = TAG_COMPOUND


class NbtList(list, Tag):
    """NBT 列表；``elem_type`` 记录声明的元素类型（空列表也要能往返）。"""

    TAG_ID = TAG_LIST

    def __init__(self, elem_type: int | None = None, items=None):
        super().__init__(items or [])
        self.elem_type = elem_type


# --------------------------------------------------------------------------- 编码
def _encode_name(name: str) -> bytes:
    raw = str(name).encode("utf-8")
    if len(raw) > 0xFFFF:
        raise ValueError(f"NBT 名称过长：{name!r}")
    return struct.pack(">H", len(raw)) + raw


def _value_tag_id(value) -> int:
    """推断一个 Python 值的 NBT 类型（显式包装类优先）。"""
    tag_id = getattr(value, "TAG_ID", -1)
    if tag_id >= 0:
        return tag_id
    if isinstance(value, dict):
        return TAG_COMPOUND
    if isinstance(value, list):
        return TAG_LIST
    if isinstance(value, bool):
        return TAG_BYTE
    if isinstance(value, int):
        return TAG_INT
    if isinstance(value, float):
        return TAG_DOUBLE
    if isinstance(value, str):
        return TAG_STRING
    if isinstance(value, (bytes, bytearray)):
        return TAG_BYTE_ARRAY
    raise TypeError(f"无法编码为 NBT：{type(value).__name__}")


def _encode_payload(tag_id: int, value) -> bytes:
    if tag_id == TAG_BYTE:
        return struct.pack(">b", int(value))
    if tag_id == TAG_SHORT:
        return struct.pack(">h", int(value))
    if tag_id == TAG_INT:
        return struct.pack(">i", int(value))
    if tag_id == TAG_LONG:
        return struct.pack(">q", int(value))
    if tag_id == TAG_FLOAT:
        return struct.pack(">f", float(value))
    if tag_id == TAG_DOUBLE:
        return struct.pack(">d", float(value))
    if tag_id == TAG_BYTE_ARRAY:
        raw = bytes(value)
        return struct.pack(">i", len(raw)) + raw
    if tag_id == TAG_STRING:
        return _encode_name(value)
    if tag_id == TAG_INT_ARRAY:
        items = [int(v) for v in value]
        return struct.pack(">i", len(items)) + b"".join(struct.pack(">i", v) for v in items)
    if tag_id == TAG_LIST:
        elem_type = getattr(value, "elem_type", None)
        items = list(value)
        if elem_type is None or elem_type == TAG_END:
            elem_type = _value_tag_id(items[0]) if items else TAG_END
        for item in items:
            if _value_tag_id(item) != elem_type:
                raise TypeError(f"NBT 列表元素类型不一致：期望 {TAG_NAMES.get(elem_type)}，"
                                f"实际 {TAG_NAMES.get(_value_tag_id(item))}")
        out = [struct.pack(">bi", elem_type, len(items))]
        for item in items:
            out.append(_encode_payload(elem_type, item))
        return b"".join(out)
    if tag_id == TAG_COMPOUND:
        out = []
        for key, item in value.items():
            item_id = _value_tag_id(item)
            out.append(struct.pack(">b", item_id))
            out.append(_encode_name(key))
            out.append(_encode_payload(item_id, item))
        out.append(b"\x00")
        return b"".join(out)
    raise ValueError(f"不支持的 NBT 类型：{tag_id}")


def dumps_nbt(tag, *, root_name: str = "") -> bytes:
    """把 NBT 结构编码成未压缩字节（根标签名默认空）。"""
    tag_id = _value_tag_id(tag)
    return struct.pack(">b", tag_id) + _encode_name(root_name) + _encode_payload(tag_id, tag)


# --------------------------------------------------------------------------- 解码
class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise ValueError("NBT 数据不完整（文件可能已损坏）")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def i8(self) -> int:
        return struct.unpack(">b", self.read(1))[0]

    def i16(self) -> int:
        return struct.unpack(">h", self.read(2))[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.read(2))[0]

    def i32(self) -> int:
        return struct.unpack(">i", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack(">q", self.read(8))[0]

    def f32(self) -> float:
        return struct.unpack(">f", self.read(4))[0]

    def f64(self) -> float:
        return struct.unpack(">d", self.read(8))[0]

    def string(self) -> str:
        raw = self.read(self.u16())
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-8", "replace")

    def payload(self, tag_id: int):
        if tag_id == TAG_BYTE:
            return Byte(self.i8())
        if tag_id == TAG_SHORT:
            return Short(self.i16())
        if tag_id == TAG_INT:
            return Int(self.i32())
        if tag_id == TAG_LONG:
            return Long(self.i64())
        if tag_id == TAG_FLOAT:
            return Float(self.f32())
        if tag_id == TAG_DOUBLE:
            return Double(self.f64())
        if tag_id == TAG_BYTE_ARRAY:
            length = self.i32()
            if length < 0:
                raise ValueError("NBT byte_array 长度非法")
            return ByteArray(self.read(length))
        if tag_id == TAG_STRING:
            return String(self.string())
        if tag_id == TAG_INT_ARRAY:
            length = self.i32()
            if length < 0:
                raise ValueError("NBT int_array 长度非法")
            return IntArray(self.i32() for _ in range(length))
        if tag_id == TAG_LIST:
            elem_type = self.i8()
            length = self.i32()
            if length < 0:
                raise ValueError("NBT list 长度非法")
            items = NbtList(elem_type, [])
            if elem_type == TAG_END:
                return items
            for _ in range(length):
                items.append(self.payload(elem_type))
            return items
        if tag_id == TAG_COMPOUND:
            out = Compound()
            while True:
                child_id = self.i8()
                if child_id == TAG_END:
                    return out
                key = self.string()
                out[key] = self.payload(child_id)
        raise ValueError(f"未知的 NBT 标签类型：{tag_id}")

    def root(self):
        tag_id = self.i8()
        if tag_id == TAG_END:
            raise ValueError("NBT 根标签为空")
        self.string()  # 根标签名（原版通常为空字符串）
        return self.payload(tag_id)


def loads_nbt(data: bytes):
    """解码未压缩的 NBT 字节。"""
    return _Reader(data).root()


# --------------------------------------------------------------------------- 文件读写
def _decompress(raw: bytes) -> bytes:
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if raw[:1] == b"\x78":  # 某些工具会用 zlib
        try:
            return zlib.decompress(raw)
        except zlib.error:
            pass
    return raw


def read_nbt(path) -> dict:
    """读取 NBT 文件（servers.dat / level.dat），自动处理 gzip 压缩。"""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(f"读取 NBT 文件失败：{path}（{exc}）") from exc
    if not raw:
        raise ValueError(f"NBT 文件为空：{path}")
    try:
        data = _decompress(raw)
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError(f"NBT 文件解压失败：{path}（{exc}）") from exc
    try:
        root = loads_nbt(data)
    except (ValueError, struct.error, EOFError) as exc:
        raise ValueError(f"NBT 文件解析失败：{path}（{exc}）") from exc
    if not isinstance(root, dict):
        raise ValueError(f"NBT 根标签不是 compound：{path}")
    return root


def write_nbt(path, tag, *, compress: bool = True) -> Path:
    """把 NBT 结构写回文件；默认保持 gzip 压缩。"""
    payload = dumps_nbt(tag)
    if compress:
        payload = gzip.compress(payload, compresslevel=6)
    return util.atomic_write_bytes(path, payload)


def server_compound(name: str, ip: str, **extra) -> Compound:
    """构造一条服务器记录（新条目默认接受服务器资源包）。"""
    item = Compound({
        "name": String(str(name or ip or "")),
        "ip": String(str(ip or "")),
        "acceptTextures": Byte(1),
    })
    for key, value in extra.items():
        item[key] = value
    return item


# --------------------------------------------------------------------------- servers.dat
def servers_path(game_dir) -> Path:
    return Path(game_dir) / _SERVERS_FILE


def _as_text(value) -> str:
    if isinstance(value, str):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def read_servers(game_dir) -> list[dict]:
    """读取 ``servers.dat``，返回 [{name, ip, icon, accept_textures, index, _nbt}]。"""
    path = servers_path(game_dir)
    if not path.is_file():
        return []
    root = read_nbt(path)
    raw = root.get("servers")
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        accept = item.get("acceptTextures")
        out.append({
            "name": _as_text(item.get("name")),
            "ip": _as_text(item.get("ip")),
            "icon": _as_text(item.get("icon")),
            "accept_textures": int(accept) if isinstance(accept, int) else 0,
            "index": index,
            "_nbt": item,
        })
    return out


def save_servers(game_dir, servers) -> list[dict]:
    """写回服务器列表；保留原有的未知字段与根 compound 里的其它键。"""
    path = servers_path(game_dir)
    root = Compound()
    if path.is_file():
        root = read_nbt(path)  # 损坏时抛 ValueError，避免静默丢数据
    items = NbtList(TAG_COMPOUND, [])
    for entry in servers or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("_nbt")
        item = Compound(raw) if isinstance(raw, dict) else Compound()
        item["name"] = String(str(entry.get("name") or ""))
        item["ip"] = String(str(entry.get("ip") or ""))
        if "acceptTextures" not in item:
            item["acceptTextures"] = Byte(1)
        items.append(item)
    root["servers"] = items
    util.ensure_dir(path.parent)
    write_nbt(path, root)
    return read_servers(game_dir)


def add_server(game_dir, name: str, ip: str) -> list[dict]:
    """新增一条服务器记录；没有 servers.dat 时直接创建。"""
    name = (name or "").strip()
    ip = (ip or "").strip()
    if not ip:
        raise ValueError("服务器地址不能为空")
    servers = read_servers(game_dir)
    servers.append({"name": name or ip, "ip": ip, "_nbt": server_compound(name or ip, ip)})
    return save_servers(game_dir, servers)


def remove_server(game_dir, index: int) -> list[dict]:
    """按序号删除服务器记录，返回删除后的列表。"""
    servers = read_servers(game_dir)
    try:
        index = int(index)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"服务器序号无效：{index!r}") from exc
    if index < 0 or index >= len(servers):
        raise IndexError(f"服务器序号越界：{index}（共 {len(servers)} 条）")
    del servers[index]
    return save_servers(game_dir, servers)
