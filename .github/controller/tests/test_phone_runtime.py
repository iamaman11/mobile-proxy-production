from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phone_runtime import PhoneRuntimeRefused, materialize_runtime_bundle

DIGEST = "b3:" + "a" * 64


def component(name: str, path: str, kind: str, executable: bool = False) -> dict[str, object]:
    return {
        "name": name,
        "archive_path": path,
        "kind": kind,
        "content_digest": DIGEST,
        "content_digest_algorithm": "blake3-256",
        "content_digest_domain": "mobile-proxy/product-release-asset/v2",
        "executable": executable,
    }


def fixture(extra: dict[str, bytes] | None = None, *, target: str = "phone-production") -> bytes:
    components = [
        component("runtime-supervisor", "bin/runtime-supervisor", "product-native-executable", True),
        component("magisk-service", "module/service.sh", "runtime-static-file", True),
        component("host-daemon-template", "templates/host-daemon.base.json", "runtime-template"),
        component("sing-box-template", "templates/sing-box.base.json", "runtime-template"),
        component("runtime-realization-contract", "realization/phone-production-runtime-realization-v1.json", "runtime-realization-contract"),
    ]
    realization = {
        "format_version": 1,
        "status": "protected",
        "target": target,
        "release_layout": "versioned-release-relative-v1",
        "absolute_device_root_owner": "deployment-controller",
        "activation_entrypoint": "service.sh",
        "component_dispositions": {
            "runtime-supervisor": {"disposition": "live-copy", "release_path": "bin/runtime-supervisor"},
            "magisk-service": {"disposition": "live-copy", "release_path": "service.sh"},
            "host-daemon-template": {"disposition": "render-input", "render_role": "host-daemon-config-template"},
            "sing-box-template": {"disposition": "render-input", "render_role": "sing-box-config-template"},
            "runtime-realization-contract": {"disposition": "identity-only", "identity_role": "product-owned-runtime-realization-contract"},
        },
        "derived_runtime_files": [
            {
                "name": "host-daemon-config",
                "release_path": "config/host-daemon.json",
                "required_for_current_production": True,
                "sensitive": True,
                "secret_values_must_not_enter_product_release": True,
            },
            {
                "name": "sing-box-config",
                "release_path": "config/sing-box.json",
                "required_for_current_production": True,
                "sensitive": True,
                "secret_values_must_not_enter_product_release": True,
            },
        ],
        "required_live_release_paths": [
            "service.sh",
            "bin/runtime-supervisor",
            "config/host-daemon.json",
            "config/sing-box.json",
        ],
        "boundaries": {
            "product_release_contains_secret_values": False,
            "controller_owns_absolute_device_root": True,
            "controller_owns_atomic_activation_and_process_order": True,
            "controller_must_not_infer_release_paths_from_archive_filenames": True,
            "vm_server_components_allowed": False,
        },
    }
    files = {
        "bin/runtime-supervisor": b"runtime\n",
        "module/service.sh": b"#!/system/bin/sh\n",
        "templates/host-daemon.base.json": b"{}\n",
        "templates/sing-box.base.json": b"{}\n",
        "realization/phone-production-runtime-realization-v1.json": json.dumps(realization).encode() + b"\n",
    }
    inventory = {
        "format_version": 1,
        "target": target,
        "runtime_abi": {"os": "android", "arch": "arm", "rust_target": "armv7-linux-androideabi", "elf_machine": 40},
        "components": components,
        "third_party_runtime": [],
    }
    files["components.json"] = json.dumps(inventory).encode() + b"\n"
    if extra:
        files.update(extra)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for rel, body in sorted(files.items()):
            info = tarfile.TarInfo("phone-production-runtime/" + rel)
            info.size = len(body)
            info.mode = 0o755 if rel.endswith("service.sh") or rel.endswith("runtime-supervisor") else 0o644
            tar.addfile(info, io.BytesIO(body))
    return stream.getvalue()


def write_bundle(root: Path, body: bytes) -> tuple[Path, str]:
    archive = root / "runtime.tar.gz"
    archive.write_bytes(body)
    return archive, hashlib.sha256(body).hexdigest()


def expect_refused(callback) -> None:
    try:
        callback()
    except PhoneRuntimeRefused:
        return
    raise AssertionError("expected PhoneRuntimeRefused")


def test_materializes_only_declared_live_copy_paths() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        archive, digest = write_bundle(root, fixture())
        result = materialize_runtime_bundle(archive_path=archive, work_root=root / "work", expected_transport_sha256=digest)
        assert (result.release_root / "service.sh").read_bytes() == b"#!/system/bin/sh\n"
        assert (result.release_root / "bin/runtime-supervisor").read_bytes() == b"runtime\n"
        assert not (result.release_root / "templates").exists()
        assert set(result.required_live_release_paths) == {
            "service.sh", "bin/runtime-supervisor", "config/host-daemon.json", "config/sing-box.json"
        }


def test_rejects_extra_archive_file() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        archive, digest = write_bundle(root, fixture({"bin/unexpected": b"x"}))
        expect_refused(lambda: materialize_runtime_bundle(archive_path=archive, work_root=root / "work", expected_transport_sha256=digest))


def test_rejects_vm_target_contract() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        archive, digest = write_bundle(root, fixture(target="vm-production"))
        expect_refused(lambda: materialize_runtime_bundle(archive_path=archive, work_root=root / "work", expected_transport_sha256=digest))


def test_rejects_transport_digest_mismatch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        archive, _ = write_bundle(root, fixture())
        expect_refused(lambda: materialize_runtime_bundle(archive_path=archive, work_root=root / "work", expected_transport_sha256="0" * 64))


def test_rejects_path_traversal_member() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        source = io.BytesIO(fixture())
        files: list[tuple[str, bytes]] = []
        with tarfile.open(fileobj=source, mode="r:gz") as tar:
            for member in tar.getmembers():
                handle = tar.extractfile(member)
                if handle is not None:
                    files.append((member.name, handle.read()))
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            for name, data in files:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            info = tarfile.TarInfo("phone-production-runtime/../evil")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        archive, digest = write_bundle(root, stream.getvalue())
        expect_refused(lambda: materialize_runtime_bundle(archive_path=archive, work_root=root / "work", expected_transport_sha256=digest))


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
    print(f"PHONE_RUNTIME_TESTS_OK count={len(tests)}")


if __name__ == "__main__":
    main()
