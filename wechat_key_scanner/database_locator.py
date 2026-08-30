"""Locate account databases and read a stable encrypted first page."""

from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path
from typing import Callable

from .models import DatabaseIdentity


def _account_id(path: Path) -> str:
    for parent in path.parents:
        if parent.name.lower().startswith("wxid_"):
            return parent.name
    if path.parent.name.lower() == "message" and len(path.parents) >= 3:
        return path.parents[2].name
    return path.parent.name


def _is_plain_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _default_roots() -> list[Path]:
    profile = Path(os.environ.get("USERPROFILE", Path.home()))
    documents = profile / "Documents"
    return [
        documents / "xwechat_files",
        documents / "WeChat Files",
        profile / "xwechat_files",
        profile / "WeChat Files",
    ]


def discover_databases(data_root: str | Path | None = None) -> list[DatabaseIdentity]:
    roots = [Path(data_root)] if data_root is not None else _default_roots()
    found: dict[Path, DatabaseIdentity] = {}
    for root in roots:
        if not root.is_dir():
            continue
        try:
            candidates = root.rglob("message_0.db")
            for candidate in candidates:
                if not _is_plain_file(candidate):
                    continue
                resolved = candidate.resolve(strict=True)
                if not _is_plain_file(resolved):
                    continue
                found[resolved] = DatabaseIdentity(resolved, _account_id(resolved))
        except OSError:
            continue
    return [found[path] for path in sorted(found, key=lambda value: str(value).lower())]


def _fingerprint(value) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def read_stable_page1(
    database: DatabaseIdentity,
    *,
    stat: Callable[[Path], object] | None = None,
) -> bytes:
    path = Path(database.path)
    if path.is_symlink():
        raise OSError("database must not be a symlink")
    stat_func = stat or (lambda item: os.stat(item, follow_symlinks=False))
    before = stat_func(path)
    if not stat_module.S_ISREG(before.st_mode):
        raise OSError("database is not a regular file")
    if before.st_size < 4096:
        raise OSError("database is shorter than 4096 bytes")
    with path.open("rb") as stream:
        page = stream.read(4096)
    after = stat_func(path)
    if _fingerprint(before) != _fingerprint(after):
        raise OSError("database changed while reading page one")
    if len(page) != 4096:
        raise OSError("database did not provide a complete 4096-byte page")
    return page
