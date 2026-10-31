"""资源包 / 光影 / 存档管理（对齐 HMCL 的资源管理功能）。

- 资源包：扫描 ``resourcepacks/``（zip 与目录），读 ``pack.mcmeta`` 取格式与
  描述；通过 ``options.txt`` 的 ``resourcePacks`` 行启用/禁用。
- 光影：扫描 ``shaderpacks/``，通过 ``config/iris.properties``（或 OptiFine 的
  ``optionsshaders.txt``）的 ``shaderPack`` 键记录当前启用项。
- 存档：扫描 ``saves/*/level.dat``，用自写的 NBT 解析器读世界名/最后游玩时间/
  版本；支持备份（zip 到 ``<game_dir>/../backups/saves``）、删除、导入世界 zip。

所有函数都接收 ``game_dir``（当前实例的游戏目录），不依赖全局状态。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from . import util
from .servers import read_nbt  # level.dat 复用 servers.py 里的 NBT 解析

RESOURCEPACKS_DIR = "resourcepacks"
SHADERPACKS_DIR = "shaderpacks"
SAVES_DIR = "saves"
OPTIONS_FILE = "options.txt"
PACK_MCMETA = "pack.mcmeta"

_IRIS_CONFIG = Path("config") / "iris.properties"
_OPTIFINE_CONFIG = Path("optionsshaders.txt")
_SHADER_OFF = {"", "off", "none", "internal", "(internal)", "off.zip"}
_INVALID_NAME_CHARS = '<>:"/\\|?*'


# --------------------------------------------------------------------------- 通用
def _text(value) -> str:
    """把 JSON/NBT 里的「文本组件」归一成字符串。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "translate", "fallback", "value"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        extra = value.get("extra")
        if isinstance(extra, list):
            parts = [_text(item) for item in extra]
            joined = "".join(part for part in parts if part)
            if joined:
                return joined
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    return str(value)


def _dir_size(path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _safe_child(directory, name: str) -> Path:
    """把「包名」变成目录下的子路径，拒绝路径穿越。"""
    name = str(name or "")
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"名称非法：{name!r}")
    return Path(directory) / name


def _unique_name(directory, base: str, suffix: str) -> str:
    """避免重名：``x.zip`` 已存在时依次尝试 ``x-2.zip``、``x-3.zip`` …"""
    base = (base or "pack").strip().strip(".") or "pack"
    candidate = f"{base}{suffix}"
    if not (Path(directory) / candidate).exists():
        return candidate
    index = 2
    while (Path(directory) / f"{base}-{index}{suffix}").exists():
        index += 1
    return f"{base}-{index}{suffix}"


def _find_mcmeta(names) -> str | None:
    normalized = [name.replace("\\", "/") for name in names]
    candidates = [
        name for name in normalized
        if name.rstrip("/") == PACK_MCMETA or name.rstrip("/").endswith("/" + PACK_MCMETA)
    ]
    if not candidates:
        return None
    return min(candidates, key=len)


def _parse_pack_meta(data) -> dict:
    """从 pack.mcmeta 的 JSON 里取 {format, description}（兼容旧/新格式）。"""
    if not isinstance(data, dict):
        return {}
    pack = data.get("pack")
    if not isinstance(pack, dict):
        pack = data
    fmt = pack.get("pack_format")
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        fmt = None
        supported = pack.get("supported_formats")
        if isinstance(supported, int) and not isinstance(supported, bool):
            fmt = supported
        elif isinstance(supported, list) and supported and isinstance(supported[0], int):
            fmt = supported[0]
        elif isinstance(supported, dict):
            value = supported.get("min_inclusive")
            fmt = value if isinstance(value, int) and not isinstance(value, bool) else None
    return {"format": fmt, "description": _text(pack.get("description"))}


def _zip_meta(path) -> tuple[dict, str]:
    """读 zip 内的 pack.mcmeta。返回 (meta, 错误说明)。"""
    try:
        with zipfile.ZipFile(path) as zf:
            target = _find_mcmeta(zf.namelist())
            if target is None:
                return {}, "压缩包内没有 pack.mcmeta"
            raw = zf.read(target)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return {}, f"无法读取压缩包：{exc}"
    try:
        data = json.loads(raw.decode("utf-8-sig", "replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {}, f"pack.mcmeta 不是合法 JSON：{exc}"
    return _parse_pack_meta(data), ""


def _dir_meta(path) -> tuple[dict, str]:
    meta_file = Path(path) / PACK_MCMETA
    if not meta_file.is_file():
        return {}, "目录内没有 pack.mcmeta"
    try:
        raw = meta_file.read_text(encoding="utf-8-sig", errors="replace")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        return {}, f"pack.mcmeta 读取失败：{exc}"
    return _parse_pack_meta(data), ""


def _pack_entry(path, enabled_names: set[str]) -> dict:
    path = Path(path)
    kind = "dir" if path.is_dir() else "zip"
    try:
        meta, error = _dir_meta(path) if kind == "dir" else _zip_meta(path)
    except OSError as exc:  # 权限/损坏等
        meta, error = {}, f"读取失败：{exc}"
    if kind == "dir":
        size = _dir_size(path)
    else:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
    return {
        "name": path.name,
        "path": str(path),
        "size": size,
        "format": meta.get("format"),
        "description": meta.get("description") or "",
        "kind": kind,
        "enabled": f"file/{path.name}" in enabled_names,
        "error": error,
    }


# --------------------------------------------------------------------------- options.txt
def options_file(game_dir) -> Path:
    return Path(game_dir) / OPTIONS_FILE


def _read_lines(path) -> list[str]:
    """读文本行；文件不存在返回空列表，读取失败则抛错（避免后续把文件写空）。"""
    path = Path(path)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return text.splitlines()


def _write_lines(path, lines: list[str]) -> None:
    util.atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def _split_option(line: str) -> tuple[str, str] | None:
    if ":" not in line:
        return None
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _parse_pack_list(value: str) -> list[str]:
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, list):
        return [str(item) for item in data if isinstance(item, (str, int, float))]
    # 容错：容错（单引号、尾逗号等）时退回正则
    return [item for item in re.findall(r"\"([^\"]*)\"", value or "")]


def enabled_packs(game_dir) -> list[str]:
    """读取 options.txt 中已启用的资源包条目（如 ``file/xxx.zip``、``vanilla``）。"""
    for line in _read_lines(options_file(game_dir)):
        parsed = _split_option(line)
        if parsed and parsed[0] == "resourcePacks":
            packs = _parse_pack_list(parsed[1])
            # 去重但保持顺序
            seen, out = set(), []
            for item in packs:
                if item not in seen:
                    seen.add(item)
                    out.append(item)
            return out
    return []


def _normalize_pack_name(name: str) -> str:
    name = str(name or "").strip()
    if name.startswith("file/"):
        name = name[len("file/"):]
    return name


def set_resourcepack_enabled(game_dir, name: str, enabled: bool) -> list[str]:
    """精确修改 options.txt 的 resourcePacks 行，保留其它所有行。"""
    target_name = _normalize_pack_name(name)
    if not target_name:
        raise ValueError("资源包名称不能为空")
    path = options_file(game_dir)
    lines = _read_lines(path)
    packs: list[str] | None = None
    line_index: int | None = None
    for index, line in enumerate(lines):
        parsed = _split_option(line)
        if parsed and parsed[0] == "resourcePacks":
            try:
                raw = json.loads(parsed[1])
            except (ValueError, TypeError):
                raw = None
            if isinstance(raw, list):
                packs = [str(item) for item in raw if isinstance(item, (str, int, float))]
            else:
                packs = _parse_pack_list(parsed[1])
            line_index = index
            break
    if packs is None:
        if not path.is_file() and not enabled:
            return []  # 没有 options.txt 且只是禁用：不凭空创建
        packs = []
    entry = f"file/{target_name}"
    if enabled:
        if entry not in packs:
            packs.append(entry)
    else:
        packs = [item for item in packs if item != entry]
    new_line = "resourcePacks:" + json.dumps(packs, ensure_ascii=False, separators=(",", ":"))
    if line_index is None:
        lines.append(new_line)
    else:
        lines[line_index] = new_line
    if packs or path.is_file() or enabled:
        _write_lines(path, lines)
    return packs


# --------------------------------------------------------------------------- 资源包
def resourcepacks_dir(game_dir) -> Path:
    return Path(game_dir) / RESOURCEPACKS_DIR


def list_resourcepacks(game_dir) -> list[dict]:
    """列出资源包（zip 与目录），带格式/描述/启用状态。"""
    directory = resourcepacks_dir(game_dir)
    if not directory.is_dir():
        return []
    enabled_names = set(enabled_packs(game_dir))
    items: list[dict] = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            items.append(_pack_entry(entry, enabled_names))
        elif entry.is_file() and entry.suffix.lower() == ".zip":
            items.append(_pack_entry(entry, enabled_names))
    return items


def install_resourcepack(game_dir, src) -> dict:
    """导入本地资源包（zip 或目录）；校验 pack.mcmeta，重名自动加 ``-2``。"""
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(f"找不到要导入的资源包：{src}")
    dest_dir = util.ensure_dir(resourcepacks_dir(game_dir))
    if src.is_dir():
        _meta, error = _dir_meta(src)
        if error:
            raise ValueError(f"不是有效的资源包：{error}")
        name = _unique_name(dest_dir, src.name, "")
        target = dest_dir / name
        try:
            shutil.copytree(src, target)
        except OSError as exc:
            util.remove_tree(target)
            raise ValueError(f"复制资源包失败：{exc}") from exc
    else:
        if src.suffix.lower() != ".zip":
            raise ValueError(f"只支持 .zip 或目录形式的资源包：{src.name}")
        _meta, error = _zip_meta(src)
        if error:
            raise ValueError(f"不是有效的资源包：{error}")
        name = _unique_name(dest_dir, src.stem, ".zip")
        target = dest_dir / name
        try:
            shutil.copy2(src, target)
        except OSError as exc:
            util.remove_tree(target)
            raise ValueError(f"复制资源包失败：{exc}") from exc
    return _pack_entry(target, set(enabled_packs(game_dir)))


def remove_resourcepack(game_dir, name: str) -> bool:
    """删除资源包，并把它从 options.txt 的启用列表里移除。"""
    clean = _normalize_pack_name(name)
    directory = resourcepacks_dir(game_dir)
    target = _safe_child(directory, clean)
    if not target.exists():
        raise FileNotFoundError(f"资源包不存在：{name}")
    util.remove_tree(target)
    if options_file(game_dir).is_file():
        set_resourcepack_enabled(game_dir, clean, False)
    return True


# --------------------------------------------------------------------------- 光影
def shaderpacks_dir(game_dir) -> Path:
    return Path(game_dir) / SHADERPACKS_DIR


def _shader_settings_path(game_dir, *, create: bool = False) -> Path | None:
    game_dir = Path(game_dir)
    for rel in (_IRIS_CONFIG, _OPTIFINE_CONFIG):
        candidate = game_dir / rel
        if candidate.is_file():
            return candidate
    return game_dir / _IRIS_CONFIG if create else None


def _properties(lines: list[str]) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    return props


def shader_pack_enabled(game_dir) -> str | None:
    """当前启用的光影文件名；没有配置文件时返回 None（表示无法判断）。"""
    path = _shader_settings_path(game_dir)
    if path is None:
        return None
    value = _properties(_read_lines(path)).get("shaderPack", "")
    return value


def list_shaderpacks(game_dir) -> list[dict]:
    """列出光影包；enabled 为 True/False，配置不支持时为 None。"""
    directory = shaderpacks_dir(game_dir)
    config = _shader_settings_path(game_dir)
    current = _properties(_read_lines(config)).get("shaderPack", "") if config else ""
    supported = config is not None
    if not directory.is_dir():
        return []
    items: list[dict] = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
        if entry.name.startswith("."):
            continue
        if not (entry.is_dir() or (entry.is_file() and entry.suffix.lower() == ".zip")):
            continue
        kind = "dir" if entry.is_dir() else "zip"
        size = _dir_size(entry) if kind == "dir" else entry.stat().st_size
        if not supported:
            enabled: bool | None = None
        else:
            value = (current or "").strip()
            enabled = bool(value) and value.lower() not in _SHADER_OFF and (
                value == entry.name
                or Path(value).name == entry.name
                or Path(value).stem == entry.stem
            )
        items.append({"name": entry.name, "path": str(entry), "size": size,
                      "kind": kind, "enabled": enabled})
    return items


def set_shaderpack_enabled(game_dir, name: str | None) -> str:
    """写入 ``shaderPack`` 键（name 为空表示关闭光影）；文件不存在时创建。"""
    path = _shader_settings_path(game_dir, create=True)
    assert path is not None
    created = not path.is_file()
    value = Path(str(name)).name if name else ""
    lines = _read_lines(path)
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("#", "!")) or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key == "shaderPack":
            lines[index] = f"shaderPack={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"shaderPack={value}")
    if created or path == Path(game_dir) / _IRIS_CONFIG:
        _write_enable_shaders(lines, enabled=bool(value))
    _write_lines(path, lines)
    return value


def _write_enable_shaders(lines: list[str], *, enabled: bool) -> None:
    """Iris 需要 enableShaders=true；新配置文件里顺手补上，已有则同步。"""
    wanted = "true" if enabled else "false"
    for index, line in enumerate(lines):
        if "=" not in line or line.strip().startswith(("#", "!")):
            continue
        if line.split("=", 1)[0].strip() == "enableShaders":
            lines[index] = f"enableShaders={wanted}"
            return
    lines.append(f"enableShaders={wanted}")


def install_shaderpack(game_dir, src) -> dict:
    """导入光影包（zip 或目录）；要求包含 shaders 目录。"""
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(f"找不到要导入的光影包：{src}")
    if src.is_dir():
        if not (src / "shaders").is_dir():
            raise ValueError("不是有效的光影包：目录内没有 shaders 文件夹")
        dest_dir = util.ensure_dir(shaderpacks_dir(game_dir))
        name = _unique_name(dest_dir, src.name, "")
        target = dest_dir / name
        try:
            shutil.copytree(src, target)
        except OSError as exc:
            util.remove_tree(target)
            raise ValueError(f"复制光影包失败：{exc}") from exc
    else:
        if src.suffix.lower() != ".zip":
            raise ValueError(f"只支持 .zip 或目录形式的光影包：{src.name}")
        try:
            with zipfile.ZipFile(src) as zf:
                has_shaders = any(
                    "shaders" in name.replace("\\", "/").split("/")
                    for name in zf.namelist()
                )
        except (OSError, zipfile.BadZipFile) as exc:
            raise ValueError(f"无法读取光影压缩包：{exc}") from exc
        if not has_shaders:
            raise ValueError("不是有效的光影包：压缩包内没有 shaders 文件夹")
        dest_dir = util.ensure_dir(shaderpacks_dir(game_dir))
        name = _unique_name(dest_dir, src.stem, ".zip")
        target = dest_dir / name
        try:
            shutil.copy2(src, target)
        except OSError as exc:
            util.remove_tree(target)
            raise ValueError(f"复制光影包失败：{exc}") from exc
    return {"name": target.name, "path": str(target), "kind": "dir" if target.is_dir() else "zip",
            "size": _dir_size(target) if target.is_dir() else target.stat().st_size,
            "enabled": False}


def remove_shaderpack(game_dir, name: str) -> bool:
    """删除光影包；如果它正在启用则把 shaderPack 清空。"""
    clean = str(name or "").strip()
    directory = shaderpacks_dir(game_dir)
    target = _safe_child(directory, clean)
    if not target.exists():
        raise FileNotFoundError(f"光影包不存在：{name}")
    was_enabled = False
    config = _shader_settings_path(game_dir)
    if config is not None:
        value = _properties(_read_lines(config)).get("shaderPack", "")
        was_enabled = bool(value) and value.lower() not in _SHADER_OFF and (
            value == clean or Path(value).name == clean or Path(value).stem == Path(clean).stem)
    util.remove_tree(target)
    if was_enabled:
        set_shaderpack_enabled(game_dir, None)
    return True


# --------------------------------------------------------------------------- 存档
def saves_dir(game_dir) -> Path:
    return Path(game_dir) / SAVES_DIR


def _format_epoch_ms(value) -> str:
    try:
        seconds = float(value) / 1000.0
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))
    except (OverflowError, OSError, ValueError):
        return ""


def _version_text(value) -> str:
    if isinstance(value, dict):
        for key in ("Name", "Id"):
            item = value.get(key)
            if isinstance(item, str) and item:
                return item
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                return str(item)
        return ""
    return _text(value)


def read_level_summary(level_dat) -> dict:
    """解析 level.dat（复用 servers.read_nbt），返回世界名/最后游玩/版本。"""
    root = read_nbt(level_dat)
    data = root.get("Data")
    if not isinstance(data, dict):
        data = root
    return {
        "level_name": _text(data.get("LevelName")),
        "last_played": _format_epoch_ms(data.get("LastPlayed")),
        "version": _version_text(data.get("Version")),
    }


def _save_entry(world) -> dict:
    world = Path(world)
    level = world / "level.dat"
    info: dict = {}
    broken = False
    error = ""
    if level.is_file():
        try:
            info = read_level_summary(level)
        except (ValueError, OSError) as exc:
            broken, error = True, str(exc)
    else:
        broken, error = True, "缺少 level.dat"
    return {
        "name": world.name,
        "level_name": info.get("level_name") or world.name,
        "last_played": info.get("last_played") or "",
        "version": info.get("version") or "",
        "size": _dir_size(world),
        "path": str(world),
        "broken": broken,
        "error": error,
    }


def list_saves(game_dir) -> list[dict]:
    """列出存档：扫描 ``saves/*/level.dat``，坏档也列出来（broken=True）。"""
    directory = saves_dir(game_dir)
    if not directory.is_dir():
        return []
    items: list[dict] = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        items.append(_save_entry(entry))
    items.sort(key=lambda item: (item["broken"], item["name"].casefold()))
    return items


def _world_dir(game_dir, name: str) -> Path:
    return _safe_child(saves_dir(game_dir), str(name or "").strip())


def backup_save(game_dir, name: str) -> Path:
    """把存档 zip 到 ``<game_dir>/../backups/saves/<name>-<时间戳>.zip``。"""
    world = _world_dir(game_dir, name)
    if not world.is_dir():
        raise FileNotFoundError(f"存档不存在：{name}")
    if not (world / "level.dat").is_file():
        raise ValueError(f"存档不完整（缺少 level.dat），无法备份：{name}")
    game_dir = Path(game_dir).expanduser()
    backups = game_dir.parent / "backups" / SAVES_DIR
    util.ensure_dir(backups)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backups / f"{world.name}-{stamp}.zip"
    index = 2
    while target.exists():
        target = backups / f"{world.name}-{stamp}-{index}.zip"
        index += 1
    util.zip_dir(world, target)
    return target


def delete_save(game_dir, name: str) -> bool:
    """删除存档目录。"""
    world = _world_dir(game_dir, name)
    if not world.is_dir():
        raise FileNotFoundError(f"存档不存在：{name}")
    util.remove_tree(world)
    return True


def _unique_dir_name(directory, base: str) -> str:
    base = "".join(ch for ch in str(base or "") if ch not in _INVALID_NAME_CHARS)
    base = base.strip().strip(".") or "world"
    if not (Path(directory) / base).exists():
        return base
    index = 2
    while (Path(directory) / f"{base}-{index}").exists():
        index += 1
    return f"{base}-{index}"


def import_world(game_dir, zip_path) -> dict:
    """导入世界 zip：解压到 saves/，要求存在 level.dat，拒绝路径穿越。"""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise FileNotFoundError(f"找不到存档压缩包：{zip_path}")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [name for name in zf.namelist() if name and not name.endswith("/")]
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"不是有效的 zip 压缩包：{zip_path.name}（{exc}）") from exc
    if not names:
        raise ValueError("压缩包是空的，没有存档文件")
    normalized: list[str] = []
    for name in names:
        norm = name.replace("\\", "/")
        parts = norm.split("/")
        if norm.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", norm):
            raise ValueError(f"压缩包包含非法路径，已拒绝导入：{name}")
        normalized.append(norm)
    tops = {name.split("/")[0] for name in normalized}
    single_top = len(tops) == 1
    saves = util.ensure_dir(saves_dir(game_dir))
    tmp = Path(tempfile.mkdtemp(prefix=".mcdecent-import-", dir=str(saves)))
    try:
        util.safe_extract_zip(zip_path, tmp, skip_meta=False)
        top_name = next(iter(tops))
        if single_top and (tmp / top_name / "level.dat").is_file():
            source, base = tmp / top_name, top_name
        elif (tmp / "level.dat").is_file():
            source, base = tmp, zip_path.stem
        else:
            raise ValueError("压缩包里没有 level.dat，不是有效的存档")
        target = saves / _unique_dir_name(saves, base)
        shutil.move(str(source), str(target))
    finally:
        util.remove_tree(tmp)
    return _save_entry(target)
