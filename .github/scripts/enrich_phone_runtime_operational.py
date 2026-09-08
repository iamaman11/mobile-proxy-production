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
from runtime_operational_observer import observe_runtime_operational_health  # noqa: E402

_MAX_REASON_CHARS = 160


def _write(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".operational.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _bounded_reason(exc: BaseException) -> str:
    value = str(exc).strip().replace("\n", " ") or exc.__class__.__name__
    return value[:_MAX_REASON_CHARS]


def _not_evaluated() -> dict[str, object]:
    return {
        "evaluated": False,
        "desired": False,
        "reason": "runtime_not_exact",
        "localhost_only": True,
        "raw_health_json_recorded": False,
        "raw_config_recorded": False,
        "secret_values_recorded": False,
        "process_ids_recorded": False,
        "process_cmdlines_recorded": False,
        "provider_access_performed": False,
        "phone_mutation_performed": False,
    }


def enrich(path: Path, *, serial: str) -> int:
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

    exact_runtime = (
        runtime.get("current_state") == "expected_release"
        and runtime.get("current_matches_expected_release") is True
        and runtime.get("exact_files_verified") is True
    )
    if not exact_runtime:
        observation["operational"] = _not_evaluated()
        observation["desired"] = False
        payload["classification"] = "DEGRADED"
        _write(path, payload)
        print("STAGE4_RUNTIME_OPERATIONAL_OBSERVED evaluated=false reason=runtime_not_exact")
        return 0

    try:
        operational = observe_runtime_operational_health(serial)
        bounded = operational.to_bounded_dict()
        observation["operational"] = bounded
        observation["desired"] = observation.get("desired") is True and operational.desired
        if not operational.desired:
            payload["classification"] = "DEGRADED"
        safety = payload.get("safety")
        if not isinstance(safety, dict):
            raise PhoneTargetUnavailable("runtime operational safety envelope is unavailable")
        safety.update(
            {
                "operational_probe_localhost_only": True,
                "raw_health_json_recorded": False,
                "process_ids_recorded": False,
                "process_cmdlines_recorded": False,
            }
        )
        _write(path, payload)
        print(
            "STAGE4_RUNTIME_OPERATIONAL_OBSERVED "
            f"desired={str(operational.desired).lower()} "
            f"health_api_authenticated={str(operational.health_api_authenticated).lower()} "
            f"readiness={operational.readiness_state} "
            f"local_serving_ready={str(operational.local_serving_ready).lower()} "
            f"tunnel_owner={operational.tunnel_owner}"
        )
        return 0
    except PhoneTargetUnavailable as exc:
        payload["classification"] = "UNKNOWN"
        payload["failure_class"] = "RuntimeOperationalObservationUnavailable"
        payload["failure_code"] = "RUNTIME_OPERATIONAL_OBSERVATION_UNAVAILABLE"
        payload["failure_reason"] = _bounded_reason(exc)
        observation["operational"] = {
            "evaluated": False,
            "desired": False,
            "reason": "observation_unavailable",
            "localhost_only": True,
            "raw_health_json_recorded": False,
            "raw_config_recorded": False,
            "secret_values_recorded": False,
            "process_ids_recorded": False,
            "process_cmdlines_recorded": False,
            "provider_access_performed": False,
            "phone_mutation_performed": False,
        }
        observation["desired"] = False
        _write(path, payload)
        print(
            "STAGE4_RUNTIME_OPERATIONAL_UNKNOWN "
            f"failure_code={payload['failure_code']} reason={payload['failure_reason']}",
            file=sys.stderr,
        )
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    if not serial:
        raise SystemExit("registered production phone binding is unavailable")
    if not args.evidence.is_file():
        raise SystemExit("Stage 4 observation evidence is unavailable before operational enrichment")
    return enrich(args.evidence, serial=serial)


if __name__ == "__main__":
    raise SystemExit(main())
