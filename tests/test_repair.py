"""repair 诊断 + 一键修复的单元测试。

全部测试都**不联网、不真实安装**：
  * 版本/实例用 tmp_path 下临时的 Config 与 InstanceManager 构造；
  * 安装、Java 下载等网络动作全部 monkeypatch 掉，只断言参数正确。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mcdecent import config, instances, repair


# --------------------------------------------------------------------------- 夹具 / 辅助


def make_cfg(tmp_path):
    cfg = config.Config(home=tmp_path / "home", load=False)
    cfg.ensure_layout()
    return cfg


def make_instance(cfg, version_id="", *, memory_mb=None, isolated=False, name="测试实例"):
    manager = instances.InstanceManager(cfg)
    inst = manager.create(name, version_id, isolated=isolated)
    if memory_mb is not None:
        inst.memory_mb = memory_mb
        manager.update(inst)
    return manager, inst


def issue_ids(issues):
    return [issue.id for issue in issues]


def find_issue(issues, issue_id):
    for issue in issues:
        if issue.id == issue_id:
            return issue
    return None


def install_fake_version(cfg, version_id="1.20.1", java_major=17,
                         component="java-runtime-gamma") -> str:
    """在 versions/ 下写一份最小可解析的版本 JSON（不含任何下载动作）。"""
    version_dir = cfg.versions_dir / version_id
    version_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": version_id,
        "type": "release",
        "javaVersion": {"majorVersion": java_major, "component": component},
    }
    (version_dir / f"{version_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return version_id


def write_crash(manager, inst, text: str, *, name: str = "crash-2026-10-30_12-00-00-client.txt"):
    directory = manager.game_dir(inst) / "crash-reports"
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / name
    report.write_text(text, encoding="utf-8")
    return report


# --------------------------------------------------------------------------- 1. 没有版本


def test_no_version_detected(tmp_path):
    cfg = make_cfg(tmp_path)
    manager = instances.InstanceManager(cfg)
    inst = manager.ensure_default()

    issues = repair.diagnose(cfg, inst)

    issue = find_issue(issues, "no-version")
    assert issue is not None, issue_ids(issues)
    assert issue.severity == "error"
    assert issue.auto_fixable is True
    assert issue.fixes and issue.fixes[0].label
    assert issue.causes and issue.detail


# --------------------------------------------------------------------------- 6. 崩溃报告分类


def test_game_crash_oom(tmp_path):
    cfg = make_cfg(tmp_path)
    manager, inst = make_instance(cfg)
    write_crash(
        manager, inst,
        "---- Minecraft Crash Report ----\n"
        "// Oh dear\n"
        "java.lang.OutOfMemoryError: Java heap space\n"
        "\tat net.minecraft.client.Minecraft.run(Minecraft.java:1)\n",
    )

    issue = find_issue(repair.diagnose(cfg, inst), "game-crash")

    assert issue is not None
    assert issue.severity == "error"
    assert issue.auto_fixable is True
    joined = " ".join(issue.causes)
    assert "OutOfMemoryError" in joined or "内存不足" in joined
    assert any("内存" in fix.label for fix in issue.fixes)


@pytest.mark.parametrize("crash_text", [
    "Failed to locate library: libglfw.so\nGLFW error 65542: The driver does not appear to support OpenGL",
    "java.lang.NoClassDefFoundError: net/caffeinemc/mods/sodium/client/SodiumClientMod (Sodium)",
])
def test_game_crash_graphics_is_auto_fixable(tmp_path, crash_text):
    cfg = make_cfg(tmp_path)
    manager, inst = make_instance(cfg)
    write_crash(manager, inst, crash_text + "\n")

    issue = find_issue(repair.diagnose(cfg, inst), "game-crash")

    assert issue is not None
    assert issue.auto_fixable is True
    joined = " ".join(issue.causes)
    assert any(word in joined for word in ("渲染", "显卡", "OpenGL", "图形"))


def test_crash_graphics_fix_disables_render_mods(tmp_path):
    cfg = make_cfg(tmp_path)
    manager, inst = make_instance(cfg)
    mods_dir = manager.mods_dir(inst)
    mods_dir.mkdir(parents=True, exist_ok=True)
    (mods_dir / "sodium-0.5.jar").write_bytes(b"x")
    (mods_dir / "iris-1.0.jar").write_bytes(b"x")
    (mods_dir / "jei-1.0.jar").write_bytes(b"x")
    write_crash(manager, inst, "Failed to locate library: libglfw.so\n")

    result = repair.apply_fix(cfg, "game-crash", instance=inst)

    assert result["ok"], result
    assert (mods_dir / "sodium-0.5.jar.disabled").is_file()
    assert (mods_dir / "iris-1.0.jar.disabled").is_file()
    assert (mods_dir / "jei-1.0.jar").is_file()
    assert any("图形" in action or "sodium" in action.lower() for action in result["actions"])


# --------------------------------------------------------------------------- 9. options.txt


def test_options_corrupt_and_repair(tmp_path):
    cfg = make_cfg(tmp_path)
    manager, inst = make_instance(cfg)
    game_dir = manager.game_dir(inst)
    game_dir.mkdir(parents=True, exist_ok=True)
    options = game_dir / "options.txt"
    options.write_text("renderDistance:12\nfov:0.5\nthis line is broken\n", encoding="utf-8")

    issue = find_issue(repair.diagnose(cfg, inst), "options-corrupt")
    assert issue is not None, issue_ids(repair.diagnose(cfg, inst))
    assert issue.auto_fixable is True

    result = repair.apply_fix(cfg, "options-corrupt", instance=inst)

    assert result["ok"], result
    backups = list(game_dir.glob("options.txt.bak-*"))
    assert backups, "修复前必须备份原文件"
    text = options.read_text(encoding="utf-8")
    assert "renderDistance:12" in text
    assert "broken" not in text
    assert all(":" in line for line in text.splitlines() if line.strip())
    assert "options-corrupt" not in issue_ids(repair.diagnose(cfg, inst))


# --------------------------------------------------------------------------- 8. 残留 game.pid


def test_stale_pid_detected_and_cleaned(tmp_path):
    cfg = make_cfg(tmp_path)
    manager, inst = make_instance(cfg)
    pid_file = cfg.root / "game.pid"
    pid_file.write_text(json.dumps({"pid": 999999999, "version": "1.20.1"}), encoding="utf-8")

    issue = find_issue(repair.diagnose(cfg, inst), "stale-pid")
    assert issue is not None
    assert issue.auto_fixable is True

    result = repair.apply_fix(cfg, "stale-pid", instance=inst)

    assert result["ok"], result
    assert not pid_file.exists()
    assert any("game.pid" in action for action in result["actions"])


# --------------------------------------------------------------------------- 4. 内存配置


def test_memory_low_detected_and_raised(tmp_path):
    cfg = make_cfg(tmp_path)
    manager, inst = make_instance(cfg, memory_mb=512)

    issue = find_issue(repair.diagnose(cfg, inst), "memory-config")
    assert issue is not None
    assert issue.severity == "warning"
    assert "512" in issue.detail
    assert issue.auto_fixable is True

    result = repair.apply_fix(cfg, "memory-config", instance=inst)

    assert result["ok"], result
    assert inst.memory_mb >= 1024
    reloaded = instances.InstanceManager(cfg).get(inst.name)
    assert reloaded is not None and (reloaded.memory_mb or 0) >= 1024
    assert "memory-config" not in issue_ids(repair.diagnose(cfg, inst))


# --------------------------------------------------------------------------- 3. Java


def test_java_missing_and_fix_args(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    version_id = install_fake_version(cfg)
    manager, inst = make_instance(cfg, version_id)

    monkeypatch.setattr(repair.java, "find_java", lambda *args, **kwargs: None)
    issue = find_issue(repair.diagnose(cfg, inst), "java-missing")
    assert issue is not None, issue_ids(repair.diagnose(cfg, inst))
    assert issue.auto_fixable is True

    calls: dict = {}
    fake_runtime = SimpleNamespace(
        path=tmp_path / "fake-java", home=tmp_path, major=17, source="fake", version="17.0.1"
    )

    def fake_ensure(cfg_arg, **kwargs):
        calls["cfg"] = cfg_arg
        calls.update(kwargs)
        return fake_runtime

    monkeypatch.setattr(repair.java, "ensure_java", fake_ensure)

    result = repair.apply_fix(cfg, "java-missing", instance=inst)

    assert result["ok"], result
    assert calls["cfg"] is cfg
    assert calls.get("major") == 17
    assert calls.get("component") == "java-runtime-gamma"
    assert any("Java" in action for action in result["actions"])


# --------------------------------------------------------------------------- 10. Fabric 加载器


def test_fabric_missing_base_and_reinstall(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    loader_id = "fabric-loader-0.15.0-1.20.1"
    loader_dir = cfg.versions_dir / loader_id
    loader_dir.mkdir(parents=True)
    (loader_dir / f"{loader_id}.json").write_text(
        json.dumps({"id": loader_id, "inheritsFrom": "1.20.1", "type": "release"}),
        encoding="utf-8",
    )
    (loader_dir / "mcdecent.meta.json").write_text(
        json.dumps({"loader": {"type": "fabric", "version": "0.15.0", "game": "1.20.1"}}),
        encoding="utf-8",
    )
    manager, inst = make_instance(cfg, loader_id)

    issue = find_issue(repair.diagnose(cfg, inst), "fabric-missing")
    assert issue is not None, issue_ids(repair.diagnose(cfg, inst))
    assert issue.auto_fixable is True
    assert "1.20.1" in issue.detail

    calls: dict = {}

    def fake_install_loader(cfg_arg, base, loader_type="fabric", loader_version=None, **kwargs):
        calls.update(base=base, loader_type=loader_type, loader_version=loader_version)
        return loader_id

    def fake_install_version(cfg_arg, version, **kwargs):
        calls["install"] = version
        return {"version": version}

    monkeypatch.setattr(repair.loaders, "install_loader", fake_install_loader)
    monkeypatch.setattr(repair.installer, "install_version", fake_install_version)

    result = repair.apply_fix(cfg, "fabric-missing", instance=inst)

    assert result["ok"], result
    assert calls["base"] == "1.20.1"
    assert calls["loader_type"] == "fabric"
    assert calls["loader_version"] == "0.15.0"
    assert calls["install"] == loader_id


# --------------------------------------------------------------------------- 7. 网络 / 镜像（假下载器）


class _FakeDownloader:
    def __init__(self):
        self.calls: list[str] = []

    def head_ok(self, url, timeout=8):
        self.calls.append(url)
        return False, "假的连接失败"


def test_network_mirror_only_when_requested(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    manager, inst = make_instance(cfg)

    # 默认（include_network=False）绝不联网
    assert "network-mirror" not in issue_ids(repair.diagnose(cfg, inst))

    fake = _FakeDownloader()
    monkeypatch.setattr(cfg, "make_downloader", lambda **kwargs: fake)

    issue = find_issue(repair.diagnose(cfg, inst, include_network=True), "network-mirror")
    assert issue is not None
    assert issue.auto_fixable is True
    assert fake.calls, "include_network=True 时应该探测官方源与镜像"

    result = repair.apply_fix(cfg, "network-mirror", instance=inst)

    assert result["ok"], result
    assert cfg.get("mirror_mode") == "off"
    assert any("镜像" in action for action in result["actions"])


# --------------------------------------------------------------------------- fix_all / 兜底


def test_fix_all_repairs_auto_fixable_issues(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    version_id = install_fake_version(cfg)
    manager, inst = make_instance(cfg, version_id, memory_mb=512)
    (cfg.root / "game.pid").write_text(json.dumps({"pid": 999999999}), encoding="utf-8")
    fake_java = SimpleNamespace(major=17, path=tmp_path / "fake-java", source="fake")
    monkeypatch.setattr(repair.java, "find_java", lambda *args, **kwargs: fake_java)

    results = repair.fix_all(cfg, inst)

    assert results, "fix_all 应该返回每个自动修复的结果"
    assert all(item["ok"] for item in results), results
    assert {item["issue_id"] for item in results} >= {"stale-pid", "memory-config"}
    assert not (cfg.root / "game.pid").exists()
    assert (inst.memory_mb or 0) >= 1024


def test_apply_fix_unknown_issue_needs_manual_handling(tmp_path):
    cfg = make_cfg(tmp_path)

    result = repair.apply_fix(cfg, "not-a-real-issue")

    assert result == {
        "ok": False,
        "issue_id": "not-a-real-issue",
        "message": "该问题需要手动处理",
        "actions": [],
    }


def test_diagnose_survives_failing_check(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    manager = instances.InstanceManager(cfg)
    inst = manager.ensure_default()

    def boom(_cfg):
        raise RuntimeError("模拟扫描失败")

    monkeypatch.setattr(repair.versions, "installed_versions", boom)

    issues = repair.diagnose(cfg, inst)

    assert isinstance(issues, list)
    # 扫描失败被降级成“没有版本”，界面不会崩
    assert "no-version" in issue_ids(issues)
