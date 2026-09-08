#!/usr/bin/env python3
"""Bounded, read-only proof of the GitHub runner-to-phone transport path."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "controller"))

from phone_target import (  # noqa: E402
    PhoneFailurePhase,
    PhoneTargetDiagnosticFailure,
    _adb,
    _probe_root_stderr_exit_contract,
    _probe_root_stdout_contract,
    _require_device,
)
from runner_transport_evidence import classify_preflight, collect_runner_transport_evidence  # noqa: E402


_T = TypeVar("_T")
_TIMING_PHASES = (
    "adb_tooling",
    "registered_device_state",
    "root_stdout_contract",
    "root_stderr_exit_contract",
)


def _parse_created_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("command provenance timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("command provenance timestamp is invalid")
    return parsed.astimezone(timezone.utc)


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _base_safety() -> dict[str, bool]:
    return {
        "phone_access_performed": False,
        "phone_mutation_performed": False,
        "provider_access_performed": False,
        "provider_mutation_performed": False,
        "deployment_created": False,
        "deployment_intent_created": False,
        "raw_device_identifier_recorded": False,
        "secret_values_recorded": False,
        "raw_runner_log_recorded": False,
        "raw_transport_url_recorded": False,
        "automatic_recovery_performed": False,
    }


def _base_timings() -> dict[str, int | None]:
    return {**{name: None for name in _TIMING_PHASES}, "total": None}


def _timed(timings: dict[str, int | None], name: str, action: Callable[[], _T]) -> _T:
    started = time.monotonic()
    try:
        return action()
    finally:
        timings[name] = int((time.monotonic() - started) * 1000)


def _payload(
    *,
    controller_revision: str,
    classification: str,
    phone_failure_phase: PhoneFailurePhase,
    timings: dict[str, int | None],
    transport: dict[str, object],
    safety: dict[str, bool],
) -> dict[str, object]:
    if classification == "NOT_READY":
        if phone_failure_phase is PhoneFailurePhase.NONE:
            raise AssertionError("NOT_READY requires a typed phone failure")
    elif classification in {"READY", "TRANSPORT_DEGRADED"}:
        if phone_failure_phase is not PhoneFailurePhase.NONE:
            raise AssertionError("phone-ready classification cannot contain a phone failure")
    else:
        raise AssertionError("phone transport classification differs")

    return {
        "schema": "phone-transport-preflight.v3",
        "controller_revision": controller_revision,
        "classification": classification,
        "phone_failure_phase": phone_failure_phase.value,
        "timing_ms": timings,
        "transport": transport,
        "safety": safety,
        "execution_semantics": {
            "workflow_execution": "success",
            "phone_readiness": classification,
            "automatic_recovery_performed": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--source-comment-created-at", required=True)
    args = parser.parse_args(argv)

    started = time.monotonic()
    timings = _base_timings()
    safety = _base_safety()
    transport: dict[str, object] = {
        "artifact_upload_required": True,
        "runner_assignment_latency_ms": None,
    }

    created_at = _parse_created_at(args.source_comment_created_at)
    assignment_latency_ms = int((datetime.now(timezone.utc) - created_at).total_seconds() * 1000)
    if assignment_latency_ms < 0:
        raise ValueError("command provenance clock differs")
    transport["runner_assignment_latency_ms"] = assignment_latency_ms

    runner_temp = os.environ.get("RUNNER_TEMP", "")
    transport.update(
        collect_runner_transport_evidence(
            runner_temp=Path(runner_temp) if runner_temp else Path("."),
            assignment_latency_ms=assignment_latency_ms,
        )
    )

    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    if not serial:
        raise RuntimeError("registered production phone binding is unavailable")

    phone_failure_phase = PhoneFailurePhase.NONE
    try:
        _timed(timings, "adb_tooling", _adb)

        safety["phone_access_performed"] = True
        _timed(timings, "registered_device_state", lambda: _require_device(serial))
        _timed(timings, "root_stdout_contract", lambda: _probe_root_stdout_contract(serial))
        _timed(
            timings,
            "root_stderr_exit_contract",
            lambda: _probe_root_stderr_exit_contract(serial),
        )
    except PhoneTargetDiagnosticFailure as exc:
        phone_failure_phase = exc.phase

    timings["total"] = int((time.monotonic() - started) * 1000)
    if phone_failure_phase is PhoneFailurePhase.NONE:
        classification = classify_preflight(phone_ready=True, transport=transport)
    else:
        classification = "NOT_READY"

    value = _payload(
        controller_revision=args.controller_revision,
        classification=classification,
        phone_failure_phase=phone_failure_phase,
        timings=timings,
        transport=transport,
        safety=safety,
    )
    _write(args.output, value)
    print(
        "PHONE_TRANSPORT_PREFLIGHT "
        f"classification={classification} phone_failure_phase={phone_failure_phase.value}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
