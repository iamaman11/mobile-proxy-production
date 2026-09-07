from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime_asset_cache import get_or_fetch_runtime_archive


def test_cache_miss_then_verified_hit() -> None:
    payload = b"immutable-runtime-archive"
    digest = hashlib.sha256(payload).hexdigest()
    calls: list[Path] = []
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "cache"

        def fetch(destination: Path) -> None:
            calls.append(destination)
            destination.write_bytes(payload)

        first = get_or_fetch_runtime_archive(cache_root=root, expected_transport_sha256=digest, fetch=fetch)
        second = get_or_fetch_runtime_archive(cache_root=root, expected_transport_sha256=digest, fetch=fetch)
        assert first.state == "miss"
        assert second.state == "hit"
        assert first.path == second.path
        assert first.path.read_bytes() == payload
        assert len(calls) == 1
        assert first.path.stat().st_mode & 0o777 == 0o600


def test_corrupt_entry_is_repaired_atomically() -> None:
    payload = b"verified-runtime"
    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "cache"
        root.mkdir()
        entry = root / f"runtime-{digest}.tar.gz"
        entry.write_bytes(b"corrupt")
        result = get_or_fetch_runtime_archive(
            cache_root=root,
            expected_transport_sha256=digest,
            fetch=lambda destination: destination.write_bytes(payload),
        )
        assert result.state == "repaired"
        assert result.path.read_bytes() == payload
        assert not list(root.glob("*.tmp"))


def test_retention_is_bounded_and_only_runtime_archive_namespace_is_touched() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "cache"
        root.mkdir()
        unrelated = root / "do-not-touch"
        unrelated.write_bytes(b"x")
        for index in range(4):
            payload = f"runtime-{index}".encode()
            digest = hashlib.sha256(payload).hexdigest()
            result = get_or_fetch_runtime_archive(
                cache_root=root,
                expected_transport_sha256=digest,
                fetch=lambda destination, body=payload: destination.write_bytes(body),
            )
            assert result.path.exists()
        assert len(list(root.glob("runtime-*.tar.gz"))) == 3
        assert unrelated.read_bytes() == b"x"


if __name__ == "__main__":
    for value in list(globals().values()):
        if callable(value) and getattr(value, "__name__", "").startswith("test_"):
            value()
