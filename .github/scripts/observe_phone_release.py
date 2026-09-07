#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent
CONTROLLER = SCRIPTS.parent / "controller"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(CONTROLLER))

from android_target import AndroidObservationUnavailable  # noqa: E402
from phone_runtime import PhoneRuntimeRefused  # noqa: E402
from phone_target import (  # noqa: E402
    PhoneTargetUnavailable,
    observe_runtime_required_file_statuses,
)
from release_resolver import ReleaseAdmissionError, resolve_release  # noqa: E402

import run_phone_release_deployment as deployment_runner  # noqa: E402

_SHA = re.compile(r"[0-9a-f]{40}")
_STAGE4_RELEASE = "v0.1.7"
_MANAGED_RELEASE_PREFIX = "/data/adb/mobile-proxy-node/releases/"
_MAX_REASON_CHARS = 240
_FILE_STATUSES = frozenset({"exact", "digest_mismatch", "missing", "wrong_type", "unreadable"})


def _bounded_apk(apk: object) -> dict[str, object]:
    value = apk.to_dict()
    return {
        "target": value.get("target"),
        "target_binding_id": value.get("target_binding_id"),
        "package_name": value.get("package_name"),
        "installed": value.get("installed"),
        "version_name": value.get("version_name"),
        "version_code": value.get("version_code"),
        "artifact_sha256": value.get("artifact_sha256"),
        "exact_artifact_verified": value.get("exact_artifact_verified"),
        "desired": value.get("desired"),
        "mode": value.get("mode"),
        "raw_device_identifier_recorded": False,
    }


def _bounded_runtime(runtime: object) -> dict[str, object]:
    expected = str(getattr(runtime, "target_release"))
    current = getattr(runtime, "current_target")
    if current is None:
        current_state = "absent"
    elif current == expected:
        current_state = "expected_release"
    elif isinstance(current, str) and current.startswith(_MANAGED_RELEASE_PREFIX):
        current_state = "other_managed_release"
    else:
        current_state = "invalid_or_unmanaged"
    return {
        "target_release_exists": bool(getattr(runtime, "target_release_exists")),
        "current_state": current_state,
        "current_matches_expected_release": current == expected,
        "exact_files_verified": bool(getattr(runtime, "exact_files_verified")),
        "required_file_count": int(getattr(runtime, "required_file_count")),
        "desired": bool(getattr(runtime, "desired")),
        "admissible_for_new_dispatch": bool(getattr(runtime, "admissible_for_new_dispatch")),
        "mode": str(getattr(runtime, "mode")),
        "raw_current_target_path_recorded": False,
        "raw_config_recorded": False,
    }


def _bounded_runtime_file_drift(
    statuses: tuple[str, ...], *, required_paths: tuple[str, ...], rendered_paths: object
) -> dict[str, object]:
    if len(statuses) != len(required_paths) or any(status not in _FILE_STATUSES for status in statuses):
        raise PhoneTargetUnavailable("bounded runtime required-file evidence differs")
    if not isinstance(rendered_paths, list) or any(not isinstance(item, str) for item in rendered_paths):
        raise PhoneTargetUnavailable("bounded runtime rendered-file classification is unavailable")
    rendered = set(rendered_paths)
    required = set(required_paths)
    if not rendered.issubset(required):
        raise PhoneTargetUnavailable("bounded runtime rendered-file classification differs")
    files = [
        {
            "required_file_index": index,
            "source_class": "sensitive_derived" if path in rendered else "static_release",
            "status": status,
        }
        for index, (path, status) in enumerate(zip(required_paths, statuses, strict=True))
    ]
    return {
        "required_file_count": len(files),
        "exact_file_count": sum(item["status"] == "exact" for item in files),
        "drift_file_count": sum(item["status"] != "exact" for item in files),
        "files": files,
        "raw_release_paths_recorded": False,
        "expected_file_digests_recorded": False,
        "observed_file_digests_recorded": False,
        "secret_derived_identifiers_recorded": False,
    }


def _bounded_materialization(raw: object) -> dict[str, object]:
    value = raw if isinstance(raw, dict) else {}
    secret_bindings = value.get("secret_binding_ids")
    rendered = value.get("derived_files_rendered")
    return {
        "exact_release_runtime": value.get("exact_release_runtime") is True,
        "artifact_name": value.get("artifact_name"),
        "product_content_digest": value.get("product_content_digest"),
        "transport_sha256": value.get("transport_sha256"),
        "component_inventory_digest": value.get("component_inventory_digest"),
        "component_count": value.get("component_count"),
        "required_live_file_count": value.get("required_live_file_count"),
        "derived_file_count": len(rendered) if isinstance(rendered, list) else 0,
        "renderer_source_sha": value.get("renderer_source_sha"),
        "runtime_manifest_sha256": value.get("runtime_manifest_sha256"),
        "secret_binding_count": len(secret_bindings) if isinstance(secret_bindings, dict) else 0,
        "secret_binding_ids_recorded": False,
        "secret_values_recorded": False,
        "raw_rendered_config_recorded": False,
    }


def _failure_reason(exc: BaseException) -> str:
    reason = str(exc).strip().replace("\n", " ")
    if not reason:
        reason = exc.__class__.__name__
    return reason[:_MAX_REASON_CHARS]


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safety(*, phone_access_started: bool) -> dict[str, object]:
    return {
        "phone_access_performed": phone_access_started,
        "phone_mutation_performed": False,
        "deployment_created": False,
        "deployment_intent_created": False,
        "provider_access_performed": False,
        "provider_mutation_performed": False,
        "raw_device_identifier_recorded": False,
        "raw_current_target_path_recorded": False,
        "raw_runtime_release_paths_recorded": False,
        "raw_config_recorded": False,
        "expected_file_digests_recorded": False,
        "observed_file_digests_recorded": False,
        "credential_derived_identifiers_recorded": False,
        "secret_values_recorded": False,
    }


def _base_payload(*, controller_revision: str, admitted: object) -> dict[str, object]:
    identity = admitted.identity
    return {
        "schema": "stage4-phone-release-observation.v1",
        "controller_revision": controller_revision,
        "target": "phone-production",
        "product_release": identity.tag,
        "release_id": identity.release_id,
        "release_source_sha": identity.source_sha,
        "mode": "read_only",
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
        raise SystemExit("Stage 4 observer is bound to phone-production@v0.1.7")
    if _SHA.fullmatch(args.controller_revision) is None or os.environ.get("GITHUB_SHA") != args.controller_revision:
        raise SystemExit("observer controller revision differs")

    phone_access_started = False
    admitted = None
    try:
        admitted = resolve_release(tag=args.release_tag, target=args.target)
        serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
        binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
        if not serial or len(binding_key) < 32:
            raise AndroidObservationUnavailable("registered production phone binding is unavailable")

        runtime_file_drift = None
        with tempfile.TemporaryDirectory(prefix="stage4-phone-observe-") as raw:
            root = Path(raw)
            facts: dict[str, object] = {}
            materialized = deployment_runner._materialize_verified_release_runtime(
                admitted,
                archive=root / "phone-runtime.tar.gz",
                work_root=root / "runtime",
                product_root=args.product_root,
                runtime_manifest_path=args.runtime_manifest,
                binding_key=binding_key,
                facts=facts,
            )
            phone_access_started = True
            apk, runtime = deployment_runner._observe_composite(
                serial=serial,
                binding_key=binding_key,
                admitted=admitted,
                materialized=materialized,
            )
            if (
                runtime.target_release_exists
                and runtime.current_target == runtime.target_release
                and not runtime.exact_files_verified
            ):
                statuses = observe_runtime_required_file_statuses(
                    serial=serial,
                    release_root=materialized.release_root,
                    release_id=admitted.identity.tag,
                    required_paths=materialized.required_live_release_paths,
                )
                verification = facts.get("runtime_verification")
                rendered_paths = verification.get("derived_files_rendered") if isinstance(verification, dict) else None
                runtime_file_drift = _bounded_runtime_file_drift(
                    statuses,
                    required_paths=materialized.required_live_release_paths,
                    rendered_paths=rendered_paths,
                )

        desired = bool(apk.desired and runtime.desired)
        payload = {
            **_base_payload(controller_revision=args.controller_revision, admitted=admitted),
            "classification": "HEALTHY_EXACT" if desired else "DEGRADED",
            "observation": {
                "apk": _bounded_apk(apk),
                "runtime": _bounded_runtime(runtime),
                "runtime_file_drift": runtime_file_drift,
                "desired": desired,
            },
            "expected_materialization": _bounded_materialization(facts.get("runtime_verification")),
            "safety": _safety(phone_access_started=phone_access_started),
        }
        _write(args.output, payload)
        print(
            "STAGE4_PHONE_RELEASE_OBSERVED "
            f"classification={payload['classification']} target={args.target} release={args.release_tag}"
        )
        return 0
    except (
        ReleaseAdmissionError,
        AndroidObservationUnavailable,
        PhoneRuntimeRefused,
        PhoneTargetUnavailable,
    ) as exc:
        identity = admitted.identity if admitted is not None else SimpleNamespace(
            tag=args.release_tag,
            release_id=None,
            source_sha=None,
        )
        payload = {
            "schema": "stage4-phone-release-observation.v1",
            "controller_revision": args.controller_revision,
            "target": args.target,
            "product_release": identity.tag,
            "release_id": identity.release_id,
            "release_source_sha": identity.source_sha,
            "mode": "read_only",
            "classification": "UNKNOWN",
            "failure_class": exc.__class__.__name__,
            "failure_reason": _failure_reason(exc),
            "safety": _safety(phone_access_started=phone_access_started),
        }
        _write(args.output, payload)
        print(
            "STAGE4_PHONE_RELEASE_OBSERVATION_UNKNOWN "
            f"failure_class={payload['failure_class']} reason={payload['failure_reason']}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
