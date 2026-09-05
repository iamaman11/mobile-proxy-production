from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import product_runtime_renderer as renderer
from phone_runtime import PhoneRuntimeMaterialization, PhoneRuntimeRefused, RuntimeComponent

DIGEST = "b3:" + "a" * 64


def component(name: str, archive_path: str, kind: str, executable: bool) -> RuntimeComponent:
    return RuntimeComponent(name, archive_path, kind, DIGEST, executable)


def materialized(root: Path) -> PhoneRuntimeMaterialization:
    source = root / "source"
    release = root / "release"
    source.mkdir(parents=True)
    release.mkdir(parents=True)
    components = (
        component("runtime-supervisor", "bin/runtime-supervisor", "product-native-executable", True),
        component("magisk-service", "module/service.sh", "runtime-static-file", True),
        component("host-daemon-template", "templates/host-daemon.base.json", "runtime-template", False),
        component("sing-box-template", "templates/sing-box.base.json", "runtime-template", False),
        component("runtime-realization-contract", "realization/phone-production-runtime-realization-v1.json", "runtime-realization-contract", False),
    )
    bodies = {
        "bin/runtime-supervisor": b"runtime\n",
        "module/service.sh": b"#!/system/bin/sh\n",
        "templates/host-daemon.base.json": b"{}\n",
        "templates/sing-box.base.json": b"{}\n",
        "realization/phone-production-runtime-realization-v1.json": b"{}\n",
    }
    for path, body in bodies.items():
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    inventory = source / "components.json"
    inventory.write_text("{}\n", encoding="utf-8")
    realization = source / "realization/phone-production-runtime-realization-v1.json"
    return PhoneRuntimeMaterialization(
        source_root=source, release_root=release, inventory_path=inventory, realization_path=realization,
        components=components,
        required_live_release_paths=("service.sh", "bin/runtime-supervisor", "config/host-daemon.json", "config/sing-box.json"),
        transport_sha256="0" * 64,
    )


def write_contract(product: Path, *, tracked_service: bytes = b"#!/system/bin/sh\n") -> None:
    items = [
        {"name":"runtime-supervisor","source":"deploy/device-runtime/bin/runtime-supervisor","archive_path":"bin/runtime-supervisor","kind":"product-native-executable","executable":True},
        {"name":"magisk-service","source":"deploy/device-runtime/module/service.sh","archive_path":"module/service.sh","kind":"runtime-static-file","executable":True},
        {"name":"host-daemon-template","source":"deploy/device-runtime/templates/host-daemon.base.json","archive_path":"templates/host-daemon.base.json","kind":"runtime-template","executable":False},
        {"name":"sing-box-template","source":"deploy/device-runtime/templates/sing-box.base.json","archive_path":"templates/sing-box.base.json","kind":"runtime-template","executable":False},
        {"name":"runtime-realization-contract","source":"contracts/operations/phone-production-runtime-realization-v1.json","archive_path":"realization/phone-production-runtime-realization-v1.json","kind":"runtime-realization-contract","executable":False},
    ]
    contract = {"contract_version":1,"status":"protected","target":"phone-production","archive_root":"phone-production-runtime","components":items}
    path = product / "contracts/operations/phone-production-release-components-v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract), encoding="utf-8")
    tracked = {
        "deploy/device-runtime/module/service.sh": tracked_service,
        "deploy/device-runtime/templates/host-daemon.base.json": b"{}\n",
        "deploy/device-runtime/templates/sing-box.base.json": b"{}\n",
        "contracts/operations/phone-production-runtime-realization-v1.json": b"{}\n",
    }
    for rel, body in tracked.items():
        target = product / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)


def expect_refused(callback) -> None:
    try:
        callback()
    except PhoneRuntimeRefused:
        return
    raise AssertionError("expected PhoneRuntimeRefused")


def test_verify_release_component_digests_binds_outer_runtime_and_inventory() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        value = materialized(root / "work")
        product = root / "product"
        product.mkdir()
        runtime_archive = root / "runtime.tar.gz"
        runtime_archive.write_bytes(b"runtime-tar")
        calls: list[tuple[str, Path]] = []
        original = renderer._product_digest

        def fake(*, product_root, asset_name, path):
            calls.append((asset_name, Path(path)))
            return DIGEST

        renderer._product_digest = fake
        try:
            renderer.verify_release_component_digests(
                value, product_root=product, runtime_archive=runtime_archive,
                expected_artifact_name="mobile-proxy-phone-production-runtime-v0.1.6.tar.gz",
                expected_artifact_digest=DIGEST, expected_inventory_digest=DIGEST,
            )
        finally:
            renderer._product_digest = original
        assert calls[0] == ("mobile-proxy-phone-production-runtime-v0.1.6.tar.gz", runtime_archive)
        assert calls[1][0] == "phone-production-runtime/components.json"
        assert len(calls) == len(value.components) + 2


def test_bind_renderer_inputs_uses_release_native_bytes_and_exact_tracked_inputs() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        value = materialized(root / "work")
        product = root / "product"
        product.mkdir()
        write_contract(product)
        renderer.bind_renderer_inputs(value, product_root=product)
        assert (product / "deploy/device-runtime/bin/runtime-supervisor").read_bytes() == b"runtime\n"


def test_bind_renderer_inputs_rejects_tracked_source_drift() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        value = materialized(root / "work")
        product = root / "product"
        product.mkdir()
        write_contract(product, tracked_service=b"different\n")
        expect_refused(lambda: renderer.bind_renderer_inputs(value, product_root=product))


def test_render_copies_only_missing_required_derived_files_and_removes_manifest() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        value = materialized(root / "work")
        product = root / "product"
        product.mkdir()
        (value.release_root / "service.sh").write_text("service\n")
        native = value.release_root / "bin/runtime-supervisor"
        native.parent.mkdir(parents=True)
        native.write_text("runtime\n")
        original = renderer._run_checked

        def fake(command, *, cwd, timeout, environment=None):
            out = Path(command[command.index("--output-dir") + 1]) / command[command.index("--release-id") + 1] / "config"
            out.mkdir(parents=True)
            (out / "host-daemon.json").write_text('{"ok":1}\n')
            (out / "sing-box.json").write_text('{"ok":1}\n')
            return ""

        renderer._run_checked = fake
        try:
            copied = renderer.render_required_runtime_configs(
                value, product_root=product, manifest_json='{"deviceId":"x"}',
                release_id="v0.1.6", environment=dict(os.environ),
            )
        finally:
            renderer._run_checked = original
        assert copied == ("config/host-daemon.json", "config/sing-box.json")
        assert not (value.source_root.parent / "phone-production-manifest.json").exists()


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
    print(f"PRODUCT_RUNTIME_RENDERER_TESTS_OK count={len(tests)}")


if __name__ == "__main__":
    main()
