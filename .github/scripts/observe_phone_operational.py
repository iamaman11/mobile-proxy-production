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

import runtime_operational_observer as runtime_observer  # noqa: E402
from phone_target import (  # noqa: E402
    PhoneFailurePhase,
    PhoneTargetDiagnosticFailure,
    PhoneTargetUnavailable,
)
from runtime_operational_observer import (  # noqa: E402
    RuntimeOperationalObservationUnavailable,
    RuntimeOperationalOutputValidationFailure,
    observe_runtime_operational_health,
)

_SCHEMA = "stage4-phone-operational-observation.v1"
_TARGET = "phone-production"
_RELEASE = "v0.1.7"
_SHA = re.compile(r"[0-9a-f]{40}")
_ALLOWED_CLASSIFICATIONS = frozenset({"READY", "DEGRADED", "UNKNOWN"})
_ALLOWED_FAILURE_CODES = frozenset(
    {
        "RUNTIME_OPERATIONAL_OBSERVATION_UNAVAILABLE",
        "PHONE_TARGET_UNAVAILABLE",
    }
)
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
_ALLOWED_PHONE_FAILURE_PHASES = frozenset(
    {
        "ADB_TOOLING_UNAVAILABLE",
        "ADB_TRANSPORT_TIMEOUT",
        "REGISTERED_DEVICE_NOT_DEVICE",
        "ROOT_SHELL_SPAWN_FAILED",
        "ROOT_SCRIPT_TIMEOUT",
        "ROOT_SCRIPT_OUTPUT_TRUNCATED",
        "ROOT_SCRIPT_PROTOCOL_MISMATCH",
        "ROOT_SCRIPT_NONZERO",
        "UNKNOWN",
    }
)
_ALLOWED_OBSERVER_FAILURE_PHASES = frozenset(
    {
        "BINDING_VALIDATION",
        "OUTPUT_VALIDATION",
    }
)
_ALLOWED_OBSERVER_FAILURE_CODES = frozenset(
    {
        "OUTPUT_STREAM_ENCODING",
        "OUTPUT_PHASE_PROTOCOL",
        "OUTPUT_PAYLOAD_ENCODING",
        "OUTPUT_PAYLOAD_STRUCTURE",
        "OUTPUT_PROCESS_COUNT",
        "OUTPUT_HEALTH_AUTH",
        "OUTPUT_UNAUTHENTICATED_PAYLOAD_CONTRACT",
        "OUTPUT_SERVING",
        "OUTPUT_CELLULAR_ROUTE_READY",
        "OUTPUT_PROXY_BIND_READY",
        "OUTPUT_LOCAL_SERVING_READY",
        "OUTPUT_READINESS_STATE",
        "OUTPUT_PROXY_STATUS",
        "OUTPUT_TUNNEL_OWNER",
        "OUTPUT_DEGRADATION_REASON_CODE",
        "OUTPUT_REVERSE_TUNNEL_CONNECTED",
        "OUTPUT_REVERSE_TUNNEL_FRESHNESS",
        "OUTPUT_REVERSE_TUNNEL_ACTIVE_TRANSPORT",
        "OUTPUT_REVERSE_TUNNEL_FAILOVER_REASON",
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


def _bounded_terminal(payload: dict[str, object]) -> str:
    classification = payload.get("classification")
    if classification not in _ALLOWED_CLASSIFICATIONS:
        raise ValueError("phone operational terminal classification is invalid")
    fields = [f"classification={classification}"]
    failure_code = payload.get("failure_code")
    if failure_code is not None:
        if failure_code not in _ALLOWED_FAILURE_CODES:
            raise ValueError("phone operational failure code is not allowlisted")
        fields.append(f"failure_code={failure_code}")
    failure_phase = payload.get("failure_phase")
    if failure_phase is not None:
        if failure_phase not in _ALLOWED_FAILURE_PHASES:
            raise ValueError("phone operational failure phase is not allowlisted")
        if failure_code != "RUNTIME_OPERATIONAL_OBSERVATION_UNAVAILABLE":
            raise ValueError("runtime operational failure phase has incompatible failure code")
        fields.append(f"failure_phase={failure_phase}")
    phone_failure_phase = payload.get("phone_failure_phase")
    if phone_failure_phase is not None:
        if phone_failure_phase not in _ALLOWED_PHONE_FAILURE_PHASES:
            raise ValueError("phone target failure phase is not allowlisted")
        if failure_code != "PHONE_TARGET_UNAVAILABLE":
            raise ValueError("phone target failure phase has incompatible failure code")
        fields.append(f"phone_failure_phase={phone_failure_phase}")
    observer_failure_phase = payload.get("observer_failure_phase")
    if observer_failure_phase is not None:
        if observer_failure_phase not in _ALLOWED_OBSERVER_FAILURE_PHASES:
            raise ValueError("observer failure phase is not allowlisted")
        if failure_code != "PHONE_TARGET_UNAVAILABLE":
            raise ValueError("observer failure phase has incompatible failure code")
        if phone_failure_phase is not None:
            raise ValueError("phone target and observer failure phases are mutually exclusive")
        fields.append(f"observer_failure_phase={observer_failure_phase}")
    observer_failure_code = payload.get("observer_failure_code")
    if observer_failure_code is not None:
        if observer_failure_code not in _ALLOWED_OBSERVER_FAILURE_CODES:
            raise ValueError("observer failure code is not allowlisted")
        if failure_code != "PHONE_TARGET_UNAVAILABLE":
            raise ValueError("observer failure code has incompatible failure code")
        if observer_failure_phase != "OUTPUT_VALIDATION":
            raise ValueError("observer failure code has incompatible observer phase")
        if phone_failure_phase is not None:
            raise ValueError("phone target and observer failure metadata overlap")
        fields.append(f"observer_failure_code={observer_failure_code}")
    fields.extend(
        (
            "phone_mutation=false",
            "provider_access=false",
            "exact_release_state_observed=false",
        )
    )
    return "STAGE4_PHONE_OPERATIONAL_OBSERVATION " + " ".join(fields)


def _observer_failure_phase(exc: PhoneTargetUnavailable) -> str | None:
    message = str(exc)
    if message == runtime_observer._UNAVAILABLE:
        return "BINDING_VALIDATION"
    if message == runtime_observer._MALFORMED:
        return "OUTPUT_VALIDATION"
    return None


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
    except PhoneTargetDiagnosticFailure as exc:
        phone_failure_phase = exc.phase.value
        if exc.phase is PhoneFailurePhase.NONE or phone_failure_phase not in _ALLOWED_PHONE_FAILURE_PHASES:
            raise ValueError("phone target diagnostic failure phase is not allowlisted")
        base["failure_code"] = "PHONE_TARGET_UNAVAILABLE"
        base["phone_failure_phase"] = phone_failure_phase
        _write(output, base)
        return base
    except RuntimeOperationalOutputValidationFailure as exc:
        observer_failure_code = exc.code.value
        if observer_failure_code not in _ALLOWED_OBSERVER_FAILURE_CODES:
            raise ValueError("observer output validation code is not allowlisted")
        base["failure_code"] = "PHONE_TARGET_UNAVAILABLE"
        base["observer_failure_phase"] = "OUTPUT_VALIDATION"
        base["observer_failure_code"] = observer_failure_code
        _write(output, base)
        return base
    except PhoneTargetUnavailable as exc:
        base["failure_code"] = "PHONE_TARGET_UNAVAILABLE"
        observer_failure_phase = _observer_failure_phase(exc)
        if observer_failure_phase is not None:
            base["observer_failure_phase"] = observer_failure_phase
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
    base.pop("phone_failure_phase", None)
    base.pop("observer_failure_phase", None)
    base.pop("observer_failure_code", None)
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
    print(_bounded_terminal(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
