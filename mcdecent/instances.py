"""实例（Instance）：一个游戏版本 + 一套独立的游戏目录。

对应 HMCL 的「版本隔离」：版本文件（client jar、libraries、assets、Java）全局共享，
但 `mods/ saves/ resourcepacks/ shaderpacks/ config/ options.txt` 等按实例隔离，
避免不同版本/整合包的模组互相污染。

存储：``<home>/instances.json``（列表 + 默认实例），隔离实例的目录在
``<home>/instances/<实例名>/``。兼容旧数据：第一次使用会创建一个名为「默认」的
**共享目录**实例，其 game_dir 仍是 ``<home>/game``，所以之前的模组和存档不会丢。
"""

from __future__ import annotations

import copy
import shutil
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import util, versions
from .config import Config

INSTANCES_FILE = "instances.json"
DEFAULT_INSTANCE_NAME = "默认"
_INVALID_CHARS = '<>:"/\\|?*'


@dataclass
class Instance:
    name: str
    version_id: str
    isolated: bool = True
    game_dir: str | None = None  # 自定义游戏目录；None 时按 isolated 规则推导
    java_path: str | None = None
    memory_mb: int | None = None
    min_memory_mb: int | None = None
    jvm_args: list[str] = field(default_factory=list)
    game_args: list[str] = field(default_factory=list)
    pre_launch: str | None = None
    icon: str | None = None
    notes: str = ""
    created_at: str = field(default_factory=util.now_iso)
    last_played_at: str | None = None
    play_time_seconds: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Instance":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def summary(self) -> dict:
        return {
            "name": self.name,
            "version": self.version_id,
            "isolated": self.isolated,
            "game_dir": self.game_dir,
            "memory_mb": self.memory_mb,
            "java": self.java_path,
            "last_played_at": self.last_played_at,
            "play_time_seconds": self.play_time_seconds,
            "id": self.id,
        }


class InstanceManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.file = Path(cfg.root) / INSTANCES_FILE
        self._items: list[Instance] = []
        self._default: str | None = None
        self._load()

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        data = util.read_json(self.file, {})
        if not isinstance(data, dict) or not data.get("items"):
            return
        self._default = data.get("default")
        for item in data.get("items") or []:
            try:
                self._items.append(Instance.from_dict(item))
            except (TypeError, ValueError):
                continue

    def save(self) -> None:
        util.write_json(self.file, {
            "version": 1,
            "default": self._default,
            "items": [item.to_dict() for item in self._items],
        })

    # ------------------------------------------------------------ 查询
    def list(self) -> list[Instance]:
        return list(self._items)

    def names(self) -> list[str]:
        return [item.name for item in self._items]

    def get(self, name: str | None) -> Instance | None:
        if not name:
            return None
        for item in self._items:
            if item.name == name or item.id == name:
                return item
        return None

    def default(self) -> Instance | None:
        inst = self.get(self._default)
        if inst:
            return inst
        return self._items[0] if self._items else None

    def ensure_default(self) -> Instance:
        """兼容旧数据：没有实例时创建一个共享目录的「默认」实例。"""
        if self._items:
            inst = self.default()
            if inst is not None:
                return inst
        version = self.cfg.get("default_version")
        if not version:
            installed = versions.installed_versions(self.cfg)
            version = installed[0]["id"] if installed else ""
        inst = Instance(
            name=DEFAULT_INSTANCE_NAME,
            version_id=version or "",
            isolated=False,
            game_dir=str(self.cfg.game_dir),
        )
        self._items = [inst]
        self._default = inst.name
        self.save()
        return inst

    # ------------------------------------------------------------ 创建 / 修改
    def _unique_name(self, base: str) -> str:
        base = (base or "实例").strip() or "实例"
        name = base
        index = 2
        while self.get(name):
            name = f"{base} {index}"
            index += 1
        return name

    @staticmethod
    def safe_dirname(name: str) -> str:
        cleaned = "".join(ch for ch in name if ch not in _INVALID_CHARS).strip().strip(".")
        return cleaned or "instance"

    def create(
        self,
        name: str,
        version_id: str,
        *,
        isolated: bool = True,
        copy_from: Instance | None = None,
        game_dir: str | None = None,
    ) -> Instance:
        name = self._unique_name(name)
        inst = Instance(
            name=name,
            version_id=str(version_id),
            isolated=isolated,
            game_dir=str(game_dir) if game_dir else None,
        )
        if copy_from is not None:
            inst.java_path = copy_from.java_path
            inst.memory_mb = copy_from.memory_mb
            inst.min_memory_mb = copy_from.min_memory_mb
            inst.jvm_args = list(copy_from.jvm_args)
            inst.game_args = list(copy_from.game_args)
            inst.pre_launch = copy_from.pre_launch
            inst.icon = copy_from.icon
            inst.notes = copy_from.notes
        self._items.append(inst)
        self.ensure_dirs(inst)
        if copy_from is not None:
            self._copy_game_files(copy_from, inst)
        if self._default is None:
            self._default = inst.name
        self.save()
        return inst

    def _copy_game_files(self, src: Instance, dst: Instance) -> None:
        """复制整合包意义上的文件（mods/config/options.txt），不含存档。"""
        src_dir, dst_dir = self.game_dir(src), self.game_dir(dst)
        if src_dir == dst_dir:
            return
        for rel in ("mods", "config", "options.txt", "servers.dat"):
            source = src_dir / rel
            if not source.exists():
                continue
            target = dst_dir / rel
            try:
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    util.ensure_dir(target.parent)
                    shutil.copy2(source, target)
            except OSError as exc:
                util.LOG.warning("复制实例文件失败 %s：%r", source, exc)

    def update(self, inst: Instance) -> None:
        for index, item in enumerate(self._items):
            if item.id == inst.id or item.name == inst.name:
                self._items[index] = inst
                break
        else:
            self._items.append(inst)
        if inst.name == self._default:
            self.cfg.set("default_version", inst.version_id)
        self.save()

    def delete(self, name: str, *, delete_files: bool = False) -> bool:
        inst = self.get(name)
        if not inst:
            return False
        self._items = [item for item in self._items if item.id != inst.id]
        if self._default == inst.name:
            self._default = self._items[0].name if self._items else None
        if delete_files:
            directory = self.game_dir(inst)
            instances_root = (Path(self.cfg.root) / "instances").resolve()
            try:
                resolved = directory.resolve()
                if instances_root in resolved.parents:
                    shutil.rmtree(resolved, ignore_errors=True)
            except OSError:
                pass
        self.save()
        return True

    def rename(self, name: str, new_name: str) -> Instance | None:
        inst = self.get(name)
        if not inst:
            return None
        new_name = self._unique_name(new_name)
        old_dir = self.game_dir(inst)
        inst.name = new_name
        if inst.isolated and not inst.game_dir:
            new_dir = self.default_isolated_dir(inst)
            try:
                util.ensure_dir(new_dir.parent)
                if old_dir.exists() and not new_dir.exists():
                    old_dir.rename(new_dir)
            except OSError as exc:
                util.LOG.warning("重命名实例目录失败：%r", exc)
                inst.isolated = False
        if self._default == name:
            self._default = new_name
        self.update(inst)
        return inst

    def set_default(self, name: str) -> Instance:
        inst = self.get(name)
        if not inst:
            raise KeyError(f"实例不存在：{name}")
        self._default = inst.name
        self.cfg.set("default_version", inst.version_id)
        self.cfg.save()
        self.save()
        return inst

    def mark_played(self, inst: Instance, seconds: int = 0) -> None:
        inst.last_played_at = util.now_iso()
        inst.play_time_seconds = int(inst.play_time_seconds or 0) + max(0, int(seconds))
        self.update(inst)

    # ------------------------------------------------------------ 路径
    def default_isolated_dir(self, inst: Instance) -> Path:
        return Path(self.cfg.root) / "instances" / self.safe_dirname(inst.name)

    def game_dir(self, inst: Instance | None) -> Path:
        if inst is None:
            return Path(self.cfg.game_dir)
        if inst.game_dir:
            return Path(inst.game_dir).expanduser()
        if inst.isolated:
            return self.default_isolated_dir(inst)
        return Path(self.cfg.game_dir)

    def ensure_dirs(self, inst: Instance) -> Path:
        directory = self.game_dir(inst)
        for sub in ("mods", "saves", "resourcepacks", "shaderpacks", "config", "logs", "screenshots"):
            util.ensure_dir(directory / sub)
        return directory

    def mods_dir(self, inst: Instance | None) -> Path:
        return self.game_dir(inst) / "mods"

    def saves_dir(self, inst: Instance | None) -> Path:
        return self.game_dir(inst) / "saves"

    def resourcepacks_dir(self, inst: Instance | None) -> Path:
        return self.game_dir(inst) / "resourcepacks"

    def shaderpacks_dir(self, inst: Instance | None) -> Path:
        return self.game_dir(inst) / "shaderpacks"

    def config_for(self, inst: Instance | None) -> Config:
        """返回一个把 game_dir 指向该实例的配置副本（供 mods/pack 等按目录工作的模块使用）。"""
        cfg = copy.copy(self.cfg)
        cfg.set_game_dir(self.game_dir(inst))
        return cfg

    def describe(self, inst: Instance) -> dict:
        loader = None
        try:
            loader = versions.read_loader_meta(self.cfg, inst.version_id)
        except Exception:  # noqa: BLE001
            pass
        directory = self.game_dir(inst)
        return {
            **inst.summary(),
            "loader": loader,
            "dir": str(directory),
            "dir_exists": directory.is_dir(),
            "mods": len(list((directory / "mods").glob("*.jar"))) if (directory / "mods").is_dir() else 0,
            "saves": len(list((directory / "saves").iterdir())) if (directory / "saves").is_dir() else 0,
            "resourcepacks": len(list((directory / "resourcepacks").iterdir())) if (directory / "resourcepacks").is_dir() else 0,
            "shaderpacks": len(list((directory / "shaderpacks").iterdir())) if (directory / "shaderpacks").is_dir() else 0,
        }


def default_isolation(cfg: Config, version_id: str) -> bool:
    """按设置里的「默认版本隔离策略」决定新实例是否隔离。

    always（默认）/ never / modded（只有带加载器的版本才隔离）。
    """
    policy = str(cfg.get("default_isolation_policy") or ("always" if cfg.get("default_isolation", True) else "never"))
    policy = policy.strip().lower()
    if policy in ("never", "false", "0"):
        return False
    if policy in ("modded", "mod"):
        return any(key in (version_id or "").lower()
                   for key in ("fabric", "quilt", "forge", "neoforge", "optifine"))
    return True


def format_play_time(seconds: int | None) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds} 秒"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"
