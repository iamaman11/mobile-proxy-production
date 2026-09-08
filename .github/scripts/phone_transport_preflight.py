#!/usr/bin/env python3
"""Bounded, read-only proof of the GitHub runner-to-phone transport path."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "controller"))

from phone_target import PhoneTargetUnavailable
from phone_transport_readiness import probe_registered_phone_transport
from runner_transport_evidence import classify_preflight, collect_runner_transport_evidence


def _parse_created_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PhoneTargetUnavailable("command provenance timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise PhoneTargetUnavailable("command provenance timestamp is invalid")
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
    }


def _write_not_ready(
    *,
    output: Path,
    controller_revision: str,
    timings: dict[str, int],
    transport: dict[str, object],
    safety: dict[str, bool],
    failure_phase: str,
    failure_code: str,
    target_state: str | None = None,
) -> int:
    payload: dict[str, object] = {
        "schema": "phone-transport-preflight.v2",
        "controller_revision": controller_revision,
        "classification": "NOT_READY",
        "failure_phase": failure_phase,
        "failure_code": failure_code,
        "timing_ms": timings,
        "transport": transport,
        "safety": safety,
    }
    if target_state is not None:
        payload["target_state"] = target_state
    _write(output, payload)
    suffix = f" failure_phase={failure_phase} failure_code={failure_code}"
    if target_state is not None:
        suffix += f" target_state={target_state}"
    print("PHONE_TRANSPORT_PREFLIGHT classification=NOT_READY" + suffix)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--source-comment-created-at", required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    timings: dict[str, int] = {}
    safety = _base_safety()
    transport: dict[str, object] = {
        "artifact_upload_required": True,
        "runner_assignment_latency_ms": None,
    }

    try:
        created_at = _parse_created_at(args.source_comment_created_at)
        assignment_latency_ms = int((datetime.now(timezone.utc) - created_at).total_seconds() * 1000)
        if assignment_latency_ms < 0:
            raise PhoneTargetUnavailable("command provenance clock differs")
        transport["runner_assignment_latency_ms"] = assignment_latency_ms
    except PhoneTargetUnavailable:
        timings["total"] = int((time.monotonic() - started) * 1000)
        return _write_not_ready(
            output=args.output,
            controller_revision=args.controller_revision,
            timings=timings,
            transport=transport,
            safety=safety,
            failure_phase="command_provenance",
            failure_code="COMMAND_PROVENANCE_INVALID",
        )

    runner_temp = os.environ.get("RUNNER_TEMP", "")
    transport.update(
        collect_runner_transport_evidence(
            runner_temp=Path(runner_temp) if runner_temp else Path("."),
            assignment_latency_ms=assignment_latency_ms,
        )
    )

    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    if not serial:
        timings["total"] = int((time.monotonic() - started) * 1000)
        return _write_not_ready(
            output=args.output,
            controller_revision=args.controller_revision,
            timings=timings,
            transport=transport,
            safety=safety,
            failure_phase="target_binding",
            failure_code="TARGET_BINDING_UNAVAILABLE",
        )

    readiness = probe_registered_phone_transport(serial)
    timings.update(readiness.timing_ms)
    if not readiness.ready:
        timings["total"] = int((time.monotonic() - started) * 1000)
        assert readiness.failure_phase is not None and readiness.failure_code is not None
        if readiness.failure_phase in {"strict_get_state", "root_contract"}:
            safety["phone_access_performed"] = True
        return _write_not_ready(
            output=args.output,
            controller_revision=args.controller_revision,
            timings=timings,
            transport=transport,
            safety=safety,
            failure_phase=readiness.failure_phase,
            failure_code=readiness.failure_code,
            target_state=readiness.target_state,
        )

    safety["phone_access_performed"] = True
    timings["total"] = int((time.monotonic() - started) * 1000)
    classification = classify_preflight(phone_ready=True, transport=transport)
    _write(
        args.output,
        {
            "schema": "phone-transport-preflight.v2",
            "controller_revision": args.controller_revision,
            "classification": classification,
            "timing_ms": timings,
            "transport": transport,
            "safety": safety,
        },
    )
    print(f"PHONE_TRANSPORT_PREFLIGHT classification={classification}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
