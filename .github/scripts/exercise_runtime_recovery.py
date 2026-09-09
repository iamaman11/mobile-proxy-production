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
_SHA = re.compile(r"[0-9a-f]{40}")
_ROOT = "/data/adb/mobile-proxy-node"
_RECOVERY_BOUND_SECONDS = 15
_ROOT_SCRIPT_TIMEOUT_SECONDS = 25
_SCHEMA = "stage4-runtime-recovery-exercise.v1"


@dataclass(frozen=True)
class MutationResult:
    outcome: str
    failure_code: str | None
    dispatched: bool | None
    recovery_observed: bool | None
    owner_generation_stable: bool | None


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safety(*, phone_access: bool, phone_mutation: bool | None) -> dict[str, object]:
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
        "phone_reboot_performed": False,
        "arbitrary_process_selector_accepted": False,
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
            "id": "host-daemon-sigterm-once",
            "critical_child": "host-daemon",
            "signal": "TERM",
            "automatic_recovery_owner": "runtime-supervisor",
            "arbitrary_process_selector": False,
            "automatic_recovery_bound_seconds": _RECOVERY_BOUND_SECONDS,
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
        "automatic_recovery": {
            "expected": True,
            "observed": False,
            "generation_changed": False,
            "owner_generation_stable": False,
            "bound_seconds": _RECOVERY_BOUND_SECONDS,
        },
        "postconditions": None,
        "safety": _safety(phone_access=False, phone_mutation=False),
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


def _fixed_recovery_script() -> bytes:
    target = f"{_ROOT}/releases/{_STAGE4_RELEASE}"
    current = f"{_ROOT}/current"
    supervisor = f"{target}/bin/runtime-supervisor"
    host = f"{target}/bin/host-daemon"
    return f'''set -eu
ROOT='{_ROOT}'
TARGET='{target}'
CURRENT='{current}'
SUPERVISOR='{supervisor}'
HOST='{host}'

find_unique_exact_exe() {{
  expected="$1"
  found=""
  count=0
  for proc in /proc/[0-9]*; do
    [ -e "$proc/exe" ] || continue
    actual="$(readlink -f "$proc/exe" 2>/dev/null || true)"
    [ "$actual" = "$expected" ] || continue
    count=$((count + 1))
    found="${{proc#/proc/}}"
  done
  [ "$count" -eq 1 ] || return 1
  printf '%s' "$found"
}}

process_generation() {{
  pid="$1"
  [ -r "/proc/$pid/stat" ] || return 1
  set -- $(cat "/proc/$pid/stat" 2>/dev/null) || return 1
  [ "$#" -ge 22 ] || return 1
  printf '%s:%s' "$1" "${{22}}"
}}

process_parent() {{
  pid="$1"
  [ -r "/proc/$pid/status" ] || return 1
  while read -r key value rest; do
    [ "$key" = 'PPid:' ] || continue
    case "$value" in
      ''|*[!0-9]*) return 1 ;;
    esac
    printf '%s' "$value"
    return 0
  done < "/proc/$pid/status"
  return 1
}}

if [ "$(readlink -f "$CURRENT" 2>/dev/null || true)" != "$TARGET" ]; then
  printf 'stage4_recovery=precondition_refused\\n'
  exit 40
fi
supervisor_pid="$(find_unique_exact_exe "$SUPERVISOR")" || {{
  printf 'stage4_recovery=precondition_refused\\n'
  exit 40
}}
host_pid="$(find_unique_exact_exe "$HOST")" || {{
  printf 'stage4_recovery=precondition_refused\\n'
  exit 40
}}
supervisor_generation="$(process_generation "$supervisor_pid")" || {{
  printf 'stage4_recovery=precondition_refused\\n'
  exit 40
}}
host_generation="$(process_generation "$host_pid")" || {{
  printf 'stage4_recovery=precondition_refused\\n'
  exit 40
}}
host_parent="$(process_parent "$host_pid")" || {{
  printf 'stage4_recovery=precondition_refused\\n'
  exit 40
}}
if [ "$host_parent" != "$supervisor_pid" ]; then
  printf 'stage4_recovery=precondition_refused\\n'
  exit 40
fi

kill -TERM "$host_pid"
printf 'stage4_recovery=mutation_dispatched\\n'

i=0
while [ "$i" -lt {_RECOVERY_BOUND_SECONDS} ]; do
  sleep 1
  current_supervisor="$(find_unique_exact_exe "$SUPERVISOR" || true)"
  if [ -z "$current_supervisor" ]; then
    printf 'stage4_recovery=owner_changed\\n'
    exit 42
  fi
  current_supervisor_generation="$(process_generation "$current_supervisor" || true)"
  if [ -z "$current_supervisor_generation" ] || [ "$current_supervisor_generation" != "$supervisor_generation" ]; then
    printf 'stage4_recovery=owner_changed\\n'
    exit 42
  fi
  current_host="$(find_unique_exact_exe "$HOST" || true)"
  if [ -n "$current_host" ]; then
    current_host_generation="$(process_generation "$current_host" || true)"
    current_host_parent="$(process_parent "$current_host" || true)"
    if [ -n "$current_host_parent" ] && [ "$current_host_parent" != "$supervisor_pid" ]; then
      printf 'stage4_recovery=owner_changed\\n'
      exit 42
    fi
    if [ -n "$current_host_generation" ] && [ "$current_host_parent" = "$supervisor_pid" ] && [ "$current_host_generation" != "$host_generation" ]; then
      printf 'stage4_recovery=generation_changed\\n'
      exit 0
    fi
  fi
  i=$((i + 1))
done
exit 41
'''.encode("utf-8")


def _run_fixed_mutation(serial: str) -> MutationResult:
    result: RootScriptResult = phone_target._run_root_script(
        serial,
        _fixed_recovery_script(),
        timeout=_ROOT_SCRIPT_TIMEOUT_SECONDS,
    )
    if result.status != "completed":
        return MutationResult("UNKNOWN", "MUTATION_OUTCOME_UNKNOWN", None, None, None)
    if result.stderr != b"":
        return MutationResult("UNKNOWN", "MUTATION_OUTPUT_AMBIGUOUS", None, None, None)
    if result.returncode == 40 and result.stdout == b"stage4_recovery=precondition_refused\n":
        return MutationResult("REFUSED", "MUTATION_PRECONDITION_CHANGED", False, False, None)
    dispatched = b"stage4_recovery=mutation_dispatched\n" in result.stdout
    if result.returncode == 0 and result.stdout == (
        b"stage4_recovery=mutation_dispatched\n"
        b"stage4_recovery=generation_changed\n"
    ):
        return MutationResult("RECOVERED", None, True, True, True)
    if result.returncode == 41 and result.stdout == b"stage4_recovery=mutation_dispatched\n":
        return MutationResult("NOT_RECOVERED", "AUTOMATIC_RECOVERY_TIMEOUT", True, False, True)
    if result.returncode == 42 and result.stdout == (
        b"stage4_recovery=mutation_dispatched\n"
        b"stage4_recovery=owner_changed\n"
    ):
        return MutationResult("OWNER_CHANGED", "AUTOMATIC_RECOVERY_OWNER_CHANGED", True, False, False)
    return MutationResult(
        "UNKNOWN",
        "MUTATION_OUTPUT_AMBIGUOUS",
        True if dispatched else None,
        None,
        None,
    )


def _set_terminal(
    payload: dict[str, object],
    *,
    classification: str,
    failure_code: str | None,
    phone_access: bool,
    phone_mutation: bool | None,
) -> int:
    payload["classification"] = classification
    if failure_code is None:
        payload.pop("failure_code", None)
    else:
        payload["failure_code"] = failure_code
    payload["safety"] = _safety(phone_access=phone_access, phone_mutation=phone_mutation)
    return 0 if classification == "RECOVERED" else 2


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

    try:
        admitted = resolve_release(tag=_STAGE4_RELEASE, target=_STAGE4_TARGET)
    except ReleaseAdmissionError:
        code = _set_terminal(
            payload,
            classification="REFUSED",
            failure_code="RELEASE_ADMISSION_FAILED",
            phone_access=False,
            phone_mutation=False,
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
        )
        _write(output, payload)
        return code

    try:
        with tempfile.TemporaryDirectory(prefix="stage4-runtime-recovery-") as raw:
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
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code="PRE_RELEASE_STATE_UNKNOWN",
                    phone_access=True,
                    phone_mutation=False,
                )
                payload["preconditions"] = {"exact_product_release": _release_condition(pre_release)}
                _write(output, payload)
                return code
            if not pre_release.desired:
                code = _set_terminal(
                    payload,
                    classification="REFUSED",
                    failure_code="PRE_RELEASE_STATE_NOT_EXACT",
                    phone_access=True,
                    phone_mutation=False,
                )
                payload["preconditions"] = {"exact_product_release": _release_condition(pre_release)}
                _write(output, payload)
                return code

            try:
                pre_operational = observe_runtime_operational_health(serial, admin_token=admin_token)
            except PhoneTargetUnavailable:
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code="PRE_OPERATIONAL_STATE_UNKNOWN",
                    phone_access=True,
                    phone_mutation=False,
                )
                payload["preconditions"] = {"exact_product_release": _release_condition(pre_release)}
                _write(output, payload)
                return code

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
                )
                _write(output, payload)
                return code

            mutation = _run_fixed_mutation(serial)
            payload["mutation"] = {
                "attempts": 1,
                "outcome": mutation.outcome,
                "dispatched": mutation.dispatched,
                "ambiguous_stop": mutation.outcome == "UNKNOWN",
            }
            payload["automatic_recovery"] = {
                "expected": True,
                "observed": mutation.recovery_observed,
                "generation_changed": mutation.recovery_observed,
                "owner_generation_stable": mutation.owner_generation_stable,
                "bound_seconds": _RECOVERY_BOUND_SECONDS,
            }
            phone_mutation = mutation.dispatched

            if mutation.outcome == "UNKNOWN":
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code=mutation.failure_code,
                    phone_access=True,
                    phone_mutation=phone_mutation,
                )
                _write(output, payload)
                return code
            if mutation.outcome == "REFUSED":
                code = _set_terminal(
                    payload,
                    classification="REFUSED",
                    failure_code=mutation.failure_code,
                    phone_access=True,
                    phone_mutation=False,
                )
                _write(output, payload)
                return code

            post_release = observe_exact_phone_release(
                serial=serial,
                binding_key=binding_key,
                admitted=admitted,
                materialized=materialized,
            )
            try:
                post_operational = observe_runtime_operational_health(serial, admin_token=admin_token)
            except PhoneTargetUnavailable:
                payload["postconditions"] = {
                    "exact_product_release": _release_condition(post_release),
                    "operational": None,
                }
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code="POST_OPERATIONAL_STATE_UNKNOWN",
                    phone_access=True,
                    phone_mutation=True,
                )
                _write(output, payload)
                return code

            payload["postconditions"] = {
                "exact_product_release": _release_condition(post_release),
                "operational": _bounded_operational(post_operational),
            }

            if post_release.classification == "UNKNOWN":
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code="POST_RELEASE_STATE_UNKNOWN",
                    phone_access=True,
                    phone_mutation=True,
                )
            elif not post_release.desired:
                code = _set_terminal(
                    payload,
                    classification="FAILED",
                    failure_code="POST_RELEASE_STATE_NOT_EXACT",
                    phone_access=True,
                    phone_mutation=True,
                )
            elif not _phone_local_ready(post_operational):
                code = _set_terminal(
                    payload,
                    classification="FAILED",
                    failure_code="POST_PHONE_LOCAL_NOT_READY",
                    phone_access=True,
                    phone_mutation=True,
                )
            elif mutation.outcome != "RECOVERED":
                code = _set_terminal(
                    payload,
                    classification="FAILED",
                    failure_code=mutation.failure_code or "AUTOMATIC_RECOVERY_NOT_PROVEN",
                    phone_access=True,
                    phone_mutation=True,
                )
            else:
                code = _set_terminal(
                    payload,
                    classification="RECOVERED",
                    failure_code=None,
                    phone_access=True,
                    phone_mutation=True,
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
        )
    except PhoneTargetUnavailable:
        code = _set_terminal(
            payload,
            classification="UNKNOWN",
            failure_code="PHONE_TARGET_UNAVAILABLE",
            phone_access=phone_access,
            phone_mutation=phone_mutation,
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
        raise SystemExit("Stage 4 runtime recovery exercise is bound to phone-production@v0.1.7")
    if _SHA.fullmatch(args.controller_revision) is None or os.environ.get("GITHUB_SHA") != args.controller_revision:
        raise SystemExit("runtime recovery exercise controller revision differs")
    if args.source_comment_id <= 0:
        raise SystemExit("runtime recovery exercise source comment id is invalid")

    return exercise(
        controller_revision=args.controller_revision,
        source_comment_id=args.source_comment_id,
        product_root=args.product_root,
        runtime_manifest=args.runtime_manifest,
        output=args.output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
