#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
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

_MAX_REASON_CHARS = 160


def _write(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".operational.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _bounded_reason(exc: BaseException) -> str:
    value = str(exc).strip().replace("\n", " ") or exc.__class__.__name__
    return value[:_MAX_REASON_CHARS]


def _operational_safety(*, performed: bool) -> dict[str, object]:
    return {
        "operational_probe_performed": performed,
        "operational_probe_localhost_only": True,
        "raw_health_json_recorded": False,
        "process_ids_recorded": False,
        "process_cmdlines_recorded": False,
        "operational_secret_values_recorded": False,
    }


def _not_evaluated(reason: str) -> dict[str, object]:
    return {
        "evaluated": False,
        "desired": False,
        "reason": reason,
        "localhost_only": True,
        "raw_health_json_recorded": False,
        "raw_config_recorded": False,
        "secret_values_recorded": False,
        "process_ids_recorded": False,
        "process_cmdlines_recorded": False,
        "provider_access_performed": False,
        "phone_mutation_performed": False,
    }


def _runtime_is_exact(runtime: dict[str, object]) -> bool:
    return (
        runtime.get("current_state") == "expected_release"
        and runtime.get("current_matches_expected_release") is True
        and runtime.get("exact_files_verified") is True
    )


def enrich(path: Path, *, serial: str, admin_token: str) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "stage4-phone-release-observation.v1":
        raise SystemExit("Stage 4 observation schema differs before operational enrichment")
    if payload.get("classification") not in {"HEALTHY_EXACT", "DEGRADED"}:
        return 0
    observation = payload.get("observation")
    if not isinstance(observation, dict):
        raise SystemExit("Stage 4 observation body is absent before operational enrichment")
    runtime = observation.get("runtime")
    if not isinstance(runtime, dict):
        raise SystemExit("Stage 4 runtime observation is absent before operational enrichment")
    safety = payload.get("safety")
    if not isinstance(safety, dict):
        raise SystemExit("Stage 4 observation safety envelope is absent before operational enrichment")

    if not _runtime_is_exact(runtime):
        observation["operational"] = _not_evaluated("runtime_not_exact")
        observation["desired"] = False
        payload["classification"] = "DEGRADED"
        safety.update(_operational_safety(performed=False))
        _write(path, payload)
        print("STAGE4_RUNTIME_OPERATIONAL_OBSERVED evaluated=false reason=runtime_not_exact")
        return 0

    payload.pop("failure_phase", None)
    try:
        operational = observe_runtime_operational_health(
            serial,
            admin_token=admin_token,
        )
        bounded = operational.to_bounded_dict()
        observation["operational"] = bounded
        observation["desired"] = observation.get("desired") is True and operational.desired
        payload["classification"] = (
            "HEALTHY_EXACT"
            if payload.get("classification") == "HEALTHY_EXACT" and operational.desired
            else "DEGRADED"
        )
        safety.update(_operational_safety(performed=True))
        _write(path, payload)
        print(
            "STAGE4_RUNTIME_OPERATIONAL_OBSERVED "
            f"desired={str(operational.desired).lower()} "
            f"health_api_authenticated={str(operational.health_api_authenticated).lower()} "
            f"readiness={operational.readiness_state} "
            f"serving={str(operational.serving).lower()} "
            f"local_serving_ready={str(operational.local_serving_ready).lower()} "
            f"tunnel_owner_matches_expected={str(operational.tunnel_owner_matches_expected).lower()}"
        )
        return 0
    except PhoneTargetUnavailable as exc:
        payload["classification"] = "UNKNOWN"
        payload["failure_class"] = "RuntimeOperationalObservationUnavailable"
        payload["failure_code"] = "RUNTIME_OPERATIONAL_OBSERVATION_UNAVAILABLE"
        payload["failure_reason"] = _bounded_reason(exc)
        if (
            isinstance(exc, RuntimeOperationalObservationUnavailable)
            and exc.last_phase is not None
        ):
            payload["failure_phase"] = exc.last_phase
        observation["operational"] = _not_evaluated("observation_unavailable")
        observation["desired"] = False
        safety.update(_operational_safety(performed=True))
        _write(path, payload)
        phase = payload.get("failure_phase", "unknown")
        print(
            "STAGE4_RUNTIME_OPERATIONAL_UNKNOWN "
            f"failure_code={payload['failure_code']} phase={phase} "
            f"reason={payload['failure_reason']}",
            file=sys.stderr,
        )
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    admin_token = os.environ.get("MOBILE_PROXY_ADMIN_TOKEN", "")
    if not serial:
        raise SystemExit("registered production phone binding is unavailable")
    if not admin_token:
        raise SystemExit("runtime operational authorization is unavailable")
    if not args.evidence.is_file():
        raise SystemExit("Stage 4 observation evidence is unavailable before operational enrichment")
    return enrich(args.evidence, serial=serial, admin_token=admin_token)


if __name__ == "__main__":
    raise SystemExit(main())
