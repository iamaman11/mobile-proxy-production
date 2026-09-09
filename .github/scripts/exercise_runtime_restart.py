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
    observe_runtime_operational_health,
)

_STAGE4_TARGET = "phone-production"
_STAGE4_RELEASE = "v0.1.7"
_SHA = re.compile(r"[0-9a-f]{40}")
_ROOT = "/data/adb/mobile-proxy-node"
_RESTART_BOUND_SECONDS = 30
_ROOT_SCRIPT_TIMEOUT_SECONDS = 40
_POST_OBSERVATION_BOUND_SECONDS = 15
_SCHEMA = "stage4-runtime-restart-exercise.v1"


@dataclass(frozen=True)
class RestartResult:
    outcome: str
    failure_code: str | None
    dispatched: bool | None
    watchdog_generation_stable: bool | None
    supervisor_generation_changed: bool | None
    host_generation_changed: bool | None
    sing_box_generation_changed: bool | None
    ownership_reestablished: bool | None


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safety(
    *,
    phone_access: bool,
    phone_mutation: bool | None,
    runtime_restart: bool | None,
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
        "runtime_restart_performed": runtime_restart,
        "phone_reboot_performed": False,
        "bootstrap_reinvoked": False,
        "service_script_reinvoked": False,
        "arbitrary_process_selector_accepted": False,
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
            "id": "runtime-supervisor-sigterm-once",
            "restart_target": "runtime-supervisor",
            "signal": "TERM",
            "automatic_rehydration_owner": "runtime-watchdog",
            "arbitrary_process_selector": False,
            "bootstrap_reinvoked": False,
            "automatic_rehydration_bound_seconds": _RESTART_BOUND_SECONDS,
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
        "automatic_rehydration": {
            "expected": True,
            "observed": False,
            "watchdog_generation_stable": False,
            "supervisor_generation_changed": False,
            "host_daemon_generation_changed": False,
            "sing_box_generation_changed": False,
            "ownership_reestablished": False,
            "bound_seconds": _RESTART_BOUND_SECONDS,
        },
        "postconditions": None,
        "safety": _safety(
            phone_access=False,
            phone_mutation=False,
            runtime_restart=False,
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


def _fixed_restart_script() -> bytes:
    target = f"{_ROOT}/releases/{_STAGE4_RELEASE}"
    current = f"{_ROOT}/current"
    supervisor = f"{target}/bin/runtime-supervisor"
    host = f"{target}/bin/host-daemon"
    sing_box = f"{target}/bin/sing-box"
    watchdog_pidfile = f"{_ROOT}/logs/runtime-watchdog.pid"
    watchdog_script = f"{_ROOT}/logs/runtime-watchdog.sh"
    return f'''set -eu
ROOT='{_ROOT}'
TARGET='{target}'
CURRENT='{current}'
SUPERVISOR='{supervisor}'
HOST='{host}'
SING_BOX='{sing_box}'
WATCHDOG_PIDFILE='{watchdog_pidfile}'
WATCHDOG_SCRIPT='{watchdog_script}'

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

process_starttime() {{
  pid="$1"
  [ -r "/proc/$pid/stat" ] || return 1
  set -- $(cat "/proc/$pid/stat")
  [ "$#" -ge 22 ] || return 1
  printf '%s' "$22"
}}

process_parent() {{
  pid="$1"
  [ -r "/proc/$pid/stat" ] || return 1
  set -- $(cat "/proc/$pid/stat")
  [ "$#" -ge 4 ] || return 1
  printf '%s' "$4"
}}

process_generation() {{
  pid="$1"
  start="$(process_starttime "$pid")" || return 1
  printf '%s:%s' "$pid" "$start"
}}

watchdog_pid() {{
  [ -r "$WATCHDOG_PIDFILE" ] || return 1
  pid="$(cat "$WATCHDOG_PIDFILE" 2>/dev/null || true)"
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ -r "/proc/$pid/cmdline" ] || return 1
  cmdline="$(tr '\\000' ' ' < "/proc/$pid/cmdline")"
  [ "$cmdline" = "sh $WATCHDOG_SCRIPT $CURRENT " ] || return 1
  printf '%s' "$pid"
}}

if [ "$(readlink -f "$CURRENT" 2>/dev/null || true)" != "$TARGET" ]; then
  printf 'stage4_restart=precondition_refused\\n'
  exit 40
fi
watchdog="$(watchdog_pid)" || {{
  printf 'stage4_restart=precondition_refused\\n'
  exit 40
}}
watchdog_generation="$(process_generation "$watchdog")" || {{
  printf 'stage4_restart=precondition_refused\\n'
  exit 40
}}
supervisor_pid="$(find_unique_exact_exe "$SUPERVISOR")" || {{
  printf 'stage4_restart=precondition_refused\\n'
  exit 40
}}
host_pid="$(find_unique_exact_exe "$HOST")" || {{
  printf 'stage4_restart=precondition_refused\\n'
  exit 40
}}
sing_box_pid="$(find_unique_exact_exe "$SING_BOX")" || {{
  printf 'stage4_restart=precondition_refused\\n'
  exit 40
}}
[ "$(process_parent "$supervisor_pid")" = "$watchdog" ] || {{
  printf 'stage4_restart=precondition_refused\\n'
  exit 40
}}
[ "$(process_parent "$host_pid")" = "$supervisor_pid" ] || {{
  printf 'stage4_restart=precondition_refused\\n'
  exit 40
}}
[ "$(process_parent "$sing_box_pid")" = "$supervisor_pid" ] || {{
  printf 'stage4_restart=precondition_refused\\n'
  exit 40
}}
supervisor_generation="$(process_generation "$supervisor_pid")" || {{
  printf 'stage4_restart=precondition_refused\\n'
  exit 40
}}
host_generation="$(process_generation "$host_pid")" || {{
  printf 'stage4_restart=precondition_refused\\n'
  exit 40
}}
sing_box_generation="$(process_generation "$sing_box_pid")" || {{
  printf 'stage4_restart=precondition_refused\\n'
  exit 40
}}

kill -TERM "$supervisor_pid"
printf 'stage4_restart=mutation_dispatched\\n'

i=0
while [ "$i" -lt {_RESTART_BOUND_SECONDS} ]; do
  sleep 1
  current_watchdog="$(watchdog_pid || true)"
  if [ -z "$current_watchdog" ]; then
    printf 'stage4_restart=watchdog_changed\\n'
    exit 42
  fi
  current_watchdog_generation="$(process_generation "$current_watchdog" || true)"
  if [ "$current_watchdog_generation" != "$watchdog_generation" ]; then
    printf 'stage4_restart=watchdog_changed\\n'
    exit 42
  fi

  current_supervisor="$(find_unique_exact_exe "$SUPERVISOR" || true)"
  if [ -z "$current_supervisor" ]; then
    i=$((i + 1))
    continue
  fi
  current_supervisor_generation="$(process_generation "$current_supervisor" || true)"
  if [ -z "$current_supervisor_generation" ] || [ "$current_supervisor_generation" = "$supervisor_generation" ]; then
    i=$((i + 1))
    continue
  fi
  if [ "$(process_parent "$current_supervisor" || true)" != "$current_watchdog" ]; then
    printf 'stage4_restart=ownership_changed\\n'
    exit 43
  fi

  current_host="$(find_unique_exact_exe "$HOST" || true)"
  current_sing_box="$(find_unique_exact_exe "$SING_BOX" || true)"
  if [ -z "$current_host" ] || [ -z "$current_sing_box" ]; then
    i=$((i + 1))
    continue
  fi
  if [ "$(process_parent "$current_host" || true)" != "$current_supervisor" ] || [ "$(process_parent "$current_sing_box" || true)" != "$current_supervisor" ]; then
    printf 'stage4_restart=ownership_changed\\n'
    exit 43
  fi
  current_host_generation="$(process_generation "$current_host" || true)"
  current_sing_box_generation="$(process_generation "$current_sing_box" || true)"
  if [ -z "$current_host_generation" ] || [ -z "$current_sing_box_generation" ]; then
    i=$((i + 1))
    continue
  fi
  if [ "$current_host_generation" = "$host_generation" ] || [ "$current_sing_box_generation" = "$sing_box_generation" ]; then
    i=$((i + 1))
    continue
  fi

  printf 'stage4_restart=stack_rehydrated\\n'
  exit 0

done
exit 41
'''.encode("utf-8")


def _run_fixed_restart(serial: str) -> RestartResult:
    result: RootScriptResult = phone_target._run_root_script(
        serial,
        _fixed_restart_script(),
        timeout=_ROOT_SCRIPT_TIMEOUT_SECONDS,
    )
    if result.status != "completed":
        return RestartResult("UNKNOWN", "MUTATION_OUTCOME_UNKNOWN", None, None, None, None, None, None)
    if result.stderr != b"":
        return RestartResult("UNKNOWN", "MUTATION_OUTPUT_AMBIGUOUS", None, None, None, None, None, None)
    if result.returncode == 40 and result.stdout == b"stage4_restart=precondition_refused\n":
        return RestartResult("REFUSED", "MUTATION_PRECONDITION_CHANGED", False, None, None, None, None, None)
    dispatched = b"stage4_restart=mutation_dispatched\n" in result.stdout
    if result.returncode == 0 and result.stdout == (
        b"stage4_restart=mutation_dispatched\n"
        b"stage4_restart=stack_rehydrated\n"
    ):
        return RestartResult("RESTARTED", None, True, True, True, True, True, True)
    if result.returncode == 41 and result.stdout == b"stage4_restart=mutation_dispatched\n":
        return RestartResult("NOT_REHYDRATED", "AUTOMATIC_REHYDRATION_TIMEOUT", True, True, None, None, None, None)
    if result.returncode == 42 and result.stdout == (
        b"stage4_restart=mutation_dispatched\n"
        b"stage4_restart=watchdog_changed\n"
    ):
        return RestartResult("WATCHDOG_CHANGED", "AUTOMATIC_REHYDRATION_OWNER_CHANGED", True, False, None, None, None, False)
    if result.returncode == 43 and result.stdout == (
        b"stage4_restart=mutation_dispatched\n"
        b"stage4_restart=ownership_changed\n"
    ):
        return RestartResult("OWNERSHIP_CHANGED", "RUNTIME_STACK_OWNERSHIP_CHANGED", True, True, True, None, None, False)
    return RestartResult(
        "UNKNOWN",
        "MUTATION_OUTPUT_AMBIGUOUS",
        True if dispatched else None,
        None,
        None,
        None,
        None,
        None,
    )


def _observe_post_operational(
    serial: str,
    *,
    admin_token: str,
) -> RuntimeOperationalObservation:
    deadline = time.monotonic() + _POST_OBSERVATION_BOUND_SECONDS
    last_error: PhoneTargetUnavailable | None = None
    while True:
        try:
            value = observe_runtime_operational_health(serial, admin_token=admin_token)
            if _phone_local_ready(value):
                return value
        except PhoneTargetUnavailable as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            if last_error is not None:
                raise last_error
            return value
        time.sleep(1)


def _set_terminal(
    payload: dict[str, object],
    *,
    classification: str,
    failure_code: str | None,
    phone_access: bool,
    phone_mutation: bool | None,
    runtime_restart: bool | None,
) -> int:
    payload["classification"] = classification
    if failure_code is None:
        payload.pop("failure_code", None)
    else:
        payload["failure_code"] = failure_code
    payload["safety"] = _safety(
        phone_access=phone_access,
        phone_mutation=phone_mutation,
        runtime_restart=runtime_restart,
    )
    return 0 if classification == "RESTARTED" else 2


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
    runtime_restart: bool | None = False

    try:
        admitted = resolve_release(tag=_STAGE4_RELEASE, target=_STAGE4_TARGET)
    except ReleaseAdmissionError:
        code = _set_terminal(
            payload,
            classification="REFUSED",
            failure_code="RELEASE_ADMISSION_FAILED",
            phone_access=False,
            phone_mutation=False,
            runtime_restart=False,
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
            runtime_restart=False,
        )
        _write(output, payload)
        return code

    try:
        with tempfile.TemporaryDirectory(prefix="stage4-runtime-restart-") as raw:
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
                    runtime_restart=False,
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
                    runtime_restart=False,
                )
                _write(output, payload)
                return code

            try:
                pre_operational = observe_runtime_operational_health(serial, admin_token=admin_token)
            except PhoneTargetUnavailable:
                payload["preconditions"] = {"exact_product_release": _release_condition(pre_release)}
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code="PRE_OPERATIONAL_STATE_UNKNOWN",
                    phone_access=True,
                    phone_mutation=False,
                    runtime_restart=False,
                )
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
                    runtime_restart=False,
                )
                _write(output, payload)
                return code

            restart = _run_fixed_restart(serial)
            payload["mutation"] = {
                "attempts": 1,
                "outcome": restart.outcome,
                "dispatched": restart.dispatched,
                "ambiguous_stop": restart.outcome == "UNKNOWN",
            }
            payload["automatic_rehydration"] = {
                "expected": True,
                "observed": restart.outcome == "RESTARTED",
                "watchdog_generation_stable": restart.watchdog_generation_stable,
                "supervisor_generation_changed": restart.supervisor_generation_changed,
                "host_daemon_generation_changed": restart.host_generation_changed,
                "sing_box_generation_changed": restart.sing_box_generation_changed,
                "ownership_reestablished": restart.ownership_reestablished,
                "bound_seconds": _RESTART_BOUND_SECONDS,
            }
            phone_mutation = restart.dispatched
            runtime_restart = restart.dispatched

            if restart.outcome == "UNKNOWN":
                code = _set_terminal(
                    payload,
                    classification="UNKNOWN",
                    failure_code=restart.failure_code,
                    phone_access=True,
                    phone_mutation=phone_mutation,
                    runtime_restart=runtime_restart,
                )
                _write(output, payload)
                return code
            if restart.outcome == "REFUSED":
                code = _set_terminal(
                    payload,
                    classification="REFUSED",
                    failure_code=restart.failure_code,
                    phone_access=True,
                    phone_mutation=False,
                    runtime_restart=False,
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
                post_operational = _observe_post_operational(serial, admin_token=admin_token)
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
                    runtime_restart=True,
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
                    runtime_restart=True,
                )
            elif not post_release.desired:
                code = _set_terminal(
                    payload,
                    classification="FAILED",
                    failure_code="POST_RELEASE_STATE_NOT_EXACT",
                    phone_access=True,
                    phone_mutation=True,
                    runtime_restart=True,
                )
            elif not _phone_local_ready(post_operational):
                code = _set_terminal(
                    payload,
                    classification="FAILED",
                    failure_code="POST_PHONE_LOCAL_NOT_READY",
                    phone_access=True,
                    phone_mutation=True,
                    runtime_restart=True,
                )
            elif restart.outcome != "RESTARTED":
                code = _set_terminal(
                    payload,
                    classification="FAILED",
                    failure_code=restart.failure_code or "AUTOMATIC_REHYDRATION_NOT_PROVEN",
                    phone_access=True,
                    phone_mutation=True,
                    runtime_restart=True,
                )
            else:
                code = _set_terminal(
                    payload,
                    classification="RESTARTED",
                    failure_code=None,
                    phone_access=True,
                    phone_mutation=True,
                    runtime_restart=True,
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
            runtime_restart=runtime_restart,
        )
    except PhoneTargetUnavailable:
        code = _set_terminal(
            payload,
            classification="UNKNOWN",
            failure_code="PHONE_TARGET_UNAVAILABLE",
            phone_access=phone_access,
            phone_mutation=phone_mutation,
            runtime_restart=runtime_restart,
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
        raise SystemExit("Stage 4 runtime restart exercise is bound to phone-production@v0.1.7")
    if _SHA.fullmatch(args.controller_revision) is None or os.environ.get("GITHUB_SHA") != args.controller_revision:
        raise SystemExit("runtime restart exercise controller revision differs")
    if args.source_comment_id <= 0:
        raise SystemExit("runtime restart exercise source comment id is invalid")

    return exercise(
        controller_revision=args.controller_revision,
        source_comment_id=args.source_comment_id,
        product_root=args.product_root,
        runtime_manifest=args.runtime_manifest,
        output=args.output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
