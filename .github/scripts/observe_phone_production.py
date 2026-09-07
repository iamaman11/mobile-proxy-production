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

from android_target import (  # noqa: E402
    AndroidArtifactRefused,
    AndroidObservationUnavailable,
    observe,
)
from phone_runtime import PhoneRuntimeRefused  # noqa: E402
from phone_target import PhoneTargetUnavailable, observe_runtime  # noqa: E402
from release_handoff import parse_admitted_release  # noqa: E402
from release_resolver import ReleaseAdmissionError  # noqa: E402
from run_phone_release_deployment import (  # noqa: E402
    _materialize_verified_release_apk,
    _materialize_verified_release_runtime,
)

_EXPECTED_TARGET = "phone-production"
_EXPECTED_RELEASE = "v0.1.7"
_EXPECTED_RELEASE_ID = 383454833
_SHA = re.compile(r"[0-9a-f]{40}")


class PhoneProductionObservationRefused(RuntimeError):
    pass


def _write(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _base_report(*, controller_revision: str) -> dict[str, object]:
    return {
        "schema": "phone-production-observation.v1",
        "operation": "observe-phone-production",
        "controller_revision": controller_revision,
        "target": _EXPECTED_TARGET,
        "product_release": _EXPECTED_RELEASE,
        "release_id": _EXPECTED_RELEASE_ID,
        "read_only": True,
        "phone_mutation_performed": False,
        "apk_mutation_performed": False,
        "runtime_mutation_performed": False,
        "durable_mutation_intent_created": False,
        "vm_provider_access_performed": False,
        "raw_device_identifier_recorded": False,
    }


def _runtime_report(observation) -> dict[str, object]:
    if observation.current_target is None:
        current_relation = "absent"
    elif observation.current_target == observation.target_release:
        current_relation = "expected-release"
    else:
        current_relation = "other"
    return {
        "mode": observation.mode,
        "target_release_exists": observation.target_release_exists,
        "current_relation": current_relation,
        "exact_files_verified": observation.exact_files_verified,
        "required_file_count": observation.required_file_count,
        "desired": observation.desired,
    }


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

    report = _base_report(controller_revision=args.controller_revision)
    try:
        if args.target != _EXPECTED_TARGET or args.release != _EXPECTED_RELEASE:
            raise PhoneProductionObservationRefused("Stage 4 observer inputs differ from accepted phone baseline")
        if _SHA.fullmatch(args.controller_revision) is None or os.environ.get("GITHUB_SHA") != args.controller_revision:
            raise PhoneProductionObservationRefused("Stage 4 observer controller revision differs")
        try:
            admitted_raw = json.loads(args.admitted_release_json)
        except json.JSONDecodeError as exc:
            raise PhoneProductionObservationRefused("hosted immutable Release handoff is invalid") from exc
        admitted = parse_admitted_release(admitted_raw, tag=args.release, target=args.target)
        if admitted.identity.release_id != _EXPECTED_RELEASE_ID:
            raise PhoneProductionObservationRefused("immutable Product Release id differs from accepted Stage 4 baseline")

        serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
        binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
        if not serial or not binding_key:
            raise PhoneProductionObservationRefused("registered phone target binding is unavailable")

        materialization_facts: dict[str, object] = {}
        with tempfile.TemporaryDirectory(prefix="mobile-proxy-stage4-observe-") as td:
            root = Path(td)
            apk = root / admitted.identity.artifact_name
            runtime_archive = root / str(admitted.identity.phone_runtime_artifact_name)
            _materialize_verified_release_apk(admitted, apk, materialization_facts)
            materialized = _materialize_verified_release_runtime(
                admitted,
                archive=runtime_archive,
                work_root=root / "runtime",
                product_root=args.product_root,
                runtime_manifest_path=args.runtime_manifest,
                binding_key=binding_key,
                facts=materialization_facts,
            )
            apk_observation = observe(
                serial=serial,
                binding_key=binding_key,
                expected_version_name=str(admitted.android_version_name),
                expected_version_code=int(admitted.android_version_code or 0),
                expected_artifact_sha256=admitted.artifact_transport_sha256,
            )
            runtime_observation = observe_runtime(
                serial=serial,
                release_root=materialized.release_root,
                release_id=args.release,
                required_paths=materialized.required_live_release_paths,
            )

        apk_report = {
            "mode": apk_observation.mode,
            "target": apk_observation.target,
            "target_binding_id": apk_observation.target_binding_id,
            "package_name": apk_observation.package_name,
            "installed": apk_observation.installed,
            "version_name": apk_observation.version_name,
            "version_code": apk_observation.version_code,
            "artifact_sha256": apk_observation.artifact_sha256,
            "exact_artifact_verified": apk_observation.exact_artifact_verified,
            "desired": apk_observation.desired,
            "raw_device_identifier_recorded": False,
        }
        runtime_report = _runtime_report(runtime_observation)
        desired = bool(
            apk_observation.desired
            and apk_observation.exact_artifact_verified
            and runtime_observation.desired
            and runtime_observation.exact_files_verified
        )
        report.update(
            {
                "status": "observed",
                "state": "DESIRED" if desired else "DEGRADED",
                "desired": desired,
                "exact_release_identity": True,
                "product_source_sha": admitted.identity.source_sha,
                "apk": apk_report,
                "runtime": runtime_report,
            }
        )
        _write(args.output, report)
        print("STAGE4_PHONE_OBSERVATION_DESIRED" if desired else "STAGE4_PHONE_OBSERVATION_DEGRADED")
        return 0 if desired else 2
    except (
        PhoneProductionObservationRefused,
        ReleaseAdmissionError,
        AndroidArtifactRefused,
        AndroidObservationUnavailable,
        PhoneRuntimeRefused,
        PhoneTargetUnavailable,
    ) as exc:
        report.update(
            {
                "status": "unavailable",
                "state": "UNAVAILABLE",
                "desired": False,
                "error_class": type(exc).__name__,
            }
        )
        _write(args.output, report)
        print(f"STAGE4_PHONE_OBSERVATION_UNAVAILABLE: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
