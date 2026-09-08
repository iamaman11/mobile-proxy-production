#!/usr/bin/env python3
"""Finalize bounded runner-transport evidence after real artifact round-trip."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBSERVER_PATH = ROOT / "scripts" / "observe_runner_transport.py"


def _load_observer():
    spec = importlib.util.spec_from_file_location("observe_runner_transport_finalize_dependency", OBSERVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("runner transport observer module unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _duration_ms(started: datetime | None, finished: datetime | None) -> int | None:
    if started is None or finished is None or finished < started:
        return None
    return int((finished - started).total_seconds() * 1000)


def _step(job: dict[str, object], name: str) -> dict[str, object] | None:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return None
    matches = [item for item in steps if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        return None
    return matches[0]


def _observer_job(jobs_value: object) -> dict[str, object]:
    if not isinstance(jobs_value, dict):
        raise ValueError("runner transport jobs metadata is not an object")
    jobs = jobs_value.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("runner transport jobs metadata lacks jobs")
    matches: list[dict[str, object]] = []
    for raw in jobs:
        if not isinstance(raw, dict):
            continue
        if _step(raw, "Run bounded runner transport observer") is not None:
            matches.append(raw)
    if len(matches) != 1:
        raise ValueError("runner transport observer job lineage is ambiguous")
    return matches[0]


def _timing_from_jobs(
    *,
    job: dict[str, object],
    source_comment_created_at: str,
    local_observer_ms: object,
) -> tuple[dict[str, object], list[str]]:
    limitations: list[str] = ["BROKER_ASSIGNMENT_TIMESTAMP_UNAVAILABLE"]
    command_at = _parse_utc(source_comment_created_at)
    job_started = _parse_utc(job.get("started_at"))
    checkout = _step(job, "Check out exact Controller revision")
    observer_step = _step(job, "Run bounded runner transport observer")

    checkout_started = _parse_utc(checkout.get("started_at")) if checkout else None
    checkout_finished = _parse_utc(checkout.get("completed_at")) if checkout else None
    observer_started = _parse_utc(observer_step.get("started_at")) if observer_step else None
    observer_finished = _parse_utc(observer_step.get("completed_at")) if observer_step else None

    command_to_job = _duration_ms(command_at, job_started)
    setup = _duration_ms(job_started, checkout_started)
    checkout_ms = _duration_ms(checkout_started, checkout_finished)
    observer_job_ms = _duration_ms(observer_started, observer_finished)

    if command_to_job is None:
        limitations.append("COMMAND_TO_SELF_HOSTED_JOB_START_TIMING_UNAVAILABLE")
    if setup is None:
        limitations.append("JOB_SETUP_ACTION_PREPARATION_TIMING_UNAVAILABLE")
    if checkout_ms is None:
        limitations.append("CHECKOUT_TIMING_UNAVAILABLE")
    if observer_job_ms is None:
        limitations.append("OBSERVER_STEP_METADATA_TIMING_UNAVAILABLE")

    local_value = local_observer_ms if type(local_observer_ms) is int and local_observer_ms >= 0 else None
    if local_value is None:
        raise ValueError("runner transport local observer timing unavailable")

    return {
        "command_to_self_hosted_job_start": {
            "value_ms": command_to_job,
            "source": "ISSUE_COMMENT_CREATED_AT_TO_ACTIONS_JOB_STARTED_AT",
            "start_utc": _format_utc(command_at),
            "finish_utc": _format_utc(job_started),
        },
        "job_setup_action_preparation": {
            "value_ms": setup,
            "source": "ACTIONS_JOB_STARTED_AT_TO_CHECKOUT_STEP_STARTED_AT",
            "start_utc": _format_utc(job_started),
            "finish_utc": _format_utc(checkout_started),
        },
        "checkout": {
            "value_ms": checkout_ms,
            "source": "ACTIONS_CHECKOUT_STEP_TIMESTAMPS",
            "start_utc": _format_utc(checkout_started),
            "finish_utc": _format_utc(checkout_finished),
        },
        "observer": {
            "value_ms": local_value,
            "source": "LOCAL_MONOTONIC",
            "step_metadata_value_ms": observer_job_ms,
        },
        "broker_assignment": {
            "value_ms": None,
            "source": "UNAVAILABLE",
            "start_utc": None,
            "finish_utc": None,
        },
    }, limitations


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def finalize(
    *,
    core_path: Path,
    jobs_path: Path,
    output_path: Path,
    expected_core_sha256: str,
    source_comment_created_at: str,
    artifact_upload_retry_observed: bool,
) -> dict[str, object]:
    if len(expected_core_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in expected_core_sha256):
        raise ValueError("runner transport expected core digest differs")
    if _sha256(core_path) != expected_core_sha256:
        raise ValueError("runner transport downloaded core digest differs")

    value = json.loads(core_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("runner transport core artifact is not an object")
    observer = _load_observer()
    observer.validate_observation(value)

    jobs_value = json.loads(jobs_path.read_text(encoding="utf-8"))
    job = _observer_job(jobs_value)
    timing = value.get("timing")
    if not isinstance(timing, dict):
        raise ValueError("runner transport core timing differs")
    local_observer = timing.get("observer")
    if not isinstance(local_observer, dict):
        raise ValueError("runner transport core observer timing differs")
    finalized_timing, timing_limitations = _timing_from_jobs(
        job=job,
        source_comment_created_at=source_comment_created_at,
        local_observer_ms=local_observer.get("value_ms"),
    )
    value["timing"] = finalized_timing

    transport = value.get("transport")
    if not isinstance(transport, dict):
        raise ValueError("runner transport core transport differs")
    transport["artifact_round_trip"] = {
        "core_upload_succeeded": True,
        "upload_retry_observed": artifact_upload_retry_observed,
        "hosted_download_succeeded": True,
        "core_digest_verified": True,
    }
    if artifact_upload_retry_observed:
        failure_classes = transport.get("failure_classes")
        if not isinstance(failure_classes, list):
            raise ValueError("runner transport failure classes differ")
        if "ARTIFACT_UPLOAD_RETRY" not in failure_classes:
            failure_classes.append("ARTIFACT_UPLOAD_RETRY")
            failure_classes.sort()
        if value.get("classification") == "HEALTHY":
            value["classification"] = "TRANSPORT_DEGRADED"

    limitations = value.get("evidence_limitations")
    if not isinstance(limitations, list):
        raise ValueError("runner transport evidence limitations differ")
    retained = [item for item in limitations if item != "BROKER_ASSIGNMENT_TIMESTAMP_UNAVAILABLE"]
    value["evidence_limitations"] = sorted(set(retained + timing_limitations))

    observer.validate_observation(value)
    output_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-core-sha256", required=True)
    parser.add_argument("--source-comment-created-at", required=True)
    parser.add_argument("--artifact-upload-retry-observed", choices=("true", "false"), required=True)
    args = parser.parse_args(argv)

    value = finalize(
        core_path=args.core,
        jobs_path=args.jobs,
        output_path=args.output,
        expected_core_sha256=args.expected_core_sha256,
        source_comment_created_at=args.source_comment_created_at,
        artifact_upload_retry_observed=args.artifact_upload_retry_observed == "true",
    )
    print(
        "RUNNER_TRANSPORT_OBSERVATION_FINAL "
        f"classification={value['classification']} failure_domain={value['failure_domain']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
