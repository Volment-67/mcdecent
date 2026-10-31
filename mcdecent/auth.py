"""身份与启动档案。

默认使用离线模式：UUID 由用户名按官方离线算法（MD5 v3）推导，不依赖任何中心认证服务器，
这与"去中心化"的整体设计一致。也支持把已有的访问令牌透传进来（--access-token）。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field


@dataclass
class Profile:
    name: str
    uuid: str
    access_token: str = "0"
    user_type: str = "legacy"  # legacy | msa
    xuid: str = ""
    client_id: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "uuid": self.uuid,
            "access_token": self.access_token,
            "user_type": self.user_type,
            "xuid": self.xuid,
        }


def offline_uuid(name: str) -> str:
    """与 Minecraft 服务端离线模式一致的 UUID：md5("OfflinePlayer:<name>") 的 v3 UUID。"""
    digest = hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest()
    raw = bytearray(digest[:16])
    raw[6] = (raw[6] & 0x0F) | 0x30  # version 3
    raw[8] = (raw[8] & 0x3F) | 0x80  # variant RFC 4122
    return str(uuid.UUID(bytes=bytes(raw)))


def offline_profile(name: str, *, access_token: str | None = None) -> Profile:
    name = (name or "Player").strip() or "Player"
    if len(name) > 16:
        name = name[:16]
    return Profile(
        name=name,
        uuid=offline_uuid(name),
        access_token=access_token or secrets.token_hex(16),
        user_type="legacy",
    )


def online_profile(name: str, uuid_str: str, access_token: str, *, user_type: str = "msa") -> Profile:
    try:
        normalized = str(uuid.UUID(uuid_str))
    except ValueError as exc:
        raise ValueError(f"非法 UUID：{uuid_str!r}") from exc
    return Profile(name=name, uuid=normalized, access_token=access_token, user_type=user_type)
