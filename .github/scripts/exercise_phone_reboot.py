#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
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
    RuntimeOperationalObservationUnavailable,
    RuntimeOperationalOutputValidationFailure,
    observe_runtime_operational_health,
)

_STAGE4_TARGET = "phone-production"
_STAGE4_RELEASE = "v0.1.7"
_SHA = re.compile(r"[0-9a-f]{40}")
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ROOT = "/data/adb/mobile-proxy-node"
_REBOOT_OBSERVATION_BOUND_SECONDS = 180
_POST_OPERATIONAL_BOUND_SECONDS = 60
_ROOT_SCRIPT_TIMEOUT_SECONDS = 15
_SCHEMA = "stage4-phone-reboot-exercise.v1"


@dataclass(frozen=True)
class RebootDispatch:
    outcome: str
    failure_code: str | None
    dispatched: bool | None


@dataclass(frozen=True)
class BootState:
    boot_id: str
    boot_completed: bool


@dataclass(frozen=True)
class PostOperationalFailure:
    failure_code: str
    failure_domain: str
    failure_phase: str | None


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safety(
    *,
    phone_access: bool,
    phone_mutation: bool | None,
    phone_reboot: bool | None,
) -> dict[str, object]:
    return {
        "phone_access_performed": phone_access,
        "phone_mutation_performed": phone_mutation,
        "deployment_created": False,
        "deployment_intent_created": False,
        "provider_access_performed": False,
        "provider_mutation_performed": False,
        "vm_access_performed": False,
        "vm_mutation_performed": False,
        "runner_proxy_mutation_performed": False,
        "runtime_restart_performed": False,
        "phone_reboot_performed": phone_reboot,
        "bootstrap_reinvoked": False,
        "service_script_reinvoked": False,
        "arbitrary_reboot_selector_accepted": False,
        "arbitrary_process_selector_accepted": False,
        "raw_boot_identity_recorded": False,
        "process_ids_recorded": False,
        "process_cmdlines_recorded": False,
        "process_generation_tokens_recorded": False,
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
            "id": "android-rooted-phone-reboot-once",
            "reboot_owner": "android-init-via-sys.powerctl",
            "post_boot_runtime_owner": "product-magisk-module-service",
            "arbitrary_reboot_selector": False,
            "service_script_reinvoked": False,
            "reboot_observation_bound_seconds": _REBOOT_OBSERVATION_BOUND_SECONDS,
            "post_operational_bound_seconds": _POST_OPERATIONAL_BOUND_SECONDS,
        },
        "classification": "UNKNOWN",
        "failure_code": "UNCLASSIFIED",
        "preconditions": None,
        "mutation": {
            "attempts": 0,
            "outcome": "NOT_ATTEMPTED",
            "dispatched": False,
            "ambiguous_stop": False,
        },
        "boot_rehydration": {
            "expected": True,
            "boot_identity_changed": False,
            "boot_completed": False,
            "runtime_stack_ready": False,
            "service_script_reinvoked": False,
            "reboot_observation_bound_seconds": _REBOOT_OBSERVATION_BOUND_SECONDS,
            "post_operational_bound_seconds": _POST_OPERATIONAL_BOUND_SECONDS,
        },
        "postconditions": None,
        "safety": _safety(
            phone_access=False,
            phone_mutation=False,
            phone_reboot=False,
        ),
    }


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


def _observe_boot_state(serial: str) -> BootState:
    boot = phone_target._read(serial, ["shell", "cat", "/proc/sys/kernel/random/boot_id"], timeout=10)
    completed = phone_target._read(serial, ["shell", "getprop", "sys.boot_completed"], timeout=10)
    if boot.returncode != 0 or completed.returncode != 0 or boot.stderr != "" or completed.stderr != "":
        raise PhoneTargetUnavailable("phone boot state observation failed")
    boot_id = boot.stdout.strip().lower()
    if _BOOT_ID.fullmatch(boot_id) is None:
        raise PhoneTargetUnavailable("phone boot identity is malformed")
    value = completed.stdout.strip()
    if value not in {"", "0", "1"}:
        raise PhoneTargetUnavailable("phone boot-completed state is malformed")
    return BootState(boot_id=boot_id, boot_completed=value == "1")


def _fixed_reboot_script() -> bytes:
    target = f"{_ROOT}/releases/{_STAGE4_RELEASE}"
    current = f"{_ROOT}/current"
    return f'''set -eu
TARGET='{target}'
CURRENT='{current}'

if [ "$(id -u)" != "0" ]; then
  printf 'stage4_reboot=precondition_refused\\n'
  exit 40
fi
if [ "$(readlink -f "$CURRENT" 2>/dev/null || true)" != "$TARGET" ]; then
  printf 'stage4_reboot=precondition_refused\\n'
  exit 40
fi
if [ "$(getprop sys.boot_completed 2>/dev/null || true)" != "1" ]; then
  printf 'stage4_reboot=precondition_refused\\n'
  exit 40
fi
for tool in nohup sh sleep setprop; do
  command -v "$tool" >/dev/null 2>&1 || {{
    printf 'stage4_reboot=precondition_refused\\n'
    exit 40
  }}
done

nohup sh -c 'sleep 2; setprop sys.powerctl reboot' </dev/null >/dev/null 2>&1 &
printf 'stage4_reboot=mutation_dispatched\\n'
exit 0
'''.encode("utf-8")


def _dispatch_fixed_reboot(serial: str) -> RebootDispatch:
    result: RootScriptResult = phone_target._run_root_script(
        serial,
        _fixed_reboot_script(),
        timeout=_ROOT_SCRIPT_TIMEOUT_SECONDS,
    )
    if result.status != "completed":
        return RebootDispatch("UNKNOWN", "MUTATION_OUTCOME_UNKNOWN", None)
    if result.stderr != b"":
        return RebootDispatch("UNKNOWN", "MUTATION_OUTPUT_AMBIGUOUS", None)
    if result.returncode == 40 and result.stdout == b"stage4_reboot=precondition_refused\n":
        return RebootDispatch("REFUSED", "MUTATION_PRECONDITION_CHANGED", False)
    if result.returncode == 0 and result.stdout == b"stage4_reboot=mutation_dispatched\n":
        return RebootDispatch("DISPATCHED", None, True)
    return RebootDispatch("UNKNOWN", "MUTATION_OUTPUT_AMBIGUOUS", None)


def _wait_for_new_boot(serial: str, *, previous_boot_id: str) -> BootState | None:
    deadline = time.monotonic() + _REBOOT_OBSERVATION_BOUND_SECONDS
    while True:
        try:
            state = _observe_boot_state(serial)
        except PhoneTargetUnavailable:
            state = None
        if state is not None and state.boot_id != previous_boot_id and state.boot_completed:
            return state
        if time.monotonic() >= deadline:
            return None
        time.sleep(2)


def _observe_post_operational(
    serial: str,
    *,
    admin_token: str,
) -> RuntimeOperationalObservation:
    deadline = time.monotonic() + _POST_OPERATIONAL_BOUND_SECONDS
    last_value: RuntimeOperationalObservation | None = None
    last_error: PhoneTargetUnavailable | None = None
    while True:
        try:
            value = observe_runtime_operational_health(serial, admin_token=admin_token)
            last_value = value
            last_error = None
            if _phone_local_ready(value):
                return value
        except PhoneTargetUnavailable as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            if last_value is not None:
                return last_value
            if last_error is not None:
                raise last_error
            raise PhoneTargetUnavailable("post-reboot operational observation produced no result")
        time.sleep(1)


def _classify_post_operational_unavailable(exc: PhoneTargetUnavailable) -> PostOperationalFailure:
    if isinstance(exc, RuntimeOperationalObservationUnavailable):
        return PostOperationalFailure(
            failure_code="POST_OPERATIONAL_OBSERVER_UNAVAILABLE",
            failure_domain="RUNTIME_OPERATIONAL_OBSERVER",
            failure_phase=exc.last_phase,
        )
    if isinstance(exc, RuntimeOperationalOutputValidationFailure):
        return PostOperationalFailure(
            failure_code="POST_OPERATIONAL_OUTPUT_INVALID",
            failure_domain="RUNTIME_OPERATIONAL_OUTPUT",
            failure_phase=exc.code.value,
        )
    if isinstance(exc, phone_target.PhoneTargetDiagnosticFailure):
        return PostOperationalFailure(
            failure_code="POST_PHONE_TRANSPORT_UNAVAILABLE",
            failure_domain="PHONE_TRANSPORT",
            failure_phase=exc.phase.value,
        )
    return PostOperationalFailure(
        failure_code="POST_PHONE_TARGET_UNAVAILABLE",
        failure_domain="PHONE_TARGET",
        failure_phase=None,
    )


def _set_terminal(
    payload: dict[str, object],
    *,
    classification: str,
    failure_code: str | None,
    phone_access: bool,
    phone_mutation: bool | None,
    phone_reboot: bool | None,
) -> int:
    payload["classification"] = classification
    if failure_code is None:
        payload.pop("failure_code", None)
    else:
        payload["failure_code"] = failure_code
    payload["safety"] = _safety(
        phone_access=phone_access,
        phone_mutation=phone_mutation,
        phone_reboot=phone_reboot,
    )
    return 0 if classification == "REBOOTED" else 2


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
    phone_reboot: bool | None = False

    try:
        admitted = resolve_release(tag=_STAGE4_RELEASE, target=_STAGE4_TARGET)
    except ReleaseAdmissionError:
        code = _set_terminal(
            payload,
            classification="REFUSED",
            failure_code="RELEASE_ADMISSION_FAILED",
            phone_access=False,
            phone_mutation=False,
            phone_reboot=False,
        )
        _write(output, payload)
        return code

    payload["release_source_sha"] = admitted.identity.source_sha
    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
    admin_token = os.environ.get("MOBILE_PROXY_ADMIN_TOKEN", "")
    if not serial or len(binding_key) < 32 or not admin_token:
        code = _set_terminal(
            payload,
            classification="UNKNOWN",
            failure_code="PHONE_BINDING_UNAVAILABLE",
            phone_access=False,
            phone_mutation=False,
            phone_reboot=False,
        )
        _write(output, payload)
        return code

    try:
        with tempfile.TemporaryDirectory(prefix="stage4-phone-reboot-") as raw:
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
                    phone_reboot=False,
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
                    phone_reboot=False,
                )
                _write(output, payload)
                return code

            try:
                pre_operational = observe_runtime_operational_health(serial, admin_token=admin_token)
                pre_boot = _observe_boot_state(serial)
            except PhoneTargetUnavailable:
                payload["preconditions"] = {"exact_product_release": _release_condition(pre_release)}
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code="PRE_PHONE_STATE_UNKNOWN",
                    phone_access=True,
                    phone_mutation=False,
                    phone_reboot=False,
                )
                _write(output, payload)
                return code

            payload["preconditions"] = {
                "exact_product_release": _release_condition(pre_release),
                "operational": _bounded_operational(pre_operational),
                "boot": {
                    "boot_completed": pre_boot.boot_completed,
                    "boot_identity_observed": True,
                },
            }
            if not pre_boot.boot_completed:
                code = _set_terminal(
                    payload,
                    classification="REFUSED",
                    failure_code="PRE_ANDROID_BOOT_NOT_COMPLETE",
                    phone_access=True,
                    phone_mutation=False,
                    phone_reboot=False,
                )
                _write(output, payload)
                return code
            if not _phone_local_ready(pre_operational):
                code = _set_terminal(
                    payload,
                    classification="REFUSED",
                    failure_code="PRE_PHONE_LOCAL_NOT_READY",
                    phone_access=True,
                    phone_mutation=False,
                    phone_reboot=False,
                )
                _write(output, payload)
                return code

            dispatch = _dispatch_fixed_reboot(serial)
            payload["mutation"] = {
                "attempts": 1,
                "outcome": dispatch.outcome,
                "dispatched": dispatch.dispatched,
                "ambiguous_stop": dispatch.outcome == "UNKNOWN",
            }
            phone_mutation = dispatch.dispatched
            phone_reboot = None if dispatch.outcome == "UNKNOWN" else False

            if dispatch.outcome == "UNKNOWN":
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code=dispatch.failure_code,
                    phone_access=True,
                    phone_mutation=phone_mutation,
                    phone_reboot=phone_reboot,
                )
                _write(output, payload)
                return code
            if dispatch.outcome == "REFUSED":
                code = _set_terminal(
                    payload,
                    classification="REFUSED",
                    failure_code=dispatch.failure_code,
                    phone_access=True,
                    phone_mutation=False,
                    phone_reboot=False,
                )
                _write(output, payload)
                return code

            post_boot = _wait_for_new_boot(serial, previous_boot_id=pre_boot.boot_id)
            if post_boot is None:
                payload["mutation"] = {
                    "attempts": 1,
                    "outcome": "UNKNOWN",
                    "dispatched": True,
                    "ambiguous_stop": True,
                }
                payload["boot_rehydration"] = {
                    "expected": True,
                    "boot_identity_changed": None,
                    "boot_completed": None,
                    "runtime_stack_ready": None,
                    "service_script_reinvoked": False,
                    "reboot_observation_bound_seconds": _REBOOT_OBSERVATION_BOUND_SECONDS,
                    "post_operational_bound_seconds": _POST_OPERATIONAL_BOUND_SECONDS,
                }
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code="REBOOT_OUTCOME_NOT_OBSERVED_WITHIN_BOUND",
                    phone_access=True,
                    phone_mutation=True,
                    phone_reboot=None,
                )
                _write(output, payload)
                return code

            phone_reboot = True
            payload["mutation"] = {
                "attempts": 1,
                "outcome": "REBOOTED",
                "dispatched": True,
                "ambiguous_stop": False,
            }
            payload["boot_rehydration"] = {
                "expected": True,
                "boot_identity_changed": True,
                "boot_completed": post_boot.boot_completed,
                "runtime_stack_ready": False,
                "service_script_reinvoked": False,
                "reboot_observation_bound_seconds": _REBOOT_OBSERVATION_BOUND_SECONDS,
                "post_operational_bound_seconds": _POST_OPERATIONAL_BOUND_SECONDS,
            }

            try:
                post_operational = _observe_post_operational(serial, admin_token=admin_token)
            except PhoneTargetUnavailable as exc:
                diagnostic = _classify_post_operational_unavailable(exc)
                payload["failure_domain"] = diagnostic.failure_domain
                if diagnostic.failure_phase is not None:
                    payload["failure_phase"] = diagnostic.failure_phase
                payload["postconditions"] = {
                    "exact_product_release": None,
                    "operational": None,
                    "boot": {
                        "boot_identity_changed": True,
                        "boot_completed": post_boot.boot_completed,
                    },
                }
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code=diagnostic.failure_code,
                    phone_access=True,
                    phone_mutation=True,
                    phone_reboot=True,
                )
                _write(output, payload)
                return code

            try:
                post_release = observe_exact_phone_release(
                    serial=serial,
                    binding_key=binding_key,
                    admitted=admitted,
                    materialized=materialized,
                )
            except PhoneTargetUnavailable:
                payload["postconditions"] = {
                    "exact_product_release": None,
                    "operational": _bounded_operational(post_operational),
                    "boot": {
                        "boot_identity_changed": True,
                        "boot_completed": post_boot.boot_completed,
                    },
                }
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code="POST_RELEASE_STATE_UNKNOWN",
                    phone_access=True,
                    phone_mutation=True,
                    phone_reboot=True,
                )
                _write(output, payload)
                return code
            payload["boot_rehydration"] = {
                "expected": True,
                "boot_identity_changed": True,
                "boot_completed": post_boot.boot_completed,
                "runtime_stack_ready": _phone_local_ready(post_operational),
                "service_script_reinvoked": False,
                "reboot_observation_bound_seconds": _REBOOT_OBSERVATION_BOUND_SECONDS,
                "post_operational_bound_seconds": _POST_OPERATIONAL_BOUND_SECONDS,
            }
            payload["postconditions"] = {
                "exact_product_release": _release_condition(post_release),
                "operational": _bounded_operational(post_operational),
                "boot": {
                    "boot_identity_changed": True,
                    "boot_completed": post_boot.boot_completed,
                },
            }

            if post_release.classification == "UNKNOWN":
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code="POST_RELEASE_STATE_UNKNOWN",
                    phone_access=True,
                    phone_mutation=True,
                    phone_reboot=True,
                )
            elif not post_release.desired:
                code = _set_terminal(
                    payload,
                    classification="FAILED",
                    failure_code="POST_RELEASE_STATE_NOT_EXACT",
                    phone_access=True,
                    phone_mutation=True,
                    phone_reboot=True,
                )
            elif not _phone_local_ready(post_operational):
                code = _set_terminal(
                    payload,
                    classification="FAILED",
                    failure_code="POST_PHONE_LOCAL_NOT_READY",
                    phone_access=True,
                    phone_mutation=True,
                    phone_reboot=True,
                )
            else:
                code = _set_terminal(
                    payload,
                    classification="REBOOTED",
                    failure_code=None,
                    phone_access=True,
                    phone_mutation=True,
                    phone_reboot=True,
                )
            _write(output, payload)
            return code
    except PhoneRuntimeRefused:
        code = _set_terminal(
            payload,
            classification="REFUSED",
            failure_code="RUNTIME_MATERIALIZATION_FAILED",
            phone_access=phone_access,
            phone_mutation=phone_mutation,
            phone_reboot=phone_reboot,
        )
    except PhoneTargetUnavailable:
        code = _set_terminal(
            payload,
            classification="UNKNOWN",
            failure_code="PHONE_TARGET_UNAVAILABLE",
            phone_access=phone_access,
            phone_mutation=phone_mutation,
            phone_reboot=phone_reboot,
        )
    _write(output, payload)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--source-comment-id", required=True, type=int)
    parser.add_argument("--product-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.target != _STAGE4_TARGET or args.release_tag != _STAGE4_RELEASE:
        raise SystemExit("Stage 4 phone reboot exercise is bound to phone-production@v0.1.7")
    if _SHA.fullmatch(args.controller_revision) is None or os.environ.get("GITHUB_SHA") != args.controller_revision:
        raise SystemExit("phone reboot exercise controller revision differs")
    if args.source_comment_id <= 0:
        raise SystemExit("phone reboot exercise source comment id is invalid")

    return exercise(
        controller_revision=args.controller_revision,
        source_comment_id=args.source_comment_id,
        product_root=args.product_root,
        runtime_manifest=args.runtime_manifest,
        output=args.output,
    )


if __name__ == "__main__":
    raise SystemExit(main())