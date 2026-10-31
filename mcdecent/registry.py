"""本地整合包注册表：名字 → 摘要 → 发布者公钥，并做签名校验。

这是"去中心化"的信任层：内容本体放在 CAS（按 SHA-256 寻址），
注册表只回答两个问题——
    1. 我见过的 ``名字/版本`` 对应哪个摘要？
    2. 这份清单是谁签的、签名是否匹配？

签名规范（与 mods-dev 的清单格式对齐）：对清单去掉 ``signature`` 字段后的
规范化 JSON 做 Ed25519 签名::

    json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

存盘：``<packs_dir>/index.json``，结构为
``{"format":"mcdecent-registry/1","packs":{名字: 条目}}``。
文件损坏时视为空注册表（不抛异常），下一次写入即修复。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import ed25519, util
from .util import LOG

REGISTRY_FORMAT = "mcdecent-registry/1"
PACK_FORMAT_PREFIX = "mcdecent-pack/"
SIGNATURE_ALG = "ed25519"
INDEX_NAME = "index.json"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------- 规范化 / 签名


def canonical_json(data) -> bytes:
    """签名用的规范化 JSON 字节串（排序键、紧凑分隔符、不转义中文）。"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_manifest(manifest: dict) -> bytes:
    """清单正文的规范化形式（永远剔除 signature 字段）。"""
    body = {key: value for key, value in (manifest or {}).items() if key != "signature"}
    return canonical_json(body)


def manifest_digest(manifest: dict) -> str:
    """清单正文（不含签名）的 SHA-256，用于快速比对。"""
    return util.sha256_bytes(canonical_manifest(manifest))


def sign_manifest(manifest: dict, secret: bytes) -> dict:
    """返回带 ``signature`` 字段的新清单（不修改入参）。

    ``secret`` 为 32 字节 Ed25519 私钥种子。
    """
    body = {key: value for key, value in (manifest or {}).items() if key != "signature"}
    signature = ed25519.sign(secret, canonical_manifest(body))
    public_hex = ed25519.public_to_hex(ed25519.public_key(secret))
    signed = dict(body)
    signed["signature"] = {
        "alg": SIGNATURE_ALG,
        "public_key": public_hex,
        "sig": signature.hex(),
    }
    return signed


def _is_sha256(value) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.match(value.strip().lower()))


def _sig_parts(entry: dict, manifest: dict) -> tuple[str, str] | None:
    """取公钥/签名十六进制：优先注册表条目，其次清单自带 signature。"""
    public_hex = str((entry or {}).get("public_key") or "").strip().lower()
    signature_hex = str((entry or {}).get("signature") or "").strip().lower()
    block = (manifest or {}).get("signature")
    if isinstance(block, dict):
        alg = str(block.get("alg") or SIGNATURE_ALG).strip().lower()
        if alg != SIGNATURE_ALG:
            LOG.warning("不支持的签名算法：%s", alg)
            return None
        public_hex = public_hex or str(block.get("public_key") or "").strip().lower()
        signature_hex = signature_hex or str(block.get("sig") or "").strip().lower()
    elif block is not None:
        return None
    if not public_hex and not signature_hex:
        return "", ""
    if not public_hex or not signature_hex:
        return None  # 只有一半，视为损坏
    return public_hex, signature_hex


# --------------------------------------------------------------------------- 注册表


class PackRegistry:
    """``<packs_dir>/index.json`` 的读写封装（线程内使用，非跨进程锁）。"""

    def __init__(self, target, *, index_name: str = INDEX_NAME):
        self.cfg = target if hasattr(target, "packs_dir") else None
        if self.cfg is not None:  # 直接传 Config
            base = Path(target.packs_dir)
        else:
            base = Path(target)
        self.root = base
        self.file = base / index_name
        self._data = self._load()

    @classmethod
    def from_config(cls, cfg) -> "PackRegistry":
        return cls(cfg)

    # ------------------------------------------------------------ 底层
    def _load(self) -> dict:
        data = util.read_json(self.file, {})
        if isinstance(data, dict) and isinstance(data.get("packs"), dict):
            packs = {str(k): v for k, v in data["packs"].items() if isinstance(v, dict)}
            return {"format": data.get("format") or REGISTRY_FORMAT, "packs": packs}
        if isinstance(data, list):  # 兼容早期"清单数组"格式
            packs = {}
            for item in data:
                if isinstance(item, dict) and item.get("name"):
                    packs[str(item["name"])] = item
            if packs:
                return {"format": REGISTRY_FORMAT, "packs": packs}
        return {"format": REGISTRY_FORMAT, "packs": {}}

    def _save(self) -> None:
        util.ensure_dir(self.root)
        util.write_json(self.file, self._data)

    def reload(self) -> None:
        """丢弃内存缓存，重新从磁盘读取（多人/多进程写后用）。"""
        self._data = self._load()

    @property
    def packs(self) -> dict:
        return self._data.setdefault("packs", {})

    # ------------------------------------------------------------ 增删查
    def add(self, name: str, digest: str, *, version=None, public_key=None, signature=None) -> dict:
        """登记（或更新）一个整合包，返回条目副本。

        * 摘要变化且未提供新签名 → 旧签名作废（避免"旧签名验新内容"）；
        * 名字大小写敏感，前后空白会被去掉。
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("整合包名字不能为空")
        digest = (digest or "").strip().lower()
        if not _is_sha256(digest):
            raise ValueError(f"非法 SHA-256：{digest!r}")
        public_hex = (str(public_key).strip().lower() if public_key else None) or None
        signature_hex = (str(signature).strip().lower() if signature else None) or None
        for label, value in (("公钥", public_hex), ("签名", signature_hex)):
            if value is not None:
                try:
                    bytes.fromhex(value)
                except ValueError as exc:
                    raise ValueError(f"{label}不是合法十六进制：{value!r}") from exc
        now = util.now_iso()
        old = self.packs.get(name) or {}
        entry = dict(old)
        entry.update(
            {
                "name": name,
                "sha256": digest,
                "version": version if version is not None else old.get("version"),
                "public_key": public_hex or old.get("public_key"),
                "signature": signature_hex or old.get("signature"),
                "added": old.get("added") or now,
                "updated": now,
            }
        )
        if old.get("sha256") and old.get("sha256") != digest and not (public_hex and signature_hex):
            entry["signature"] = None
            LOG.info("整合包 %s 的摘要已变化，旧签名作废，请重新签名", name)
        self.packs[name] = entry
        self._save()
        return dict(entry)

    def lookup(self, name: str) -> dict | None:
        entry = self.packs.get((name or "").strip())
        return dict(entry) if isinstance(entry, dict) else None

    def list(self) -> list[dict]:
        entries = [dict(entry) for entry in self.packs.values() if isinstance(entry, dict)]
        entries.sort(key=lambda e: (str(e.get("name") or ""), str(e.get("version") or "")))
        return entries

    def remove(self, name: str) -> bool:
        name = (name or "").strip()
        if name not in self.packs:
            return False
        del self.packs[name]
        self._save()
        return True

    # ------------------------------------------------------------ 校验
    def verify(self, entry: dict, manifest: dict) -> bool:
        """校验清单签名：条目未签名时返回 True（内容完整性由摘要保证）。"""
        if not isinstance(entry, dict) or not isinstance(manifest, dict):
            return False
        parts = _sig_parts(entry, manifest)
        if parts is None:
            return False
        public_hex, signature_hex = parts
        if not public_hex and not signature_hex:
            return True  # 未签名：只做摘要层面的信任
        try:
            public = bytes.fromhex(public_hex)
            signature = bytes.fromhex(signature_hex)
        except ValueError:
            return False
        return ed25519.verify(public, canonical_manifest(manifest), signature)

    def verify_bytes(self, name: str, raw: bytes) -> bool:
        """校验一段清单原始字节：摘要必须与登记一致，然后验签。"""
        entry = self.lookup(name)
        if not entry:
            return False
        if util.sha256_bytes(raw) != entry.get("sha256"):
            return False
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False
        if not isinstance(manifest, dict):
            return False
        return self.verify(entry, manifest)

    def trusted(self, entry: dict) -> bool:
        """条目是否由本机 ``trusted_keys`` 中的公钥签名（用于"只信熟人"策略）。"""
        if not isinstance(entry, dict):
            return False
        public_hex = str(entry.get("public_key") or "").strip().lower()
        if not public_hex:
            return False
        for key in self._trusted_keys:
            if str(key or "").strip().lower() == public_hex:
                return True
        return False

    @property
    def _trusted_keys(self) -> list:
        cfg = getattr(self, "cfg", None)
        if cfg is None:
            return []
        keys = cfg.get("trusted_keys", []) or []
        return list(keys)
