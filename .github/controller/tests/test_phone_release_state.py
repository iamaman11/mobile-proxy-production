from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(CONTROLLER))

import phone_release_state as state  # noqa: E402
import product_runtime_renderer as renderer  # noqa: E402
import product_tool_cache as tool_cache  # noqa: E402


def _admitted() -> object:
    return SimpleNamespace(
        android_version_name="0.1.7",
        android_version_code=1007,
        artifact_transport_sha256="a" * 64,
        identity=SimpleNamespace(tag="v0.1.7"),
    )


def _materialized() -> object:
    return SimpleNamespace(
        release_root=Path("/tmp/release"),
        required_live_release_paths=("bin/a", "config/b"),
    )


def _preparation_admitted() -> object:
    return SimpleNamespace(
        phone_runtime_download_url="https://github.com/iamaman11/mobile-proxy/releases/download/v0.1.7/runtime.tar.gz",
        phone_runtime_transport_sha256="a" * 64,
        identity=SimpleNamespace(
            tag="v0.1.7",
            source_sha="b" * 40,
            phone_runtime_artifact_name="runtime.tar.gz",
            phone_runtime_artifact_digest="b3:" + "c" * 64,
            phone_runtime_inventory_digest="b3:" + "d" * 64,
        ),
    )


def _preparation_materialized() -> object:
    return SimpleNamespace(
        transport_sha256="a" * 64,
        components=(SimpleNamespace(name="runtime-supervisor"), SimpleNamespace(name="host-daemon")),
        required_live_release_paths=("bin/a", "config/b"),
        release_root=Path("/tmp/release"),
    )


def _tool_identity(**overrides: object) -> tool_cache.ProductToolIdentity:
    values: dict[str, object] = {
        "source_sha": "a" * 40,
        "cargo_lock_sha256": "b" * 64,
        "rustc_version": "rustc 1.95.0 (deadbeef 2026-01-01)",
        "host_target": "x86_64-unknown-linux-gnu",
        "cargo_profile": "release",
        "build_flags": (
            "--locked", "--release", "-p", "operator-cli",
            "--bin", "product-release-asset-digest",
            "--bin", "operator-cli",
        ),
    }
    values.update(overrides)
    return tool_cache.ProductToolIdentity(**values)  # type: ignore[arg-type]


def _fake_tool_builder(counter: list[int]):
    def build(verifier: Path, renderer_path: Path) -> None:
        counter[0] += 1
        verifier.write_text(
            "#!/usr/bin/env python3\n"
            "import hashlib,pathlib,sys\n"
            "name=sys.argv[1].encode()\n"
            "body=pathlib.Path(sys.argv[2]).read_bytes()\n"
            "print('b3:'+hashlib.sha256(name+b'\\0'+body).hexdigest())\n",
            encoding="utf-8",
        )
        renderer_path.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "raise SystemExit(0 if sys.argv[1]=='package-device-release' else 2)\n",
            encoding="utf-8",
        )
        os.chmod(verifier, 0o500)
        os.chmod(renderer_path, 0o500)
    return build


def test_exact_snapshot_classifies_healthy_and_degraded_from_same_pair() -> None:
    apk = SimpleNamespace(desired=True)
    runtime = SimpleNamespace(desired=True)
    with mock.patch.object(state, "observe", return_value=apk), mock.patch.object(
        state, "observe_runtime", return_value=runtime
    ):
        snapshot = state.observe_exact_phone_release(
            serial="serial", binding_key="k" * 32, admitted=_admitted(), materialized=_materialized()
        )
    assert snapshot.classification == "HEALTHY_EXACT"
    assert snapshot.desired is True
    assert state.require_observed_pair(snapshot) == (apk, runtime)

    runtime.desired = False
    with mock.patch.object(state, "observe", return_value=apk), mock.patch.object(
        state, "observe_runtime", return_value=runtime
    ):
        snapshot = state.observe_exact_phone_release(
            serial="serial", binding_key="k" * 32, admitted=_admitted(), materialized=_materialized()
        )
    assert snapshot.classification == "DEGRADED"
    assert snapshot.desired is False


def test_exact_snapshot_preserves_unknown_exception_for_deployment_state_machine() -> None:
    failure = state.AndroidObservationUnavailable("registered target unavailable")
    with mock.patch.object(state, "observe", side_effect=failure):
        snapshot = state.observe_exact_phone_release(
            serial="serial", binding_key="k" * 32, admitted=_admitted(), materialized=_materialized()
        )
    assert snapshot.classification == "UNKNOWN"
    assert snapshot.failure is failure
    try:
        state.require_observed_pair(snapshot)
    except state.AndroidObservationUnavailable as exc:
        assert exc is failure
    else:
        raise AssertionError("UNKNOWN shared snapshot lost deployment exception semantics")


def test_preparation_records_exact_nonsecret_phase_and_cache_contract() -> None:
    materialized = _preparation_materialized()
    facts: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as raw, mock.patch.object(
        state, "_runtime_cache_root", return_value=None
    ), mock.patch.object(
        state, "_product_tool_cache_root", return_value=None
    ), mock.patch.object(
        state, "_download_release_asset", return_value=None
    ), mock.patch.object(
        state, "materialize_runtime_bundle", return_value=materialized
    ), mock.patch.object(
        state, "verify_product_source", return_value=None
    ), mock.patch.object(
        state, "verify_release_component_digests", return_value=None
    ), mock.patch.object(
        state, "bind_renderer_inputs", return_value=None
    ), mock.patch.object(
        state, "_read_runtime_manifest", return_value=("{}", {})
    ), mock.patch.object(
        state, "render_required_runtime_configs", return_value=("config/b",)
    ), mock.patch.object(
        state, "_runtime_secret_binding_ids", return_value={}
    ):
        result = state.prepare_verified_release_runtime(
            _preparation_admitted(), archive=Path(raw) / "runtime.tar.gz",
            work_root=Path(raw) / "work", product_root=Path(raw) / "product",
            runtime_manifest_path=Path(raw) / "manifest.json", binding_key="k" * 32, facts=facts,
        )
    assert result is materialized
    verification = facts["runtime_verification"]
    assert isinstance(verification, dict)
    timings = verification["phase_timing_ms"]
    assert isinstance(timings, dict)
    assert tuple(timings) == state.PHONE_RUNTIME_PREPARATION_TIMING_FIELDS
    assert all(type(value) is int and value >= 0 for value in timings.values())
    assert verification["product_tool_cache"] == {
        "state": "disabled", "persistent": False, "binary_identity_verified": False,
        "verification_results_cached": False, "rendered_outputs_cached": False,
        "runtime_or_config_state_cached": False,
    }
    assert "product_tool_preparation" in timings


def test_tool_cache_key_changes_with_source_lock_rust_target_profile_or_flags() -> None:
    base = _tool_identity()
    variants = (
        _tool_identity(source_sha="c" * 40),
        _tool_identity(cargo_lock_sha256="d" * 64),
        _tool_identity(rustc_version="rustc 1.96.0 (cafebabe 2026-02-02)"),
        _tool_identity(host_target="aarch64-unknown-linux-gnu"),
        _tool_identity(cargo_profile="release-stage4"),
        _tool_identity(build_flags=(*base.build_flags, "--features", "stage4-proof")),
    )
    assert len({base.cache_key(), *(item.cache_key() for item in variants)}) == 1 + len(variants)


def test_tool_cache_hit_does_not_build_and_verifies_both_binaries() -> None:
    identity = _tool_identity()
    builds = [0]
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "cache"
        first = tool_cache.get_or_build_product_tools(
            cache_root=root, product_root=Path(raw) / "product", identity=identity,
            build_tools=_fake_tool_builder(builds),
        )
        assert first.state == "miss" and builds == [1]
        second = tool_cache.get_or_build_product_tools(
            cache_root=root, product_root=Path(raw) / "product", identity=identity,
            build_tools=lambda *_: (_ for _ in ()).throw(AssertionError("cache hit invoked build")),
        )
        assert second.state == "hit" and builds == [1]
        assert second.verifier_path == first.verifier_path
        assert second.renderer_path == first.renderer_path


def test_tool_cache_corrupt_binary_or_manifest_rebuilds_only_exact_entry() -> None:
    identity = _tool_identity()
    builds = [0]
    builder = _fake_tool_builder(builds)
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "cache"
        first = tool_cache.get_or_build_product_tools(
            cache_root=root, product_root=Path(raw) / "product", identity=identity, build_tools=builder
        )
        first.verifier_path.write_bytes(b"corrupt")
        repaired = tool_cache.get_or_build_product_tools(
            cache_root=root, product_root=Path(raw) / "product", identity=identity, build_tools=builder
        )
        assert repaired.state == "repaired" and builds == [2]
        (repaired.renderer_path.parent / "manifest.json").write_text("{}\n", encoding="utf-8")
        repaired = tool_cache.get_or_build_product_tools(
            cache_root=root, product_root=Path(raw) / "product", identity=identity, build_tools=builder
        )
        assert repaired.state == "repaired" and builds == [3]


def test_tool_cache_cold_and_hit_verifier_return_identical_digest_result() -> None:
    identity = _tool_identity()
    builds = [0]
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "cache"
        asset = Path(raw) / "asset.bin"
        asset.write_bytes(b"immutable product bytes\n")
        cold = tool_cache.get_or_build_product_tools(
            cache_root=root, product_root=Path(raw) / "product", identity=identity,
            build_tools=_fake_tool_builder(builds),
        )
        command = [str(cold.verifier_path), "phone-production-runtime/bin/host-daemon", str(asset)]
        cold_value = subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
        hit = tool_cache.get_or_build_product_tools(
            cache_root=root, product_root=Path(raw) / "product", identity=identity,
            build_tools=lambda *_: (_ for _ in ()).throw(AssertionError("hit rebuilt tools")),
        )
        hit_value = subprocess.run(
            [str(hit.verifier_path), "phone-production-runtime/bin/host-daemon", str(asset)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert cold_value == hit_value and cold_value.startswith("b3:")
        assert builds == [1]


def test_tool_cache_contains_only_two_binaries_manifest_and_lock() -> None:
    identity = _tool_identity()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "cache"
        result = tool_cache.get_or_build_product_tools(
            cache_root=root, product_root=Path(raw) / "product", identity=identity,
            build_tools=_fake_tool_builder([0]),
        )
        files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
        entry = result.verifier_path.parent.name
        assert files == sorted([
            ".product-tool-cache.lock", f"{entry}/manifest.json",
            f"{entry}/product-release-asset-digest", f"{entry}/operator-cli",
        ])
        rendered = "\n".join(files) + (result.verifier_path.parent / "manifest.json").read_text(encoding="utf-8")
        for forbidden in ("runtime.tar", "rendered/", "config/", "token", "credential", "secret"):
            assert forbidden not in rendered.lower()


def test_component_verification_still_hashes_archive_inventory_and_every_component_every_time() -> None:
    components = (
        SimpleNamespace(name="runtime-supervisor", archive_path="bin/runtime-supervisor", content_digest="b3:" + "1" * 64),
        SimpleNamespace(name="host-daemon", archive_path="bin/host-daemon", content_digest="b3:" + "2" * 64),
    )
    materialized = SimpleNamespace(
        inventory_path=Path("/tmp/components.json"), components=components,
        component_source=lambda name: Path("/tmp") / name,
    )
    expected_archive = "b3:" + "3" * 64
    expected_inventory = "b3:" + "4" * 64
    values = [expected_archive, expected_inventory, components[0].content_digest, components[1].content_digest] * 2
    verifier = Path("/verified/cache/product-release-asset-digest")
    with mock.patch.object(renderer, "_product_digest", side_effect=values) as digest:
        for _ in range(2):
            renderer.verify_release_component_digests(
                materialized, product_root=Path("/product"), runtime_archive=Path("/runtime.tar.gz"),
                expected_artifact_name="runtime.tar.gz", expected_artifact_digest=expected_archive,
                expected_inventory_digest=expected_inventory, verifier_binary=verifier,
            )
    assert digest.call_count == 8
    assert all(call.kwargs["verifier_binary"] == verifier for call in digest.call_args_list)


def test_cached_renderer_is_invoked_directly_not_through_cargo() -> None:
    materialized = SimpleNamespace(
        source_root=Path("/tmp/source"), required_live_release_paths=(), release_root=Path("/tmp/release")
    )
    cached = Path("/verified/cache/operator-cli")
    with mock.patch.object(renderer, "_run_checked", return_value="") as run, mock.patch.object(
        renderer.Path, "is_file", return_value=True
    ):
        renderer.render_required_runtime_configs(
            materialized, product_root=Path("/product"), manifest_json="{}", release_id="v0.1.7",
            environment={}, renderer_binary=cached,
        )
    command = run.call_args.args[0]
    assert command[0] == str(cached)
    assert command[1] == "package-device-release"
    assert "cargo" not in command


def test_observer_and_deployment_consume_shared_concrete_state_owner() -> None:
    observer = (SCRIPTS / "observe_phone_release.py").read_text(encoding="utf-8")
    deployment = (SCRIPTS / "run_phone_release_deployment.py").read_text(encoding="utf-8")
    assert "run_phone_release_deployment as deployment_runner" not in observer
    assert "from phone_release_state import" in observer
    assert "PHONE_RUNTIME_PREPARATION_TIMING_FIELDS" in observer
    assert "phase_timing_ms" in observer
    assert "prepare_verified_release_runtime(" in observer
    assert "observe_exact_phone_release(" in observer
    assert "from phone_release_state import" in deployment
    assert "return prepare_verified_release_runtime(" in deployment
    assert "observe_exact_phone_release(" in deployment


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"PHONE_RELEASE_STATE_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
