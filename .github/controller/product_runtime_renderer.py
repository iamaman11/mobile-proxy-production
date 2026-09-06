from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Mapping

from phone_runtime import PhoneRuntimeMaterialization, PhoneRuntimeRefused

_PRODUCT_COMPONENT_CONTRACT = Path("contracts/operations/phone-production-release-components-v1.json")
_TYPED_DIGEST = re.compile(r"b3:[0-9a-f]{64}")
_SHA = re.compile(r"[0-9a-f]{40}")
_NATIVE_COMPONENTS = frozenset({"runtime-supervisor", "host-daemon", "sing-box"})
_TUNNEL_OWNER = "first_party_reverse_tunnel"
_PRODUCT_RELEASE_COMPONENT_DIGEST_FAILURE = "PRODUCT_RELEASE_COMPONENT_DIGEST_COMMAND_FAILED"
_PRODUCT_RUNTIME_RENDER_FAILURE = "PRODUCT_RUNTIME_RENDER_COMMAND_FAILED"


class ProductReleaseComponentDigestRefused(PhoneRuntimeRefused):
    pass


class ProductRuntimeRenderRefused(PhoneRuntimeRefused):
    pass


def _safe_relative(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise PhoneRuntimeRefused(f"{label} is invalid")
    path = PurePosixPath(raw)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise PhoneRuntimeRefused(f"{label} escapes PRODUCT root")
    return path.as_posix()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PhoneRuntimeRefused("PRODUCT renderer input is unreadable") from exc
    return digest.hexdigest()


def _run_checked(
    command: list[str], *, cwd: Path, timeout: int,
    environment: Mapping[str, str] | None = None,
) -> str:
    try:
        result = subprocess.run(
            command, cwd=cwd, env=None if environment is None else dict(environment),
            capture_output=True, text=True, timeout=timeout, check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PhoneRuntimeRefused("PRODUCT runtime verification/render command failed") from exc
    return result.stdout.strip()


def verify_product_source(*, product_root: Path, expected_source_sha: str) -> None:
    if _SHA.fullmatch(expected_source_sha) is None or not product_root.is_dir():
        raise PhoneRuntimeRefused("PRODUCT source identity input is invalid")
    if _run_checked(["git", "rev-parse", "HEAD"], cwd=product_root, timeout=15) != expected_source_sha:
        raise PhoneRuntimeRefused("PRODUCT renderer source SHA differs from admitted Release")
    if _run_checked(["git", "status", "--porcelain", "--untracked-files=no"], cwd=product_root, timeout=15):
        raise PhoneRuntimeRefused("PRODUCT renderer source has tracked worktree drift")


def _product_digest(*, product_root: Path, asset_name: str, path: Path) -> str:
    try:
        value = _run_checked(
            [
                "cargo", "run", "--quiet", "--locked", "--release", "-p", "operator-cli",
                "--bin", "product-release-asset-digest", "--", asset_name, str(path.resolve()),
            ],
            cwd=product_root, timeout=600,
        )
    except PhoneRuntimeRefused:
        raise ProductReleaseComponentDigestRefused(_PRODUCT_RELEASE_COMPONENT_DIGEST_FAILURE) from None
    if _TYPED_DIGEST.fullmatch(value) is None:
        raise ProductReleaseComponentDigestRefused(_PRODUCT_RELEASE_COMPONENT_DIGEST_FAILURE)
    return value


def verify_release_component_digests(
    materialized: PhoneRuntimeMaterialization, *, product_root: Path,
    runtime_archive: Path, expected_artifact_name: str,
    expected_artifact_digest: str, expected_inventory_digest: str,
) -> None:
    if (
        not expected_artifact_name or "/" in expected_artifact_name
        or _TYPED_DIGEST.fullmatch(expected_artifact_digest) is None
        or _TYPED_DIGEST.fullmatch(expected_inventory_digest) is None
    ):
        raise PhoneRuntimeRefused("admitted phone runtime Product identity is invalid")
    if _product_digest(product_root=product_root, asset_name=expected_artifact_name, path=runtime_archive) != expected_artifact_digest:
        raise PhoneRuntimeRefused("phone runtime outer Product content digest differs")
    if _product_digest(
        product_root=product_root, asset_name="phone-production-runtime/components.json",
        path=materialized.inventory_path,
    ) != expected_inventory_digest:
        raise PhoneRuntimeRefused("phone runtime component inventory digest differs")
    for component in materialized.components:
        actual = _product_digest(
            product_root=product_root,
            asset_name=f"phone-production-runtime/{component.archive_path}",
            path=materialized.component_source(component.name),
        )
        if actual != component.content_digest:
            raise PhoneRuntimeRefused(f"phone runtime component digest differs: {component.name}")


def _load_product_component_contract(product_root: Path) -> dict[str, Mapping[str, object]]:
    path = product_root / _PRODUCT_COMPONENT_CONTRACT
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhoneRuntimeRefused("exact PRODUCT component contract is unavailable") from exc
    if (
        not isinstance(value, Mapping) or value.get("contract_version") != 1
        or value.get("status") != "protected" or value.get("target") != "phone-production"
        or value.get("archive_root") != "phone-production-runtime"
    ):
        raise PhoneRuntimeRefused("exact PRODUCT component contract metadata differs")
    raw = value.get("components")
    if not isinstance(raw, list):
        raise PhoneRuntimeRefused("exact PRODUCT component contract set is invalid")
    result: dict[str, Mapping[str, object]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise PhoneRuntimeRefused("exact PRODUCT component contract entry is invalid")
        name = str(item.get("name", ""))
        if not name or name in result:
            raise PhoneRuntimeRefused("exact PRODUCT component contract names are invalid")
        result[name] = item
    return result


def bind_renderer_inputs(materialized: PhoneRuntimeMaterialization, *, product_root: Path) -> None:
    contract = _load_product_component_contract(product_root)
    by_name = {item.name: item for item in materialized.components}
    if set(contract) != set(by_name):
        raise PhoneRuntimeRefused("PRODUCT renderer source component set differs from Release inventory")
    for name, component in by_name.items():
        spec = contract[name]
        if (
            spec.get("archive_path") != component.archive_path
            or spec.get("kind") != component.kind
            or spec.get("executable") is not component.executable
        ):
            raise PhoneRuntimeRefused(f"PRODUCT renderer component mapping differs: {name}")
        source_rel = _safe_relative(spec.get("source"), "PRODUCT renderer component source")
        target = product_root / source_rel
        source = materialized.component_source(name)
        if name in _NATIVE_COMPONENTS:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            os.chmod(target, 0o755)
        elif not target.is_file() or _sha256_file(target) != _sha256_file(source):
            raise PhoneRuntimeRefused(f"PRODUCT renderer tracked input differs from Release component: {name}")


def render_required_runtime_configs(
    materialized: PhoneRuntimeMaterialization, *, product_root: Path,
    manifest_json: str, release_id: str, environment: Mapping[str, str],
) -> tuple[str, ...]:
    try:
        manifest = json.loads(manifest_json)
    except json.JSONDecodeError as exc:
        raise PhoneRuntimeRefused("phone production runtime manifest is invalid JSON") from exc
    if not isinstance(manifest, Mapping):
        raise PhoneRuntimeRefused("phone production runtime manifest is not an object")
    manifest_path = materialized.source_root.parent / "phone-production-manifest.json"
    render_root = materialized.source_root.parent / "rendered"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    try:
        try:
            _run_checked(
                [
                    "cargo", "run", "--quiet", "--locked", "--release", "-p", "operator-cli",
                    "--bin", "operator-cli", "--", "package-device-release",
                    "--manifest-path", str(manifest_path.resolve()), "--release-id", release_id,
                    "--output-dir", str(render_root.resolve()), "--tunnel-owner", _TUNNEL_OWNER,
                ],
                cwd=product_root, timeout=600, environment=environment,
            )
        except PhoneRuntimeRefused:
            raise ProductRuntimeRenderRefused(_PRODUCT_RUNTIME_RENDER_FAILURE) from None
    finally:
        try:
            manifest_path.unlink()
        except FileNotFoundError:
            pass
    rendered_release = render_root / release_id
    copied: list[str] = []
    for relative in materialized.required_live_release_paths:
        destination = materialized.release_root / relative
        if destination.is_file():
            continue
        source = rendered_release / relative
        if not source.is_file() or source.stat().st_size <= 0:
            raise PhoneRuntimeRefused(f"PRODUCT renderer did not create required runtime file: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        copied.append(relative)
    if any(not (materialized.release_root / relative).is_file() for relative in materialized.required_live_release_paths):
        raise PhoneRuntimeRefused("materialized runtime release lacks required live files")
    return tuple(sorted(copied))
