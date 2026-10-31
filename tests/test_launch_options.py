"""HMCL 对齐的启动选项单元测试：窗口类型、快速游戏、环境变量、隔离策略。"""

from __future__ import annotations

from mcdecent import config, instances, launch


def test_patch_options_updates_keeps_others(tmp_path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "options.txt").write_text(
        "version:3955\nfov:0.0\nfullscreen:false\nlang:zh_cn\n", encoding="utf-8")
    launch.patch_options(game_dir, {"fullscreen": "true"})
    text = (game_dir / "options.txt").read_text(encoding="utf-8")
    assert "fullscreen:true" in text
    assert "fov:0.0" in text and "lang:zh_cn" in text
    assert text.count("fullscreen:") == 1


def test_patch_options_creates_file(tmp_path):
    game_dir = tmp_path / "new_game"
    launch.patch_options(game_dir, {"fullscreen": "false", "overrideWidth": "1280"})
    text = (game_dir / "options.txt").read_text(encoding="utf-8")
    assert "fullscreen:false" in text and "overrideWidth:1280" in text


def test_apply_window_options(tmp_path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    assert launch.apply_window_options(game_dir, "fullscreen") == (None, None)
    assert "fullscreen:true" in (game_dir / "options.txt").read_text(encoding="utf-8")
    assert launch.apply_window_options(game_dir, "windowed") == (None, None)
    assert "fullscreen:false" in (game_dir / "options.txt").read_text(encoding="utf-8")
    width, height = launch.apply_window_options(game_dir, "maximized")
    assert width > 0 and height > 0
    text = (game_dir / "options.txt").read_text(encoding="utf-8")
    assert f"overrideWidth:{width}" in text


def test_apply_window_options_unknown_is_noop(tmp_path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    assert launch.apply_window_options(game_dir, None) == (None, None)
    assert not (game_dir / "options.txt").exists()


def test_quick_play_args():
    assert launch.quick_play_args("singleplayer", "新的世界") == ["--quickPlaySingleplayer", "新的世界"]
    assert launch.quick_play_args("multiplayer", "mc.example.com") == ["--quickPlayMultiplayer", "mc.example.com"]
    assert launch.quick_play_args("realms", "abc") == ["--quickPlayRealms", "abc"]
    assert launch.quick_play_args("none", "x") == []
    assert launch.quick_play_args("singleplayer", "") == []
    assert launch.quick_play_args(None, None) == []


def test_screen_size_positive():
    width, height = launch.screen_size()
    assert width > 0 and height > 0


def test_run_hook_executes(tmp_path):
    marker = tmp_path / "hook.txt"
    command = f'python -c "open(r\'{marker}\', \'w\').write(\'ok\')"'
    assert launch.run_hook(command, tmp_path, "测试") == 0
    assert marker.read_text(encoding="utf-8") == "ok"


def test_run_hook_empty_is_noop(tmp_path):
    assert launch.run_hook(None, tmp_path, "测试") == 0


def test_config_has_hmcl_style_options(tmp_path):
    cfg = config.Config(home=tmp_path / "home", load=False)
    for key, expected in (
        ("window_type", "windowed"),
        ("quick_play", "none"),
        ("process_priority", "normal"),
        ("check_game_completeness", True),
        ("default_isolation_policy", "always"),
    ):
        assert cfg.get(key) == expected


def test_default_isolation_policy(tmp_path):
    cfg = config.Config(home=tmp_path / "home", load=False)
    cfg.set("default_isolation_policy", "always")
    assert instances.default_isolation(cfg, "26.3") is True
    cfg.set("default_isolation_policy", "never")
    assert instances.default_isolation(cfg, "26.3") is False
    cfg.set("default_isolation_policy", "modded")
    assert instances.default_isolation(cfg, "26.3") is False
    assert instances.default_isolation(cfg, "fabric-loader-0.20.1-26.3") is True
    assert instances.default_isolation(cfg, "quilt-loader-0.30-26.3") is True
