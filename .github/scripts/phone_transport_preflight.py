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

from phone_target import PhoneTargetUnavailable, _probe_root_capability, _require_device


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


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--source-comment-created-at", required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    timings: dict[str, int] = {}
    safety = {
        "phone_access_performed": False,
        "phone_mutation_performed": False,
        "provider_access_performed": False,
        "provider_mutation_performed": False,
        "deployment_created": False,
        "deployment_intent_created": False,
        "raw_device_identifier_recorded": False,
        "secret_values_recorded": False,
    }
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
        serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
        if not serial:
            raise PhoneTargetUnavailable("registered production phone binding is unavailable")
        phase = time.monotonic()
        _require_device(serial)
        timings["registered_device_state"] = int((time.monotonic() - phase) * 1000)
        safety["phone_access_performed"] = True
        phase = time.monotonic()
        _probe_root_capability(serial)
        timings["root_capability"] = int((time.monotonic() - phase) * 1000)
        timings["total"] = int((time.monotonic() - started) * 1000)
        _write(args.output, {
            "schema": "phone-transport-preflight.v1",
            "controller_revision": args.controller_revision,
            "classification": "READY",
            "timing_ms": timings,
            "transport": transport,
            "safety": safety,
        })
        print("PHONE_TRANSPORT_PREFLIGHT classification=READY")
        return 0
    except PhoneTargetUnavailable as exc:
        timings["total"] = int((time.monotonic() - started) * 1000)
        _write(args.output, {
            "schema": "phone-transport-preflight.v1",
            "controller_revision": args.controller_revision,
            "classification": "NOT_READY",
            "failure_class": exc.__class__.__name__,
            "timing_ms": timings,
            "transport": transport,
            "safety": safety,
        })
        print("PHONE_TRANSPORT_PREFLIGHT classification=NOT_READY")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
