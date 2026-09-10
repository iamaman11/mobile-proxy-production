#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
if str(CONTROLLER) not in sys.path:
    sys.path.insert(0, str(CONTROLLER))

import phone_target
from phone_resource_sanity import (
    PhoneResourceSanityUnavailable,
    observe_phone_resource_sanity,
)

_SHA = re.compile(r"[0-9a-f]{40}")
_SCHEMA = "stage4-phone-resource-sanity.v2"
_EXERCISE_REQUEST_COUNT = 12


def _atomic_write(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _base_safety(*, synthetic_load_performed: bool | None) -> dict[str, object]:
    return {
        "phone_access_performed": True,
        "exact_phone_release_state_observed": False,
        "phone_mutation_performed": False,
        "deployment_created": False,
        "deployment_intent_created": False,
        "provider_access_performed": False,
        "provider_mutation_performed": False,
        "synthetic_load_performed": synthetic_load_performed,
        "production_scale_load_performed": False,
        "external_network_traffic_performed": False,
        "performance_threshold_applied": False,
        "raw_status_json_recorded": False,
        "raw_config_recorded": False,
        "secret_values_recorded": False,
        "process_ids_recorded": False,
        "process_cmdlines_recorded": False,
    }


def _unknown_observation(
    *,
    exercise_requests_attempted: int | None,
    exercise_requests_succeeded: int | None,
) -> dict[str, object]:
    return {
        "evaluated": False,
        "queue_authority": "product-v1-status-current-job",
        "resource_headroom_measured": False,
        "exercise_request_count": _EXERCISE_REQUEST_COUNT,
        "exercise_requests_attempted": exercise_requests_attempted,
        "exercise_requests_succeeded": exercise_requests_succeeded,
        "bounded_local_exercise_completed": None,
        "synthetic_load_performed": None,
        "production_scale_load_performed": False,
        "external_network_traffic_performed": False,
        "performance_threshold_applied": False,
    }


def observe(
    *,
    target: str,
    release_tag: str,
    controller_revision: str,
    serial: str,
    admin_token: str,
    output: Path,
) -> dict[str, object]:
    if target != "phone-production" or release_tag != "v0.1.7":
        raise ValueError("resource sanity identity differs from fixed Stage 4 contract")
    if _SHA.fullmatch(controller_revision) is None:
        raise ValueError("controller revision must be a full SHA")
    if not serial or not admin_token:
        raise ValueError("registered phone binding is unavailable")

    failure_code: str | None = None
    phone_failure_phase: str | None = None
    observation: dict[str, object]
    synthetic_load_performed: bool | None
    try:
        measured = observe_phone_resource_sanity(serial, admin_token=admin_token)
        classification = measured.classification
        observation = measured.to_bounded_dict()
        synthetic_load_performed = (
            True if measured.bounded_local_exercise_completed else False
        )
    except PhoneResourceSanityUnavailable as exc:
        classification = "UNKNOWN"
        failure_code = exc.failure_code
        observation = _unknown_observation(
            exercise_requests_attempted=exc.exercise_requests_attempted,
            exercise_requests_succeeded=exc.exercise_requests_succeeded,
        )
        synthetic_load_performed = None
    except phone_target.PhoneTargetDiagnosticFailure as exc:
        classification = "UNKNOWN"
        failure_code = "PHONE_TARGET_UNAVAILABLE"
        phone_failure_phase = exc.phase.value
        observation = _unknown_observation(
            exercise_requests_attempted=None,
            exercise_requests_succeeded=None,
        )
        synthetic_load_performed = None
    except phone_target.PhoneTargetUnavailable:
        classification = "UNKNOWN"
        failure_code = "PHONE_TARGET_UNAVAILABLE"
        phone_failure_phase = "UNKNOWN"
        observation = _unknown_observation(
            exercise_requests_attempted=None,
            exercise_requests_succeeded=None,
        )
        synthetic_load_performed = None

    payload: dict[str, object] = {
        "schema": _SCHEMA,
        "classification": classification,
        "target": target,
        "release_tag": release_tag,
        "controller_revision": controller_revision,
        "failure_code": failure_code,
        "phone_failure_phase": phone_failure_phase,
        "observation": observation,
        "safety": _base_safety(
            synthetic_load_performed=synthetic_load_performed
        ),
    }
    _atomic_write(output, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        payload = observe(
            target=args.target,
            release_tag=args.release_tag,
            controller_revision=args.controller_revision,
            serial=os.environ.get("ANDROID_PRODUCTION_SERIAL", ""),
            admin_token=os.environ.get("MOBILE_PROXY_ADMIN_TOKEN", ""),
            output=args.output,
        )
    except ValueError as exc:
        print(f"STAGE4_PHONE_RESOURCE_SANITY_REFUSED reason={exc}", file=sys.stderr)
        return 2

    synthetic = payload["safety"].get("synthetic_load_performed")
    synthetic_text = "unknown" if synthetic is None else str(synthetic).lower()
    fields = [
        "STAGE4_PHONE_RESOURCE_SANITY_CLASSIFIED",
        f"classification={payload['classification']}",
        "phone_mutation=false",
        "provider_access=false",
        f"synthetic_load={synthetic_text}",
        "production_scale_load=false",
        "external_network_traffic=false",
        "performance_threshold=false",
    ]
    failure_code = payload.get("failure_code")
    if failure_code is not None:
        fields.append(f"failure_code={failure_code}")
    phone_failure_phase = payload.get("phone_failure_phase")
    if phone_failure_phase is not None:
        fields.append(f"phone_failure_phase={phone_failure_phase}")
    observation = payload.get("observation")
    if isinstance(observation, dict):
        attempted = observation.get("exercise_requests_attempted")
        succeeded = observation.get("exercise_requests_succeeded")
        if isinstance(attempted, int) and isinstance(succeeded, int):
            fields.extend(
                (
                    f"exercise_requests_attempted={attempted}",
                    f"exercise_requests_succeeded={succeeded}",
                )
            )
    print(" ".join(fields))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
