from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
SCRIPTS = ROOT / "scripts"


def load_operational_observer():
    if str(CONTROLLER) not in sys.path:
        sys.path.insert(0, str(CONTROLLER))
    spec = importlib.util.spec_from_file_location(
        "runtime_operational_phase_observer_acceptance",
        CONTROLLER / "runtime_operational_observer.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_enricher():
    if str(CONTROLLER) not in sys.path:
        sys.path.insert(0, str(CONTROLLER))
    spec = importlib.util.spec_from_file_location(
        "runtime_operational_phase_evidence_acceptance",
        SCRIPTS / "enrich_phone_runtime_operational.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _evidence() -> dict[str, object]:
    return {
        "schema": "stage4-phone-release-observation.v1",
        "classification": "HEALTHY_EXACT",
        "observation": {
            "desired": True,
            "runtime": {
                "current_state": "expected_release",
                "current_matches_expected_release": True,
                "exact_files_verified": True,
            },
        },
        "safety": {},
    }


def test_runtime_health_probe_phase_markers_are_allowlisted_and_ordered() -> None:
    module = load_operational_observer()
    script = module._operational_script("stage4-admin-token-safe-value")

    markers = [f"stage4_phase={phase}".encode("ascii") for phase in module._PHASE_SEQUENCE]
    offsets = [script.index(marker) for marker in markers]

    assert offsets == sorted(offsets)
    assert len(module._ALLOWED_PHASES) == len(module._PHASE_SEQUENCE)
    assert set(module._PHASE_SEQUENCE) == module._ALLOWED_PHASES


def test_runtime_process_count_is_single_pass_and_independently_bounded() -> None:
    module = load_operational_observer()
    script = module._operational_script("stage4-admin-token-safe-value")

    assert module._PROCESS_COUNT_TIMEOUT_SECONDS == 3
    assert script.count(b"for cmdfile in /proc/[0-9]*/cmdline") == 1
    assert b'"$BB_BIN" timeout -t 3 "$BB_BIN" sh -c' in script
    assert script.index(b"stage4_phase=process_count_start") < script.index(
        b"for cmdfile in /proc/[0-9]*/cmdline"
    )
    assert script.index(b"for cmdfile in /proc/[0-9]*/cmdline") < script.index(
        b"stage4_phase=process_count_done"
    )
    assert b"process_ids_recorded" not in script
    assert b"process_cmdlines_recorded" not in script


def test_runtime_health_outer_timeout_retains_only_last_allowlisted_phase() -> None:
    module = load_operational_observer()
    module.phone_target._probe_root_capability = lambda serial: None
    module.phone_target._run_root_script = lambda serial, script, timeout: module.phone_target.RootScriptResult(
        status="timeout",
        returncode=None,
        stdout=(
            b"stage4_phase=busybox_selected\n"
            b"stage4_phase=process_count_start\n"
            b"stage4_phase=process_count_done\n"
            b"stage4_phase=health_transport_start\n"
        ),
        stderr=b"",
    )

    try:
        module.observe_runtime_operational_health("registered-phone", admin_token="safe-token")
    except module.RuntimeOperationalObservationUnavailable as exc:
        assert str(exc) == module._UNAVAILABLE
        assert exc.last_phase == "health_transport_start"
    else:
        raise AssertionError("outer timeout must fail closed with bounded phase evidence")


def test_runtime_process_count_failure_retains_started_phase() -> None:
    module = load_operational_observer()
    module.phone_target._probe_root_capability = lambda serial: None
    module.phone_target._run_root_script = lambda serial, script, timeout: module.phone_target.RootScriptResult(
        status="completed",
        returncode=21,
        stdout=(
            b"stage4_phase=busybox_selected\n"
            b"stage4_phase=process_count_start\n"
        ),
        stderr=b"",
    )

    try:
        module.observe_runtime_operational_health("registered-phone", admin_token="safe-token")
    except module.RuntimeOperationalObservationUnavailable as exc:
        assert exc.last_phase == "process_count_start"
    else:
        raise AssertionError("bounded process-count failure must fail closed")


def test_runtime_health_phase_parser_rejects_unallowlisted_or_out_of_order_markers() -> None:
    module = load_operational_observer()

    for raw in (
        b"stage4_phase=secret_dump\n",
        b"stage4_phase=process_count_start\n",
        b"stage4_phase=busybox_selected\nstage4_phase=process_count_done\n",
    ):
        try:
            module._split_phase_markers(raw)
        except module.phone_target.PhoneTargetUnavailable:
            pass
        else:
            raise AssertionError("invalid phase evidence must fail closed")


def test_operational_timeout_projects_only_allowlisted_failure_phase(tmp_path: Path) -> None:
    module = load_enricher()
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(_evidence()), encoding="utf-8")

    def fail(serial: str, *, admin_token: str):
        raise module.RuntimeOperationalObservationUnavailable(
            "runtime operational observation is unavailable",
            last_phase="health_transport_start",
        )

    module.observe_runtime_operational_health = fail
    assert module.enrich(path, serial="registered-phone", admin_token="safe-token") == 2

    retained = json.loads(path.read_text(encoding="utf-8"))
    assert retained["classification"] == "UNKNOWN"
    assert retained["failure_code"] == "RUNTIME_OPERATIONAL_OBSERVATION_UNAVAILABLE"
    assert retained["failure_phase"] == "health_transport_start"
    assert retained["observation"]["operational"]["evaluated"] is False
    assert retained["safety"]["operational_probe_performed"] is True
    assert "safe-token" not in path.read_text(encoding="utf-8")


def test_operational_nonphase_failure_does_not_manufacture_phase(tmp_path: Path) -> None:
    module = load_enricher()
    path = tmp_path / "evidence.json"
    payload = _evidence()
    payload["failure_phase"] = "stale-value"
    path.write_text(json.dumps(payload), encoding="utf-8")

    def fail(serial: str, *, admin_token: str):
        raise module.PhoneTargetUnavailable("runtime operational observation is unavailable")

    module.observe_runtime_operational_health = fail
    assert module.enrich(path, serial="registered-phone", admin_token="safe-token") == 2

    retained = json.loads(path.read_text(encoding="utf-8"))
    assert "failure_phase" not in retained
