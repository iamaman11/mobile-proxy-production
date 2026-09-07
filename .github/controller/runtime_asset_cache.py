"""Small, verified, service-owned cache for immutable phone-runtime archives.

Only public Release bytes are cached. Extracted trees and rendered configuration
are deliberately never retained because they can contain secret-derived data.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class RuntimeAssetCacheError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeAssetCacheResult:
    path: Path
    state: str  # disabled | hit | miss | repaired


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _cache_entry(root: Path, digest: str) -> Path:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise RuntimeAssetCacheError("runtime archive transport digest is invalid")
    return root / f"runtime-{digest}.tar.gz"


def _prepare_root(root: Path) -> None:
    if not root.is_absolute():
        raise RuntimeAssetCacheError("runtime cache root must be absolute")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = root.lstat().st_mode
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise RuntimeAssetCacheError("runtime cache root is unsafe")


def _evict(root: Path, *, protected: Path, keep: int = 3) -> None:
    entries = [path for path in root.glob("runtime-*.tar.gz") if _regular(path)]
    entries = [path for path in entries if path != protected]
    entries.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    for path in entries[max(keep - 1, 0):]:
        path.unlink(missing_ok=True)


def get_or_fetch_runtime_archive(
    *, cache_root: Path, expected_transport_sha256: str, fetch: Callable[[Path], None]
) -> RuntimeAssetCacheResult:
    """Return a locally verified immutable archive, publishing only atomically.

    A bad cache entry is removed only inside the controlled cache namespace and
    replaced through an atomic rename after a fresh, caller-verified download.
    """
    _prepare_root(cache_root)
    entry = _cache_entry(cache_root, expected_transport_sha256)
    lock = cache_root / ".runtime-cache.lock"
    if lock.exists() and not _regular(lock):
        raise RuntimeAssetCacheError("runtime cache lock is unsafe")
    with lock.open("a+b") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        repaired = False
        try:
            if entry.exists() or entry.is_symlink():
                if _regular(entry) and _sha256(entry) == expected_transport_sha256:
                    os.utime(entry, None)
                    return RuntimeAssetCacheResult(path=entry, state="hit")
                entry.unlink(missing_ok=True)
                repaired = True
            fd, temporary_name = tempfile.mkstemp(prefix=".runtime-", suffix=".tmp", dir=cache_root)
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                os.chmod(temporary, 0o600)
                fetch(temporary)
                if not _regular(temporary) or _sha256(temporary) != expected_transport_sha256:
                    raise RuntimeAssetCacheError("fresh runtime archive transport digest differs")
                os.replace(temporary, entry)
                os.chmod(entry, 0o600)
                _evict(cache_root, protected=entry)
            finally:
                temporary.unlink(missing_ok=True)
            return RuntimeAssetCacheResult(path=entry, state="repaired" if repaired else "miss")
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
