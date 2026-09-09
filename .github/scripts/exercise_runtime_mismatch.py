#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
CONTROLLER = SCRIPTS.parent / "controller"
sys.path.insert(0, str(CONTROLLER))

from phone_release_state import (  # noqa: E402
    observe_exact_phone_release,
    prepare_verified_release_runtime,
)
from phone_runtime import PhoneRuntimeRefused  # noqa: E402
from phone_target import PhoneTargetUnavailable, RootScriptResult  # noqa: E402
import phone_target  # noqa: E402
from release_resolver import ReleaseAdmissionError, resolve_release  # noqa: E402
from runtime_operational_observer import (  # noqa: E402
    RuntimeOperationalObservation,
    observe_runtime_operational_health,
)

_STAGE4_TARGET = "phone-production"
_STAGE4_RELEASE = "v0.1.7"
_ROOT = "/data/adb/mobile-proxy-node"
_ROOT_SCRIPT_TIMEOUT_SECONDS = 15
_SCHEMA = "stage4-runtime-mismatch-exercise.v1"
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class LinkTransition:
    outcome: str
    failure_code: str | None
    dispatched: bool | None
    verified: bool | None


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _phone_local_ready(value: RuntimeOperationalObservation) -> bool:
    return bool(
        value.watchdog_count == 1
        and value.runtime_supervisor_count == 1
        and value.host_daemon_count == 1
        and value.sing_box_count == 1
        and value.health_api_authenticated
        and value.cellular_route_ready is True
        and value.proxy_bind_ready is True
        and value.local_serving_ready is True
        and value.tunnel_owner_matches_expected
    )


def _topology_disposition(value: RuntimeOperationalObservation) -> str:
    if not _phone_local_ready(value):
        return "PHONE_LOCAL_NOT_READY"
    if value.desired:
        return "READY"
    if value.degradation_reason_code == "reverse_tunnel_not_ready":
        return "TOPOLOGY_DEFERRED"
    return "NONLOCAL_DEGRADED"


def _bounded_operational(value: RuntimeOperationalObservation) -> dict[str, object]:
    return {
        "phone_local": {
            "ready": _phone_local_ready(value),
            "watchdog_count": value.watchdog_count,
            "runtime_supervisor_count": value.runtime_supervisor_count,
            "host_daemon_count": value.host_daemon_count,
            "sing_box_count": value.sing_box_count,
            "health_api_authenticated": value.health_api_authenticated,
            "cellular_route_ready": value.cellular_route_ready,
            "proxy_bind_ready": value.proxy_bind_ready,
            "local_serving_ready": value.local_serving_ready,
            "tunnel_owner_matches_expected": value.tunnel_owner_matches_expected,
        },
        "topology": {
            "disposition": _topology_disposition(value),
            "serving": value.serving,
            "readiness_state": value.readiness_state,
            "proxy_status": value.proxy_status,
            "degradation_reason_code": value.degradation_reason_code,
            "reverse_tunnel_connected": value.reverse_tunnel_connected,
            "reverse_tunnel_freshness": value.reverse_tunnel_freshness,
            "reverse_tunnel_active_transport": value.reverse_tunnel_active_transport,
            "reverse_tunnel_failover_reason": value.reverse_tunnel_failover_reason,
        },
    }


def _release_condition(snapshot: object) -> dict[str, object]:
    classification = str(getattr(snapshot, "classification", "UNKNOWN"))
    return {
        "classification": classification,
        "exact": classification == "HEALTHY_EXACT" and bool(getattr(snapshot, "desired", False)),
    }


def _safety(
    *,
    phone_access: bool,
    phone_mutation: bool | None,
    injected: bool | None,
    restore_attempted: bool,
    restored: bool | None,
) -> dict[str, object]:
    return {
        "phone_access_performed": phone_access,
        "phone_mutation_performed": phone_mutation,
        "runtime_current_mismatch_injected": injected,
        "runtime_current_restore_attempted": restore_attempted,
        "runtime_current_restored": restored,
        "release_bytes_modified": False,
        "runtime_config_modified": False,
        "process_signal_sent": False,
        "runtime_restart_performed": False,
        "phone_reboot_performed": False,
        "deployment_created": False,
        "deployment_intent_created": False,
        "provider_access_performed": False,
        "provider_mutation_performed": False,
        "vm_access_performed": False,
        "vm_mutation_performed": False,
        "runner_proxy_mutation_performed": False,
        "bootstrap_reinvoked": False,
        "service_script_reinvoked": False,
        "arbitrary_path_selector_accepted": False,
        "raw_current_target_recorded": False,
        "process_ids_recorded": False,
        "process_cmdlines_recorded": False,
        "raw_device_identifier_recorded": False,
        "raw_config_recorded": False,
        "secret_values_recorded": False,
        "blind_retry_performed": False,
    }


def _base_payload(*, controller_revision: str, source_comment_id: int) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "controller_revision": controller_revision,
        "target": _STAGE4_TARGET,
        "product_release": _STAGE4_RELEASE,
        "source_comment_id": source_comment_id,
        "scenario": {
            "id": "current-symlink-noncanonical-equivalent-once",
            "mismatch_owner": "runtime-current-link",
            "same_release_bytes": True,
            "process_restart_expected": False,
            "arbitrary_path_selector": False,
            "root_script_bound_seconds": _ROOT_SCRIPT_TIMEOUT_SECONDS,
        },
        "classification": "UNKNOWN",
        "failure_code": "UNCLASSIFIED",
        "preconditions": None,
        "mismatch_transition": {
            "outcome": "NOT_ATTEMPTED",
            "dispatched": False,
            "verified": False,
            "ambiguous_stop": False,
        },
        "mismatch_observation": None,
        "restore_transition": {
            "outcome": "NOT_ATTEMPTED",
            "dispatched": False,
            "verified": False,
            "ambiguous_stop": False,
        },
        "postconditions": None,
        "safety": _safety(
            phone_access=False,
            phone_mutation=False,
            injected=False,
            restore_attempted=False,
            restored=False,
        ),
    }


def _transition_script(*, restore: bool) -> bytes:
    target = f"{_ROOT}/releases/{_STAGE4_RELEASE}"
    probe = f"{_ROOT}/releases/../releases/{_STAGE4_RELEASE}"
    current = f"{_ROOT}/current"
    if restore:
        return f'''set -eu
TARGET='{target}'
PROBE='{probe}'
CURRENT='{current}'
command -v ln >/dev/null 2>&1 || exit 40
command -v readlink >/dev/null 2>&1 || exit 40
[ -d "$TARGET" ] || exit 40
[ "$(readlink "$CURRENT" 2>/dev/null || true)" = "$PROBE" ] || {{
  printf 'stage4_mismatch=restore_precondition_refused\\n'
  exit 40
}}
[ "$(readlink -f "$CURRENT" 2>/dev/null || true)" = "$TARGET" ] || {{
  printf 'stage4_mismatch=restore_precondition_refused\\n'
  exit 40
}}
ln -sfn "$TARGET" "$CURRENT"
printf 'stage4_mismatch=restore_dispatched\\n'
[ "$(readlink "$CURRENT" 2>/dev/null || true)" = "$TARGET" ] || exit 41
[ "$(readlink -f "$CURRENT" 2>/dev/null || true)" = "$TARGET" ] || exit 41
printf 'stage4_mismatch=canonical_current_restored\\n'
'''.encode("utf-8")
    return f'''set -eu
TARGET='{target}'
PROBE='{probe}'
CURRENT='{current}'
command -v ln >/dev/null 2>&1 || exit 40
command -v readlink >/dev/null 2>&1 || exit 40
[ -d "$TARGET" ] || exit 40
[ "$(readlink "$CURRENT" 2>/dev/null || true)" = "$TARGET" ] || {{
  printf 'stage4_mismatch=inject_precondition_refused\\n'
  exit 40
}}
[ "$(readlink -f "$PROBE" 2>/dev/null || true)" = "$TARGET" ] || {{
  printf 'stage4_mismatch=inject_precondition_refused\\n'
  exit 40
}}
ln -sfn "$PROBE" "$CURRENT"
printf 'stage4_mismatch=inject_dispatched\\n'
[ "$(readlink "$CURRENT" 2>/dev/null || true)" = "$PROBE" ] || exit 41
[ "$(readlink -f "$CURRENT" 2>/dev/null || true)" = "$TARGET" ] || exit 41
printf 'stage4_mismatch=noncanonical_current_verified\\n'
'''.encode("utf-8")


def _run_transition(serial: str, *, restore: bool) -> LinkTransition:
    result: RootScriptResult = phone_target._run_root_script(
        serial,
        _transition_script(restore=restore),
        timeout=_ROOT_SCRIPT_TIMEOUT_SECONDS,
    )
    prefix = b"stage4_mismatch=restore" if restore else b"stage4_mismatch=inject"
    refused_line = (
        b"stage4_mismatch=restore_precondition_refused\n"
        if restore
        else b"stage4_mismatch=inject_precondition_refused\n"
    )
    expected = (
        b"stage4_mismatch=restore_dispatched\n"
        b"stage4_mismatch=canonical_current_restored\n"
        if restore
        else b"stage4_mismatch=inject_dispatched\n"
        b"stage4_mismatch=noncanonical_current_verified\n"
    )
    dispatched_line = (
        b"stage4_mismatch=restore_dispatched\n"
        if restore
        else b"stage4_mismatch=inject_dispatched\n"
    )
    if result.status != "completed":
        return LinkTransition("UNKNOWN", "LINK_TRANSITION_OUTCOME_UNKNOWN", None, None)
    if result.stderr != b"":
        return LinkTransition("UNKNOWN", "LINK_TRANSITION_PROTOCOL_MISMATCH", None, None)
    if result.returncode == 40 and result.stdout in {b"", refused_line}:
        return LinkTransition("REFUSED", "LINK_TRANSITION_PRECONDITION_CHANGED", False, False)
    if result.returncode == 0 and result.stdout == expected:
        return LinkTransition("RESTORED" if restore else "INJECTED", None, True, True)
    dispatched = dispatched_line in result.stdout
    return LinkTransition(
        "UNKNOWN",
        "LINK_TRANSITION_PROTOCOL_MISMATCH",
        True if dispatched else None,
        None,
    )


def _mismatch_condition(snapshot: object) -> dict[str, object]:
    apk = getattr(snapshot, "apk", None)
    runtime = getattr(snapshot, "runtime", None)
    if getattr(snapshot, "classification", "UNKNOWN") == "UNKNOWN" or apk is None or runtime is None:
        return {
            "classification": "UNKNOWN",
            "apk_exact": None,
            "runtime_desired": None,
            "target_release_exists": None,
            "current_matches_canonical_target": None,
            "exact_files_verified": None,
            "detected": False,
        }
    current_matches = getattr(runtime, "current_target", None) == getattr(runtime, "target_release", None)
    detected = bool(
        getattr(snapshot, "classification", "") == "DEGRADED"
        and getattr(apk, "desired", False)
        and not getattr(runtime, "desired", True)
        and getattr(runtime, "target_release_exists", False)
        and not current_matches
    )
    return {
        "classification": str(getattr(snapshot, "classification", "UNKNOWN")),
        "apk_exact": bool(getattr(apk, "desired", False)),
        "runtime_desired": bool(getattr(runtime, "desired", False)),
        "target_release_exists": bool(getattr(runtime, "target_release_exists", False)),
        "current_matches_canonical_target": current_matches,
        "exact_files_verified": bool(getattr(runtime, "exact_files_verified", False)),
        "detected": detected,
    }


def _set_terminal(
    payload: dict[str, object],
    *,
    classification: str,
    failure_code: str | None,
    phone_access: bool,
    phone_mutation: bool | None,
    injected: bool | None,
    restore_attempted: bool,
    restored: bool | None,
) -> int:
    payload["classification"] = classification
    if failure_code is None:
        payload.pop("failure_code", None)
    else:
        payload["failure_code"] = failure_code
    payload["safety"] = _safety(
        phone_access=phone_access,
        phone_mutation=phone_mutation,
        injected=injected,
        restore_attempted=restore_attempted,
        restored=restored,
    )
    return 0 if classification == "MISMATCH_DETECTED_AND_RESTORED" else 2


def exercise(
    *,
    controller_revision: str,
    source_comment_id: int,
    product_root: Path,
    runtime_manifest: Path,
    output: Path,
) -> int:
    payload = _base_payload(
        controller_revision=controller_revision,
        source_comment_id=source_comment_id,
    )
    phone_access = False
    phone_mutation: bool | None = False
    injected: bool | None = False
    restore_attempted = False
    restored: bool | None = False

    try:
        admitted = resolve_release(tag=_STAGE4_RELEASE, target=_STAGE4_TARGET)
        payload["release_source_sha"] = admitted.identity.source_sha

        serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
        binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
        admin_token = os.environ.get("MOBILE_PROXY_ADMIN_TOKEN", "")
        if not serial or len(binding_key) < 32 or not admin_token:
            raise PhoneTargetUnavailable("phone binding unavailable")

        with tempfile.TemporaryDirectory(prefix="stage4-runtime-mismatch-") as raw:
            root = Path(raw)
            materialized = prepare_verified_release_runtime(
                admitted,
                archive=root / "phone-runtime.tar.gz",
                work_root=root / "runtime",
                product_root=product_root,
                runtime_manifest_path=runtime_manifest,
                binding_key=binding_key,
                facts={},
            )

            phone_access = True
            pre_release = observe_exact_phone_release(
                serial=serial,
                binding_key=binding_key,
                admitted=admitted,
                materialized=materialized,
            )
            if pre_release.classification == "UNKNOWN":
                payload["preconditions"] = {"exact_product_release": _release_condition(pre_release)}
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code="PRE_RELEASE_STATE_UNKNOWN",
                    phone_access=True,
                    phone_mutation=False,
                    injected=False,
                    restore_attempted=False,
                    restored=False,
                )
                _write(output, payload)
                return code
            if not pre_release.desired:
                payload["preconditions"] = {"exact_product_release": _release_condition(pre_release)}
                code = _set_terminal(
                    payload,
                    classification="REFUSED",
                    failure_code="PRE_RELEASE_STATE_NOT_EXACT",
                    phone_access=True,
                    phone_mutation=False,
                    injected=False,
                    restore_attempted=False,
                    restored=False,
                )
                _write(output, payload)
                return code

            pre_operational = observe_runtime_operational_health(serial, admin_token=admin_token)
            payload["preconditions"] = {
                "exact_product_release": _release_condition(pre_release),
                "operational": _bounded_operational(pre_operational),
            }
            if not _phone_local_ready(pre_operational):
                code = _set_terminal(
                    payload,
                    classification="REFUSED",
                    failure_code="PRE_PHONE_LOCAL_NOT_READY",
                    phone_access=True,
                    phone_mutation=False,
                    injected=False,
                    restore_attempted=False,
                    restored=False,
                )
                _write(output, payload)
                return code

            transition = _run_transition(serial, restore=False)
            payload["mismatch_transition"] = {
                "outcome": transition.outcome,
                "dispatched": transition.dispatched,
                "verified": transition.verified,
                "ambiguous_stop": transition.outcome == "UNKNOWN",
            }
            phone_mutation = transition.dispatched
            injected = transition.verified
            if transition.outcome != "INJECTED":
                code = _set_terminal(
                    payload,
                    classification="REFUSED" if transition.outcome == "REFUSED" else "UNKNOWN",
                    failure_code=transition.failure_code,
                    phone_access=True,
                    phone_mutation=phone_mutation,
                    injected=injected,
                    restore_attempted=False,
                    restored=False,
                )
                _write(output, payload)
                return code

            mismatch_snapshot = observe_exact_phone_release(
                serial=serial,
                binding_key=binding_key,
                admitted=admitted,
                materialized=materialized,
            )
            mismatch = _mismatch_condition(mismatch_snapshot)
            payload["mismatch_observation"] = mismatch

            restore_attempted = True
            restore = _run_transition(serial, restore=True)
            payload["restore_transition"] = {
                "outcome": restore.outcome,
                "dispatched": restore.dispatched,
                "verified": restore.verified,
                "ambiguous_stop": restore.outcome == "UNKNOWN",
            }
            restored = restore.verified
            phone_mutation = True
            if restore.outcome != "RESTORED":
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code=restore.failure_code or "CANONICAL_RESTORE_NOT_PROVEN",
                    phone_access=True,
                    phone_mutation=True,
                    injected=True,
                    restore_attempted=True,
                    restored=restored,
                )
                _write(output, payload)
                return code

            post_release = observe_exact_phone_release(
                serial=serial,
                binding_key=binding_key,
                admitted=admitted,
                materialized=materialized,
            )
            post_operational = observe_runtime_operational_health(serial, admin_token=admin_token)
            payload["postconditions"] = {
                "exact_product_release": _release_condition(post_release),
                "operational": _bounded_operational(post_operational),
            }

            if mismatch_snapshot.classification == "UNKNOWN":
                classification, failure_code = "UNKNOWN", "MISMATCH_OBSERVATION_UNKNOWN"
            elif mismatch.get("detected") is not True:
                classification, failure_code = "FAILED", "MISMATCH_NOT_DETECTED"
            elif post_release.classification == "UNKNOWN":
                classification, failure_code = "UNKNOWN", "POST_RELEASE_STATE_UNKNOWN"
            elif not post_release.desired:
                classification, failure_code = "FAILED", "POST_RELEASE_STATE_NOT_EXACT"
            elif not _phone_local_ready(post_operational):
                classification, failure_code = "FAILED", "POST_PHONE_LOCAL_NOT_READY"
            else:
                classification, failure_code = "MISMATCH_DETECTED_AND_RESTORED", None

            code = _set_terminal(
                payload,
                classification=classification,
                failure_code=failure_code,
                phone_access=True,
                phone_mutation=True,
                injected=True,
                restore_attempted=True,
                restored=True,
            )
            _write(output, payload)
            return code
    except ReleaseAdmissionError:
        code = _set_terminal(
            payload,
            classification="REFUSED",
            failure_code="RELEASE_ADMISSION_FAILED",
            phone_access=False,
            phone_mutation=False,
            injected=False,
            restore_attempted=False,
            restored=False,
        )
    except PhoneRuntimeRefused:
        code = _set_terminal(
            payload,
            classification="REFUSED",
            failure_code="RUNTIME_MATERIALIZATION_FAILED",
            phone_access=phone_access,
            phone_mutation=phone_mutation,
            injected=injected,
            restore_attempted=restore_attempted,
            restored=restored,
        )
    except PhoneTargetUnavailable:
        code = _set_terminal(
            payload,
            classification="UNKNOWN",
            failure_code="PHONE_TARGET_UNAVAILABLE",
            phone_access=phone_access,
            phone_mutation=phone_mutation,
            injected=injected,
            restore_attempted=restore_attempted,
            restored=restored,
        )
    except Exception:
        code = _set_terminal(
            payload,
            classification="UNKNOWN",
            failure_code="UNEXPECTED_CONTROLLER_ERROR",
            phone_access=phone_access,
            phone_mutation=phone_mutation,
            injected=injected,
            restore_attempted=restore_attempted,
            restored=restored,
        )
    _write(output, payload)
    return code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one fixed Stage 4 runtime current-link mismatch exercise")
    parser.add_argument("--target", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--source-comment-id", required=True, type=int)
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.target != _STAGE4_TARGET or args.release_tag != _STAGE4_RELEASE:
        raise SystemExit("Stage 4 runtime mismatch identity differs")
    if _SHA.fullmatch(args.controller_revision) is None:
        raise SystemExit("Stage 4 runtime mismatch Controller revision is invalid")
    if args.source_comment_id <= 0:
        raise SystemExit("Stage 4 runtime mismatch source comment id is invalid")
    return exercise(
        controller_revision=args.controller_revision,
        source_comment_id=args.source_comment_id,
        product_root=args.product_root,
        runtime_manifest=args.runtime_manifest,
        output=args.output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
