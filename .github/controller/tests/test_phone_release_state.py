from __future__ import annotations

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


def test_exact_snapshot_classifies_healthy_and_degraded_from_same_pair() -> None:
    apk = SimpleNamespace(desired=True)
    runtime = SimpleNamespace(desired=True)
    with mock.patch.object(state, "observe", return_value=apk), mock.patch.object(
        state, "observe_runtime", return_value=runtime
    ):
        snapshot = state.observe_exact_phone_release(
            serial="serial",
            binding_key="k" * 32,
            admitted=_admitted(),
            materialized=_materialized(),
        )
    assert snapshot.classification == "HEALTHY_EXACT"
    assert snapshot.desired is True
    assert state.require_observed_pair(snapshot) == (apk, runtime)

    runtime.desired = False
    with mock.patch.object(state, "observe", return_value=apk), mock.patch.object(
        state, "observe_runtime", return_value=runtime
    ):
        snapshot = state.observe_exact_phone_release(
            serial="serial",
            binding_key="k" * 32,
            admitted=_admitted(),
            materialized=_materialized(),
        )
    assert snapshot.classification == "DEGRADED"
    assert snapshot.desired is False
    assert state.require_observed_pair(snapshot) == (apk, runtime)


def test_exact_snapshot_preserves_unknown_exception_for_deployment_state_machine() -> None:
    failure = state.AndroidObservationUnavailable("registered target unavailable")
    with mock.patch.object(state, "observe", side_effect=failure):
        snapshot = state.observe_exact_phone_release(
            serial="serial",
            binding_key="k" * 32,
            admitted=_admitted(),
            materialized=_materialized(),
        )
    assert snapshot.classification == "UNKNOWN"
    assert snapshot.apk is None and snapshot.runtime is None
    assert snapshot.failure is failure
    try:
        state.require_observed_pair(snapshot)
    except state.AndroidObservationUnavailable as exc:
        assert exc is failure
    else:
        raise AssertionError("UNKNOWN shared snapshot lost deployment exception semantics")


def test_preparation_records_exact_nonsecret_phase_timing_contract() -> None:
    materialized = _preparation_materialized()
    facts: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as raw, mock.patch.object(
        state, "_runtime_cache_root", return_value=None
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
            _preparation_admitted(),
            archive=Path(raw) / "runtime.tar.gz",
            work_root=Path(raw) / "work",
            product_root=Path(raw) / "product",
            runtime_manifest_path=Path(raw) / "manifest.json",
            binding_key="k" * 32,
            facts=facts,
        )
    assert result is materialized
    verification = facts.get("runtime_verification")
    assert isinstance(verification, dict)
    timings = verification.get("phase_timing_ms")
    assert isinstance(timings, dict)
    assert tuple(timings) == state.PHONE_RUNTIME_PREPARATION_TIMING_FIELDS
    assert all(type(value) is int and value >= 0 for value in timings.values())
    assert verification["runtime_archive_cache"] == {
        "state": "disabled",
        "persistent": False,
        "rendered_trees_retained": False,
    }
    rendered = repr(timings)
    for forbidden in ("SECRET", "token", "config/b", "runtime.tar.gz", "hmac-sha256"):
        assert forbidden not in rendered


def test_observer_and_deployment_consume_shared_concrete_state_owner() -> None:
    observer = (SCRIPTS / "observe_phone_release.py").read_text(encoding="utf-8")
    deployment = (SCRIPTS / "run_phone_release_deployment.py").read_text(encoding="utf-8")
    assert "run_phone_release_deployment as deployment_runner" not in observer
    assert "from phone_release_state import" in observer
    assert "PHONE_RUNTIME_PREPARATION_TIMING_FIELDS" in observer
    assert "phase_timing_ms" in observer
    assert 'summary["runtime_preparation"]' in observer
    assert "prepare_verified_release_runtime(" in observer
    assert "observe_exact_phone_release(" in observer
    assert "from phone_release_state import" in deployment
    assert "return prepare_verified_release_runtime(" in deployment
    assert "observe_exact_phone_release(" in deployment
    for obsolete in (
        "def _read_runtime_manifest(",
        "def _runtime_secret_binding_ids(",
        "def _runtime_cache_root(",
    ):
        assert obsolete not in deployment


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"PHONE_RELEASE_STATE_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
