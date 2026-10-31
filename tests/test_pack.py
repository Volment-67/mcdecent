"""pack.py：整合包导出 → 导入往返、内容寻址、路径穿越与签名降级。"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from mcdecent import pack, util
from mcdecent.cas import ContentStore
from mcdecent.config import Config


def write_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def cfg(tmp_path):
    c = Config(home=tmp_path / "home")
    c.ensure_layout()
    return c


def seed_game(cfg) -> None:
    write_file(cfg.game_dir / "mods" / "demo.jar", b"jar-content")
    write_file(cfg.game_dir / "mods" / "other.jar.disabled", b"other-content")
    write_file(cfg.game_dir / "config" / "demo.json", b'{"a":1}')


# ---------------------------------------------------------------- 导出


def test_export_creates_content_addressed_manifest(cfg):
    seed_game(cfg)
    result = pack.export_pack(cfg, "demo-pack", game_version="1.21.4", version="1.2.0")

    assert len(result["digest"]) == 64
    assert result["url"] == f"mcdecent://pack/{result['digest']}"
    assert result["format"] == pack.PACK_FORMAT
    assert result["files"] == 3
    assert Path(result["path"]).is_file()

    store = ContentStore(cfg.cas_dir)
    assert store.has(result["digest"])
    manifest = json.loads(store.get(result["digest"]).read_text(encoding="utf-8"))
    assert manifest["format"] == "mcdecent-pack/1"
    assert manifest["name"] == "demo-pack"
    assert manifest["version"] == "1.2.0"
    assert manifest["game_version"] == "1.21.4"
    assert isinstance(manifest["loader"], dict)
    paths = {entry["path"] for entry in manifest["files"]}
    assert paths == {"mods/demo.jar", "mods/other.jar.disabled", "config/demo.json"}
    for entry in manifest["files"]:
        assert len(entry["sha256"]) == 64
        assert store.has(entry["sha256"])
        assert entry["urls"] == []


def test_load_manifest_and_list_packs(cfg):
    seed_game(cfg)
    result = pack.export_pack(cfg, "demo-pack", game_version="1.21.4")
    manifest = pack.load_manifest(cfg, result["digest"])
    assert manifest["name"] == "demo-pack"
    # mcdecent:// 引用也能读
    assert pack.load_manifest(cfg, f"mcdecent://pack/{result['digest']}")["name"] == "demo-pack"

    listed = pack.list_packs(cfg)
    assert len(listed) == 1
    assert listed[0]["digest"] == result["digest"]
    assert listed[0]["files"] == 3


def test_export_empty_include_warns_but_works(cfg):
    result = pack.export_pack(cfg, "empty", game_version="1.21.4", include=("mods",))
    assert result["files"] == 0
    assert pack.load_manifest(cfg, result["digest"])["files"] == []


# ---------------------------------------------------------------- 导入往返


def test_export_import_roundtrip(cfg):
    seed_game(cfg)
    result = pack.export_pack(cfg, "demo-pack", game_version="1.21.4")
    # 清空 game_dir 后导入应完整还原
    shutil.rmtree(cfg.game_dir / "mods")
    shutil.rmtree(cfg.game_dir / "config")

    imported = pack.import_pack(cfg, result["digest"])

    assert imported["name"] == "demo-pack"
    assert imported["digest"] == result["digest"]
    assert sorted(imported["written"]) == ["config/demo.json", "mods/demo.jar", "mods/other.jar.disabled"]
    assert imported["skipped"] == []
    assert (cfg.game_dir / "mods" / "demo.jar").read_bytes() == b"jar-content"
    assert (cfg.game_dir / "config" / "demo.json").read_bytes() == b'{"a":1}'

    # 再导入一次：全部跳过（幂等）
    again = pack.import_pack(cfg, result["digest"])
    assert again["written"] == []
    assert len(again["skipped"]) == 3


def test_import_from_local_manifest_path(cfg):
    seed_game(cfg)
    result = pack.export_pack(cfg, "demo-pack", game_version="1.21.4")
    shutil.rmtree(cfg.game_dir / "mods")
    imported = pack.import_pack(cfg, result["path"])
    assert "mods/demo.jar" in imported["written"]


def test_import_from_url_reference(cfg):
    seed_game(cfg)
    result = pack.export_pack(cfg, "demo-pack", game_version="1.21.4")
    shutil.rmtree(cfg.game_dir / "mods")
    imported = pack.import_pack(cfg, f"mcdecent://pack/{result['digest']}")
    assert imported["url"] == result["url"]
    assert (cfg.game_dir / "mods" / "demo.jar").is_file()


def test_import_into_other_home_with_shared_cas(cfg, tmp_path):
    seed_game(cfg)
    result = pack.export_pack(cfg, "demo-pack", game_version="1.21.4")

    other = Config(home=tmp_path / "home2")
    other.ensure_layout()
    shutil.copytree(cfg.cas_dir, other.cas_dir, dirs_exist_ok=True)  # 模拟 P2P 同步来的 CAS

    imported = pack.import_pack(other, result["digest"])
    assert len(imported["written"]) == 3
    assert (other.game_dir / "mods" / "demo.jar").read_bytes() == b"jar-content"


def test_import_no_mods_skips_mods_dir(cfg):
    seed_game(cfg)
    result = pack.export_pack(cfg, "demo-pack", game_version="1.21.4")
    shutil.rmtree(cfg.game_dir / "mods")
    shutil.rmtree(cfg.game_dir / "config")

    imported = pack.import_pack(cfg, result["digest"], no_mods=True)
    assert imported["written"] == ["config/demo.json"]
    assert not (cfg.game_dir / "mods").exists()


# ---------------------------------------------------------------- 安全校验


def _put_manifest(cfg, manifest: dict) -> str:
    return ContentStore(cfg.cas_dir).put_bytes(json.dumps(manifest, ensure_ascii=False).encode("utf-8"))


def _manifest(files: list[dict]) -> dict:
    return {
        "format": pack.PACK_FORMAT,
        "name": "evil",
        "version": "1.0.0",
        "game_version": "1.21.4",
        "loader": {"type": "fabric", "version": "0.16.14"},
        "author": "x",
        "created": util.now_iso(),
        "files": files,
    }


@pytest.mark.parametrize("bad_path", ["../evil.txt", "mods/../../evil.txt", "/abs.txt", "C:/evil.txt"])
def test_import_rejects_path_traversal(cfg, bad_path):
    store = ContentStore(cfg.cas_dir)
    digest = store.put_bytes(b"payload")
    manifest = _manifest([{"path": bad_path, "sha256": digest, "size": 7, "urls": []}])
    manifest_digest = _put_manifest(cfg, manifest)

    with pytest.raises(ValueError, match="非法路径"):
        pack.import_pack(cfg, manifest_digest)
    # 什么都没写出去
    assert not (cfg.root / "evil.txt").exists()
    assert not (cfg.game_dir.parent / "evil.txt").exists()
    assert not (cfg.game_dir / "abs.txt").exists()


def test_import_missing_blob_raises_before_writing(cfg):
    missing = util.sha256_bytes(b"never-stored")
    manifest = _manifest([{"path": "mods/x.jar", "sha256": missing, "size": 11, "urls": []}])
    manifest_digest = _put_manifest(cfg, manifest)

    with pytest.raises(FileNotFoundError, match="缺少对象"):
        pack.import_pack(cfg, manifest_digest)
    assert not (cfg.game_dir / "mods" / "x.jar").exists()


def test_import_rejects_corrupted_cas_object(cfg):
    good = b"good-content"
    digest = util.sha256_bytes(good)
    bad_blob = ContentStore(cfg.cas_dir).path_for(digest)
    util.ensure_dir(bad_blob.parent)
    bad_blob.write_bytes(b"tampered")  # 绕过 put 直接写入坏内容

    manifest = _manifest([{"path": "mods/x.jar", "sha256": digest, "size": len(good), "urls": []}])
    manifest_digest = _put_manifest(cfg, manifest)

    with pytest.raises(ValueError, match="损坏"):
        pack.import_pack(cfg, manifest_digest)


def test_import_rejects_unknown_format(cfg):
    manifest = _manifest([])
    manifest["format"] = "something-else/9"
    manifest_digest = _put_manifest(cfg, manifest)
    with pytest.raises(ValueError, match="不支持的整合包格式"):
        pack.import_pack(cfg, manifest_digest)


def test_import_rejects_bad_sha_field(cfg):
    manifest = _manifest([{"path": "mods/x.jar", "sha256": "not-a-hash", "size": 1, "urls": []}])
    manifest_digest = _put_manifest(cfg, manifest)
    with pytest.raises(ValueError, match="缺少合法 sha256"):
        pack.import_pack(cfg, manifest_digest)


def test_import_unknown_reference(cfg, tmp_path):
    with pytest.raises(FileNotFoundError):
        pack.import_pack(cfg, tmp_path / "nope.json")
    with pytest.raises(FileNotFoundError):
        pack.import_pack(cfg, "0" * 64)


# ---------------------------------------------------------------- 签名（可降级）


def test_export_without_ed25519_degrades_to_unsigned(cfg, monkeypatch):
    seed_game(cfg)
    monkeypatch.setattr(pack, "_load_ed25519", lambda: None, raising=True)
    result = pack.export_pack(cfg, "unsigned", game_version="1.21.4", key="ab" * 32)
    assert result["signed"] is False
    assert "signature" not in result["manifest"]


def test_export_and_import_with_real_ed25519(cfg):
    """p2p-dev 的 Ed25519 真实实现就绪后，端到端验证签名/验签。"""
    from mcdecent import ed25519

    seed_game(cfg)
    secret, public = ed25519.generate_keypair()
    result = pack.export_pack(
        cfg, "signed-real", game_version="1.21.4", key=ed25519.secret_to_hex(secret)
    )
    assert result["signed"] is True
    signature = result["manifest"]["signature"]
    assert signature["public_key"] == ed25519.public_to_hex(public)
    assert len(signature["sig"]) == 128

    shutil.rmtree(cfg.game_dir / "mods")
    imported = pack.import_pack(cfg, result["digest"])
    assert imported["signature_valid"] is True
    assert (cfg.game_dir / "mods" / "demo.jar").is_file()


def test_export_and_import_with_fake_ed25519(cfg, monkeypatch):
    """用假 Ed25519 模块验证签名/验签接线（真实实现由 p2p-dev 提供）。"""

    class FakeEd25519:
        @staticmethod
        def _sig(payload: bytes) -> str:
            # 32 字节"签名"占位；真实 Ed25519 是 64 字节，这里凑够长度
            return hashlib.sha256(b"sig:" + payload).hexdigest()

        def sign(self, secret, payload):
            assert isinstance(secret, bytes) and len(secret) == 32
            return bytes.fromhex(self._sig(payload)) * 2, b"\x01" * 32

        def verify(self, public_key, payload, signature):
            return public_key == b"\x01" * 32 and signature == bytes.fromhex(self._sig(payload)) * 2

    monkeypatch.setattr(pack, "_load_ed25519", lambda: FakeEd25519(), raising=True)
    seed_game(cfg)
    result = pack.export_pack(cfg, "signed", game_version="1.21.4", key="cd" * 32)
    assert result["signed"] is True
    signature = result["manifest"]["signature"]
    assert signature["alg"] == "ed25519"
    signed_payload = pack.canonical_manifest_bytes(
        {k: v for k, v in result["manifest"].items() if k != "signature"}
    )
    assert signature["sig"] == FakeEd25519._sig(signed_payload) * 2
    assert signature["public_key"] == (b"\x01" * 32).hex()

    shutil.rmtree(cfg.game_dir / "mods")
    imported = pack.import_pack(cfg, result["digest"])
    assert imported["signature_valid"] is True

    # 篡改后签名应失效，strict 模式拒绝导入
    tampered = dict(result["manifest"])
    tampered["name"] = "tampered"
    tampered_digest = _put_manifest(cfg, tampered)
    with pytest.raises(ValueError, match="签名"):
        pack.import_pack(cfg, tampered_digest, strict_signature=True)


def test_import_warns_when_signature_unverifiable(cfg, monkeypatch):
    seed_game(cfg)
    manifest = _manifest([])
    manifest["signature"] = {"alg": "ed25519", "public_key": "aa" * 32, "sig": "bb" * 64}
    digest = _put_manifest(cfg, manifest)
    monkeypatch.setattr(pack, "_load_ed25519", lambda: None, raising=True)
    imported = pack.import_pack(cfg, digest)
    assert imported["signature_valid"] is None

    # 无法校验时 strict 也要拒绝
    with pytest.raises(ValueError, match="签名"):
        pack.import_pack(cfg, digest, strict_signature=True)
