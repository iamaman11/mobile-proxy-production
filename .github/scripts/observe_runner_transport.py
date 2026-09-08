#!/usr/bin/env python3
"""Bounded read-only observation of the self-hosted runner ↔ GitHub transport."""
from __future__ import annotations

import argparse
import json
import os
import re
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
_MAX_INT = 2_147_483_647
_ERROR_KEYS = {
    "broker_reconnect", "runserver_reconnect", "tls_error", "eof_error", "connection_reset",
    "action_download_error", "artifact_transport_error", "transport_error_lines",
}
_PROBE_RESULTS = {
    "SUCCESS", "PROXY_DNS_FAILURE", "DNS_FAILURE", "CONNECT_FAILURE", "TLS_FAILURE",
    "TIMEOUT", "TRANSPORT_FAILURE", "TOOL_UNAVAILABLE", "UNKNOWN_FAILURE", "BUDGET_EXHAUSTED",
}
_FAILURE_CLASSES = {
    "BROKER_RECONNECT", "RUNSERVER_RECONNECT", "TLS_ERROR", "EOF_ERROR", "CONNECTION_RESET",
    "ACTION_DOWNLOAD_TRANSPORT", "ARTIFACT_TRANSPORT", "SESSION_WINDOW_UNRESOLVED",
    "DIAGNOSTIC_WINDOW_TRUNCATED", "RUNNER_VERSION_UNAVAILABLE", "SESSION_ESTABLISHMENT_UNOBSERVED",
    "DIAGNOSTIC_EVIDENCE_UNAVAILABLE", "ARTIFACT_UPLOAD_RETRY",
}
_LIMITATIONS = {
    "BROKER_ASSIGNMENT_TIMESTAMP_UNAVAILABLE", "RUNNER_SERVICE_NOT_CONFIRMED_ACTIVE",
    "RUNNER_JOB_ACTIVITY_UNAVAILABLE", "CURRENT_SESSION_EVIDENCE_INCOMPLETE",
    "SERVICE_ENVIRONMENT_UNAVAILABLE", "SERVICE_AND_JOB_ENVIRONMENT_PRESENCE_DIFFER",
    "ACTIVE_PROBE_CAPABILITY_UNAVAILABLE", "ACTIVE_PROBE_BUDGET_EXHAUSTED",
    "WATCHDOG_STATE_UNAVAILABLE", "WATCHDOG_TIMER_STATE_UNAVAILABLE", "WATCHDOG_DECISION_UNKNOWN",
    "COMMAND_TO_SELF_HOSTED_JOB_START_TIMING_UNAVAILABLE",
    "JOB_SETUP_ACTION_PREPARATION_TIMING_UNAVAILABLE", "CHECKOUT_TIMING_UNAVAILABLE",
    "OBSERVER_STEP_METADATA_TIMING_UNAVAILABLE",
}


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


def _object(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise AssertionError(f"runner transport {label} schema differs")
    return value


def _integer(value: object, label: str, *, maximum: int = _MAX_INT) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise AssertionError(f"runner transport {label} differs")
    return value


def _timestamp(value: object, *, allow_none: bool = False) -> datetime | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or len(value) > 64:
        return None
    return _parse_utc(value)


def _validate_timing(timing: object, *, finalized: bool) -> None:
    value = _object(timing, {
        "command_to_self_hosted_job_start", "job_setup_action_preparation", "checkout", "observer",
        "broker_assignment",
    }, "timing")
    if finalized:
        sources = {
            "command_to_self_hosted_job_start": "ISSUE_COMMENT_CREATED_AT_TO_ACTIONS_JOB_STARTED_AT",
            "job_setup_action_preparation": "ACTIONS_JOB_STARTED_AT_TO_CHECKOUT_STEP_STARTED_AT",
            "checkout": "ACTIONS_CHECKOUT_STEP_TIMESTAMPS",
        }
        for name, source in sources.items():
            block = _object(value[name], {"value_ms", "source", "start_utc", "finish_utc"}, f"timing {name}")
            if block["source"] != source or (block["value_ms"] is not None and type(block["value_ms"]) is not int):
                raise AssertionError(f"runner transport timing {name} differs")
            if _timestamp(block["start_utc"], allow_none=True) is None and block["start_utc"] is not None:
                raise AssertionError(f"runner transport timing {name} start differs")
            if _timestamp(block["finish_utc"], allow_none=True) is None and block["finish_utc"] is not None:
                raise AssertionError(f"runner transport timing {name} finish differs")
        observer = _object(value["observer"], {"value_ms", "source", "step_metadata_value_ms"}, "observer timing")
        _integer(observer["value_ms"], "observer timing")
        if observer["source"] != "LOCAL_MONOTONIC" or (
            observer["step_metadata_value_ms"] is not None and type(observer["step_metadata_value_ms"]) is not int
        ):
            raise AssertionError("runner transport observer timing differs")
        broker = _object(value["broker_assignment"], {"value_ms", "source", "start_utc", "finish_utc"}, "broker timing")
        if broker != {"value_ms": None, "source": "UNAVAILABLE", "start_utc": None, "finish_utc": None}:
            raise AssertionError("runner transport broker timing differs")
        return

    pending = {"value_ms": None, "source": "GITHUB_JOB_METADATA_PENDING"}
    for name in ("command_to_self_hosted_job_start", "job_setup_action_preparation", "checkout"):
        if _object(value[name], {"value_ms", "source"}, f"timing {name}") != pending:
            raise AssertionError(f"runner transport timing {name} differs")
    observer = _object(value["observer"], {"value_ms", "source"}, "observer timing")
    _integer(observer["value_ms"], "observer timing")
    if observer["source"] != "LOCAL_MONOTONIC":
        raise AssertionError("runner transport observer timing differs")
    if _object(value["broker_assignment"], {"value_ms", "source"}, "broker timing") != {
        "value_ms": None, "source": "UNAVAILABLE",
    }:
        raise AssertionError("runner transport broker timing differs")


def _validate_active_probes(value: object, *, policy: dict[str, object]) -> dict[str, object]:
    active = _object(value, {"roles", "retry_observed", "budget_exhausted"}, "active probes")
    roles = active["roles"]
    expected_roles = {str(item["role"]) for item in policy["destinations"]}
    if not isinstance(roles, list) or len(roles) != len(expected_roles):
        raise AssertionError("runner transport active probe role count differs")
    seen: set[str] = set()
    retry = False
    exhausted = False
    for raw_role in roles:
        role = _object(raw_role, {"role", "attempts", "final_result"}, "active probe role")
        if role["role"] not in expected_roles or role["role"] in seen:
            raise AssertionError("runner transport active probe role differs")
        seen.add(str(role["role"]))
        attempts = role["attempts"]
        if not isinstance(attempts, list) or not attempts or len(attempts) > int(policy["max_attempts_per_destination"]):
            raise AssertionError("runner transport active probe attempts differ")
        for number, raw_attempt in enumerate(attempts, 1):
            attempt = _object(raw_attempt, {"attempt", "result", "duration_ms"}, "active probe attempt")
            if attempt["attempt"] != number or attempt["result"] not in _PROBE_RESULTS:
                raise AssertionError("runner transport active probe attempt differs")
            _integer(attempt["duration_ms"], "active probe duration")
            exhausted = exhausted or attempt["result"] == "BUDGET_EXHAUSTED"
        if role["final_result"] != attempts[-1]["result"]:
            raise AssertionError("runner transport active probe final result differs")
        retry = retry or len(attempts) > 1
    if seen != expected_roles or active["retry_observed"] is not retry or active["budget_exhausted"] is not exhausted:
        raise AssertionError("runner transport active probe summary differs")
    return active


def validate_observation(value: dict[str, object]) -> None:
    expected_top = {
        "schema", "controller_revision", "observation_started_at_utc", "observation_finished_at_utc",
        "classification", "session", "timing", "transport", "capabilities", "environment_presence",
        "watchdog", "failure_domain", "evidence_limitations", "safety",
    }
    if set(value) != expected_top or value.get("schema") != _SCHEMA:
        raise AssertionError("runner transport observation schema differs")

    policy = load_policy()
    classification = value.get("classification")
    if classification not in policy["classifications"] or value.get("failure_domain") not in policy["failure_domains"]:
        raise AssertionError("runner transport observation classification differs")
    revision = value.get("controller_revision")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise AssertionError("runner transport controller revision differs")
    started = _timestamp(value.get("observation_started_at_utc"))
    finished = _timestamp(value.get("observation_finished_at_utc"))
    if started is None or finished is None or finished < started:
        raise AssertionError("runner transport observation timestamps differ")

    session = _object(value.get("session"), {
        "diagnostic_window", "evidence_available", "evidence_complete", "diagnostics_truncated",
        "listener_session_started_at_utc", "last_successful_session_establishment_at_utc",
        "session_age_seconds", "runner_version", "listener_files_scanned", "worker_files_scanned",
    }, "session")
    if session["diagnostic_window"] != "CURRENT_LISTENER_SESSION" or any(
        type(session[field]) is not bool for field in ("evidence_available", "evidence_complete", "diagnostics_truncated")
    ):
        raise AssertionError("runner transport session differs")
    session_started = _timestamp(session["listener_session_started_at_utc"], allow_none=True)
    established = _timestamp(session["last_successful_session_establishment_at_utc"], allow_none=True)
    if session["listener_session_started_at_utc"] is not None and session_started is None:
        raise AssertionError("runner transport session start differs")
    if session["last_successful_session_establishment_at_utc"] is not None and established is None:
        raise AssertionError("runner transport session establishment differs")
    if (session_started is not None and session_started > finished) or (established is not None and established > finished):
        raise AssertionError("runner transport session evidence is in the future")
    if session_started is not None and established is not None and established < session_started:
        raise AssertionError("runner transport session establishment precedes session start")
    if session["session_age_seconds"] is not None:
        _integer(session["session_age_seconds"], "session age")
    if session["runner_version"] is not None and (
        not isinstance(session["runner_version"], str)
        or re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", session["runner_version"]) is None
    ):
        raise AssertionError("runner transport runner version differs")
    _integer(session["listener_files_scanned"], "listener files scanned", maximum=1)
    _integer(session["worker_files_scanned"], "worker files scanned", maximum=32)
    if session["evidence_complete"] is True and (
        session["evidence_available"] is not True or session["diagnostics_truncated"] is not False
        or session_started is None or established is None or session["listener_files_scanned"] != 1
    ):
        raise AssertionError("runner transport complete session evidence differs")

    transport = value.get("transport")
    if not isinstance(transport, dict):
        raise AssertionError("runner transport transport schema differs")
    finalized = "artifact_round_trip" in transport
    transport_keys = {"error_counters", "context_counters", "failure_classes", "repeated_transport_error", "active_probes"}
    if finalized:
        transport_keys.add("artifact_round_trip")
    transport = _object(transport, transport_keys, "transport")
    counters = _object(transport["error_counters"], _ERROR_KEYS, "error counters")
    for name in _ERROR_KEYS:
        _integer(counters[name], f"error counter {name}")
    context = _object(transport["context_counters"], {"expected_local_poll_cancellation"}, "context counters")
    _integer(context["expected_local_poll_cancellation"], "context counter")
    failure_classes = transport["failure_classes"]
    if not isinstance(failure_classes, list) or failure_classes != sorted(set(failure_classes)) or any(
        not isinstance(item, str) or item not in _FAILURE_CLASSES for item in failure_classes
    ):
        raise AssertionError("runner transport failure classes differ")
    if type(transport["repeated_transport_error"]) is not bool:
        raise AssertionError("runner transport repeated transport error differs")
    active = _validate_active_probes(transport["active_probes"], policy=policy)
    if finalized:
        round_trip = _object(transport["artifact_round_trip"], {
            "core_upload_succeeded", "upload_retry_observed", "hosted_download_succeeded", "core_digest_verified",
        }, "artifact round trip")
        if any(type(round_trip[field]) is not bool for field in round_trip) or any(
            round_trip[field] is not True for field in ("core_upload_succeeded", "hosted_download_succeeded", "core_digest_verified")
        ):
            raise AssertionError("runner transport artifact round trip differs")
        retry_class = "ARTIFACT_UPLOAD_RETRY" in failure_classes
        if round_trip["upload_retry_observed"] is not retry_class or (retry_class and classification == "HEALTHY"):
            raise AssertionError("runner transport artifact retry lineage differs")
    _validate_timing(value.get("timing"), finalized=finalized)

    capabilities = _object(value.get("capabilities"), {
        "active_probe_budget_seconds", "attempt_timeout_seconds", "max_destinations",
        "max_attempts_per_destination", "max_concurrency", "tls_verification_enforced",
        "artifact_round_trip_required", "exact_broker_assignment_timing_available",
    }, "capabilities")
    if capabilities != {
        "active_probe_budget_seconds": policy["active_probe_budget_seconds"],
        "attempt_timeout_seconds": policy["attempt_timeout_seconds"],
        "max_destinations": policy["max_destinations"],
        "max_attempts_per_destination": policy["max_attempts_per_destination"],
        "max_concurrency": policy["max_concurrency"],
        "tls_verification_enforced": policy["tls_verification_required"],
        "artifact_round_trip_required": True,
        "exact_broker_assignment_timing_available": False,
    }:
        raise AssertionError("runner transport capabilities differ")

    environment = _object(value.get("environment_presence"), {
        "proxy_present", "custom_ca_present", "git_tls_override_present", "runtime_tls_override_present",
        "source", "service_environment_observed", "observer_environment_matches_presence",
    }, "environment presence")
    for field in ("proxy_present", "custom_ca_present", "git_tls_override_present", "runtime_tls_override_present", "service_environment_observed"):
        if type(environment[field]) is not bool:
            raise AssertionError("runner transport environment presence differs")
    if environment["source"] not in {"SERVICE_PROCESS", "OBSERVER_PROCESS"} or (
        environment["observer_environment_matches_presence"] is not None
        and type(environment["observer_environment_matches_presence"]) is not bool
    ):
        raise AssertionError("runner transport environment presence differs")

    watchdog = _object(value.get("watchdog"), {
        "timer_state", "state_readable", "decision", "cooldown_active", "restart_budget_remaining",
        "post_restart_grace_active", "restart_count_window",
    }, "watchdog")
    if watchdog["timer_state"] not in {"ACTIVE", "INACTIVE", "UNKNOWN"} or type(watchdog["state_readable"]) is not bool or watchdog["decision"] not in {
        "HEALTHY", "OBSERVE", "RESTART_ELIGIBLE", "RATE_LIMITED", "UNKNOWN",
    }:
        raise AssertionError("runner transport watchdog differs")
    if watchdog["state_readable"] is True:
        if type(watchdog["cooldown_active"]) is not bool or type(watchdog["post_restart_grace_active"]) is not bool:
            raise AssertionError("runner transport watchdog context differs")
        _integer(watchdog["restart_budget_remaining"], "watchdog restart budget", maximum=1000)
        _integer(watchdog["restart_count_window"], "watchdog restart count", maximum=1000)
    elif watchdog != {
        **watchdog,
        "decision": "UNKNOWN",
        "cooldown_active": None,
        "restart_budget_remaining": None,
        "post_restart_grace_active": None,
        "restart_count_window": None,
    }:
        raise AssertionError("runner transport unreadable watchdog context differs")

    limitations = value.get("evidence_limitations")
    if not isinstance(limitations, list) or limitations != sorted(set(limitations)) or any(
        not isinstance(item, str) or item not in _LIMITATIONS for item in limitations
    ):
        raise AssertionError("runner transport observation limitations differ")
    if value.get("safety") != _safety():
        raise AssertionError("runner transport observation safety differs")

    if classification == "HEALTHY" and (
        session["evidence_complete"] is not True
        or environment["service_environment_observed"] is not True
        or environment["observer_environment_matches_presence"] is not True
        or watchdog["state_readable"] is not True
        or watchdog["timer_state"] != "ACTIVE"
        or watchdog["decision"] in {"UNKNOWN", "RATE_LIMITED", "RESTART_ELIGIBLE"}
        or watchdog["cooldown_active"] is not False
        or watchdog["post_restart_grace_active"] is not False
        or watchdog["restart_count_window"] != 0
        or any(counters[name] != 0 for name in _ERROR_KEYS)
        or active["retry_observed"] is not False
        or active["budget_exhausted"] is not False
        or any(item["final_result"] != "SUCCESS" for item in active["roles"])
    ):
        raise AssertionError("runner transport healthy classification contradicts evidence")

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
