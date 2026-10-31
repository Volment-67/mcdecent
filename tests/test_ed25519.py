"""Ed25519（RFC 8032）测试：官方向量逐字节比对 + 随机往返 + 篡改/长度异常。

补充：``mcdecent/p2p.py`` 与 ``registry.py`` 的签名都建立在这个实现之上，
所以这里既测"算法对不对"，也测"接口约定稳不稳"。
"""

from __future__ import annotations

import os

import pytest

from mcdecent import ed25519

# RFC 8032 §7.1 的三组测试向量：(标签, 私钥, 公钥, 消息, 签名)
RFC8032_VECTORS = [
    (
        "TEST 1（空消息）",
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "TEST 2（1 字节消息）",
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "TEST 3（2 字节消息）",
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


def _hex(text: str) -> bytes:
    return bytes.fromhex(text)


@pytest.mark.parametrize("label,secret_hex,public_hex,message_hex,signature_hex", RFC8032_VECTORS)
def test_rfc8032_vectors(label, secret_hex, public_hex, message_hex, signature_hex):
    """RFC 8032 官方向量：公钥、签名逐字节一致，验签通过。"""
    secret = _hex(secret_hex)
    public = _hex(public_hex)
    message = _hex(message_hex)
    signature = _hex(signature_hex)

    assert ed25519.public_key(secret) == public, f"{label}: 公钥不符"
    assert ed25519.sign(secret, message) == signature, f"{label}: 签名不符"
    assert ed25519.verify(public, message, signature) is True, f"{label}: 验签应通过"


def test_verify_rejects_tampered_signature():
    """篡改签名任意字节 → 验签失败。"""
    secret, public = ed25519.generate_keypair()
    message = b"mcdecent p2p"
    signature = ed25519.sign(secret, message)
    assert ed25519.verify(public, message, signature)

    for index in (0, 31, 32, 63):
        broken = bytearray(signature)
        broken[index] ^= 0x01
        assert ed25519.verify(public, message, bytes(broken)) is False, f"第 {index} 字节被篡改应失败"


def test_verify_rejects_tampered_message_and_key():
    secret, public = ed25519.generate_keypair()
    message = b"manifest-v1"
    signature = ed25519.sign(secret, message)

    assert ed25519.verify(public, message + b"x", signature) is False
    other_secret, other_public = ed25519.generate_keypair()
    assert other_public != public
    assert ed25519.verify(other_public, message, signature) is False


def test_random_roundtrip():
    """随机密钥/消息往返：公钥匹配、验签通过、签名确定性。"""
    for size in (0, 1, 33, 256, 4096):
        secret, public = ed25519.generate_keypair()
        message = os.urandom(size)
        assert ed25519.public_key(secret) == public
        signature = ed25519.sign(secret, message)
        assert len(signature) == 64
        assert ed25519.verify(public, message, signature) is True
        assert ed25519.sign(secret, message) == signature  # Ed25519 是确定性签名


def test_length_errors():
    """长度/类型不对：构造接口抛 ValueError，verify 一律返回 False。"""
    secret, public = ed25519.generate_keypair()
    message = b"hi"
    signature = ed25519.sign(secret, message)

    with pytest.raises(ValueError):
        ed25519.public_key(b"short")
    with pytest.raises(ValueError):
        ed25519.sign(b"short", message)

    assert ed25519.verify(b"short", message, signature) is False
    assert ed25519.verify(public, message, signature[:32]) is False
    assert ed25519.verify(public, message, b"") is False
    assert ed25519.verify(public, "字符串不是 bytes", signature) is False  # type: ignore[arg-type]


def test_non_canonical_s_rejected():
    """S 不在 [0, L) 的签名必须被拒绝（RFC 8032 规范性要求）。"""
    secret, public = ed25519.generate_keypair()
    message = b"canonical check"
    signature = bytearray(ed25519.sign(secret, message))
    s = int.from_bytes(signature[32:], "little")
    signature[32:] = (s + ed25519.L).to_bytes(32, "little")  # 数学上等价，但非规范
    assert ed25519.verify(public, message, bytes(signature)) is False


def test_invalid_public_key_bytes():
    """公钥不是曲线上合法点 → 验签失败而不是崩溃。"""
    message = b"x"
    signature = bytes(64)
    for bad in (b"\x00" * 32, b"\xff" * 32, bytes.fromhex("ff" * 32)):
        assert ed25519.verify(bad, message, signature) is False


def test_hex_helpers():
    """明文十六进制工具：解析/输出可往返，非法输入抛 ValueError。"""
    secret, public = ed25519.generate_keypair()
    assert ed25519.secret_from_hex(ed25519.secret_to_hex(secret)) == secret
    assert ed25519.public_to_hex(public) == public.hex()
    assert ed25519.secret_from_hex(ed25519.secret_to_hex(secret).upper()) == secret

    with pytest.raises(ValueError):
        ed25519.secret_from_hex("不是十六进制")
    with pytest.raises(ValueError):
        ed25519.secret_from_hex("ab" * 31)  # 少一字节
    with pytest.raises(ValueError):
        ed25519.public_to_hex(b"short")
