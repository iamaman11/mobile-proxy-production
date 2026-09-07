"""Verified persistent cache for the immutable Product Release digest tool.

Only the compiled `product-release-asset-digest` executable and a strict build
identity manifest are retained. Runtime archives, extracted trees, rendered
configuration, credentials, and digest verification results are never cached.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BINARY_NAME = "product-release-asset-digest"
_SCHEMA = "product-release-asset-digest-cache.v1"
_PROFILE = "release"
_BUILD_FLAGS = (
    "--locked",
    "--release",
    "-p",
    "operator-cli",
    "--bin",
    _BINARY_NAME,
)
_KEEP_ENTRIES = 3


class VerifierToolCacheError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifierToolIdentity:
    source_sha: str
    cargo_lock_sha256: str
    rustc_version: str
    host_target: str
    cargo_profile: str = _PROFILE
    build_flags: tuple[str, ...] = _BUILD_FLAGS

    def __post_init__(self) -> None:
        if _SHA.fullmatch(self.source_sha) is None:
            raise VerifierToolCacheError("verifier Product source SHA is invalid")
        if _SHA256.fullmatch(self.cargo_lock_sha256) is None:
            raise VerifierToolCacheError("verifier Cargo.lock digest is invalid")
        if not self.rustc_version or "\n" in self.rustc_version:
            raise VerifierToolCacheError("verifier rustc version is invalid")
        if not self.host_target or "\n" in self.host_target:
            raise VerifierToolCacheError("verifier host target is invalid")
        if self.cargo_profile != _PROFILE or tuple(self.build_flags) != _BUILD_FLAGS:
            raise VerifierToolCacheError("verifier build contract differs")

    def metadata(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "source_sha": self.source_sha,
            "cargo_lock_sha256": self.cargo_lock_sha256,
            "rustc_version": self.rustc_version,
            "host_target": self.host_target,
            "cargo_profile": self.cargo_profile,
            "build_flags": list(self.build_flags),
        }

    def cache_key(self) -> str:
        raw = json.dumps(self.metadata(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class VerifierToolCacheResult:
    path: Path
    state: str  # hit | miss | repaired
    identity: VerifierToolIdentity


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise VerifierToolCacheError("verifier cache file is unreadable") from exc
    return digest.hexdigest()


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _safe_directory(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISDIR(mode) and not stat.S_ISLNK(mode)


def _run_text(command: list[str], *, cwd: Path | None = None, timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise VerifierToolCacheError("verifier toolchain identity command failed") from exc
    return result.stdout.strip()


def derive_verifier_tool_identity(*, product_root: Path, source_sha: str) -> VerifierToolIdentity:
    if not product_root.is_dir() or _SHA.fullmatch(source_sha) is None:
        raise VerifierToolCacheError("verifier Product source identity input is invalid")
    cargo_lock = product_root / "Cargo.lock"
    if not _regular(cargo_lock):
        raise VerifierToolCacheError("verifier Cargo.lock is unavailable")
    rustc_version = _run_text(["rustc", "--version"])
    verbose = _run_text(["rustc", "--version", "--verbose"])
    host_lines = [line.removeprefix("host: ") for line in verbose.splitlines() if line.startswith("host: ")]
    if len(host_lines) != 1 or not host_lines[0]:
        raise VerifierToolCacheError("verifier rustc host target is unavailable")
    return VerifierToolIdentity(
        source_sha=source_sha,
        cargo_lock_sha256=_sha256(cargo_lock),
        rustc_version=rustc_version,
        host_target=host_lines[0],
    )


def _prepare_root(root: Path) -> None:
    if not root.is_absolute():
        raise VerifierToolCacheError("verifier cache root must be absolute")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _safe_directory(root):
        raise VerifierToolCacheError("verifier cache root is unsafe")


def _entry(root: Path, identity: VerifierToolIdentity) -> Path:
    return root / f"{_BINARY_NAME}-{identity.cache_key()}"


def _remove_entry(entry: Path) -> None:
    if entry.is_symlink() or entry.is_file():
        entry.unlink(missing_ok=True)
        return
    if entry.exists():
        shutil.rmtree(entry)


def _manifest_matches(path: Path, identity: VerifierToolIdentity, binary: Path) -> bool:
    if not _regular(path) or not _regular(binary):
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    expected = identity.metadata()
    if not isinstance(value, dict) or set(value) != set(expected) | {"binary_sha256"}:
        return False
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            return False
    binary_digest = value.get("binary_sha256")
    return isinstance(binary_digest, str) and _SHA256.fullmatch(binary_digest) is not None and _sha256(binary) == binary_digest


def _evict(root: Path, *, protected: Path, keep: int = _KEEP_ENTRIES) -> None:
    entries = [
        path
        for path in root.glob(f"{_BINARY_NAME}-*")
        if path != protected and _safe_directory(path)
    ]
    entries.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    for path in entries[max(keep - 1, 0):]:
        _remove_entry(path)


def _cargo_build_verifier(*, product_root: Path, destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="verifier-build-") as raw:
        target_dir = Path(raw) / "target"
        environment = dict(os.environ)
        environment["CARGO_TARGET_DIR"] = str(target_dir)
        try:
            subprocess.run(
                ["cargo", "build", *_BUILD_FLAGS],
                cwd=product_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=600,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise VerifierToolCacheError("Product Release verifier build failed") from exc
        built = target_dir / _PROFILE / _BINARY_NAME
        if not _regular(built) or built.stat().st_size <= 0:
            raise VerifierToolCacheError("Product Release verifier build output is unavailable")
        shutil.copyfile(built, destination)
        os.chmod(destination, 0o500)


def get_or_build_verifier_tool(
    *,
    cache_root: Path,
    product_root: Path,
    identity: VerifierToolIdentity,
    build_binary: Callable[[Path], None] | None = None,
) -> VerifierToolCacheResult:
    """Return a per-use verified verifier binary, atomically publishing misses.

    Cache corruption removes only the exact identity entry and triggers a safe
    rebuild. The cached executable is re-hashed against its manifest on every
    use. No verification result or runtime/config state is persisted here.
    """

    _prepare_root(cache_root)
    entry = _entry(cache_root, identity)
    lock = cache_root / ".verifier-tool-cache.lock"
    if lock.exists() and not _regular(lock):
        raise VerifierToolCacheError("verifier cache lock is unsafe")
    with lock.open("a+b") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        repaired = False
        try:
            if entry.exists() or entry.is_symlink():
                binary = entry / _BINARY_NAME
                manifest = entry / "manifest.json"
                if _safe_directory(entry) and _manifest_matches(manifest, identity, binary):
                    os.utime(entry, None)
                    return VerifierToolCacheResult(path=binary, state="hit", identity=identity)
                _remove_entry(entry)
                repaired = True

            temporary = Path(tempfile.mkdtemp(prefix=".verifier-entry-", dir=cache_root))
            try:
                binary = temporary / _BINARY_NAME
                builder = build_binary or (
                    lambda destination: _cargo_build_verifier(
                        product_root=product_root,
                        destination=destination,
                    )
                )
                builder(binary)
                if not _regular(binary) or binary.stat().st_size <= 0:
                    raise VerifierToolCacheError("fresh verifier binary is unavailable")
                os.chmod(binary, 0o500)
                binary_digest = _sha256(binary)
                manifest_value = {**identity.metadata(), "binary_sha256": binary_digest}
                manifest = temporary / "manifest.json"
                manifest.write_text(
                    json.dumps(manifest_value, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                os.chmod(manifest, 0o600)
                if not _manifest_matches(manifest, identity, binary):
                    raise VerifierToolCacheError("fresh verifier cache identity verification failed")
                os.replace(temporary, entry)
                _evict(cache_root, protected=entry)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            return VerifierToolCacheResult(
                path=entry / _BINARY_NAME,
                state="repaired" if repaired else "miss",
                identity=identity,
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
