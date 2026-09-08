from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str, path: Path):
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def finalizer():
    return _load("finalize_runner_transport_observation_tests", SCRIPTS / "finalize_runner_transport_observation.py")


def _core(*, watchdog_readable: bool = True) -> dict[str, object]:
    return {
        "schema": "runner-transport-observation.v1",
        "controller_revision": "a" * 40,
        "observation_started_at_utc": "2026-09-08T20:00:00Z",
        "observation_finished_at_utc": "2026-09-08T20:00:01Z",
        "classification": "HEALTHY",
        "session": {
            "diagnostic_window": "CURRENT_LISTENER_SESSION",
            "evidence_available": True,
            "evidence_complete": True,
            "diagnostics_truncated": False,
            "listener_session_started_at_utc": "2026-09-08T19:00:23Z",
            "last_successful_session_establishment_at_utc": "2026-09-08T19:00:27Z",
            "session_age_seconds": 3577,
            "runner_version": "2.337.0",
            "listener_files_scanned": 1,
            "worker_files_scanned": 1,
        },
        "timing": {
            "command_to_self_hosted_job_start": {"value_ms": None, "source": "GITHUB_JOB_METADATA_PENDING"},
            "job_setup_action_preparation": {"value_ms": None, "source": "GITHUB_JOB_METADATA_PENDING"},
            "checkout": {"value_ms": None, "source": "GITHUB_JOB_METADATA_PENDING"},
            "observer": {"value_ms": 850, "source": "LOCAL_MONOTONIC"},
            "broker_assignment": {"value_ms": None, "source": "UNAVAILABLE"},
        },
        "transport": {
            "error_counters": {
                "broker_reconnect": 0,
                "runserver_reconnect": 0,
                "tls_error": 0,
                "eof_error": 0,
                "connection_reset": 0,
                "action_download_error": 0,
                "artifact_transport_error": 0,
                "transport_error_lines": 0,
            },
            "context_counters": {"expected_local_poll_cancellation": 0},
            "failure_classes": [],
            "repeated_transport_error": False,
            "active_probes": {"roles": [], "retry_observed": False, "budget_exhausted": False},
        },
        "capabilities": {
            "active_probe_budget_seconds": 120,
            "attempt_timeout_seconds": 10,
            "max_destinations": 5,
            "max_attempts_per_destination": 3,
            "max_concurrency": 2,
            "tls_verification_enforced": True,
            "artifact_round_trip_required": True,
            "exact_broker_assignment_timing_available": False,
        },
        "environment_presence": {
            "proxy_present": True,
            "custom_ca_present": False,
            "git_tls_override_present": False,
            "runtime_tls_override_present": False,
            "source": "SERVICE_PROCESS",
            "service_environment_observed": True,
            "observer_environment_matches_presence": True,
        },
        "watchdog": {
            "timer_state": "ACTIVE",
            "state_readable": watchdog_readable,
            "decision": "UNKNOWN",
            "cooldown_active": False if watchdog_readable else None,
            "restart_budget_remaining": 3 if watchdog_readable else None,
            "post_restart_grace_active": False if watchdog_readable else None,
            "restart_count_window": 0 if watchdog_readable else None,
        },
        "failure_domain": "UNKNOWN",
        "evidence_limitations": [
            "BROKER_ASSIGNMENT_TIMESTAMP_UNAVAILABLE",
            *([] if watchdog_readable else ["WATCHDOG_STATE_UNAVAILABLE"]),
        ],
        "safety": {
            "phone_access_performed": False,
            "phone_mutation_performed": False,
            "provider_access_performed": False,
            "provider_mutation_performed": False,
            "deployment_created": False,
            "deployment_intent_created": False,
            "runner_restart_performed": False,
            "runner_registration_changed": False,
            "network_configuration_changed": False,
            "automatic_recovery_performed": False,
            "raw_runner_log_recorded": False,
            "raw_transport_url_recorded": False,
            "secret_values_recorded": False,
            "network_probe_performed": True,
        },
    }


def _jobs() -> dict[str, object]:
    return {
        "total_count": 2,
        "jobs": [
            {
                "id": 100,
                "name": "caller / Observe self-hosted runner transport once",
                "runner_name": "must-not-escape",
                "runner_id": 999,
                "started_at": "2026-09-08T20:00:05Z",
                "steps": [
                    {
                        "name": "Check out exact Controller revision",
                        "started_at": "2026-09-08T20:00:07Z",
                        "completed_at": "2026-09-08T20:00:09Z",
                    },
                    {
                        "name": "Run bounded runner transport observer",
                        "started_at": "2026-09-08T20:00:10Z",
                        "completed_at": "2026-09-08T20:00:12Z",
                    },
                ],
            },
            {
                "id": 101,
                "name": "other hosted job",
                "started_at": "2026-09-08T20:00:01Z",
                "steps": [],
            },
        ],
    }


def _write_inputs(root: Path, core: dict[str, object]) -> tuple[Path, Path, Path, str]:
    core_path = root / "core.json"
    jobs_path = root / "jobs.json"
    output_path = root / "final.json"
    core_path.write_text(json.dumps(core, sort_keys=True) + "\n", encoding="utf-8")
    jobs_path.write_text(json.dumps(_jobs(), sort_keys=True) + "\n", encoding="utf-8")
    digest = hashlib.sha256(core_path.read_bytes()).hexdigest()
    return core_path, jobs_path, output_path, digest


def test_finalizer_uses_exact_job_step_timestamps_and_keeps_broker_unknown() -> None:
    module = finalizer()
    with tempfile.TemporaryDirectory() as raw:
        core_path, jobs_path, output_path, digest = _write_inputs(Path(raw), _core())
        value = module.finalize(
            core_path=core_path,
            jobs_path=jobs_path,
            output_path=output_path,
            expected_core_sha256=digest,
            source_comment_created_at="2026-09-08T20:00:00Z",
            artifact_upload_retry_observed=False,
        )
    timing = value["timing"]
    assert timing["command_to_self_hosted_job_start"]["value_ms"] == 5000
    assert timing["job_setup_action_preparation"]["value_ms"] == 2000
    assert timing["checkout"]["value_ms"] == 2000
    assert timing["observer"]["value_ms"] == 850
    assert timing["observer"]["step_metadata_value_ms"] == 2000
    assert timing["broker_assignment"]["value_ms"] is None
    assert timing["broker_assignment"]["source"] == "UNAVAILABLE"
    assert value["classification"] == "HEALTHY"
    assert value["transport"]["artifact_round_trip"] == {
        "core_upload_succeeded": True,
        "upload_retry_observed": False,
        "hosted_download_succeeded": True,
        "core_digest_verified": True,
    }
    rendered = json.dumps(value, sort_keys=True)
    assert "must-not-escape" not in rendered
    assert "runner_id" not in rendered


def test_artifact_upload_retry_downgrades_healthy_to_degraded_without_reobservation() -> None:
    module = finalizer()
    with tempfile.TemporaryDirectory() as raw:
        core_path, jobs_path, output_path, digest = _write_inputs(Path(raw), _core())
        value = module.finalize(
            core_path=core_path,
            jobs_path=jobs_path,
            output_path=output_path,
            expected_core_sha256=digest,
            source_comment_created_at="2026-09-08T20:00:00Z",
            artifact_upload_retry_observed=True,
        )
    assert value["classification"] == "TRANSPORT_DEGRADED"
    assert value["transport"]["artifact_round_trip"]["upload_retry_observed"] is True
    assert "ARTIFACT_UPLOAD_RETRY" in value["transport"]["failure_classes"]


def test_unreadable_watchdog_state_prevents_final_healthy() -> None:
    module = finalizer()
    with tempfile.TemporaryDirectory() as raw:
        core_path, jobs_path, output_path, digest = _write_inputs(Path(raw), _core(watchdog_readable=False))
        value = module.finalize(
            core_path=core_path,
            jobs_path=jobs_path,
            output_path=output_path,
            expected_core_sha256=digest,
            source_comment_created_at="2026-09-08T20:00:00Z",
            artifact_upload_retry_observed=False,
        )
    assert value["classification"] == "TRANSPORT_UNAVAILABLE"
    assert "WATCHDOG_STATE_UNAVAILABLE" in value["evidence_limitations"]


def test_digest_mismatch_fails_closed_before_finalization() -> None:
    module = finalizer()
    with tempfile.TemporaryDirectory() as raw:
        core_path, jobs_path, output_path, _ = _write_inputs(Path(raw), _core())
        try:
            module.finalize(
                core_path=core_path,
                jobs_path=jobs_path,
                output_path=output_path,
                expected_core_sha256="0" * 64,
                source_comment_created_at="2026-09-08T20:00:00Z",
                artifact_upload_retry_observed=False,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("runner transport finalizer accepted mismatched core digest")
        assert not output_path.exists()


def test_ambiguous_observer_job_lineage_fails_closed() -> None:
    module = finalizer()
    jobs = _jobs()
    jobs["jobs"].append(dict(jobs["jobs"][0]))
    try:
        module._observer_job(jobs)
    except ValueError:
        return
    raise AssertionError("runner transport finalizer accepted ambiguous observer job lineage")


def main() -> int:
    tests = sorted(
        (value for name, value in globals().items() if name.startswith("test_") and callable(value)),
        key=lambda item: item.__name__,
    )
    for test in tests:
        test()
    print(f"RUNNER_TRANSPORT_FINALIZER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
