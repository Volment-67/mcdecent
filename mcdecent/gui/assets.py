"""打包资源定位（图标/背景图）。"""

from __future__ import annotations

import sys
from pathlib import Path


def assets_dir() -> Path:
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        return Path(frozen) / "mcdecent" / "assets"
    return Path(__file__).resolve().parent.parent / "assets"


def asset(name: str) -> Path | None:
    path = assets_dir() / name
    return path if path.is_file() else None
