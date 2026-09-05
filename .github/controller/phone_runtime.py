from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

_ARCHIVE_ROOT = PurePosixPath("phone-production-runtime")
_INVENTORY_MEMBER = _ARCHIVE_ROOT / "components.json"
_REALIZATION_ARCHIVE_PATH = "realization/phone-production-runtime-realization-v1.json"
_TYPED_DIGEST = re.compile(r"b3:[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PHONE_ABI = {
    "os": "android",
    "arch": "arm",
    "rust_target": "armv7-linux-androideabi",
    "elf_machine": 40,
}
_MAX_MEMBERS = 128
_MAX_TOTAL_BYTES = 250 * 1024 * 1024


class PhoneRuntimeRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeComponent:
    name: str
    archive_path: str
    kind: str
    content_digest: str
    executable: bool


@dataclass(frozen=True)
class PhoneRuntimeMaterialization:
    source_root: Path
    release_root: Path
    inventory_path: Path
    realization_path: Path
    components: tuple[RuntimeComponent, ...]
    required_live_release_paths: tuple[str, ...]
    transport_sha256: str

    def component(self, name: str) -> RuntimeComponent:
        matches = [item for item in self.components if item.name == name]
        if len(matches) != 1:
            raise PhoneRuntimeRefused(f"runtime component is absent or ambiguous: {name}")
        return matches[0]

    def component_source(self, name: str) -> Path:
        return self.source_root / self.component(name).archive_path


def _safe_relative(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise PhoneRuntimeRefused(f"{label} is invalid")
    path = PurePosixPath(raw)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise PhoneRuntimeRefused(f"{label} escapes runtime root")
    if any(not part or any(ord(ch) < 32 for ch in part) for part in path.parts):
        raise PhoneRuntimeRefused(f"{label} contains unsafe path text")
    return path.as_posix()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PhoneRuntimeRefused("phone runtime asset is unreadable") from exc
    return digest.hexdigest()


def _read_json_member(archive: tarfile.TarFile, member: tarfile.TarInfo, label: str) -> Mapping[str, object]:
    if member.size <= 0 or member.size > 2_000_000:
        raise PhoneRuntimeRefused(f"{label} size is invalid")
    handle = archive.extractfile(member)
    if handle is None:
        raise PhoneRuntimeRefused(f"{label} is unavailable")
    try:
        value = json.loads(handle.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhoneRuntimeRefused(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise PhoneRuntimeRefused(f"{label} is not a JSON object")
    return value


def _validate_inventory(value: Mapping[str, object]) -> tuple[RuntimeComponent, ...]:
    if value.get("format_version") != 1 or value.get("target") != "phone-production":
        raise PhoneRuntimeRefused("phone runtime inventory metadata differs")
    if value.get("runtime_abi") != _PHONE_ABI:
        raise PhoneRuntimeRefused("phone runtime ABI identity differs")
    raw = value.get("components")
    if not isinstance(raw, list) or not raw or len(raw) > 64:
        raise PhoneRuntimeRefused("phone runtime component inventory is invalid")
    names: set[str] = set()
    paths: set[str] = set()
    components: list[RuntimeComponent] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise PhoneRuntimeRefused("phone runtime component record is invalid")
        name = str(item.get("name", ""))
        path = _safe_relative(item.get("archive_path"), "phone runtime component path")
        kind = str(item.get("kind", ""))
        digest = str(item.get("content_digest", ""))
        executable = item.get("executable")
        if (
            not name
            or name in names
            or path in paths
            or not kind
            or item.get("content_digest_algorithm") != "blake3-256"
            or item.get("content_digest_domain") != "mobile-proxy/product-release-asset/v2"
            or _TYPED_DIGEST.fullmatch(digest) is None
            or not isinstance(executable, bool)
        ):
            raise PhoneRuntimeRefused("phone runtime component identity is invalid or ambiguous")
        names.add(name)
        paths.add(path)
        components.append(RuntimeComponent(name, path, kind, digest, executable))
    realization = [item for item in components if item.name == "runtime-realization-contract"]
    if len(realization) != 1 or realization[0].kind != "runtime-realization-contract" or realization[0].archive_path != _REALIZATION_ARCHIVE_PATH:
        raise PhoneRuntimeRefused("phone runtime realization component identity differs")
    return tuple(sorted(components, key=lambda item: item.archive_path))


def _validate_realization(value: Mapping[str, object], components: tuple[RuntimeComponent, ...]) -> tuple[dict[str, str], tuple[str, ...]]:
    if (
        value.get("format_version") != 1
        or value.get("status") != "protected"
        or value.get("target") != "phone-production"
        or value.get("release_layout") != "versioned-release-relative-v1"
        or value.get("absolute_device_root_owner") != "deployment-controller"
        or value.get("activation_entrypoint") != "service.sh"
    ):
        raise PhoneRuntimeRefused("phone runtime realization metadata differs")
    boundaries = value.get("boundaries")
    if not isinstance(boundaries, Mapping) or any(
        boundaries.get(key) is not expected
        for key, expected in {
            "product_release_contains_secret_values": False,
            "controller_owns_absolute_device_root": True,
            "controller_owns_atomic_activation_and_process_order": True,
            "controller_must_not_infer_release_paths_from_archive_filenames": True,
            "vm_server_components_allowed": False,
        }.items()
    ):
        raise PhoneRuntimeRefused("phone runtime realization boundaries differ")

    by_name = {item.name: item for item in components}
    dispositions = value.get("component_dispositions")
    if not isinstance(dispositions, Mapping) or set(dispositions) != set(by_name):
        raise PhoneRuntimeRefused("phone runtime realization component set differs")
    live_copy: dict[str, str] = {}
    for name, raw in dispositions.items():
        if not isinstance(raw, Mapping):
            raise PhoneRuntimeRefused("phone runtime component disposition is invalid")
        disposition = raw.get("disposition")
        if disposition == "live-copy":
            release_path = _safe_relative(raw.get("release_path"), "phone runtime release path")
            if release_path in live_copy.values():
                raise PhoneRuntimeRefused("phone runtime live release path is duplicate")
            live_copy[str(name)] = release_path
        elif disposition == "render-input":
            role = raw.get("render_role")
            if not isinstance(role, str) or not role:
                raise PhoneRuntimeRefused("phone runtime render-input role is invalid")
        elif disposition == "identity-only":
            role = raw.get("identity_role")
            if not isinstance(role, str) or not role:
                raise PhoneRuntimeRefused("phone runtime identity-only role is invalid")
        else:
            raise PhoneRuntimeRefused("phone runtime component disposition is unsupported")
    if dispositions["runtime-realization-contract"].get("disposition") != "identity-only":
        raise PhoneRuntimeRefused("realization contract must be identity-only")

    derived = value.get("derived_runtime_files")
    if not isinstance(derived, list) or not derived:
        raise PhoneRuntimeRefused("phone runtime derived file contract is invalid")
    required_derived: set[str] = set()
    for item in derived:
        if not isinstance(item, Mapping):
            raise PhoneRuntimeRefused("phone runtime derived file entry is invalid")
        release_path = _safe_relative(item.get("release_path"), "phone runtime derived release path")
        if item.get("required_for_current_production") is True:
            if item.get("sensitive") is not True or item.get("secret_values_must_not_enter_product_release") is not True:
                raise PhoneRuntimeRefused("required sensitive derived runtime boundary differs")
            required_derived.add(release_path)

    required = value.get("required_live_release_paths")
    if not isinstance(required, list) or not required:
        raise PhoneRuntimeRefused("required live runtime paths are invalid")
    required_paths = tuple(sorted(_safe_relative(item, "required live runtime path") for item in required))
    if len(set(required_paths)) != len(required_paths):
        raise PhoneRuntimeRefused("required live runtime paths are duplicate")
    expected_required = set(live_copy.values()) | required_derived
    if set(required_paths) != expected_required:
        raise PhoneRuntimeRefused("required live runtime path set contradicts realization mapping")
    return live_copy, required_paths


def materialize_runtime_bundle(
    *,
    archive_path: Path,
    work_root: Path,
    expected_transport_sha256: str,
) -> PhoneRuntimeMaterialization:
    if _SHA256.fullmatch(expected_transport_sha256) is None:
        raise PhoneRuntimeRefused("phone runtime transport digest is invalid")
    if not archive_path.is_file() or archive_path.stat().st_size <= 0:
        raise PhoneRuntimeRefused("phone runtime Release asset is unavailable")
    actual_transport = _sha256_file(archive_path)
    if actual_transport != expected_transport_sha256:
        raise PhoneRuntimeRefused("phone runtime Release transport digest differs")
    if work_root.exists() and any(work_root.iterdir()):
        raise PhoneRuntimeRefused("phone runtime materialization root is not empty")
    work_root.mkdir(parents=True, exist_ok=True)

    try:
        archive = tarfile.open(archive_path, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise PhoneRuntimeRefused("phone runtime Release asset is not a valid tar.gz") from exc
    with archive:
        members = archive.getmembers()
        if not members or len(members) > _MAX_MEMBERS:
            raise PhoneRuntimeRefused("phone runtime archive member count is invalid")
        total = 0
        regular: dict[str, tarfile.TarInfo] = {}
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or "." in path.parts or ".." in path.parts or not path.parts or path.parts[0] != _ARCHIVE_ROOT.as_posix():
                raise PhoneRuntimeRefused("phone runtime archive path escapes declared root")
            if member.isdir():
                continue
            if not member.isreg():
                raise PhoneRuntimeRefused("phone runtime archive contains non-regular member")
            if member.name in regular:
                raise PhoneRuntimeRefused("phone runtime archive member is duplicate")
            total += member.size
            if total > _MAX_TOTAL_BYTES:
                raise PhoneRuntimeRefused("phone runtime archive exceeds bounded size")
            regular[member.name] = member

        inventory_member = regular.get(_INVENTORY_MEMBER.as_posix())
        if inventory_member is None:
            raise PhoneRuntimeRefused("phone runtime component inventory is missing")
        inventory = _read_json_member(archive, inventory_member, "phone runtime component inventory")
        components = _validate_inventory(inventory)
        expected_members = {_INVENTORY_MEMBER.as_posix()} | {
            (_ARCHIVE_ROOT / item.archive_path).as_posix() for item in components
        }
        if set(regular) != expected_members:
            raise PhoneRuntimeRefused("phone runtime archive contains missing or extra component files")

        realization_component = next(item for item in components if item.name == "runtime-realization-contract")
        realization_member = regular[(_ARCHIVE_ROOT / realization_component.archive_path).as_posix()]
        realization = _read_json_member(archive, realization_member, "phone runtime realization contract")
        live_copy, required_paths = _validate_realization(realization, components)

        source_root = work_root / "source"
        release_root = work_root / "release"
        source_root.mkdir()
        release_root.mkdir()
        for component in components:
            member = regular[(_ARCHIVE_ROOT / component.archive_path).as_posix()]
            handle = archive.extractfile(member)
            if handle is None:
                raise PhoneRuntimeRefused("phone runtime component bytes are unavailable")
            destination = source_root / component.archive_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as output:
                shutil.copyfileobj(handle, output)
            os.chmod(destination, 0o755 if component.executable else 0o644)
        inventory_path = source_root / "components.json"
        inventory_handle = archive.extractfile(inventory_member)
        if inventory_handle is None:
            raise PhoneRuntimeRefused("phone runtime inventory bytes are unavailable")
        with inventory_path.open("wb") as output:
            shutil.copyfileobj(inventory_handle, output)
        os.chmod(inventory_path, 0o644)

    for name, release_path in live_copy.items():
        component = next(item for item in components if item.name == name)
        source = source_root / component.archive_path
        destination = release_root / release_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o755 if component.executable else 0o644)

    realization_path = source_root / realization_component.archive_path
    return PhoneRuntimeMaterialization(
        source_root=source_root,
        release_root=release_root,
        inventory_path=inventory_path,
        realization_path=realization_path,
        components=components,
        required_live_release_paths=required_paths,
        transport_sha256=actual_transport,
    )
