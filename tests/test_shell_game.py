"""交互式 shell 与 game 进程控制的单元测试（不真正启动游戏）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from mcdecent import shell, util
from mcdecent.commands import cmds_game


def make_args(tmp_path: Path, **extra) -> argparse.Namespace:
    base = dict(home=str(tmp_path), json=False, verbose=False, quiet=False, no_progress=True)
    base.update(extra)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------- 拆分与别名


def test_split_command_quotes_and_windows_paths():
    assert shell.split_command(r'mods add "C:\MC\my mod.jar"') == ["mods", "add", r"C:\MC\my mod.jar"]
    assert shell.split_command("  list   --available ") == ["list", "--available"]
    assert shell.split_command("") == []
    assert shell.split_command("play --memory 1536") == ["play", "--memory", "1536"]


def test_apply_alias():
    assert shell.apply_alias(["play", "--memory", "1536"]) == ["game", "start", "--memory", "1536"]
    assert shell.apply_alias(["ls"]) == ["list"]
    assert shell.apply_alias(["status"]) == ["game", "status"]
    assert shell.apply_alias(["mods", "list"]) == ["mods", "list"]
    assert shell.apply_alias([]) == []


def test_history_expand():
    sh = shell.Shell(quiet=True)
    sh.history = ["list", "game status"]
    assert sh._history_expand("!!") == "game status"
    assert sh._history_expand("!1") == "list"
    assert sh._history_expand("!-1") == "game status"
    assert sh._history_expand("!99") == ""
    assert sh._history_expand("list") == "list"


# --------------------------------------------------------------------------- shell 行为


def test_shell_execute_help(capsys):
    sh = shell.Shell(quiet=True)
    assert sh.execute("help") == 0
    out = capsys.readouterr().out
    assert "play" in out and "game start" in out and "exit" in out


def test_shell_history_command(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path))
    sh = shell.Shell(quiet=True)
    sh.execute("status")
    sh.execute("history")
    out = capsys.readouterr().out
    assert "status" in out


def test_shell_run_with_piped_input(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path))
    sh = shell.Shell(quiet=True)
    lines = iter(["status", "help", "exit"])

    def fake_input(*_args, **_kwargs):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    assert sh.run() == 0
    out = capsys.readouterr().out
    assert "游戏未在运行" in out


def test_shell_unknown_command_does_not_crash(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path))
    sh = shell.Shell(quiet=True)
    rc = sh.execute("不存在的命令")
    out = capsys.readouterr()
    assert rc == 2
    assert "invalid choice" in out.err


# --------------------------------------------------------------------------- game 进程控制


def test_pid_alive_invalid():
    assert cmds_game.pid_alive(0) is False
    assert cmds_game.pid_alive(-1) is False
    assert cmds_game.pid_alive(999999) is False


def test_game_status_not_running(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path))
    rc = cmds_game.cmd_game_status(make_args(tmp_path))
    assert rc == 0
    assert "游戏未在运行" in capsys.readouterr().out


def test_game_status_cleans_stale_pid_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path))
    pid_file = tmp_path / "game.pid"
    pid_file.write_text(json.dumps({"pid": 999999, "version": "26.3"}), encoding="utf-8")
    rc = cmds_game.cmd_game_status(make_args(tmp_path))
    assert rc == 0
    assert not pid_file.exists()
    assert "已结束" in capsys.readouterr().out


def test_game_stop_without_pid_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path))
    assert cmds_game.cmd_game_stop(make_args(tmp_path)) == 0
    assert "未在运行" in capsys.readouterr().out


def test_game_stop_clears_pid_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path))
    pid_file = tmp_path / "game.pid"
    pid_file.write_text(json.dumps({"pid": 999999, "version": "26.3"}), encoding="utf-8")
    assert cmds_game.cmd_game_stop(make_args(tmp_path)) == 0
    assert not pid_file.exists()


def test_game_log_without_logs(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path))
    rc = cmds_game.cmd_game_log(make_args(tmp_path, lines=10, follow=False))
    assert rc == 1
    assert "还没有游戏日志" in capsys.readouterr().err


def test_game_log_tails_latest(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MCDECENT_HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (logs / "game-26.3-20260101-000000.log").write_text("a\nb\nc\n", encoding="utf-8")
    rc = cmds_game.cmd_game_log(make_args(tmp_path, lines=2, follow=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "b" in out and "c" in out


def test_latest_game_log(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "game-a.log").write_text("x", encoding="utf-8")
    cfg = argparse.Namespace(logs_dir=logs)
    assert cmds_game.latest_game_log(cfg).name == "game-a.log"
