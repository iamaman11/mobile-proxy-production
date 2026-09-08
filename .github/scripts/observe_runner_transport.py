#!/usr/bin/env python3
"""Bounded read-only observation of the self-hosted runner ↔ GitHub transport."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "controller"))

from runner_transport_evidence import collect_runner_transport_evidence  # noqa: E402
from runner_transport_probe import (  # noqa: E402
    collect_runner_runtime,
    collect_watchdog,
    load_policy,
    run_active_probes,
)

_SCHEMA = "runner-transport-observation.v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _session_view(raw: dict[str, object], *, now: datetime) -> dict[str, object]:
    started = _parse_utc(raw.get("listener_session_started_at_utc"))
    established = _parse_utc(raw.get("last_successful_session_establishment_at_utc"))
    failure_classes = set(raw.get("failure_classes", [])) if isinstance(raw.get("failure_classes"), list) else set()
    complete = bool(
        raw.get("diagnostic_evidence_available") is True
        and raw.get("diagnostics_truncated") is False
        and started is not None
        and established is not None
        and "SESSION_WINDOW_UNRESOLVED" not in failure_classes
    )
    age_seconds = None
    if started is not None:
        age_seconds = max(0, int((now - started).total_seconds()))
    return {
        "diagnostic_window": "CURRENT_LISTENER_SESSION",
        "evidence_available": raw.get("diagnostic_evidence_available") is True,
        "evidence_complete": complete,
        "diagnostics_truncated": raw.get("diagnostics_truncated") is True,
        "listener_session_started_at_utc": raw.get("listener_session_started_at_utc"),
        "last_successful_session_establishment_at_utc": raw.get("last_successful_session_establishment_at_utc"),
        "session_age_seconds": age_seconds,
        "runner_version": raw.get("runner_version"),
        "listener_files_scanned": raw.get("listener_files_scanned"),
        "worker_files_scanned": raw.get("worker_files_scanned"),
    }


def _probe_final_results(active: dict[str, object]) -> list[str]:
    roles = active.get("roles")
    if not isinstance(roles, list):
        raise AssertionError("runner transport active probe roles differ")
    results: list[str] = []
    for item in roles:
        if not isinstance(item, dict) or not isinstance(item.get("final_result"), str):
            raise AssertionError("runner transport active probe result differs")
        results.append(str(item["final_result"]))
    return results


def _failure_domain(runner: dict[str, object], active: dict[str, object]) -> str:
    if runner.get("service_state") == "INACTIVE":
        return "RUNNER_SERVICE"
    finals = _probe_final_results(active)
    if any(item in {"DNS_FAILURE", "PROXY_DNS_FAILURE"} for item in finals):
        return "DNS_RESOLUTION"
    # TLS/EOF/reset symptoms do not by themselves prove a TLS trust-domain cause.
    return "UNKNOWN"


def _watchdog_recovery_context(watchdog: dict[str, object]) -> bool:
    restart_count = watchdog.get("restart_count_window")
    return bool(
        watchdog.get("cooldown_active") is True
        or watchdog.get("post_restart_grace_active") is True
        or (type(restart_count) is int and restart_count > 0)
    )


def _classification(
    *,
    runner: dict[str, object],
    session: dict[str, object],
    transport: dict[str, object],
    environment_presence: dict[str, object],
    watchdog: dict[str, object],
    active: dict[str, object],
) -> tuple[str, list[str]]:
    limitations: list[str] = ["BROKER_ASSIGNMENT_TIMESTAMP_UNAVAILABLE"]
    finals = _probe_final_results(active)

    unavailable = False
    if runner.get("service_state") != "ACTIVE":
        limitations.append("RUNNER_SERVICE_NOT_CONFIRMED_ACTIVE")
        unavailable = True
    if runner.get("runner_activity") != "BUSY":
        limitations.append("RUNNER_JOB_ACTIVITY_UNAVAILABLE")
        unavailable = True
    if session.get("evidence_complete") is not True:
        limitations.append("CURRENT_SESSION_EVIDENCE_INCOMPLETE")
        unavailable = True
    if environment_presence.get("service_environment_observed") is not True:
        limitations.append("SERVICE_ENVIRONMENT_UNAVAILABLE")
        unavailable = True
    if environment_presence.get("observer_environment_matches_presence") is False:
        limitations.append("SERVICE_AND_JOB_ENVIRONMENT_PRESENCE_DIFFER")
        unavailable = True
    if any(item in {"TOOL_UNAVAILABLE", "BUDGET_EXHAUSTED"} for item in finals):
        limitations.append("ACTIVE_PROBE_CAPABILITY_UNAVAILABLE")
        unavailable = True
    if active.get("budget_exhausted") is True:
        limitations.append("ACTIVE_PROBE_BUDGET_EXHAUSTED")
        unavailable = True
    if watchdog.get("state_readable") is not True:
        limitations.append("WATCHDOG_STATE_UNAVAILABLE")
        unavailable = True
    if watchdog.get("timer_state") == "UNKNOWN":
        limitations.append("WATCHDOG_TIMER_STATE_UNAVAILABLE")
        unavailable = True
    if watchdog.get("decision") == "UNKNOWN":
        limitations.append("WATCHDOG_DECISION_UNKNOWN")
        unavailable = True

    if unavailable:
        return "TRANSPORT_UNAVAILABLE", sorted(set(limitations))

    counters = transport.get("error_counters")
    if not isinstance(counters, dict):
        raise AssertionError("runner transport error counters differ")
    observed_transport_symptom = any(type(value) is int and value > 0 for value in counters.values())
    degraded = bool(
        observed_transport_symptom
        or any(item != "SUCCESS" for item in finals)
        or active.get("retry_observed") is True
        or watchdog.get("timer_state") == "INACTIVE"
        or watchdog.get("decision") in {"RATE_LIMITED", "RESTART_ELIGIBLE"}
        or _watchdog_recovery_context(watchdog)
    )
    return ("TRANSPORT_DEGRADED" if degraded else "HEALTHY"), sorted(set(limitations))


def _timing(observer_ms: int) -> dict[str, object]:
    return {
        "command_to_self_hosted_job_start": {
            "value_ms": None,
            "source": "GITHUB_JOB_METADATA_PENDING",
        },
        "job_setup_action_preparation": {
            "value_ms": None,
            "source": "GITHUB_JOB_METADATA_PENDING",
        },
        "checkout": {
            "value_ms": None,
            "source": "GITHUB_JOB_METADATA_PENDING",
        },
        "observer": {
            "value_ms": observer_ms,
            "source": "LOCAL_MONOTONIC",
        },
        "broker_assignment": {
            "value_ms": None,
            "source": "UNAVAILABLE",
        },
    }


def _safety() -> dict[str, bool]:
    return {
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
    }


def observe(*, controller_revision: str, runner_temp: Path) -> dict[str, object]:
    started_wall = _utc_now()
    started_mono = time.monotonic()
    policy = load_policy()

    legacy = collect_runner_transport_evidence(
        runner_temp=runner_temp,
        assignment_latency_ms=0,
        include_context=True,
    )
    runner, environment_presence = collect_runner_runtime()
    watchdog = collect_watchdog()
    active = run_active_probes(policy)

    finished_wall = _utc_now()
    observer_ms = max(0, int((time.monotonic() - started_mono) * 1000))
    session = _session_view(legacy, now=finished_wall)
    context_counters = legacy.get("context_counters")
    if not isinstance(context_counters, dict):
        raise AssertionError("runner transport context counters differ")
    transport = {
        "error_counters": legacy.get("error_counters"),
        "context_counters": context_counters,
        "failure_classes": legacy.get("failure_classes"),
        "repeated_transport_error": legacy.get("repeated_transport_error"),
        "active_probes": active,
    }
    classification, limitations = _classification(
        runner=runner,
        session=session,
        transport=transport,
        environment_presence=environment_presence,
        watchdog=watchdog,
        active=active,
    )
    failure_domain = _failure_domain(runner, active)

    value: dict[str, object] = {
        "schema": _SCHEMA,
        "controller_revision": controller_revision,
        "observation_started_at_utc": _format(started_wall),
        "observation_finished_at_utc": _format(finished_wall),
        "classification": classification,
        "session": session,
        "timing": _timing(observer_ms),
        "transport": transport,
        "capabilities": {
            "active_probe_budget_seconds": policy["active_probe_budget_seconds"],
            "attempt_timeout_seconds": policy["attempt_timeout_seconds"],
            "max_destinations": policy["max_destinations"],
            "max_attempts_per_destination": policy["max_attempts_per_destination"],
            "max_concurrency": policy["max_concurrency"],
            "tls_verification_enforced": policy["tls_verification_required"],
            "artifact_round_trip_required": True,
            "exact_broker_assignment_timing_available": False,
        },
        "environment_presence": environment_presence,
        "watchdog": watchdog,
        "failure_domain": failure_domain,
        "evidence_limitations": limitations,
        "safety": _safety(),
    }
    return value


def validate_observation(value: dict[str, object]) -> None:
    expected_top = {
        "schema", "controller_revision", "observation_started_at_utc", "observation_finished_at_utc",
        "classification", "session", "timing", "transport", "capabilities", "environment_presence",
        "watchdog", "failure_domain", "evidence_limitations", "safety",
    }
    if set(value) != expected_top or value.get("schema") != _SCHEMA:
        raise AssertionError("runner transport observation schema differs")
    if value.get("classification") not in {"HEALTHY", "TRANSPORT_DEGRADED", "TRANSPORT_UNAVAILABLE"}:
        raise AssertionError("runner transport observation classification differs")
    if value.get("failure_domain") not in {"UNKNOWN", "RUNNER_SERVICE", "DNS_RESOLUTION", "TLS_VALIDATION"}:
        raise AssertionError("runner transport observation failure domain differs")
    if not isinstance(value.get("evidence_limitations"), list):
        raise AssertionError("runner transport observation limitations differ")

    safety = value.get("safety")
    if not isinstance(safety, dict) or safety.get("network_probe_performed") is not True:
        raise AssertionError("runner transport observation safety differs")
    for field, item in safety.items():
        if field == "network_probe_performed":
            continue
        if item is not False:
            raise AssertionError("runner transport observation safety differs: " + field)

    rendered = json.dumps(value, sort_keys=True)
    forbidden = (
        "authorization:", "bearer ", "token=", "password=", "runner_name", "runner_id",
        "android_production_serial", "http://", "https://", "/var/", "/opt/", "/home/",
    )
    lowered = rendered.lower()
    if any(item in lowered for item in forbidden):
        raise AssertionError("runner transport observation exposed forbidden raw material")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--controller-revision", required=True)
    args = parser.parse_args(argv)
    if len(args.controller_revision) != 40 or any(ch not in "0123456789abcdef" for ch in args.controller_revision):
        raise ValueError("controller revision differs")

    runner_temp_text = os.environ.get("RUNNER_TEMP", "")
    runner_temp = Path(runner_temp_text) if runner_temp_text else Path(".")
    value = observe(controller_revision=args.controller_revision, runner_temp=runner_temp)
    validate_observation(value)
    args.output.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "RUNNER_TRANSPORT_OBSERVATION "
        f"classification={value['classification']} failure_domain={value['failure_domain']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
