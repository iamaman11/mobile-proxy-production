from __future__ import annotations

import sys
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


def test_observer_and_deployment_consume_shared_concrete_state_owner() -> None:
    observer = (SCRIPTS / "observe_phone_release.py").read_text(encoding="utf-8")
    deployment = (SCRIPTS / "run_phone_release_deployment.py").read_text(encoding="utf-8")
    assert "run_phone_release_deployment as deployment_runner" not in observer
    assert "from phone_release_state import" in observer
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
