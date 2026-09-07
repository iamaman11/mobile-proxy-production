"""Verified persistent cache for immutable PRODUCT runtime tooling.

Only two compiled executables are retained: `product-release-asset-digest` for
fresh Release content verification and `operator-cli` for fresh secret-derived
runtime rendering. Runtime archives, extracted trees, rendered configuration,
credentials, and verification results are deliberately never cached.
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
_SCHEMA = "product-runtime-tool-cache.v1"
_PROFILE = "release"
_VERIFIER_BINARY = "product-release-asset-digest"
_RENDERER_BINARY = "operator-cli"
_BINARY_NAMES = (_VERIFIER_BINARY, _RENDERER_BINARY)
_BUILD_FLAGS = (
    "--locked",
    "--release",
    "-p",
    "operator-cli",
    "--bin",
    _VERIFIER_BINARY,
    "--bin",
    _RENDERER_BINARY,
)
_KEEP_ENTRIES = 3
_FIXED_BUILD_ENV_KEYS = (
    "RUSTFLAGS",
    "CARGO_ENCODED_RUSTFLAGS",
    "RUSTC_WRAPPER",
    "RUSTC_WORKSPACE_WRAPPER",
    "CARGO_BUILD_TARGET",
    "RUSTC_BOOTSTRAP",
)


class ProductToolCacheError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProductToolIdentity:
    source_sha: str
    cargo_lock_sha256: str
    rustc_version: str
    host_target: str
    cargo_profile: str = _PROFILE
    build_flags: tuple[str, ...] = _BUILD_FLAGS

    def __post_init__(self) -> None:
        if _SHA.fullmatch(self.source_sha) is None:
            raise ProductToolCacheError("Product tool source SHA is invalid")
        if _SHA256.fullmatch(self.cargo_lock_sha256) is None:
            raise ProductToolCacheError("Product tool Cargo.lock digest is invalid")
        if not self.rustc_version or "\n" in self.rustc_version:
            raise ProductToolCacheError("Product tool rustc version is invalid")
        if not self.host_target or "\n" in self.host_target:
            raise ProductToolCacheError("Product tool host target is invalid")
        if not self.cargo_profile or "\n" in self.cargo_profile:
            raise ProductToolCacheError("Product tool Cargo profile is invalid")
        if (
            not isinstance(self.build_flags, tuple)
            or not self.build_flags
            or any(not isinstance(item, str) or not item or "\n" in item for item in self.build_flags)
        ):
            raise ProductToolCacheError("Product tool build flags are invalid")

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
class ProductToolCacheResult:
    verifier_path: Path
    renderer_path: Path
    state: str  # hit | miss | repaired
    identity: ProductToolIdentity


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ProductToolCacheError("Product tool cache file is unreadable") from exc
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


def _run_text(command: list[str], *, timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ProductToolCacheError("Product toolchain identity command failed") from exc
    return result.stdout.strip()


def derive_product_tool_identity(*, product_root: Path, source_sha: str) -> ProductToolIdentity:
    if not product_root.is_dir() or _SHA.fullmatch(source_sha) is None:
        raise ProductToolCacheError("Product tool source identity input is invalid")
    cargo_lock = product_root / "Cargo.lock"
    if not _regular(cargo_lock):
        raise ProductToolCacheError("Product tool Cargo.lock is unavailable")
    rustc_version = _run_text(["rustc", "--version"])
    verbose = _run_text(["rustc", "--version", "--verbose"])
    host_lines = [line.removeprefix("host: ") for line in verbose.splitlines() if line.startswith("host: ")]
    if len(host_lines) != 1 or not host_lines[0]:
        raise ProductToolCacheError("Product tool rustc host target is unavailable")
    return ProductToolIdentity(
        source_sha=source_sha,
        cargo_lock_sha256=_sha256(cargo_lock),
        rustc_version=rustc_version,
        host_target=host_lines[0],
        cargo_profile=_PROFILE,
        build_flags=_BUILD_FLAGS,
    )


def _prepare_root(root: Path) -> None:
    if not root.is_absolute():
        raise ProductToolCacheError("Product tool cache root must be absolute")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _safe_directory(root):
        raise ProductToolCacheError("Product tool cache root is unsafe")


def _entry(root: Path, identity: ProductToolIdentity) -> Path:
    return root / f"product-runtime-tools-{identity.cache_key()}"


def _remove_entry(entry: Path) -> None:
    if entry.is_symlink() or entry.is_file():
        entry.unlink(missing_ok=True)
        return
    if entry.exists():
        shutil.rmtree(entry)


def _manifest_matches(path: Path, identity: ProductToolIdentity, entry: Path) -> bool:
    if not _regular(path) or not _safe_directory(entry):
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
    binary_digests = value.get("binary_sha256")
    if not isinstance(binary_digests, dict) or set(binary_digests) != set(_BINARY_NAMES):
        return False
    for name in _BINARY_NAMES:
        expected_digest = binary_digests.get(name)
        binary = entry / name
        if (
            not isinstance(expected_digest, str)
            or _SHA256.fullmatch(expected_digest) is None
            or not _regular(binary)
            or _sha256(binary) != expected_digest
        ):
            return False
    return True


def _evict(root: Path, *, protected: Path, keep: int = _KEEP_ENTRIES) -> None:
    entries = [
        path
        for path in root.glob("product-runtime-tools-*")
        if path != protected and _safe_directory(path)
    ]
    entries.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    for path in entries[max(keep - 1, 0):]:
        _remove_entry(path)


def _controlled_build_environment(*, target_dir: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key in _FIXED_BUILD_ENV_KEYS:
        environment.pop(key, None)
    for key in tuple(environment):
        if key.startswith("CARGO_PROFILE_RELEASE_"):
            environment.pop(key, None)
    environment["CARGO_TARGET_DIR"] = str(target_dir)
    return environment


def _cargo_build_tools(
    *,
    product_root: Path,
    verifier_destination: Path,
    renderer_destination: Path,
    identity: ProductToolIdentity,
) -> None:
    with tempfile.TemporaryDirectory(prefix="product-tool-build-") as raw:
        target_dir = Path(raw) / "target"
        environment = _controlled_build_environment(target_dir=target_dir)
        try:
            subprocess.run(
                ["cargo", "build", *identity.build_flags, "--target", identity.host_target],
                cwd=product_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=600,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ProductToolCacheError("Product runtime tooling build failed") from exc
        build_root = target_dir / identity.host_target / identity.cargo_profile
        for name, destination in (
            (_VERIFIER_BINARY, verifier_destination),
            (_RENDERER_BINARY, renderer_destination),
        ):
            built = build_root / name
            if not _regular(built) or built.stat().st_size <= 0:
                raise ProductToolCacheError("Product runtime tooling build output is unavailable")
            shutil.copyfile(built, destination)
            os.chmod(destination, 0o500)


def get_or_build_product_tools(
    *,
    cache_root: Path,
    product_root: Path,
    identity: ProductToolIdentity,
    build_tools: Callable[[Path, Path], None] | None = None,
) -> ProductToolCacheResult:
    """Return a per-run verified immutable Product tooling bundle.

    Cache corruption removes only the exact identity entry and triggers a safe
    rebuild. Both executable SHA-256 values and the full build identity are
    verified on every cache use. No runtime state or verification result is
    persisted in this namespace.
    """

    _prepare_root(cache_root)
    entry = _entry(cache_root, identity)
    lock = cache_root / ".product-tool-cache.lock"
    if lock.exists() and not _regular(lock):
        raise ProductToolCacheError("Product tool cache lock is unsafe")
    with lock.open("a+b") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        repaired = False
        try:
            manifest = entry / "manifest.json"
            if entry.exists() or entry.is_symlink():
                if _manifest_matches(manifest, identity, entry):
                    os.utime(entry, None)
                    return ProductToolCacheResult(
                        verifier_path=entry / _VERIFIER_BINARY,
                        renderer_path=entry / _RENDERER_BINARY,
                        state="hit",
                        identity=identity,
                    )
                _remove_entry(entry)
                repaired = True

            temporary = Path(tempfile.mkdtemp(prefix=".product-tools-", dir=cache_root))
            try:
                verifier = temporary / _VERIFIER_BINARY
                renderer = temporary / _RENDERER_BINARY
                builder = build_tools or (
                    lambda verifier_destination, renderer_destination: _cargo_build_tools(
                        product_root=product_root,
                        verifier_destination=verifier_destination,
                        renderer_destination=renderer_destination,
                        identity=identity,
                    )
                )
                builder(verifier, renderer)
                for binary in (verifier, renderer):
                    if not _regular(binary) or binary.stat().st_size <= 0:
                        raise ProductToolCacheError("fresh Product tool binary is unavailable")
                    os.chmod(binary, 0o500)
                manifest_value = {
                    **identity.metadata(),
                    "binary_sha256": {name: _sha256(temporary / name) for name in _BINARY_NAMES},
                }
                manifest = temporary / "manifest.json"
                manifest.write_text(
                    json.dumps(manifest_value, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                os.chmod(manifest, 0o600)
                if not _manifest_matches(manifest, identity, temporary):
                    raise ProductToolCacheError("fresh Product tool cache identity verification failed")
                os.replace(temporary, entry)
                _evict(cache_root, protected=entry)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            return ProductToolCacheResult(
                verifier_path=entry / _VERIFIER_BINARY,
                renderer_path=entry / _RENDERER_BINARY,
                state="repaired" if repaired else "miss",
                identity=identity,
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
