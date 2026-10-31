"""GUI 包与命令发现的单元测试（不打开窗口、不联网）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tkinter")

from mcdecent import gui


def test_app_constructs_and_pages_load(monkeypatch, tmp_path):
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path / "home"))
    try:
        application = gui.App()
    except Exception as exc:  # noqa: BLE001 - 无显示环境时跳过
        pytest.skip(f"Tk 不可用：{exc!r}")
    try:
        application.withdraw()
        application.update()
        assert "home" in application._pages
        for name in ("accounts", "instance_list", "instance_manage", "download",
                     "settings", "multiplayer", "group"):
            application.show_page(name)
            application.update()
            assert name in application._pages, f"页面未创建：{name}"
        application.show_page("home")
        assert application._stack[-1] == "home"
    finally:
        application.on_close()


def test_instance_switching(monkeypatch, tmp_path):
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path / "home"))
    try:
        application = gui.App()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Tk 不可用：{exc!r}")
    try:
        application.withdraw()
        application.update()
        second = application.instances.create("测试实例", "26.3", isolated=True)
        application.set_instance(second.name)
        application.update()
        assert application.current_instance().name == second.name
        assert application.instance_cfg().game_dir == application.instances.game_dir(second)
    finally:
        application.on_close()


def test_settings_page_views(monkeypatch, tmp_path):
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path / "home"))
    try:
        application = gui.App()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Tk 不可用：{exc!r}")
    try:
        application.withdraw()
        application.show_page("settings")
        application.update()
        page = application._pages["settings"]
        for view in ("global", "java", "general", "appearance", "download", "help", "feedback", "about"):
            page.select(view)
            application.update()
    finally:
        application.on_close()


def test_discover_fallback_when_pkgutil_empty(monkeypatch):
    """冻结环境（PyInstaller）下 pkgutil 扫不到目录，必须靠静态清单兜底。"""
    import mcdecent.commands as pkg
    from mcdecent.commands import _KNOWN_COMMANDS

    monkeypatch.setattr(pkg.pkgutil, "iter_modules", lambda _paths: [])
    modules = {m.__name__.rsplit(".", 1)[-1] for m in pkg.discover()}
    assert set(_KNOWN_COMMANDS) <= modules


def test_discover_covers_all_command_modules():
    import mcdecent.commands as pkg
    from mcdecent.commands import _KNOWN_COMMANDS, discover

    package_dir = Path(pkg.__file__).parent
    files = {p.stem for p in package_dir.glob("cmds_*.py")}
    assert files, "应至少有一个命令模块"
    assert files <= set(_KNOWN_COMMANDS), f"静态兜底清单缺少：{files - set(_KNOWN_COMMANDS)}"
    discovered = {m.__name__.rsplit(".", 1)[-1] for m in discover()}
    assert files <= discovered
