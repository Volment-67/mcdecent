"""整合包导出/导入（去中心化分发的核心）。

清单格式 ``mcdecent-pack/1`` 同时是"索引"和"可验证的地址"：
    * 每个文件按 SHA-256 存入 CAS，清单只引用摘要；
    * 清单自身的 JSON 也按 SHA-256 存入 CAS，因此 ``mcdecent://pack/<sha256>``
      就是一份自验证的整合包地址，任何持有该对象的节点都能分发；
    * 可选 Ed25519 签名（签名对象 = 去掉 signature 字段后的规范化 JSON），
      用于验证"这份清单确实出自作者"。

本模块对 ``mcdecent.ed25519`` 与 ``mcdecent.p2p`` 都做延迟导入 + 优雅降级：
它们还没就绪时，导出等于"不签名"，导入等于"只用本地 CAS"。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from . import util
from .cas import ContentStore

LOG = util.LOG

PACK_FORMAT = "mcdecent-pack/1"
_SAFE_PATH_RE = re.compile(r"^[A-Za-z]:")


# --------------------------------------------------------------------------- 基础工具


def _store(cfg) -> ContentStore:
    return ContentStore(cfg.cas_dir)


def canonical_manifest_bytes(manifest: dict) -> bytes:
    """清单的规范 JSON 字节：排序键、紧凑分隔符、UTF-8。

    签名与 CAS 地址都基于它，因此必须与文件写入格式完全一致。
    """
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_rel(raw) -> Path | None:
    """把清单/配置里的相对路径变成安全 Path；绝对路径、盘符、``..`` 一律拒绝。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("\\", "/")
    if _SAFE_PATH_RE.match(text) or text.startswith("/") or text.startswith("//"):
        return None
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    for part in parts:
        if ":" in part or "\x00" in part:
            return None
    return Path(*parts)


def _normalize_digest(ref) -> str:
    text = str(ref or "").strip().lower()
    if text.startswith("mcdecent://"):
        parts = [p for p in text[len("mcdecent://") :].split("/") if p]
        if len(parts) == 2 and parts[0] in ("pack", "cas", "sha256"):
            text = parts[1]
    if not ContentStore.is_hash(text):
        raise ValueError(f"非法的 SHA-256 引用：{ref!r}（需要 64 位十六进制）")
    return text


def _pack_filename(name: str, version: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .") or "pack"
    safe_ver = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(version or "1.0.0")).strip(" .") or "1.0.0"
    return f"{safe}-{safe_ver}.json"


# --------------------------------------------------------------------------- 收集文件


def collect_pack_files(cfg, include=("mods", "config")) -> list[dict]:
    """把 include 指定的文件加入 CAS，返回清单的 files 数组。"""
    game_dir = Path(cfg.game_dir)
    store = _store(cfg)
    entries: list[dict] = []
    seen: set[str] = set()
    for item in include or ():
        rel = _safe_rel(str(item))
        if rel is None:
            raise ValueError(f"include 项不合法（路径穿越/绝对路径）：{item!r}")
        src = game_dir / rel
        paths: list[Path] = []
        if src.is_file():
            paths = [src]
        elif src.is_dir():
            for root, dirs, names in os.walk(src):
                dirs.sort()
                names.sort()
                for name in names:
                    paths.append(Path(root) / name)
        else:
            LOG.warning("整合包包含项不存在，跳过：%s", rel.as_posix())
            continue
        for path in paths:
            try:
                rel_path = path.relative_to(game_dir).as_posix()
            except ValueError:
                LOG.warning("跳过不在 game_dir 内的文件：%s", path)
                continue
            if rel_path in seen:
                continue
            seen.add(rel_path)
            try:
                digest = store.put_file(path)
                size = path.stat().st_size
            except OSError as exc:
                raise RuntimeError(f"读取文件失败，无法导出整合包：{path}（{exc}）") from exc
            entries.append({"path": rel_path, "sha256": digest, "size": size, "urls": []})
    entries.sort(key=lambda entry: entry["path"])
    return entries


# --------------------------------------------------------------------------- 签名（可降级）


def _load_ed25519():
    """延迟加载 p2p-dev 提供的 Ed25519 模块；未就绪返回 None。"""
    try:
        from . import ed25519  # noqa: F401 - 延迟导入，避免核心未就绪时整个模块不可用
        return ed25519
    except Exception as exc:  # noqa: BLE001 - 模块缺失/导入期错误都按"不可用"处理
        LOG.warning("Ed25519 签名模块不可用（%r），整合包将以未签名形式处理", exc)
        return None


def _hex(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, str):
        return value.strip()
    return ""


def _secret_bytes(mod, key: str) -> bytes | None:
    """把 hex 私钥转换成 32 字节种子；格式不对时返回 None（调用方降级）。"""
    text = str(key or "").strip()
    parser = getattr(mod, "secret_from_hex", None)
    try:
        secret = parser(text) if callable(parser) else bytes.fromhex(text)
    except (ValueError, TypeError) as exc:
        LOG.warning("Ed25519 私钥格式非法（需要 64 位十六进制）：%r", exc)
        return None
    if not isinstance(secret, (bytes, bytearray)) or len(secret) != 32:
        LOG.warning("Ed25519 私钥必须是 32 字节")
        return None
    return bytes(secret)


def _try_sign(mod, secret: bytes, payload: bytes) -> tuple[str, str]:
    """适配 Ed25519 实现，返回 (签名hex, 公钥hex[可能为空])。"""
    attempts = ((secret, payload), (payload, secret), ({"private_key": secret, "message": payload},))
    for name in ("sign", "sign_bytes", "sign_detached", "sign_hex", "sign_message"):
        fn = getattr(mod, name, None)
        if not callable(fn):
            continue
        for args in attempts:
            try:
                result = fn(*args)
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Ed25519 %s 调用失败：%r", name, exc)
                break
            if isinstance(result, (tuple, list)) and len(result) == 2:
                return _hex(result[0]), _hex(result[1])
            sig = _hex(result)
            if sig:
                return sig, ""
    return "", ""


def _try_public_key(mod, secret: bytes) -> str:
    for name in ("public_key", "derive_public_key", "public_from_private", "get_public_key", "pubkey"):
        fn = getattr(mod, name, None)
        if not callable(fn):
            continue
        for args in ((secret,), ({"secret": secret},), ({"private_key": secret},)):
            try:
                result = fn(*args)
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Ed25519 %s 调用失败：%r", name, exc)
                break
            pub = _hex(result)
            if pub:
                return pub
    return ""


def _sign_manifest(manifest: dict, key: str) -> dict | None:
    """对清单签名；任何环节不可用都降级为 None（调用方记录警告）。"""
    if not key:
        return None
    mod = _load_ed25519()
    if mod is None:
        return None
    secret = _secret_bytes(mod, key)
    if secret is None:
        return None
    payload = canonical_manifest_bytes(manifest)
    try:
        sig, pub = _try_sign(mod, secret, payload)
        if not pub:
            pub = _try_public_key(mod, secret)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("整合包签名失败，降级为未签名：%r", exc)
        return None
    if not sig or not pub:
        LOG.warning("Ed25519 模块接口不匹配（缺少 sign/public_key），整合包降级为未签名")
        return None
    return {"alg": "ed25519", "public_key": pub, "sig": sig}


def _try_verify(mod, public_hex: str, payload: bytes, sig_hex: str) -> bool:
    try:
        public = bytes.fromhex(public_hex)
        signature = bytes.fromhex(sig_hex)
    except (ValueError, TypeError):
        LOG.warning("整合包签名格式非法（公钥/签名不是十六进制）")
        return False
    if len(public) != 32 or len(signature) != 64:
        LOG.warning("整合包签名长度非法（公钥 32 / 签名 64 字节）")
        return False
    for name in ("verify", "verify_signature", "verify_hex"):
        fn = getattr(mod, name, None)
        if not callable(fn):
            continue
        for args in ((public, payload, signature), (public, signature, payload)):
            try:
                return bool(fn(*args))
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Ed25519 %s 调用失败：%r", name, exc)
                return False
    return False


def verify_manifest_signature(manifest: dict) -> bool | None:
    """校验清单签名：True/False 为校验结果，None 表示无法校验（无签名或模块缺失）。"""
    signature = manifest.get("signature")
    if not isinstance(signature, dict) or not signature.get("sig"):
        return None
    mod = _load_ed25519()
    if mod is None:
        return None
    payload = canonical_manifest_bytes({k: v for k, v in manifest.items() if k != "signature"})
    try:
        ok = _try_verify(mod, str(signature.get("public_key") or ""), payload, str(signature.get("sig")))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("整合包签名校验异常：%r", exc)
        return None
    if not ok:
        LOG.warning("整合包签名无效：name=%s", manifest.get("name"))
    return ok


# --------------------------------------------------------------------------- 导出


def _detect_loader(cfg) -> dict:
    """尽力推断当前实例的加载器信息（版本目录 meta → 配置默认值）。"""
    try:
        from . import loaders

        info = loaders.detect_loader(cfg)
    except Exception as exc:  # noqa: BLE001 - 加载器信息缺失不应阻止导出
        LOG.debug("推断加载器失败：%r", exc)
        info = None
    if info:
        return {"type": info.get("type") or "fabric", "version": info.get("version")}
    default = cfg.get("default_loader")
    if isinstance(default, str) and default:
        return {"type": default, "version": None}
    return {}


def export_pack(
    cfg,
    name: str,
    *,
    game_version: str,
    version: str = "1.0.0",
    include=("mods", "config"),
    out=None,
    key: str | None = None,
    loader: dict | None = None,
    author: str | None = None,
) -> dict:
    """导出整合包：文件入 CAS → 生成清单 → 清单入 CAS → 写 packs/ 副本。"""
    if not str(name or "").strip():
        raise ValueError("整合包名称不能为空")
    if not str(game_version or "").strip():
        raise ValueError("必须指定整合包对应的游戏版本（--game-version）")

    store = _store(cfg)
    files = collect_pack_files(cfg, include)
    if not files:
        LOG.warning("整合包 %s 没有收集到任何文件（include=%s）", name, list(include or ()))
    manifest: dict = {
        "format": PACK_FORMAT,
        "name": str(name),
        "version": str(version or "1.0.0"),
        "game_version": str(game_version),
        "loader": loader if loader is not None else _detect_loader(cfg),
        "author": author if author is not None else str(cfg.get("player.name") or ""),
        "created": util.now_iso(),
        "files": files,
    }
    signature = _sign_manifest(manifest, key) if key else None
    if signature:
        manifest["signature"] = signature
    else:
        if key:
            LOG.warning("已请求签名但签名不可用，整合包将以未签名形式导出")

    data = canonical_manifest_bytes(manifest)
    digest = store.put_bytes(data)  # 清单本身即内容寻址对象
    pack_path = Path(cfg.packs_dir) / _pack_filename(str(name), str(version or "1.0.0"))
    util.atomic_write_bytes(pack_path, data)
    out_path = None
    if out:
        out_path = Path(out)
        util.atomic_write_bytes(out_path, data)

    total = sum(int(entry.get("size") or 0) for entry in files)
    LOG.info("已导出整合包 %s %s：%d 个文件，%s，清单 %s", name, version, len(files), util.human_size(total), digest[:12])
    result = {
        "format": PACK_FORMAT,
        "name": manifest["name"],
        "version": manifest["version"],
        "game_version": manifest["game_version"],
        "loader": manifest["loader"],
        "digest": digest,
        "sha256": digest,
        "url": f"mcdecent://pack/{digest}",
        "path": str(pack_path),
        "cas_path": str(store.path_for(digest)),
        "files": len(files),
        "bytes": total,
        "signed": bool(signature),
        "manifest": manifest,
    }
    if out_path is not None:
        result["out"] = str(out_path)
    return result


# --------------------------------------------------------------------------- 导入


def load_manifest(cfg, digest: str) -> dict:
    """从 CAS 读取并校验一份清单（digest 可为 64 位 sha256 或 mcdecent://pack/<sha>）。"""
    digest = _normalize_digest(digest)
    blob = _store(cfg).get(digest)
    if blob is None:
        raise FileNotFoundError(f"本地 CAS 中找不到整合包清单：{digest}")
    raw = blob.read_bytes()
    if util.sha256_bytes(raw) != digest:
        raise ValueError(f"整合包清单已损坏（摘要不符）：{digest}")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"整合包清单不是合法 JSON：{digest}") from exc
    _validate_manifest(manifest, digest)
    return manifest


def _validate_manifest(manifest, digest: str = "") -> None:
    if not isinstance(manifest, dict):
        raise ValueError(f"整合包清单格式错误（不是 JSON 对象）：{digest}")
    fmt = manifest.get("format")
    if fmt != PACK_FORMAT:
        raise ValueError(f"不支持的整合包格式：{fmt!r}（需要 {PACK_FORMAT}）")
    if not isinstance(manifest.get("files"), list):
        raise ValueError(f"整合包清单缺少 files 数组：{digest}")


def _fetch_peer_blob(cfg, digest: str, peer=None) -> Path | None:
    """尝试用 mcdecent.p2p 从节点取一个 CAS 对象；接口不兼容时给出明确警告。"""
    store = _store(cfg)
    try:
        from . import p2p
    except Exception as exc:  # noqa: BLE001
        LOG.warning("P2P 模块不可用，无法从节点获取 %s：%r", digest[:12], exc)
        return None
    calls: list[tuple[tuple, dict]] = []
    if peer:
        calls.append(((cfg, digest), {"peers": [peer]}))
        calls.append(((cfg, digest), {"peer": peer}))
    calls.append(((cfg, digest), {}))
    for name in ("fetch_blob", "fetch_object", "fetch_into_cas", "download_blob", "fetch_pack"):
        fn = getattr(p2p, name, None)
        if not callable(fn):
            continue
        for args, kwargs in calls:
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 节点不可达/P2P 未就绪都按"取不到"处理
                LOG.debug("P2P %s 获取 %s 失败：%r", name, digest[:12], exc)
                continue
            if isinstance(result, (bytes, bytearray)):
                store.put_bytes(bytes(result))
            elif isinstance(result, dict):
                pass  # 例如 fetch_pack 已自行写入 CAS
            elif result is not None:
                path = Path(result)
                if path.is_file() and util.sha256_file(path) == digest:
                    store.put_file(path, verify=False)
            blob = store.get(digest)
            if blob is not None:
                return blob
    LOG.warning("P2P 未能提供对象 %s（可能没有可用节点）", digest[:12])
    return None


def _load_by_digest(cfg, digest: str, peer=None, *, source: str = "cas") -> tuple[dict, str, str]:
    digest = _normalize_digest(digest)
    store = _store(cfg)
    blob = store.get(digest)
    if blob is None and peer:
        blob = _fetch_peer_blob(cfg, digest, peer)
    if blob is None:
        hint = "从 P2P 节点获取失败" if peer else "可加 --peer 从 P2P 节点获取"
        raise FileNotFoundError(f"本地 CAS 中找不到整合包清单 {digest}（{hint}）")
    raw = blob.read_bytes()
    if util.sha256_bytes(raw) != digest:
        raise ValueError(f"整合包清单摘要不符，拒绝导入：{digest}")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"整合包清单不是合法 JSON：{digest}") from exc
    _validate_manifest(manifest, digest)
    return manifest, digest, source


def import_pack(
    cfg,
    ref,
    *,
    peer=None,
    no_mods: bool = False,
    verify_signature: bool = True,
    strict_signature: bool = False,
) -> dict:
    """导入整合包：解析清单 → 校验所有路径/摘要 → 从 CAS 还原文件到 game_dir。"""
    text = str(ref or "").strip()
    if not text:
        raise ValueError("必须提供整合包引用（本地清单路径、sha256 或 mcdecent://pack/<sha256>）")

    # 1) 解析清单：本地文件 / CAS 摘要 / mcdecent:// 地址（可经 P2P 拉取）
    if text.startswith("mcdecent://"):
        manifest, digest, source = _load_by_digest(cfg, text, peer, source=text)
    elif ContentStore.is_hash(text.lower()):
        manifest, digest, source = _load_by_digest(cfg, text, peer, source="cas")
    elif Path(text).is_file():
        raw = Path(text).read_bytes()
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"整合包清单不是合法 JSON：{text}") from exc
        _validate_manifest(manifest, text)
        digest = _store(cfg).put_bytes(raw)  # 让本地清单也变成可寻址对象
        source = str(text)
    else:
        raise FileNotFoundError(f"找不到整合包引用：{text}（既不是本地文件，也不是 64 位 sha256）")

    # 2) 先整体校验，绝不"写一半才发现坏包"
    store = _store(cfg)
    game_dir = Path(cfg.game_dir).resolve()
    plan: list[tuple[str, str, Path]] = []
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise ValueError(f"整合包文件条目格式错误：{entry!r}")
        rel = _safe_rel(entry.get("path"))
        if rel is None:
            raise ValueError(f"整合包包含非法路径（路径穿越/绝对路径），已拒绝导入：{entry.get('path')!r}")
        rel_text = rel.as_posix()
        if no_mods and (rel_text == "mods" or rel_text.startswith("mods/")):
            continue
        digest_file = str(entry.get("sha256") or "").strip().lower()
        if not ContentStore.is_hash(digest_file):
            raise ValueError(f"整合包文件条目缺少合法 sha256：{rel_text}")
        blob = store.get(digest_file)
        if blob is None and peer:
            blob = _fetch_peer_blob(cfg, digest_file, peer)
        if blob is None:
            raise FileNotFoundError(f"缺少对象 {digest_file[:12]}…（{rel_text}），无法导入整合包")
        if util.sha256_file(blob) != digest_file:
            raise ValueError(f"CAS 对象已损坏（摘要不符），拒绝导入：{digest_file}")
        plan.append((rel_text, digest_file, blob))

    sig_valid = verify_manifest_signature(manifest) if verify_signature else None
    if strict_signature and sig_valid is not True:
        raise ValueError("整合包签名校验未通过（或无法校验），已按 --strict-signature 拒绝导入")

    # 3) 落盘
    written: list[str] = []
    skipped: list[str] = []
    for rel_text, digest_file, blob in plan:
        dest = game_dir / rel_text
        resolved = dest.resolve()
        if resolved != game_dir and game_dir not in resolved.parents:
            raise ValueError(f"整合包路径越界，已拒绝写入：{rel_text}")
        if resolved.is_file() and util.sha256_file(resolved) == digest_file:
            skipped.append(rel_text)
            continue
        util.ensure_dir(resolved.parent)
        tmp = resolved.with_name(f".{resolved.name}.part")
        shutil.copyfile(blob, tmp)
        os.replace(tmp, resolved)
        written.append(rel_text)

    LOG.info("已导入整合包 %s %s：写入 %d 个文件，跳过 %d 个", manifest.get("name"), manifest.get("version"), len(written), len(skipped))
    return {
        "format": PACK_FORMAT,
        "name": manifest.get("name"),
        "version": manifest.get("version"),
        "game_version": manifest.get("game_version"),
        "loader": manifest.get("loader") or {},
        "digest": digest,
        "url": f"mcdecent://pack/{digest}",
        "source": source,
        "files": len(plan),
        "written": written,
        "skipped": skipped,
        "signature_valid": sig_valid,
        "manifest": manifest,
    }


# --------------------------------------------------------------------------- 本地清单列表


def list_packs(cfg) -> list[dict]:
    """列出 packs/ 下的整合包清单（按名称排序）。"""
    directory = Path(cfg.packs_dir)
    out: list[dict] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            raw = path.read_bytes()
            manifest = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            LOG.warning("无法读取整合包清单 %s：%r", path, exc)
            continue
        if not isinstance(manifest, dict):
            LOG.warning("整合包清单不是 JSON 对象，跳过：%s", path)
            continue
        out.append(
            {
                "name": manifest.get("name") or path.stem,
                "version": manifest.get("version"),
                "game_version": manifest.get("game_version"),
                "loader": manifest.get("loader") or {},
                "format": manifest.get("format"),
                "created": manifest.get("created"),
                "files": len(manifest.get("files") or []),
                "signed": isinstance(manifest.get("signature"), dict),
                "path": str(path),
                "digest": util.sha256_bytes(raw),
            }
        )
    out.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("version") or "")))
    return out
