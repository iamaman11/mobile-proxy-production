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
_MAX_EVIDENCE_INT = 2_147_483_647
_ERROR_COUNTER_KEYS = frozenset({
    "broker_reconnect",
    "runserver_reconnect",
    "tls_error",
    "eof_error",
    "connection_reset",
    "action_download_error",
    "artifact_transport_error",
    "transport_error_lines",
})
_FAILURE_CLASSES = frozenset({
    "BROKER_RECONNECT",
    "RUNSERVER_RECONNECT",
    "TLS_ERROR",
    "EOF_ERROR",
    "CONNECTION_RESET",
    "ACTION_DOWNLOAD_TRANSPORT",
    "ARTIFACT_TRANSPORT",
    "SESSION_WINDOW_UNRESOLVED",
    "DIAGNOSTIC_WINDOW_TRUNCATED",
    "RUNNER_VERSION_UNAVAILABLE",
    "SESSION_ESTABLISHMENT_UNOBSERVED",
    "DIAGNOSTIC_EVIDENCE_UNAVAILABLE",
    "ARTIFACT_UPLOAD_RETRY",
})
_PROBE_RESULTS = frozenset({
    "SUCCESS",
    "PROXY_DNS_FAILURE",
    "DNS_FAILURE",
    "CONNECT_FAILURE",
    "TLS_FAILURE",
    "TIMEOUT",
    "TRANSPORT_FAILURE",
    "TOOL_UNAVAILABLE",
    "UNKNOWN_FAILURE",
    "BUDGET_EXHAUSTED",
})
_EVIDENCE_LIMITATIONS = frozenset({
    "BROKER_ASSIGNMENT_TIMESTAMP_UNAVAILABLE",
    "RUNNER_SERVICE_NOT_CONFIRMED_ACTIVE",
    "RUNNER_JOB_ACTIVITY_UNAVAILABLE",
    "CURRENT_SESSION_EVIDENCE_INCOMPLETE",
    "SERVICE_ENVIRONMENT_UNAVAILABLE",
    "SERVICE_AND_JOB_ENVIRONMENT_PRESENCE_DIFFER",
    "ACTIVE_PROBE_CAPABILITY_UNAVAILABLE",
    "ACTIVE_PROBE_BUDGET_EXHAUSTED",
    "WATCHDOG_STATE_UNAVAILABLE",
    "WATCHDOG_TIMER_STATE_UNAVAILABLE",
    "WATCHDOG_DECISION_UNKNOWN",
    "COMMAND_TO_SELF_HOSTED_JOB_START_TIMING_UNAVAILABLE",
    "JOB_SETUP_ACTION_PREPARATION_TIMING_UNAVAILABLE",
    "CHECKOUT_TIMING_UNAVAILABLE",
    "OBSERVER_STEP_METADATA_TIMING_UNAVAILABLE",
})
_SAFETY_FIELDS = frozenset({
    "phone_access_performed",
    "phone_mutation_performed",
    "provider_access_performed",
    "provider_mutation_performed",
    "deployment_created",
    "deployment_intent_created",
    "runner_restart_performed",
    "runner_registration_changed",
    "network_configuration_changed",
    "automatic_recovery_performed",
    "raw_runner_log_recorded",
    "raw_transport_url_recorded",
    "secret_values_recorded",
    "network_probe_performed",
})


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


def _strict_object(value: object, expected_keys: set[str] | frozenset[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(expected_keys):
        raise AssertionError(f"runner transport {label} schema differs")
    return value


def _strict_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise AssertionError(f"runner transport {label} type differs")
    return value


def _strict_int(value: object, label: str, *, maximum: int = _MAX_EVIDENCE_INT) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise AssertionError(f"runner transport {label} value differs")
    return value


def _strict_timestamp(value: object, label: str, *, allow_none: bool = False) -> datetime | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise AssertionError(f"runner transport {label} timestamp differs")
    parsed = _parse_utc(value)
    if parsed is None:
        raise AssertionError(f"runner transport {label} timestamp differs")
    return parsed


def _validate_session(value: object, *, observation_finished: datetime) -> dict[str, object]:
    session = _strict_object(value, {
        "diagnostic_window",
        "evidence_available",
        "evidence_complete",
        "diagnostics_truncated",
        "listener_session_started_at_utc",
        "last_successful_session_establishment_at_utc",
        "session_age_seconds",
        "runner_version",
        "listener_files_scanned",
        "worker_files_scanned",
    }, "session")
    if session.get("diagnostic_window") != "CURRENT_LISTENER_SESSION":
        raise AssertionError("runner transport session diagnostic window differs")
    available = _strict_bool(session.get("evidence_available"), "session evidence_available")
    complete = _strict_bool(session.get("evidence_complete"), "session evidence_complete")
    truncated = _strict_bool(session.get("diagnostics_truncated"), "session diagnostics_truncated")
    started = _strict_timestamp(
        session.get("listener_session_started_at_utc"),
        "listener session start",
        allow_none=True,
    )
    established = _strict_timestamp(
        session.get("last_successful_session_establishment_at_utc"),
        "listener session establishment",
        allow_none=True,
    )
    age = session.get("session_age_seconds")
    if age is not None:
        _strict_int(age, "session age")
    version = session.get("runner_version")
    if version is not None and (
        not isinstance(version, str)
        or re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", version) is None
    ):
        raise AssertionError("runner transport runner version differs")
    listener_files = _strict_int(session.get("listener_files_scanned"), "listener files scanned", maximum=1)
    _strict_int(session.get("worker_files_scanned"), "worker files scanned", maximum=32)
    if complete and (not available or truncated or started is None or established is None or listener_files != 1):
        raise AssertionError("runner transport complete session evidence is inconsistent")
    if established is not None and started is not None and established < started:
        raise AssertionError("runner transport session establishment precedes session start")
    if started is not None and started > observation_finished:
        raise AssertionError("runner transport listener session start is in the future")
    if established is not None and established > observation_finished:
        raise AssertionError("runner transport listener session establishment is in the future")
    return session


def _validate_timed_metadata_block(value: object, *, source: str, label: str) -> None:
    block = _strict_object(value, {"value_ms", "source", "start_utc", "finish_utc"}, label)
    if block.get("source") != source:
        raise AssertionError(f"runner transport {label} source differs")
    started = _strict_timestamp(block.get("start_utc"), f"{label} start", allow_none=True)
    finished = _strict_timestamp(block.get("finish_utc"), f"{label} finish", allow_none=True)
    duration = block.get("value_ms")
    if duration is not None:
        observed_ms = _strict_int(duration, f"{label} duration")
        if started is None or finished is None or finished < started:
            raise AssertionError(f"runner transport {label} timing differs")
        derived_ms = int((finished - started).total_seconds() * 1000)
        if observed_ms != derived_ms:
            raise AssertionError(f"runner transport {label} duration differs")
    elif started is not None and finished is not None and finished >= started:
        raise AssertionError(f"runner transport {label} omitted available duration")


def _validate_timing(value: object, *, finalized: bool) -> None:
    timing = _strict_object(value, {
        "command_to_self_hosted_job_start",
        "job_setup_action_preparation",
        "checkout",
        "observer",
        "broker_assignment",
    }, "timing")
    if not finalized:
        for name in ("command_to_self_hosted_job_start", "job_setup_action_preparation", "checkout"):
            block = _strict_object(timing.get(name), {"value_ms", "source"}, f"timing {name}")
            if block != {"value_ms": None, "source": "GITHUB_JOB_METADATA_PENDING"}:
                raise AssertionError(f"runner transport timing {name} core state differs")
        observer = _strict_object(timing.get("observer"), {"value_ms", "source"}, "timing observer")
        _strict_int(observer.get("value_ms"), "local observer timing")
        if observer.get("source") != "LOCAL_MONOTONIC":
            raise AssertionError("runner transport local observer timing source differs")
        broker = _strict_object(timing.get("broker_assignment"), {"value_ms", "source"}, "timing broker assignment")
        if broker != {"value_ms": None, "source": "UNAVAILABLE"}:
            raise AssertionError("runner transport broker assignment core timing differs")
        return

    _validate_timed_metadata_block(
        timing.get("command_to_self_hosted_job_start"),
        source="ISSUE_COMMENT_CREATED_AT_TO_ACTIONS_JOB_STARTED_AT",
        label="command to job timing",
    )
    _validate_timed_metadata_block(
        timing.get("job_setup_action_preparation"),
        source="ACTIONS_JOB_STARTED_AT_TO_CHECKOUT_STEP_STARTED_AT",
        label="job setup timing",
    )
    _validate_timed_metadata_block(
        timing.get("checkout"),
        source="ACTIONS_CHECKOUT_STEP_TIMESTAMPS",
        label="checkout timing",
    )
    observer = _strict_object(
        timing.get("observer"),
        {"value_ms", "source", "step_metadata_value_ms"},
        "timing observer",
    )
    _strict_int(observer.get("value_ms"), "local observer timing")
    if observer.get("source") != "LOCAL_MONOTONIC":
        raise AssertionError("runner transport local observer timing source differs")
    step_ms = observer.get("step_metadata_value_ms")
    if step_ms is not None:
        _strict_int(step_ms, "observer step metadata timing")
    broker = _strict_object(
        timing.get("broker_assignment"),
        {"value_ms", "source", "start_utc", "finish_utc"},
        "timing broker assignment",
    )
    if broker != {"value_ms": None, "source": "UNAVAILABLE", "start_utc": None, "finish_utc": None}:
        raise AssertionError("runner transport broker assignment final timing differs")


def _validate_active_probes(value: object, *, policy: dict[str, object]) -> dict[str, object]:
    active = _strict_object(value, {"roles", "retry_observed", "budget_exhausted"}, "active probes")
    roles = active.get("roles")
    if not isinstance(roles, list):
        raise AssertionError("runner transport active probe roles differ")
    expected_roles = {str(item["role"]) for item in policy["destinations"]}
    if len(roles) != len(expected_roles):
        raise AssertionError("runner transport active probe role count differs")
    seen: set[str] = set()
    retry_observed = False
    budget_exhausted = False
    for raw_role in roles:
        role = _strict_object(raw_role, {"role", "attempts", "final_result"}, "active probe role")
        role_name = role.get("role")
        if not isinstance(role_name, str) or role_name not in expected_roles or role_name in seen:
            raise AssertionError("runner transport active probe role differs")
        seen.add(role_name)
        attempts = role.get("attempts")
        if not isinstance(attempts, list) or not attempts or len(attempts) > int(policy["max_attempts_per_destination"]):
            raise AssertionError("runner transport active probe attempts differ")
        for index, raw_attempt in enumerate(attempts, start=1):
            attempt = _strict_object(raw_attempt, {"attempt", "result", "duration_ms"}, "active probe attempt")
            if attempt.get("attempt") != index:
                raise AssertionError("runner transport active probe attempt number differs")
            result = attempt.get("result")
            if result not in _PROBE_RESULTS:
                raise AssertionError("runner transport active probe result differs")
            _strict_int(attempt.get("duration_ms"), "active probe duration")
            budget_exhausted = budget_exhausted or result == "BUDGET_EXHAUSTED"
        if role.get("final_result") != attempts[-1]["result"]:
            raise AssertionError("runner transport active probe final result differs")
        retry_observed = retry_observed or len(attempts) > 1
    if seen != expected_roles:
        raise AssertionError("runner transport active probe role set differs")
    if _strict_bool(active.get("retry_observed"), "active probe retry_observed") != retry_observed:
        raise AssertionError("runner transport active probe retry summary differs")
    if _strict_bool(active.get("budget_exhausted"), "active probe budget_exhausted") != budget_exhausted:
        raise AssertionError("runner transport active probe budget summary differs")
    return active


def _validate_transport(
    value: object,
    *,
    policy: dict[str, object],
    classification: str,
) -> tuple[dict[str, object], bool]:
    if not isinstance(value, dict):
        raise AssertionError("runner transport transport schema differs")
    finalized = "artifact_round_trip" in value
    expected = {
        "error_counters",
        "context_counters",
        "failure_classes",
        "repeated_transport_error",
        "active_probes",
    }
    if finalized:
        expected.add("artifact_round_trip")
    transport = _strict_object(value, expected, "transport")

    counters = _strict_object(transport.get("error_counters"), _ERROR_COUNTER_KEYS, "error counters")
    for key in _ERROR_COUNTER_KEYS:
        _strict_int(counters.get(key), f"error counter {key}")
    context = _strict_object(
        transport.get("context_counters"),
        {"expected_local_poll_cancellation"},
        "context counters",
    )
    _strict_int(context.get("expected_local_poll_cancellation"), "expected local poll cancellation counter")
    failure_classes = transport.get("failure_classes")
    if (
        not isinstance(failure_classes, list)
        or any(not isinstance(item, str) or item not in _FAILURE_CLASSES for item in failure_classes)
        or failure_classes != sorted(set(failure_classes))
    ):
        raise AssertionError("runner transport failure classes differ")
    repeated = _strict_bool(transport.get("repeated_transport_error"), "repeated transport error")
    expected_repeated = any(
        counters[key] >= 2 for key in ("transport_error_lines", "broker_reconnect", "runserver_reconnect")
    )
    if repeated != expected_repeated:
        raise AssertionError("runner transport repeated transport error summary differs")
    active = _validate_active_probes(transport.get("active_probes"), policy=policy)

    if finalized:
        round_trip = _strict_object(
            transport.get("artifact_round_trip"),
            {"core_upload_succeeded", "upload_retry_observed", "hosted_download_succeeded", "core_digest_verified"},
            "artifact round trip",
        )
        for field in ("core_upload_succeeded", "hosted_download_succeeded", "core_digest_verified"):
            if _strict_bool(round_trip.get(field), f"artifact round trip {field}") is not True:
                raise AssertionError("runner transport final artifact round trip is incomplete")
        upload_retry = _strict_bool(round_trip.get("upload_retry_observed"), "artifact upload retry")
        if upload_retry != ("ARTIFACT_UPLOAD_RETRY" in failure_classes):
            raise AssertionError("runner transport artifact retry lineage differs")
        if upload_retry and classification == "HEALTHY":
            raise AssertionError("runner transport healthy classification hid artifact retry")

    if classification == "HEALTHY":
        if any(counters[key] != 0 for key in _ERROR_COUNTER_KEYS):
            raise AssertionError("runner transport healthy classification hid transport counters")
        if active.get("retry_observed") is not False or active.get("budget_exhausted") is not False:
            raise AssertionError("runner transport healthy classification hid active probe retry")
        if any(item.get("final_result") != "SUCCESS" for item in active["roles"]):
            raise AssertionError("runner transport healthy classification hid active probe failure")
    return transport, finalized


def _validate_environment(value: object) -> dict[str, object]:
    environment = _strict_object(value, {
        "proxy_present",
        "custom_ca_present",
        "git_tls_override_present",
        "runtime_tls_override_present",
        "source",
        "service_environment_observed",
        "observer_environment_matches_presence",
    }, "environment presence")
    for field in ("proxy_present", "custom_ca_present", "git_tls_override_present", "runtime_tls_override_present"):
        _strict_bool(environment.get(field), f"environment {field}")
    source = environment.get("source")
    if source not in {"SERVICE_PROCESS", "OBSERVER_PROCESS"}:
        raise AssertionError("runner transport environment source differs")
    service_observed = _strict_bool(
        environment.get("service_environment_observed"),
        "service environment observed",
    )
    matches = environment.get("observer_environment_matches_presence")
    if matches is not None:
        _strict_bool(matches, "observer environment presence match")
    if source == "SERVICE_PROCESS" and (not service_observed or matches is None):
        raise AssertionError("runner transport service environment evidence is inconsistent")
    if source == "OBSERVER_PROCESS" and (service_observed or matches is not None):
        raise AssertionError("runner transport observer environment fallback is inconsistent")
    return environment


def _validate_watchdog(value: object) -> dict[str, object]:
    watchdog = _strict_object(value, {
        "timer_state",
        "state_readable",
        "decision",
        "cooldown_active",
        "restart_budget_remaining",
        "post_restart_grace_active",
        "restart_count_window",
    }, "watchdog")
    if watchdog.get("timer_state") not in {"ACTIVE", "INACTIVE", "UNKNOWN"}:
        raise AssertionError("runner transport watchdog timer state differs")
    readable = _strict_bool(watchdog.get("state_readable"), "watchdog state readable")
    if watchdog.get("decision") not in {"HEALTHY", "OBSERVE", "RESTART_ELIGIBLE", "RATE_LIMITED", "UNKNOWN"}:
        raise AssertionError("runner transport watchdog decision differs")
    if not readable:
        if any(
            watchdog.get(field) is not None
            for field in (
                "cooldown_active",
                "restart_budget_remaining",
                "post_restart_grace_active",
                "restart_count_window",
            )
        ) or watchdog.get("decision") != "UNKNOWN":
            raise AssertionError("runner transport unreadable watchdog state is inconsistent")
        return watchdog
    _strict_bool(watchdog.get("cooldown_active"), "watchdog cooldown active")
    _strict_int(watchdog.get("restart_budget_remaining"), "watchdog restart budget", maximum=1000)
    _strict_bool(watchdog.get("post_restart_grace_active"), "watchdog post restart grace")
    _strict_int(watchdog.get("restart_count_window"), "watchdog restart count", maximum=1000)
    return watchdog


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
    if classification not in set(policy["classifications"]):
        raise AssertionError("runner transport observation classification differs")
    if value.get("failure_domain") not in set(policy["failure_domains"]):
        raise AssertionError("runner transport observation failure domain differs")
    revision = value.get("controller_revision")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise AssertionError("runner transport controller revision differs")
    observation_started = _strict_timestamp(value.get("observation_started_at_utc"), "observation start")
    observation_finished = _strict_timestamp(value.get("observation_finished_at_utc"), "observation finish")
    if observation_started is None or observation_finished is None or observation_finished < observation_started:
        raise AssertionError("runner transport observation timing order differs")

    session = _validate_session(value.get("session"), observation_finished=observation_finished)
    environment = _validate_environment(value.get("environment_presence"))
    watchdog = _validate_watchdog(value.get("watchdog"))
    transport, finalized = _validate_transport(
        value.get("transport"),
        policy=policy,
        classification=str(classification),
    )
    _validate_timing(value.get("timing"), finalized=finalized)

    capabilities = _strict_object(value.get("capabilities"), {
        "active_probe_budget_seconds",
        "attempt_timeout_seconds",
        "max_destinations",
        "max_attempts_per_destination",
        "max_concurrency",
        "tls_verification_enforced",
        "artifact_round_trip_required",
        "exact_broker_assignment_timing_available",
    }, "capabilities")
    expected_capabilities = {
        "active_probe_budget_seconds": policy["active_probe_budget_seconds"],
        "attempt_timeout_seconds": policy["attempt_timeout_seconds"],
        "max_destinations": policy["max_destinations"],
        "max_attempts_per_destination": policy["max_attempts_per_destination"],
        "max_concurrency": policy["max_concurrency"],
        "tls_verification_enforced": policy["tls_verification_required"],
        "artifact_round_trip_required": True,
        "exact_broker_assignment_timing_available": False,
    }
    if capabilities != expected_capabilities:
        raise AssertionError("runner transport observation capabilities differ")

    limitations = value.get("evidence_limitations")
    if (
        not isinstance(limitations, list)
        or any(not isinstance(item, str) or item not in _EVIDENCE_LIMITATIONS for item in limitations)
        or limitations != sorted(set(limitations))
    ):
        raise AssertionError("runner transport observation limitations differ")

    safety = _strict_object(value.get("safety"), _SAFETY_FIELDS, "safety")
    for field in _SAFETY_FIELDS:
        expected = field == "network_probe_performed"
        if _strict_bool(safety.get(field), f"safety {field}") is not expected:
            raise AssertionError("runner transport observation safety differs: " + field)

    if classification == "HEALTHY":
        if session.get("evidence_complete") is not True:
            raise AssertionError("runner transport healthy classification lacks complete session evidence")
        if environment.get("service_environment_observed") is not True or environment.get("observer_environment_matches_presence") is not True:
            raise AssertionError("runner transport healthy classification lacks trusted service environment evidence")
        if (
            watchdog.get("state_readable") is not True
            or watchdog.get("timer_state") != "ACTIVE"
            or watchdog.get("decision") in {"UNKNOWN", "RATE_LIMITED", "RESTART_ELIGIBLE"}
            or watchdog.get("cooldown_active") is not False
            or watchdog.get("post_restart_grace_active") is not False
            or watchdog.get("restart_count_window") != 0
        ):
            raise AssertionError("runner transport healthy classification hid watchdog degradation context")
        if finalized and transport["artifact_round_trip"]["upload_retry_observed"] is not False:
            raise AssertionError("runner transport healthy classification hid artifact retry")

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
