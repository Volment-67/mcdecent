"""内容寻址存储（Content-Addressed Storage，CAS）。

这是"去中心化"的地基：任何内容（模组 jar、整合包清单、资源文件）都以它自己的
SHA-256 作为地址。于是：

  * 内容不可篡改：地址即校验和；
  * 副本可互换：只要哈希相同，来自 Mojang、镜像、还是隔壁同学的节点都等价；
  * 天生可 P2P：知道哈希就能向任意持有者索取，无需中心目录。

磁盘布局与 git 类似：``<root>/objects/sha256/<前两位>/<其余 62 位>``。
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Iterator

from . import util

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContentStore:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.objects = self.root / "objects" / "sha256"

    # ------------------------------------------------------------- 基本操作
    @staticmethod
    def is_hash(value: str) -> bool:
        return bool(_SHA256_RE.match(value or ""))

    def path_for(self, digest: str) -> Path:
        h = (digest or "").strip().lower()
        if not self.is_hash(h):
            raise ValueError(f"非法 SHA-256：{digest!r}")
        return self.objects / h[:2] / h[2:]

    def has(self, digest: str) -> bool:
        if not self.is_hash(digest):
            return False
        return self.path_for(digest).is_file()

    def get(self, digest: str) -> Path | None:
        if not self.has(digest):
            return None
        return self.path_for(digest)

    def size(self, digest: str) -> int | None:
        p = self.get(digest)
        return p.stat().st_size if p else None

    # ------------------------------------------------------------- 写入
    def put_file(self, src: str | os.PathLike, *, move: bool = False, verify: bool = True) -> str:
        """把一个文件放进 CAS，返回其 SHA-256。默认校验已有副本。"""
        src_p = Path(src)
        digest = util.sha256_file(src_p)
        dest = self.path_for(digest)
        if dest.is_file():
            if verify and dest.stat().st_size != src_p.stat().st_size:
                # 极少见的损坏：用源文件覆盖
                util.atomic_write_bytes(dest, src_p.read_bytes())
            if move:
                try:
                    src_p.unlink()
                except OSError:
                    pass
            return digest
        util.ensure_dir(dest.parent)
        if move:
            try:
                os.replace(src_p, dest)
                return digest
            except OSError:
                pass
        tmp = dest.with_name(f".tmp-{os.getpid()}-{dest.name}")
        shutil.copyfile(src_p, tmp)
        os.replace(tmp, dest)
        if move:
            try:
                src_p.unlink()
            except OSError:
                pass
        return digest

    def put_bytes(self, data: bytes) -> str:
        digest = util.sha256_bytes(data)
        dest = self.path_for(digest)
        if not dest.is_file():
            util.atomic_write_bytes(dest, data)
        return digest

    # ------------------------------------------------------------- 列举
    def iter_objects(self) -> Iterator[tuple[str, int]]:
        if not self.objects.is_dir():
            return
        for shard in sorted(self.objects.iterdir()):
            if not shard.is_dir():
                continue
            for blob in sorted(shard.iterdir()):
                if blob.is_file() and not blob.name.startswith("."):
                    yield shard.name + blob.name, blob.stat().st_size

    def index(self) -> dict[str, int]:
        return dict(self.iter_objects())

    def stat(self) -> dict:
        count = 0
        total = 0
        for _digest, size in self.iter_objects():
            count += 1
            total += size
        return {"objects": count, "bytes": total, "root": str(self.root)}

    def verify_all(self) -> tuple[int, list[str]]:
        ok, bad = 0, []
        for digest, _size in self.iter_objects():
            if util.sha256_file(self.path_for(digest)) == digest:
                ok += 1
            else:
                bad.append(digest)
        return ok, bad

    def delete(self, digest: str) -> bool:
        p = self.get(digest)
        if not p:
            return False
        try:
            p.unlink()
            return True
        except OSError:
            return False

    def add_directory(self, directory: str | os.PathLike, *, patterns: tuple[str, ...] = ("*",)) -> dict[str, str]:
        """把一个目录里的文件全部加入 CAS，返回 {相对路径: sha256}（用于整合包）。"""
        base = Path(directory)
        result: dict[str, str] = {}
        for root, _dirs, files in os.walk(base):
            for name in files:
                full = Path(root) / name
                if not any(full.match(p) for p in patterns):
                    continue
                rel = full.relative_to(base).as_posix()
                result[rel] = self.put_file(full)
        return result
