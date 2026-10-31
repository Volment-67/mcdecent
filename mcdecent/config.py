"""配置与目录布局。

默认根目录 ~/.mcdecent，可用 MCDECENT_HOME 环境变量或 --home 覆盖。
配置为 JSON，可手工编辑；命令行选项优先级最高。
"""

from __future__ import annotations

import copy
import os
import platform
from pathlib import Path
from typing import Any

from . import util

# 官方源（默认直连）之外的镜像。hosts 形如 {源主机: [镜像基址, 追加路径前缀]}。
DEFAULT_MIRRORS: list[dict] = [
    {
        "name": "bmclapi",
        "enabled": True,
        "hosts": {
            "piston-meta.mojang.com": ["https://bmclapi2.bangbang93.com", ""],
            "launchermeta.mojang.com": ["https://bmclapi2.bangbang93.com", ""],
            "piston-data.mojang.com": ["https://bmclapi2.bangbang93.com", ""],
            "libraries.minecraft.net": ["https://bmclapi2.bangbang93.com", "/maven"],
            "resources.download.minecraft.net": ["https://bmclapi2.bangbang93.com", "/assets"],
            "maven.fabricmc.net": ["https://bmclapi2.bangbang93.com", "/maven"],
            "meta.fabricmc.net": ["https://bmclapi2.bangbang93.com", "/fabric-meta"],
        },
    },
    {
        "name": "maven-central",
        "enabled": False,
        "hosts": {
            "libraries.minecraft.net": ["https://repo1.maven.org/maven2", ""],
        },
    },
]

DEFAULTS: dict[str, Any] = {
    "mirror_mode": "auto",  # auto=镜像优先/官方兜底；off=只走官方；only=只走镜像
    "download_workers": 16,
    "mirrors": DEFAULT_MIRRORS,
    "memory_mb": 2048,
    "jvm_args": [],
    "player": {"name": "Steve", "uuid": None},
    "default_version": None,
    "default_loader": None,
    "java": {"path": None, "auto": True},
    "share": {
        "port": 25577,
        "bind": "0.0.0.0",
        "multicast_group": "239.255.77.77",
        "multicast_port": 25578,
        "announce_interval": 10.0,
        "peers": [],
    },
    "trusted_keys": [],
    "tls_verify": "fallback",  # strict=严格校验；fallback=证书链失败时对带摘要内容降级
    "modrinth_api": "https://api.modrinth.com/v2",
    # 界面 / 实例相关
    "default_isolation": True,
    "default_isolation_policy": "always",  # always | modded | never
    "launcher_visibility": "keep",  # keep | minimize | hide | hide_and_reopen | exit
    "theme": "light",
    "accounts": [],
    "default_instance": None,
    # 启动选项（对齐 HMCL 全局游戏设置）
    "window_type": "windowed",  # windowed | fullscreen | maximized
    "quick_play": "none",  # none | singleplayer | multiplayer | realms
    "quick_play_value": "",
    "pre_call": None,  # 启动前命令
    "post_exit": None,  # 游戏结束后命令
    "environment": {},  # 传给游戏进程的环境变量
    "process_priority": "normal",  # low | below_normal | normal | above_normal | high
    "wrapper": None,  # 包装命令（如 optirun）
    "check_game_completeness": True,
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def default_home() -> Path:
    env = os.environ.get("MCDECENT_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".mcdecent"


class Config:
    """一个启动器实例的全部配置与路径。"""

    def __init__(self, home: str | os.PathLike | None = None, *, load: bool = True):
        self.root = Path(home).expanduser().resolve() if home else default_home().resolve()
        self.file = self.root / "config.json"
        self.data = copy.deepcopy(DEFAULTS)
        if load:
            saved = util.read_json(self.file, {})
            if isinstance(saved, dict):
                self.data = _deep_merge(DEFAULTS, saved)
        # 允许用环境变量临时切换镜像策略
        env_mode = os.environ.get("MCDECENT_MIRROR_MODE")
        if env_mode:
            self.data["mirror_mode"] = env_mode
        # 实例隔离：把 game_dir 指向某个实例目录（由 instances.InstanceManager 设置）
        self._game_dir_override: Path | None = None

    def set_game_dir(self, path: str | os.PathLike | None) -> None:
        self._game_dir_override = Path(path).expanduser().resolve() if path else None

    # ------------------------------------------------------------- 路径
    @property
    def game_dir(self) -> Path:
        if self._game_dir_override is not None:
            return self._game_dir_override
        return self.root / "game"

    @property
    def versions_dir(self) -> Path:
        return self.root / "versions"

    @property
    def libraries_dir(self) -> Path:
        return self.root / "libraries"

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def cas_dir(self) -> Path:
        return self.root / "cas"

    @property
    def java_dir(self) -> Path:
        return self.root / "java"

    @property
    def packs_dir(self) -> Path:
        return self.root / "packs"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def peers_file(self) -> Path:
        return self.root / "peers.json"

    @property
    def mods_dir(self) -> Path:
        return self.game_dir / "mods"

    @property
    def logs_file(self) -> Path:
        return self.logs_dir / "mcdecent.log"

    def ensure_layout(self) -> None:
        for p in (
            self.root, self.game_dir, self.versions_dir, self.libraries_dir,
            self.assets_dir, self.cache_dir, self.cas_dir, self.java_dir,
            self.packs_dir, self.logs_dir,
        ):
            util.ensure_dir(p)

    # ------------------------------------------------------------- 读写
    def get(self, dotted: str, default=None):
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value) -> None:
        parts = dotted.split(".")
        cur = self.data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value

    def save(self) -> None:
        self.ensure_layout()
        util.write_json(self.file, self.data)

    # ------------------------------------------------------------- 组件
    def mirror_objects(self):
        from .net import Mirror

        out = []
        for item in self.get("mirrors", []) or []:
            try:
                out.append(Mirror.from_dict(item))
            except Exception:  # 配置损坏时忽略该镜像
                continue
        return out

    def make_downloader(self, *, progress=None, quiet: bool = False):
        from .net import Downloader

        self.ensure_layout()
        return Downloader(
            cache_dir=self.cache_dir,
            mirrors=self.mirror_objects(),
            mirror_mode=self.get("mirror_mode", "auto"),
            workers=int(self.get("download_workers", 8)),
            progress=progress,
            quiet=quiet,
            tls_verify=self.get("tls_verify", "fallback"),
        )

    def describe(self) -> dict:
        return {
            "home": str(self.root),
            "game_dir": str(self.game_dir),
            "mirror_mode": self.get("mirror_mode"),
            "mirrors": [m["name"] for m in (self.get("mirrors") or []) if m.get("enabled")],
            "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()} {platform.machine()}",
        }
