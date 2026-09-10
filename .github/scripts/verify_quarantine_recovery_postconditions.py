#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from phone_release_state import observe_exact_phone_release, prepare_verified_release_runtime  # noqa: E402
from phone_runtime import PhoneRuntimeRefused  # noqa: E402
from phone_target import PhoneTargetUnavailable  # noqa: E402
from release_handoff import parse_admitted_release  # noqa: E402
from release_resolver import ReleaseAdmissionError  # noqa: E402
from runtime_operational_observer import (  # noqa: E402
    RuntimeOperationalObservationUnavailable,
    RuntimeOperationalOutputValidationFailure,
    observe_runtime_operational_health,
)

_SCHEMA = "production-quarantine-recovery-independent-postcondition.v1"
_TARGET = "phone-production"
_SHA = re.compile(r"[0-9a-f]{40}")


def _bounded_apk(apk: object | None) -> dict[str, object] | None:
    if apk is None:
        return None
    raw = apk.to_dict()
    return {
        "installed": raw.get("installed"),
        "version_name": raw.get("version_name"),
        "version_code": raw.get("version_code"),
        "exact_artifact_verified": raw.get("exact_artifact_verified"),
        "desired": raw.get("desired"),
        "mode": raw.get("mode"),
        "raw_device_identifier_recorded": False,
        "target_binding_id_recorded": False,
        "artifact_digest_recorded": False,
    }


def _bounded_runtime(runtime: object | None) -> dict[str, object] | None:
    if runtime is None:
        return None
    target = getattr(runtime, "target_release", None)
    current = getattr(runtime, "current_target", None)
    if current is None:
        current_state = "absent"
    elif current == target:
        current_state = "expected_release"
    elif isinstance(current, str) and current.startswith("/data/adb/mobile-proxy-node/releases/"):
        current_state = "other_managed_release"
    else:
        current_state = "invalid_or_unmanaged"
    return {
        "target_release_exists": bool(getattr(runtime, "target_release_exists", False)),
        "current_state": current_state,
        "current_matches_expected_release": current == target,
        "exact_files_verified": bool(getattr(runtime, "exact_files_verified", False)),
        "required_file_count": int(getattr(runtime, "required_file_count", 0)),
        "desired": bool(getattr(runtime, "desired", False)),
        "mode": str(getattr(runtime, "mode", "read_only")),
        "raw_current_target_path_recorded": False,
        "raw_runtime_release_paths_recorded": False,
        "expected_file_digests_recorded": False,
        "observed_file_digests_recorded": False,
    }


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--admitted-release-json", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--product-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.target != _TARGET:
        raise SystemExit("independent recovery postcondition target differs")
    if _SHA.fullmatch(args.controller_revision) is None or os.environ.get("GITHUB_SHA") != args.controller_revision:
        raise SystemExit("independent recovery postcondition controller revision differs")

    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
    admin_token = os.environ.get("MOBILE_PROXY_ADMIN_TOKEN", "")
    if not serial or len(binding_key) < 32 or not admin_token:
        raise SystemExit("independent recovery postcondition binding is unavailable")

    payload: dict[str, object] = {
        "schema": _SCHEMA,
        "controller_revision": args.controller_revision,
        "target": args.target,
        "product_release": args.release,
        "classification": "UNKNOWN",
        "exact_release": None,
        "operational": None,
        "safety": {
            "phone_access_performed": True,
            "phone_mutation_performed": False,
            "deployment_created": False,
            "deployment_intent_created": False,
            "recovery_intent_created": False,
            "provider_access_performed": False,
            "provider_mutation_performed": False,
            "vm_access_performed": False,
            "vm_mutation_performed": False,
            "raw_device_identifier_recorded": False,
            "raw_current_target_path_recorded": False,
            "raw_config_recorded": False,
            "secret_values_recorded": False,
        },
    }

    try:
        admitted = parse_admitted_release(json.loads(args.admitted_release_json), tag=args.release, target=args.target)
        with tempfile.TemporaryDirectory(prefix="mobile-proxy-recovery-postcondition-") as td:
            root = Path(td)
            facts: dict[str, object] = {}
            materialized = prepare_verified_release_runtime(
                admitted,
                archive=root / "phone-runtime.tar.gz",
                work_root=root / "runtime",
                product_root=args.product_root,
                runtime_manifest_path=args.runtime_manifest,
                binding_key=binding_key,
                facts=facts,
            )
            snapshot = observe_exact_phone_release(
                serial=serial,
                binding_key=binding_key,
                admitted=admitted,
                materialized=materialized,
            )
            if snapshot.classification == "UNKNOWN":
                raise snapshot.failure or PhoneTargetUnavailable("exact phone Release postcondition is unavailable")
            operational = observe_runtime_operational_health(serial, admin_token=admin_token)
    except (
        json.JSONDecodeError,
        ReleaseAdmissionError,
        PhoneRuntimeRefused,
        PhoneTargetUnavailable,
        RuntimeOperationalObservationUnavailable,
        RuntimeOperationalOutputValidationFailure,
    ):
        _write(args.output, payload)
        print("QUARANTINE_RECOVERY_POSTCONDITION_UNKNOWN")
        return 2

    exact_desired = bool(snapshot.desired)
    operational_desired = bool(operational.desired)
    payload["exact_release"] = {
        "classification": snapshot.classification,
        "desired": exact_desired,
        "apk": _bounded_apk(snapshot.apk),
        "runtime": _bounded_runtime(snapshot.runtime),
    }
    payload["operational"] = {
        "desired": operational_desired,
        "mode": "read_only",
        "localhost_only": True,
        "raw_health_json_recorded": False,
        "raw_config_recorded": False,
        "secret_values_recorded": False,
        "process_ids_recorded": False,
        "process_cmdlines_recorded": False,
    }
    payload["classification"] = "ACCEPTED" if exact_desired and operational_desired else "DEGRADED"
    _write(args.output, payload)
    print(f"QUARANTINE_RECOVERY_POSTCONDITION_{payload['classification']}")
    return 0 if payload["classification"] == "ACCEPTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
