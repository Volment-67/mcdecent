"""测试公共配置：把仓库根目录加入 sys.path，并注册自定义 marker。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_configure(config):
    config.addinivalue_line("markers", "network: 需要真实联网的测试（Fabric meta / Modrinth API）")
