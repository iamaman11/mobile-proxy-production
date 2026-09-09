#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
CONTROLLER = SCRIPTS.parent / "controller"
sys.path.insert(0, str(CONTROLLER))

from phone_release_state import (  # noqa: E402
    observe_exact_phone_release,
    prepare_verified_release_runtime,
)
from phone_target import PhoneTargetUnavailable, observe_runtime  # noqa: E402
from release_resolver import ReleaseAdmissionError, resolve_release  # noqa: E402

_STAGE4_TARGET = "phone-production"
_STAGE4_RELEASE = "v0.1.7"
_SHADOW_EXPECTED_RELEASE = "v9999.9999.9999"
_MANAGED_RELEASE_PREFIX = "/data/adb/mobile-proxy-node/releases/"
_SHA = re.compile(r"[0-9a-f]{40}")
_SCHEMA = "stage4-runtime-mismatch-exercise.v1"


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safety(*, phone_access: bool) -> dict[str, object]:
    return {
        "phone_access_performed": phone_access,
        "phone_mutation_performed": False,
        "runtime_mutation_performed": False,
        "current_link_mutation_performed": False,
        "deployment_created": False,
        "deployment_intent_created": False,
        "recovery_dispatched": False,
        "reconciliation_dispatched": False,
        "provider_access_performed": False,
        "provider_mutation_performed": False,
        "vm_access_performed": False,
        "vm_mutation_performed": False,
        "runner_proxy_mutation_performed": False,
        "raw_device_identifier_recorded": False,
        "raw_current_target_path_recorded": False,
        "raw_runtime_release_paths_recorded": False,
        "raw_config_recorded": False,
        "expected_file_digests_recorded": False,
        "observed_file_digests_recorded": False,
        "secret_values_recorded": False,
    }


def _release_condition(snapshot: object) -> dict[str, object]:
    classification = str(getattr(snapshot, "classification", "UNKNOWN"))
    return {
        "classification": classification,
        "exact": classification == "HEALTHY_EXACT" and bool(getattr(snapshot, "desired", False)),
    }


def _base(*, controller_revision: str) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "controller_revision": controller_revision,
        "target": _STAGE4_TARGET,
        "product_release": _STAGE4_RELEASE,
        "mode": "read_only_fixed_expectation",
        "scenario": {
            "id": "managed-current-shadow-expectation-mismatch",
            "detector_owner": "phone_target.observe_runtime",
            "shadow_expected_release": _SHADOW_EXPECTED_RELEASE,
            "phone_state_injection": False,
            "arbitrary_release_selector": False,
        },
        "classification": "UNKNOWN",
        "failure_code": "UNCLASSIFIED",
        "baseline": None,
        "mismatch_detection": None,
        "postcondition": None,
        "safety": _safety(phone_access=False),
    }


def exercise(
    *,
    target: str,
    release_tag: str,
    controller_revision: str,
    product_root: Path,
    runtime_manifest: Path,
    output: Path,
) -> tuple[dict[str, object], int]:
    if target != _STAGE4_TARGET or release_tag != _STAGE4_RELEASE:
        raise ValueError("Stage 4 mismatch exercise is bound to phone-production@v0.1.7")
    if _SHA.fullmatch(controller_revision) is None or os.environ.get("GITHUB_SHA") != controller_revision:
        raise ValueError("mismatch exercise controller revision differs")

    payload = _base(controller_revision=controller_revision)
    phone_access = False
    try:
        admitted = resolve_release(tag=release_tag, target=target)
        if admitted.identity.tag != release_tag or admitted.identity.immutable is not True:
            raise ReleaseAdmissionError("Stage 4 Product Release admission differs")
        serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
        binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
        if not serial or len(binding_key) < 32:
            raise PhoneTargetUnavailable("registered production phone binding is unavailable")

        with tempfile.TemporaryDirectory(prefix="stage4-runtime-mismatch-") as raw:
            root = Path(raw)
            facts: dict[str, object] = {}
            materialized = prepare_verified_release_runtime(
                admitted,
                archive=root / "phone-runtime.tar.gz",
                work_root=root / "runtime",
                product_root=product_root,
                runtime_manifest_path=runtime_manifest,
                binding_key=binding_key,
                facts=facts,
            )

            phone_access = True
            baseline = observe_exact_phone_release(
                serial=serial,
                binding_key=binding_key,
                admitted=admitted,
                materialized=materialized,
            )
            payload["baseline"] = _release_condition(baseline)
            if baseline.classification != "HEALTHY_EXACT" or not baseline.desired:
                payload["classification"] = "REFUSED"
                payload["failure_code"] = "BASELINE_NOT_HEALTHY_EXACT"
                payload["safety"] = _safety(phone_access=phone_access)
                _write(output, payload)
                return payload, 2

            shadow = observe_runtime(
                serial=serial,
                release_root=materialized.release_root,
                release_id=_SHADOW_EXPECTED_RELEASE,
                required_paths=materialized.required_live_release_paths,
            )
            current = shadow.current_target
            current_state = (
                "absent"
                if current is None
                else "shadow_expected_release"
                if current == shadow.target_release
                else "other_managed_release"
                if isinstance(current, str) and current.startswith(_MANAGED_RELEASE_PREFIX)
                else "invalid_or_unmanaged"
            )
            payload["mismatch_detection"] = {
                "detected": bool(
                    shadow.target_release_exists is False
                    and current_state == "other_managed_release"
                    and shadow.desired is False
                ),
                "shadow_target_release_exists": bool(shadow.target_release_exists),
                "current_state_relative_to_shadow": current_state,
                "current_matches_shadow_expected_release": current == shadow.target_release,
                "exact_files_verified_for_shadow": bool(shadow.exact_files_verified),
                "desired_for_shadow": bool(shadow.desired),
                "admissible_for_new_dispatch": bool(shadow.admissible_for_new_dispatch),
                "raw_current_target_path_recorded": False,
                "raw_release_paths_recorded": False,
            }
            if shadow.target_release_exists:
                payload["classification"] = "REFUSED"
                payload["failure_code"] = "SHADOW_RELEASE_COLLISION"
            elif current_state != "other_managed_release":
                payload["classification"] = "UNKNOWN"
                payload["failure_code"] = "MANAGED_CURRENT_CLASSIFICATION_UNEXPECTED"
            elif shadow.desired or current == shadow.target_release:
                payload["classification"] = "UNKNOWN"
                payload["failure_code"] = "MISMATCH_NOT_DETECTED"
            else:
                post = observe_exact_phone_release(
                    serial=serial,
                    binding_key=binding_key,
                    admitted=admitted,
                    materialized=materialized,
                )
                payload["postcondition"] = _release_condition(post)
                if post.classification == "HEALTHY_EXACT" and post.desired:
                    payload["classification"] = "MISMATCH_DETECTED"
                    payload["failure_code"] = None
                else:
                    payload["classification"] = "UNKNOWN"
                    payload["failure_code"] = "POSTCONDITION_NOT_HEALTHY_EXACT"
    except (ReleaseAdmissionError, PhoneTargetUnavailable, OSError, ValueError) as exc:
        payload["classification"] = "UNKNOWN"
        payload["failure_code"] = "OBSERVATION_UNAVAILABLE"
        payload["failure_class"] = exc.__class__.__name__
    payload["safety"] = _safety(phone_access=phone_access)
    _write(output, payload)
    return payload, 0 if payload["classification"] == "MISMATCH_DETECTED" else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--product-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        _payload, code = exercise(
            target=args.target,
            release_tag=args.release_tag,
            controller_revision=args.controller_revision,
            product_root=args.product_root,
            runtime_manifest=args.runtime_manifest,
            output=args.output,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return code


if __name__ == "__main__":
    raise SystemExit(main())
