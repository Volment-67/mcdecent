"""实例（版本隔离）系统的单元测试。"""

from __future__ import annotations

from mcdecent import config, instances


def make_cfg(tmp_path):
    cfg = config.Config(home=tmp_path / "home", load=False)
    cfg.ensure_layout()
    return cfg


def test_ensure_default_is_shared_legacy_dir(tmp_path):
    cfg = make_cfg(tmp_path)
    manager = instances.InstanceManager(cfg)
    inst = manager.ensure_default()
    assert inst.name == instances.DEFAULT_INSTANCE_NAME
    assert inst.isolated is False
    assert manager.game_dir(inst) == cfg.game_dir
    assert manager.mods_dir(inst) == cfg.mods_dir
    # 幂等
    assert manager.ensure_default().name == inst.name
    assert len(manager.list()) == 1


def test_create_isolated_instance_dirs(tmp_path):
    cfg = make_cfg(tmp_path)
    manager = instances.InstanceManager(cfg)
    manager.ensure_default()
    inst = manager.create("整合包 A", "fabric-loader-0.20.1-26.3", isolated=True)
    directory = manager.game_dir(inst)
    assert directory == (cfg.root / "instances" / "整合包 A")
    assert (directory / "mods").is_dir()
    assert (directory / "saves").is_dir()
    assert (directory / "resourcepacks").is_dir()
    assert (directory / "shaderpacks").is_dir()
    # 名称唯一化
    another = manager.create("整合包 A", "26.3")
    assert another.name != inst.name


def test_create_copy_instance_copies_mods(tmp_path):
    cfg = make_cfg(tmp_path)
    manager = instances.InstanceManager(cfg)
    source = manager.ensure_default()
    mods = manager.mods_dir(source)
    mods.mkdir(parents=True, exist_ok=True)
    (mods / "demo.jar").write_bytes(b"x")
    copied = manager.create("副本", "26.3", isolated=True, copy_from=source)
    assert (manager.mods_dir(copied) / "demo.jar").is_file()
    # 存档不复制
    assert not (manager.saves_dir(copied) / "world").exists()


def test_rename_moves_isolated_dir(tmp_path):
    cfg = make_cfg(tmp_path)
    manager = instances.InstanceManager(cfg)
    manager.ensure_default()
    inst = manager.create("旧名字", "26.3", isolated=True)
    old_dir = manager.game_dir(inst)
    (old_dir / "mods" / "a.jar").write_bytes(b"a")
    renamed = manager.rename(inst.name, "新名字")
    assert renamed is not None
    new_dir = manager.game_dir(renamed)
    assert new_dir.exists() and not old_dir.exists()
    assert (new_dir / "mods" / "a.jar").is_file()


def test_delete_keeps_files_by_default(tmp_path):
    cfg = make_cfg(tmp_path)
    manager = instances.InstanceManager(cfg)
    manager.ensure_default()
    inst = manager.create("待删", "26.3", isolated=True)
    directory = manager.game_dir(inst)
    assert manager.delete(inst.name) is True
    assert directory.exists()
    assert manager.get(inst.name) is None


def test_delete_with_files(tmp_path):
    cfg = make_cfg(tmp_path)
    manager = instances.InstanceManager(cfg)
    manager.ensure_default()
    inst = manager.create("删干净", "26.3", isolated=True)
    directory = manager.game_dir(inst)
    assert manager.delete(inst.name, delete_files=True) is True
    assert not directory.exists()


def test_set_default_updates_config(tmp_path):
    cfg = make_cfg(tmp_path)
    manager = instances.InstanceManager(cfg)
    first = manager.ensure_default()
    second = manager.create("第二个", "26.3", isolated=True)
    manager.set_default(second.name)
    assert manager.default().name == second.name
    assert cfg.get("default_version") == "26.3"
    reloaded = instances.InstanceManager(config.Config(home=cfg.root))
    assert reloaded.default().name == second.name


def test_config_for_points_game_dir_to_instance(tmp_path):
    cfg = make_cfg(tmp_path)
    manager = instances.InstanceManager(cfg)
    manager.ensure_default()
    inst = manager.create("隔离", "26.3", isolated=True)
    instance_cfg = manager.config_for(inst)
    assert instance_cfg.game_dir == manager.game_dir(inst)
    assert instance_cfg.mods_dir == manager.game_dir(inst) / "mods"
    assert instance_cfg.root == cfg.root
    # 原配置不受影响
    assert cfg.game_dir == cfg.root / "game"


def test_safe_dirname_filters_illegal_chars():
    assert instances.InstanceManager.safe_dirname('a<b>c:d"e/f\\g|h?i*j') == "abcdefghij"
    assert instances.InstanceManager.safe_dirname("...") == "instance"


def test_format_play_time():
    assert instances.format_play_time(30) == "30 秒"
    assert instances.format_play_time(90) == "1 分"
    assert instances.format_play_time(3700).startswith("1 小时")
