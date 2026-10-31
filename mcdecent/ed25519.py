"""纯标准库 Ed25519 实现（RFC 8032）。

只依赖 ``hashlib`` / ``os``，不引入任何第三方密码库：这是全项目唯一需要"密码学正确性"
的模块，因此保持单文件、可逐行审计。曲线为 edwards25519：

    -x² + y² = 1 + d·x²·y²，  d = -121665/121666，  q = 2²⁵⁵ - 19

约定：
    * 私钥 / 公钥 / 签名均为 ``bytes``，长度分别 32 / 32 / 64；
    * 签名是 RFC 8032 §5.1 的 PureEdDSA（消息不做预哈希，直接喂 SHA-512）；
    * ``verify`` 遇到任何格式问题一律返回 ``False``，不抛异常（调用方不必包 try）。

点运算使用扩展扭曲爱德华兹坐标 (X:Y:Z:T)，``T = X·Y/Z``。
edwards25519 的 d 不是二次剩余，因此下面的加法公式是完备的（无需例外分支），
于是"倍点"可以直接复用 ``_point_add(P, P)``，少写一份容易出错的公式。
"""

from __future__ import annotations

import hashlib
import os
import re

# --------------------------------------------------------------------------- 参数

Q = 2**255 - 19  # 基域素模数
L = 2**252 + 27742317777372353535851937790883648493  # 基点阶（签名标量取模）
_D = -121665 * pow(121666, Q - 2, Q) % Q  # d = -121665/121666 mod q
_I = pow(2, (Q - 1) // 4, Q)  # sqrt(-1)，用于开平方的修正

_IDENTITY = (0, 1, 1, 0)  # 单位元 (0, 1)
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _inv(x: int) -> int:
    """模 q 求逆（费马小定理，q 为素数）。"""
    return pow(x, Q - 2, Q)


def _recover_x(y: int, sign: int) -> int | None:
    """由 y 坐标与 x 的符号位还原 x；该 y 不在曲线上时返回 None。"""
    if not (0 <= y < Q):
        return None
    xx = (y * y - 1) * _inv(_D * y * y + 1) % Q
    x = pow(xx, (Q + 3) // 8, Q)
    if (x * x - xx) % Q != 0:  # 尝试乘以 sqrt(-1) 再验一次
        x = x * _I % Q
    if (x * x - xx) % Q != 0:  # 仍不是平方剩余：非法点
        return None
    if (x & 1) != sign:
        x = Q - x
    return x


# --------------------------------------------------------------------------- 点运算


def _point_add(P: tuple[int, int, int, int], R: tuple[int, int, int, int]):
    """扩展坐标点加（a = -1 的 twisted Edwards 完备公式）。"""
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = R
    a = (y1 - x1) * (y2 - x2) % Q
    b = (y1 + x1) * (y2 + x2) % Q
    c = t1 * 2 * _D % Q * t2 % Q
    d = z1 * 2 * z2 % Q
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % Q, g * h % Q, f * g % Q, e * h % Q)


def _point_double(P: tuple[int, int, int, int]):
    """倍点：加法公式在 edwards25519 上是完备的，P + P 即正确倍点。"""
    return _point_add(P, P)


def _scalar_mult(scalar: int, point: tuple[int, int, int, int]):
    """标量乘（双倍-相加）。基点/任意点均适用，标量先降到 [0, L)。"""
    scalar %= L
    result = _IDENTITY
    base = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, base)
        base = _point_double(base)
        scalar >>= 1
    return result


def _encode_point(P: tuple[int, int, int, int]) -> bytes:
    """压缩点编码：32 字节小端 y，最高位存 x 的奇偶。"""
    x, y, z, _t = P
    zinv = _inv(z)
    x = x * zinv % Q
    y = y * zinv % Q
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode_point(data: bytes):
    """解析压缩点；长度、y 范围、曲线方程任一不合法都返回 None。"""
    if not isinstance(data, (bytes, bytearray)) or len(data) != 32:
        return None
    value = int.from_bytes(data, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    if y >= Q:
        return None
    x = _recover_x(y, sign)
    if x is None:
        return None
    # 曲线方程：-x² + y² = 1 + d·x²·y²
    if (y * y - x * x - 1 - _D * x * x * y * y) % Q != 0:
        return None
    return (x, y, 1, x * y % Q)


def _point_equal(P, R) -> bool:
    """投影坐标比较：Z 不同但几何上同一点也算相等。"""
    x1, y1, z1, _t1 = P
    x2, y2, z2, _t2 = R
    return (x1 * z2 - x2 * z1) % Q == 0 and (y1 * z2 - y2 * z1) % Q == 0


def _has_small_order(point) -> bool:
    """点是否落在小的余子群（阶 1/2/4/8）——即 8P == 单位元。

    RFC 8032 §5.1.7 要求验签时拒绝这类公钥/签名点：否则会放大"可伪造签名"
    的余地（例如公钥全零这类非规范输入）。
    """
    acc = point
    for _ in range(3):
        acc = _point_double(acc)
    return _point_equal(acc, _IDENTITY)


# 基点 B：y = 4/5，x 取偶数值
_BY = 4 * _inv(5) % Q
_BX = _recover_x(_BY, 0)
if _BX is None:  # pragma: no cover - 参数写错时才会发生
    raise RuntimeError("Ed25519 基点还原失败：曲线参数损坏")
_BASE = (_BX, _BY, 1, _BX * _BY % Q)


# --------------------------------------------------------------------------- 密钥派生


def _check_secret(secret: bytes) -> None:
    if not isinstance(secret, (bytes, bytearray)) or len(secret) != 32:
        raise ValueError("Ed25519 私钥必须是 32 字节")


def _expand_secret(secret: bytes) -> tuple[int, bytes]:
    """SHA-512(seed) 的左半做 clamp 得标量 a，右半作为确定性签名前缀。"""
    digest = hashlib.sha512(bytes(secret)).digest()
    a = int.from_bytes(digest[:32], "little")
    a &= (1 << 254) - 8  # 清低 3 位
    a |= 1 << 254  # 置第 254 位
    return a, digest[32:]


def public_key(secret: bytes) -> bytes:
    """由 32 字节私钥种子导出 32 字节公钥。"""
    _check_secret(secret)
    a, _prefix = _expand_secret(secret)
    return _encode_point(_scalar_mult(a, _BASE))


def sign(secret: bytes, message: bytes) -> bytes:
    """对 message 做 Ed25519 签名，返回 64 字节（R ‖ S）。"""
    _check_secret(secret)
    if not isinstance(message, (bytes, bytearray)):
        raise ValueError("待签名消息必须是 bytes")
    message = bytes(message)
    a, prefix = _expand_secret(secret)
    public = _encode_point(_scalar_mult(a, _BASE))

    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % L
    r_encoded = _encode_point(_scalar_mult(r, _BASE))
    k = int.from_bytes(hashlib.sha512(r_encoded + public + message).digest(), "little") % L
    s = (r + k * a) % L
    return r_encoded + s.to_bytes(32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """验证签名。任何格式/长度/数学问题都返回 False。"""
    try:
        if not isinstance(public, (bytes, bytearray)) or len(public) != 32:
            return False
        if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
            return False
        if not isinstance(message, (bytes, bytearray)):
            return False
        public = bytes(public)
        signature = bytes(signature)
        message = bytes(message)

        a_point = _decode_point(public)
        if a_point is None or _has_small_order(a_point):
            return False
        r_point = _decode_point(signature[:32])
        if r_point is None or _has_small_order(r_point):
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= L:  # RFC 8032 要求拒绝非规范 S
            return False
        k = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % L
        return _point_equal(_scalar_mult(s, _BASE), _point_add(r_point, _scalar_mult(k, a_point)))
    except (ValueError, TypeError, OverflowError):
        return False


def generate_keypair() -> tuple[bytes, bytes]:
    """随机生成 (私钥, 公钥)。"""
    secret = os.urandom(32)
    return secret, public_key(secret)


# --------------------------------------------------------------------------- 十六进制工具


def secret_from_hex(s: str) -> bytes:
    """解析 64 位十六进制私钥。"""
    text = (s or "").strip().lower()
    if not _HEX64_RE.match(text):
        raise ValueError(f"私钥必须是 64 位十六进制字符串：{s!r}")
    return bytes.fromhex(text)


def public_to_hex(p: bytes) -> str:
    """公钥转十六进制。"""
    if not isinstance(p, (bytes, bytearray)) or len(p) != 32:
        raise ValueError("Ed25519 公钥必须是 32 字节")
    return bytes(p).hex()


def secret_to_hex(secret: bytes) -> str:
    """私钥转十六进制（打印/备份用）。"""
    _check_secret(secret)
    return bytes(secret).hex()


def signature_to_hex(signature: bytes) -> str:
    """签名转十六进制。"""
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
        raise ValueError("Ed25519 签名必须是 64 字节")
    return bytes(signature).hex()
