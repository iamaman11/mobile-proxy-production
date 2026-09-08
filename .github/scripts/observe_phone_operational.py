#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
CONTROLLER = SCRIPTS.parent / "controller"
sys.path.insert(0, str(CONTROLLER))

from phone_target import PhoneTargetUnavailable  # noqa: E402
from runtime_operational_observer import (  # noqa: E402
    RuntimeOperationalObservationUnavailable,
    observe_runtime_operational_health,
)

_SCHEMA = "stage4-phone-operational-observation.v1"
_TARGET = "phone-production"
_RELEASE = "v0.1.7"
_SHA = re.compile(r"[0-9a-f]{40}")
_ALLOWED_CLASSIFICATIONS = frozenset({"READY", "DEGRADED", "UNKNOWN"})
_ALLOWED_FAILURE_PHASES = frozenset(
    {
        "busybox_selected",
        "process_count_start",
        "process_count_done",
        "health_transport_start",
        "health_transport_done",
        "health_parse_done",
    }
)


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _safety(*, phone_access_performed: bool) -> dict[str, object]:
    return {
        "phone_access_performed": phone_access_performed,
        "phone_mutation_performed": False,
        "deployment_created": False,
        "deployment_intent_created": False,
        "provider_access_performed": False,
        "provider_mutation_performed": False,
        "exact_phone_release_state_observed": False,
        "localhost_only": True,
        "raw_health_json_recorded": False,
        "raw_config_recorded": False,
        "secret_values_recorded": False,
        "process_ids_recorded": False,
        "process_cmdlines_recorded": False,
    }


def _unknown_observation() -> dict[str, object]:
    return {
        "evaluated": False,
        "desired": False,
        "mode": "read_only",
        "localhost_only": True,
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
    if target != _TARGET or release_tag != _RELEASE:
        raise ValueError("phone operational identity differs from Stage 4 contract")
    if _SHA.fullmatch(controller_revision) is None:
        raise ValueError("controller revision is not an exact SHA")
    if not serial or not admin_token:
        raise ValueError("required phone operational binding is unavailable")

    base: dict[str, object] = {
        "schema": _SCHEMA,
        "target": target,
        "release_tag": release_tag,
        "controller_revision": controller_revision,
        "classification": "UNKNOWN",
        "observation": _unknown_observation(),
        "safety": _safety(phone_access_performed=True),
    }

    try:
        operational = observe_runtime_operational_health(serial, admin_token=admin_token)
    except RuntimeOperationalObservationUnavailable as exc:
        base["failure_code"] = "RUNTIME_OPERATIONAL_OBSERVATION_UNAVAILABLE"
        if exc.last_phase is not None:
            if exc.last_phase not in _ALLOWED_FAILURE_PHASES:
                raise ValueError("operational failure phase is not allowlisted")
            base["failure_phase"] = exc.last_phase
        _write(output, base)
        return base
    except PhoneTargetUnavailable:
        base["failure_code"] = "PHONE_TARGET_UNAVAILABLE"
        _write(output, base)
        return base

    bounded = operational.to_bounded_dict()
    classification = "READY" if operational.desired else "DEGRADED"
    if classification not in _ALLOWED_CLASSIFICATIONS:
        raise AssertionError("phone operational classification is invalid")
    base["classification"] = classification
    base["observation"] = bounded
    base.pop("failure_code", None)
    base.pop("failure_phase", None)
    _write(output, base)
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    admin_token = os.environ.get("MOBILE_PROXY_ADMIN_TOKEN", "")
    payload = observe(
        target=args.target,
        release_tag=args.release_tag,
        controller_revision=args.controller_revision,
        serial=serial,
        admin_token=admin_token,
        output=args.output,
    )
    print(
        "STAGE4_PHONE_OPERATIONAL_OBSERVATION "
        f"classification={payload['classification']} "
        "phone_mutation=false provider_access=false exact_release_state_observed=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
