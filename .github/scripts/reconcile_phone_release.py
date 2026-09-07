#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from android_target import AndroidTargetStateUnavailable  # noqa: E402
from evidence_store import EvidenceError, IssueEvidenceStore  # noqa: E402
from github_projection import ProjectionError, PublicDeploymentProjection  # noqa: E402
from phone_release_reconcile import (  # noqa: E402
    PhoneReleaseReconcileError,
    reconcile_healthy_exact_projection,
)
from phone_release_state import (  # noqa: E402
    PhoneReleaseSnapshot,
    observe_exact_phone_release,
    prepare_verified_release_runtime,
)
from phone_runtime import PhoneRuntimeRefused  # noqa: E402
from release_resolver import ReleaseAdmissionError, resolve_release  # noqa: E402

_SHA = re.compile(r"[0-9a-f]{40}")
_STAGE4_RELEASE = "v0.1.7"
_MAX_REASON_CHARS = 240


def _failure_reason(exc: BaseException) -> str:
    value = str(exc).strip().replace("\n", " ")
    return (value or exc.__class__.__name__)[:_MAX_REASON_CHARS]


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    safe = {
        "schema": payload.get("schema"),
        "controller_revision": payload.get("controller_revision"),
        "target": payload.get("target"),
        "product_release": payload.get("product_release"),
        "classification": payload.get("classification"),
        "outcome": payload.get("outcome"),
        "phone_snapshot_classification": payload.get("phone_snapshot_classification"),
        "projection": payload.get("projection"),
        "safety": payload.get("safety"),
    }
    if payload.get("classification") == "UNKNOWN":
        safe["failure_class"] = payload.get("failure_class")
        safe["failure_code"] = payload.get("failure_code")
        safe["failure_reason"] = payload.get("failure_reason")
        safe["target_state"] = payload.get("target_state")
    print(
        "STAGE4_PHONE_RELEASE_RECONCILE_EVIDENCE "
        + json.dumps(safe, sort_keys=True, separators=(",", ":"))
    )


def _safety(
    *,
    phone_access_performed: bool,
    projection_write_outcome: str,
) -> dict[str, object]:
    if projection_write_outcome not in {"none", "confirmed", "unknown"}:
        raise ValueError("projection write outcome is invalid")
    return {
        "phone_access_performed": phone_access_performed,
        "phone_mutation_performed": False,
        "deployment_created": False,
        "deployment_intent_created": False,
        "private_terminal_written": False,
        "private_evidence_written": False,
        "provider_access_performed": False,
        "provider_mutation_performed": False,
        "projection_control_plane_side_effect": True,
        "projection_write_outcome": projection_write_outcome,
        "raw_device_identifier_recorded": False,
        "raw_current_target_path_recorded": False,
        "raw_config_recorded": False,
        "secret_values_recorded": False,
    }


def _base(
    *,
    controller_revision: str,
    admitted: object,
    timings_ms: dict[str, int],
) -> dict[str, object]:
    return {
        "schema": "stage4-phone-release-reconcile.v1",
        "controller_revision": controller_revision,
        "target": "phone-production",
        "product_release": admitted.identity.tag,
        "release_id": admitted.identity.release_id,
        "release_source_sha": admitted.identity.source_sha,
        "operation_class": "RECONCILE",
        "target_access_mode": "read_only",
        "timing_ms": timings_ms,
    }


def _bounded_snapshot(snapshot: PhoneReleaseSnapshot) -> dict[str, object]:
    if snapshot.classification == "UNKNOWN":
        return {"classification": "UNKNOWN", "desired": False}
    apk = snapshot.apk
    runtime = snapshot.runtime
    if apk is None or runtime is None:
        return {"classification": "UNKNOWN", "desired": False}
    return {
        "classification": snapshot.classification,
        "desired": snapshot.desired,
        "apk": {
            "package_name": getattr(apk, "package_name", None),
            "installed": getattr(apk, "installed", None),
            "version_name": getattr(apk, "version_name", None),
            "version_code": getattr(apk, "version_code", None),
            "exact_artifact_verified": getattr(apk, "exact_artifact_verified", None),
            "desired": getattr(apk, "desired", None),
        },
        "runtime": {
            "target_release_exists": bool(getattr(runtime, "target_release_exists", False)),
            "current_matches_expected_release": (
                getattr(runtime, "current_target", None) == getattr(runtime, "target_release", object())
            ),
            "exact_files_verified": bool(getattr(runtime, "exact_files_verified", False)),
            "required_file_count": int(getattr(runtime, "required_file_count", 0)),
            "desired": bool(getattr(runtime, "desired", False)),
        },
        "raw_device_identifier_recorded": False,
        "raw_release_paths_recorded": False,
        "artifact_digest_recorded": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--product-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.target != "phone-production" or args.release_tag != _STAGE4_RELEASE:
        raise SystemExit("Stage 4 reconcile is bound to phone-production@v0.1.7")
    if _SHA.fullmatch(args.controller_revision) is None or os.environ.get("GITHUB_SHA") != args.controller_revision:
        raise SystemExit("reconcile controller revision differs")

    started = time.monotonic()
    timings_ms: dict[str, int] = {}
    admitted = None
    phone_access_started = False
    projection_phase_started = False
    try:
        phase = time.monotonic()
        admitted = resolve_release(tag=args.release_tag, target=args.target)
        timings_ms["release_admission"] = int((time.monotonic() - phase) * 1000)

        serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
        binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
        if not serial or len(binding_key) < 32:
            raise PhoneRuntimeRefused("registered production phone binding is unavailable")

        facts: dict[str, object] = {}
        with tempfile.TemporaryDirectory(prefix="stage4-phone-reconcile-") as raw:
            root = Path(raw)
            phase = time.monotonic()
            materialized = prepare_verified_release_runtime(
                admitted,
                archive=root / "phone-runtime.tar.gz",
                work_root=root / "runtime",
                product_root=args.product_root,
                runtime_manifest_path=args.runtime_manifest,
                binding_key=binding_key,
                facts=facts,
            )
            timings_ms["runtime_materialization"] = int((time.monotonic() - phase) * 1000)

            phone_access_started = True
            phase = time.monotonic()
            snapshot = observe_exact_phone_release(
                serial=serial,
                binding_key=binding_key,
                admitted=admitted,
                materialized=materialized,
            )
            timings_ms["phone_observation"] = int((time.monotonic() - phase) * 1000)

        timings_ms["total_before_projection"] = int((time.monotonic() - started) * 1000)
        if snapshot.classification == "UNKNOWN":
            failure = snapshot.failure or PhoneRuntimeRefused("exact phone Release observation is unavailable")
            payload = {
                **_base(
                    controller_revision=args.controller_revision,
                    admitted=admitted,
                    timings_ms=timings_ms,
                ),
                "classification": "UNKNOWN",
                "outcome": "NO_PROJECTION_WRITE",
                "phone_snapshot_classification": "UNKNOWN",
                "phone_snapshot": _bounded_snapshot(snapshot),
                "failure_class": failure.__class__.__name__,
                "failure_reason": _failure_reason(failure),
                "projection": {"evaluated": False, "write_performed": False},
                "safety": _safety(
                    phone_access_performed=True,
                    projection_write_outcome="none",
                ),
            }
            if isinstance(failure, AndroidTargetStateUnavailable):
                payload["failure_code"] = "ANDROID_TARGET_STATE_NOT_DEVICE"
                payload["target_state"] = failure.state
            _write(args.output, payload)
            return 2

        if snapshot.classification != "HEALTHY_EXACT":
            payload = {
                **_base(
                    controller_revision=args.controller_revision,
                    admitted=admitted,
                    timings_ms=timings_ms,
                ),
                "classification": "DEGRADED",
                "outcome": "NO_PROJECTION_WRITE",
                "phone_snapshot_classification": snapshot.classification,
                "phone_snapshot": _bounded_snapshot(snapshot),
                "projection": {"evaluated": False, "write_performed": False},
                "safety": _safety(
                    phone_access_performed=True,
                    projection_write_outcome="none",
                ),
            }
            _write(args.output, payload)
            return 2

        projection_phase_started = True
        phase = time.monotonic()
        evidence = IssueEvidenceStore(os.environ.get("GITHUB_TOKEN", ""))
        projection = PublicDeploymentProjection(os.environ.get("PUBLIC_DEPLOYMENTS_TOKEN", ""))
        result = reconcile_healthy_exact_projection(
            evidence=evidence,
            projection=projection,
            admitted=admitted,
            environment=args.target,
        )
        timings_ms["projection_reconcile"] = int((time.monotonic() - phase) * 1000)
        timings_ms["total"] = int((time.monotonic() - started) * 1000)
        payload = {
            **_base(
                controller_revision=args.controller_revision,
                admitted=admitted,
                timings_ms=timings_ms,
            ),
            "classification": "HEALTHY_EXACT",
            "outcome": "REPAIRED" if result.projection_updated else "NOOP",
            "phone_snapshot_classification": snapshot.classification,
            "phone_snapshot": _bounded_snapshot(snapshot),
            "projection": {
                "evaluated": True,
                "deployment_id": result.deployment_id,
                "durable_admission_count": result.durable_admission_count,
                "previous_state": result.previous_state,
                "final_state": result.final_state,
                "write_performed": result.projection_updated,
                "readback_verified": True,
                "new_deployment_created": False,
                "historical_terminal_rewritten": False,
            },
            "safety": _safety(
                phone_access_performed=True,
                projection_write_outcome="confirmed" if result.projection_updated else "none",
            ),
        }
        _write(args.output, payload)
        return 0

    except (
        ReleaseAdmissionError,
        PhoneRuntimeRefused,
        EvidenceError,
        ProjectionError,
        PhoneReleaseReconcileError,
    ) as exc:
        timings_ms["total"] = int((time.monotonic() - started) * 1000)
        if admitted is None:
            class Identity:
                tag = args.release_tag
                release_id = None
                source_sha = None
            class Admitted:
                identity = Identity()
            admitted_for_output = Admitted()
        else:
            admitted_for_output = admitted
        payload = {
            **_base(
                controller_revision=args.controller_revision,
                admitted=admitted_for_output,
                timings_ms=timings_ms,
            ),
            "classification": "UNKNOWN",
            "outcome": "RECONCILE_UNKNOWN",
            "phone_snapshot_classification": (
                "HEALTHY_EXACT" if projection_phase_started else "UNKNOWN"
            ),
            "failure_class": exc.__class__.__name__,
            "failure_reason": _failure_reason(exc),
            "projection": {
                "evaluated": projection_phase_started,
                "write_performed": None if projection_phase_started else False,
                "write_outcome": "unknown" if projection_phase_started else "none",
                "new_deployment_created": False,
                "historical_terminal_rewritten": False,
            },
            "safety": _safety(
                phone_access_performed=phone_access_started,
                projection_write_outcome="unknown" if projection_phase_started else "none",
            ),
        }
        _write(args.output, payload)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
