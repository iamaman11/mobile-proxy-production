from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass
from enum import Enum

import phone_target

_ALLOWED_READINESS = frozenset(
    {
        "booting",
        "waiting_wireguard",
        "waiting_cellular",
        "starting_proxy",
        "healthy",
        "quarantined",
        "unknown",
    }
)
_ALLOWED_PROXY_STATUS = frozenset({"starting", "running", "degraded"})
_ALLOWED_TUNNEL_OWNERS = frozenset(
    {
        "stock_wireguard_bridge",
        "first_party_vpn_service",
        "first_party_reverse_tunnel",
        "first_party_android_egress",
    }
)
_ALLOWED_DEGRADATION = frozenset(
    {
        "none",
        "wireguard_path_not_ready",
        "cellular_network_unvalidated",
        "reverse_tunnel_not_ready",
        "proxy_bind_failed",
        "public_probe_failed",
        "local_probe_failed",
    }
)
_ALLOWED_REVERSE_TUNNEL_FRESHNESS = frozenset({"none", "unknown", "fresh", "stale"})
_ALLOWED_REVERSE_TUNNEL_TRANSPORT = frozenset({"none", "tcp", "quic", "tls_tcp"})
_ALLOWED_REVERSE_TUNNEL_FAILOVER_REASON = frozenset(
    {
        "none",
        "connect_timeout",
        "connect_failed",
        "authentication_failed",
        "session_closed",
        "session_error",
    }
)
_PHASE_SEQUENCE = (
    "busybox_selected",
    "process_count_start",
    "process_count_done",
    "health_transport_start",
    "health_transport_done",
    "health_parse_done",
)
_ALLOWED_PHASES = frozenset(_PHASE_SEQUENCE)
_PROCESS_COUNT_TIMEOUT_PHASE = "process_count_timeout"
_PROCESS_COUNT_NO_READABLE_PROC_PHASE = "process_count_no_readable_proc"
_PROCESS_COUNT_EXECUTION_FAILED_PHASE = "process_count_execution_failed"
_ALLOWED_FAILURE_PHASES = _ALLOWED_PHASES | frozenset(
    {
        _PROCESS_COUNT_TIMEOUT_PHASE,
        _PROCESS_COUNT_NO_READABLE_PROC_PHASE,
        _PROCESS_COUNT_EXECUTION_FAILED_PHASE,
    }
)
_PROCESS_COUNT_FAILURE_BY_RETURN_CODE = {
    31: (_PROCESS_COUNT_NO_READABLE_PROC_PHASE, "process_count_done"),
    32: (_PROCESS_COUNT_TIMEOUT_PHASE, "process_count_start"),
    33: (_PROCESS_COUNT_EXECUTION_FAILED_PHASE, "process_count_start"),
}
_PHASE_PREFIX = "stage4_phase="
_EXPECTED_TUNNEL_OWNER = "first_party_reverse_tunnel"
_HEALTH_HOST = "127.0.0.1"
_HEALTH_PORT = 8088
_MAX_PROCESS_COUNT = 8
_MAX_ADMIN_TOKEN_CHARS = 4096
_MAX_HEALTH_RESPONSE_BYTES = 16 * 1024
_PROCESS_COUNT_TIMEOUT_SECONDS = 5
_HEALTH_TRANSPORT_TIMEOUT_SECONDS = 5
_ROOT_SCRIPT_TIMEOUT_SECONDS = 15
_MALFORMED = "runtime operational observation is malformed"
_UNAVAILABLE = "runtime operational observation is unavailable"


class RuntimeOperationalObservationUnavailable(phone_target.PhoneTargetUnavailable):
    def __init__(self, message: str, *, last_phase: str | None = None) -> None:
        super().__init__(message)
        if last_phase is not None and last_phase not in _ALLOWED_FAILURE_PHASES:
            raise ValueError("runtime operational failure phase is not allowlisted")
        self.last_phase = last_phase


class RuntimeOperationalOutputFailureCode(str, Enum):
    STREAM_ENCODING = "OUTPUT_STREAM_ENCODING"
    PHASE_PROTOCOL = "OUTPUT_PHASE_PROTOCOL"
    PAYLOAD_ENCODING = "OUTPUT_PAYLOAD_ENCODING"
    PAYLOAD_STRUCTURE = "OUTPUT_PAYLOAD_STRUCTURE"
    PROCESS_COUNT = "OUTPUT_PROCESS_COUNT"
    HEALTH_AUTH = "OUTPUT_HEALTH_AUTH"
    UNAUTHENTICATED_PAYLOAD_CONTRACT = "OUTPUT_UNAUTHENTICATED_PAYLOAD_CONTRACT"
    SERVING = "OUTPUT_SERVING"
    CELLULAR_ROUTE_READY = "OUTPUT_CELLULAR_ROUTE_READY"
    PROXY_BIND_READY = "OUTPUT_PROXY_BIND_READY"
    LOCAL_SERVING_READY = "OUTPUT_LOCAL_SERVING_READY"
    READINESS_STATE = "OUTPUT_READINESS_STATE"
    PROXY_STATUS = "OUTPUT_PROXY_STATUS"
    TUNNEL_OWNER = "OUTPUT_TUNNEL_OWNER"
    DEGRADATION_REASON_CODE = "OUTPUT_DEGRADATION_REASON_CODE"
    REVERSE_TUNNEL_CONNECTED = "OUTPUT_REVERSE_TUNNEL_CONNECTED"
    REVERSE_TUNNEL_FRESHNESS = "OUTPUT_REVERSE_TUNNEL_FRESHNESS"
    REVERSE_TUNNEL_ACTIVE_TRANSPORT = "OUTPUT_REVERSE_TUNNEL_ACTIVE_TRANSPORT"
    REVERSE_TUNNEL_FAILOVER_REASON = "OUTPUT_REVERSE_TUNNEL_FAILOVER_REASON"


class RuntimeOperationalOutputValidationFailure(phone_target.PhoneTargetUnavailable):
    """Bounded observer-local classification for malformed operational output."""

    def __init__(self, code: RuntimeOperationalOutputFailureCode) -> None:
        super().__init__(_MALFORMED)
        self.code = code


def _output_invalid(
    code: RuntimeOperationalOutputFailureCode,
) -> RuntimeOperationalOutputValidationFailure:
    return RuntimeOperationalOutputValidationFailure(code)


@dataclass(frozen=True)
class RuntimeOperationalObservation:
    watchdog_count: int
    runtime_supervisor_count: int
    host_daemon_count: int
    sing_box_count: int
    health_api_authenticated: bool
    readiness_state: str
    serving: bool | None
    proxy_status: str
    cellular_route_ready: bool | None
    proxy_bind_ready: bool | None
    local_serving_ready: bool | None
    tunnel_owner: str
    degradation_reason_code: str
    reverse_tunnel_connected: bool | None
    reverse_tunnel_freshness: str
    reverse_tunnel_active_transport: str
    reverse_tunnel_failover_reason: str
    mode: str = "read_only"

    @property
    def tunnel_owner_matches_expected(self) -> bool:
        return self.tunnel_owner == _EXPECTED_TUNNEL_OWNER

    @property
    def desired(self) -> bool:
        return (
            self.watchdog_count == 1
            and self.runtime_supervisor_count == 1
            and self.host_daemon_count == 1
            and self.sing_box_count == 1
            and self.health_api_authenticated
            and self.readiness_state == "healthy"
            and self.serving is True
            and self.proxy_status == "running"
            and self.cellular_route_ready is True
            and self.proxy_bind_ready is True
            and self.local_serving_ready is True
            and self.tunnel_owner_matches_expected
            and self.degradation_reason_code == "none"
        )

    def to_bounded_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.update(
            {
                "evaluated": True,
                "desired": self.desired,
                "tunnel_owner_matches_expected": self.tunnel_owner_matches_expected,
                "localhost_only": True,
                "raw_health_json_recorded": False,
                "raw_config_recorded": False,
                "secret_values_recorded": False,
                "process_ids_recorded": False,
                "process_cmdlines_recorded": False,
                "provider_access_performed": False,
                "phone_mutation_performed": False,
            }
        )
        return value


def _validate_admin_token(raw: str) -> str:
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > _MAX_ADMIN_TOKEN_CHARS
        or any(character in raw for character in "\r\n\x00")
        or any(ord(character) < 32 for character in raw)
    ):
        raise phone_target.PhoneTargetUnavailable(_UNAVAILABLE)
    return raw


def _operational_script(admin_token: str) -> bytes:
    token = shlex.quote(_validate_admin_token(admin_token))
    return f'''set -u
ADMIN_TOKEN={token}

BB_BIN=""
if [ -x /data/adb/magisk/busybox ]; then
  BB_BIN=/data/adb/magisk/busybox
elif [ -x /debug_ramdisk/.magisk/busybox/busybox ]; then
  BB_BIN=/debug_ramdisk/.magisk/busybox/busybox
fi
[ -n "$BB_BIN" ] || exit 20
printf '{_PHASE_PREFIX}busybox_selected\\n'

TIMEOUT_STYLE=""
if "$BB_BIN" timeout 1 "$BB_BIN" true >/dev/null 2>&1; then
  TIMEOUT_STYLE=positional
elif "$BB_BIN" timeout -t 1 "$BB_BIN" true >/dev/null 2>&1; then
  TIMEOUT_STYLE=legacy
fi
[ -n "$TIMEOUT_STYLE" ] || exit 24

run_timeout() {{
  timeout_seconds="$1"
  shift
  if [ "$TIMEOUT_STYLE" = positional ]; then
    "$BB_BIN" timeout "$timeout_seconds" "$@"
  else
    "$BB_BIN" timeout -t "$timeout_seconds" "$@"
  fi
}}

WATCHDOG_NEEDLE='/data/adb/mobile-proxy-node/logs/runtime-watchdog.sh'
RUNTIME_SUPERVISOR_NEEDLE='/data/adb/mobile-proxy-node/current/bin/runtime-supervisor'
HOST_DAEMON_NEEDLE='/data/adb/mobile-proxy-node/current/bin/host-daemon'
SING_BOX_NEEDLE='/data/adb/mobile-proxy-node/current/bin/sing-box'
export BB_BIN WATCHDOG_NEEDLE RUNTIME_SUPERVISOR_NEEDLE HOST_DAEMON_NEEDLE SING_BOX_NEEDLE
printf '{_PHASE_PREFIX}process_count_start\\n'
process_counts="$(
  run_timeout {_PROCESS_COUNT_TIMEOUT_SECONDS} "$BB_BIN" sh -s <<'STAGE4_PROCESS_COUNT'
watchdog_count=0
runtime_supervisor_count=0
host_daemon_count=0
sing_box_count=0
readable_processes=0

for process_dir in /proc/[0-9]*; do
  cmdline_path="$process_dir/cmdline"
  [ -r "$cmdline_path" ] || continue
  process_args="$("$BB_BIN" tr '\\000' '\\n' < "$cmdline_path" 2>/dev/null)" || continue
  readable_processes=$((readable_processes + 1))
  process_args="
$process_args
"
  case "$process_args" in *"
$WATCHDOG_NEEDLE
"*) watchdog_count=$((watchdog_count + 1)) ;; esac
  case "$process_args" in *"
$RUNTIME_SUPERVISOR_NEEDLE
"*) runtime_supervisor_count=$((runtime_supervisor_count + 1)) ;; esac
  case "$process_args" in *"
$HOST_DAEMON_NEEDLE
"*) host_daemon_count=$((host_daemon_count + 1)) ;; esac
  case "$process_args" in *"
$SING_BOX_NEEDLE
"*) sing_box_count=$((sing_box_count + 1)) ;; esac
  process_args=''
done

[ "$readable_processes" -gt 0 ] || exit 26
printf "%s %s %s %s" \
  "$watchdog_count" "$runtime_supervisor_count" "$host_daemon_count" "$sing_box_count"
STAGE4_PROCESS_COUNT
)"
process_count_status="$?"
if [ "$process_count_status" -ne 0 ]; then
  case "$process_count_status" in
    26)
      printf '{_PHASE_PREFIX}process_count_done\\n'
      exit 31
      ;;
    124|143)
      exit 32
      ;;
    *)
      exit 33
      ;;
  esac
fi
set -- $process_counts
[ "$#" -eq 4 ] || exit 22
for process_count in "$@"; do
  case "$process_count" in ''|*[!0-9]*) exit 23 ;; esac
done
watchdog_count="$1"
runtime_supervisor_count="$2"
host_daemon_count="$3"
sing_box_count="$4"
process_counts=''
unset WATCHDOG_NEEDLE RUNTIME_SUPERVISOR_NEEDLE HOST_DAEMON_NEEDLE SING_BOX_NEEDLE
printf '{_PHASE_PREFIX}process_count_done\\n'

health_api_authenticated=false
readiness_state=unknown
serving=unknown
proxy_status=unknown
cellular_route_ready=unknown
proxy_bind_ready=unknown
local_serving_ready=unknown
tunnel_owner=unknown
degradation_reason_code=unknown
reverse_tunnel_connected=unknown
reverse_tunnel_freshness=unknown
reverse_tunnel_active_transport=unknown
reverse_tunnel_failover_reason=unknown

health_raw=""
printf '{_PHASE_PREFIX}health_transport_start\\n'
health_raw="$(
  printf 'GET /v1/health HTTP/1.1\\r\\nHost: localhost\\r\\nAuthorization: Bearer %s\\r\\nConnection: close\\r\\n\\r\\n' "$ADMIN_TOKEN" |
    run_timeout {_HEALTH_TRANSPORT_TIMEOUT_SECONDS} "$BB_BIN" nc -w {_HEALTH_TRANSPORT_TIMEOUT_SECONDS} {_HEALTH_HOST} {_HEALTH_PORT} 2>/dev/null |
    "$BB_BIN" head -c {_MAX_HEALTH_RESPONSE_BYTES} || true
)"
printf '{_PHASE_PREFIX}health_transport_done\\n'
ADMIN_TOKEN=''

status_line="$(printf '%s\\n' "$health_raw" | head -n1 | tr -d '\\r')"
case "$status_line" in
  'HTTP/1.1 200 '*|'HTTP/1.0 200 '*) health_api_authenticated=true ;;
esac

if [ "$health_api_authenticated" = true ]; then
  extract_string() {{
    key="$1"
    printf '%s' "$health_raw" | sed -n "s/.*\\\"$key\\\"[[:space:]]*:[[:space:]]*\\\"\\([^\\\"]*\\)\\\".*/\\1/p" | head -n1
  }}
  extract_bool() {{
    key="$1"
    value="$(printf '%s' "$health_raw" | sed -n "s/.*\\\"$key\\\"[[:space:]]*:[[:space:]]*\\([a-z][a-z]*\\).*/\\1/p" | head -n1)"
    case "$value" in true|false) printf '%s' "$value" ;; *) printf 'unknown' ;; esac
  }}
  extract_optional_string() {{
    key="$1"
    if printf '%s' "$health_raw" | grep -Eq "\\\"$key\\\"[[:space:]]*:[[:space:]]*null"; then
      printf 'none'
    else
      value="$(extract_string "$key")"
      if [ -n "$value" ]; then printf '%s' "$value"; else printf 'invalid'; fi
    fi
  }}
  readiness_state="$(extract_string readiness_state)"
  proxy_status="$(extract_string proxy_status)"
  serving="$(extract_bool serving)"
  cellular_route_ready="$(extract_bool cellular_route_ready)"
  proxy_bind_ready="$(extract_bool proxy_bind_ready)"
  local_serving_ready="$(extract_bool local_serving_ready)"
  tunnel_owner="$(extract_string tunnel_owner)"
  reverse_tunnel_connected="$(extract_bool reverse_tunnel_connected)"
  reverse_tunnel_freshness="$(extract_optional_string reverse_tunnel_freshness)"
  reverse_tunnel_active_transport="$(extract_optional_string reverse_tunnel_active_transport)"
  reverse_tunnel_failover_reason="$(extract_optional_string reverse_tunnel_failover_reason)"
  if printf '%s' "$health_raw" | grep -Eq '\"degradation_reason_code\"[[:space:]]*:[[:space:]]*null'; then
    degradation_reason_code=none
  else
    degradation_reason_code="$(extract_string degradation_reason_code)"
  fi
  [ -n "$readiness_state" ] || readiness_state=invalid
  [ -n "$proxy_status" ] || proxy_status=invalid
  [ -n "$tunnel_owner" ] || tunnel_owner=invalid
  [ -n "$degradation_reason_code" ] || degradation_reason_code=invalid
fi
health_raw=''
printf '{_PHASE_PREFIX}health_parse_done\\n'

printf 'watchdog_count=%s\\n' "$watchdog_count"
printf 'runtime_supervisor_count=%s\\n' "$runtime_supervisor_count"
printf 'host_daemon_count=%s\\n' "$host_daemon_count"
printf 'sing_box_count=%s\\n' "$sing_box_count"
printf 'health_api_authenticated=%s\\n' "$health_api_authenticated"
printf 'readiness_state=%s\\n' "$readiness_state"
printf 'serving=%s\\n' "$serving"
printf 'proxy_status=%s\\n' "$proxy_status"
printf 'cellular_route_ready=%s\\n' "$cellular_route_ready"
printf 'proxy_bind_ready=%s\\n' "$proxy_bind_ready"
printf 'local_serving_ready=%s\\n' "$local_serving_ready"
printf 'tunnel_owner=%s\\n' "$tunnel_owner"
printf 'degradation_reason_code=%s\\n' "$degradation_reason_code"
printf 'reverse_tunnel_connected=%s\\n' "$reverse_tunnel_connected"
printf 'reverse_tunnel_freshness=%s\\n' "$reverse_tunnel_freshness"
printf 'reverse_tunnel_active_transport=%s\\n' "$reverse_tunnel_active_transport"
printf 'reverse_tunnel_failover_reason=%s\\n' "$reverse_tunnel_failover_reason"
'''.encode("utf-8")


def _split_phase_markers(raw: bytes) -> tuple[tuple[str, ...], bytes]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise _output_invalid(RuntimeOperationalOutputFailureCode.STREAM_ENCODING) from exc
    phases: list[str] = []
    payload: list[str] = []
    for line in lines:
        if line.startswith(_PHASE_PREFIX):
            phase = line[len(_PHASE_PREFIX) :]
            if phase not in _ALLOWED_PHASES:
                raise _output_invalid(RuntimeOperationalOutputFailureCode.PHASE_PROTOCOL)
            phases.append(phase)
        else:
            payload.append(line)
    if tuple(phases) != _PHASE_SEQUENCE[: len(phases)]:
        raise _output_invalid(RuntimeOperationalOutputFailureCode.PHASE_PROTOCOL)
    body = ("\n".join(payload) + ("\n" if payload else "")).encode("utf-8")
    return tuple(phases), body


def _last_allowlisted_phase(raw: bytes) -> str | None:
    try:
        phases, _ = _split_phase_markers(raw)
    except phone_target.PhoneTargetUnavailable:
        return None
    return phases[-1] if phases else None


def _parse_bool(
    raw: str, *, failure_code: RuntimeOperationalOutputFailureCode
) -> bool | None:
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "unknown":
        return None
    raise _output_invalid(failure_code)


def _parse_count(raw: str) -> int:
    if not raw.isdigit():
        raise _output_invalid(RuntimeOperationalOutputFailureCode.PROCESS_COUNT)
    value = int(raw)
    if value > _MAX_PROCESS_COUNT:
        raise _output_invalid(RuntimeOperationalOutputFailureCode.PROCESS_COUNT)
    return value


def _require_enum(
    raw: str,
    allowed: frozenset[str],
    *,
    failure_code: RuntimeOperationalOutputFailureCode,
) -> str:
    if raw not in allowed:
        raise _output_invalid(failure_code)
    return raw


def _parse_output(raw: bytes) -> RuntimeOperationalObservation:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _output_invalid(RuntimeOperationalOutputFailureCode.PAYLOAD_ENCODING) from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            raise _output_invalid(RuntimeOperationalOutputFailureCode.PAYLOAD_STRUCTURE)
        key, value = line.split("=", 1)
        if key in values:
            raise _output_invalid(RuntimeOperationalOutputFailureCode.PAYLOAD_STRUCTURE)
        values[key] = value
    expected = {
        "watchdog_count",
        "runtime_supervisor_count",
        "host_daemon_count",
        "sing_box_count",
        "health_api_authenticated",
        "readiness_state",
        "serving",
        "proxy_status",
        "cellular_route_ready",
        "proxy_bind_ready",
        "local_serving_ready",
        "tunnel_owner",
        "degradation_reason_code",
        "reverse_tunnel_connected",
        "reverse_tunnel_freshness",
        "reverse_tunnel_active_transport",
        "reverse_tunnel_failover_reason",
    }
    if set(values) != expected:
        raise _output_invalid(RuntimeOperationalOutputFailureCode.PAYLOAD_STRUCTURE)

    health_authenticated = _parse_bool(
        values["health_api_authenticated"],
        failure_code=RuntimeOperationalOutputFailureCode.HEALTH_AUTH,
    )
    if health_authenticated is None:
        raise _output_invalid(RuntimeOperationalOutputFailureCode.HEALTH_AUTH)

    serving = _parse_bool(
        values["serving"],
        failure_code=RuntimeOperationalOutputFailureCode.SERVING,
    )
    cellular_route_ready = _parse_bool(
        values["cellular_route_ready"],
        failure_code=RuntimeOperationalOutputFailureCode.CELLULAR_ROUTE_READY,
    )
    proxy_bind_ready = _parse_bool(
        values["proxy_bind_ready"],
        failure_code=RuntimeOperationalOutputFailureCode.PROXY_BIND_READY,
    )
    local_serving_ready = _parse_bool(
        values["local_serving_ready"],
        failure_code=RuntimeOperationalOutputFailureCode.LOCAL_SERVING_READY,
    )
    reverse_tunnel_connected = _parse_bool(
        values["reverse_tunnel_connected"],
        failure_code=RuntimeOperationalOutputFailureCode.REVERSE_TUNNEL_CONNECTED,
    )
    readiness = values["readiness_state"]
    proxy_status = values["proxy_status"]
    tunnel_owner = values["tunnel_owner"]
    degradation = values["degradation_reason_code"]
    reverse_tunnel_freshness = values["reverse_tunnel_freshness"]
    reverse_tunnel_active_transport = values["reverse_tunnel_active_transport"]
    reverse_tunnel_failover_reason = values["reverse_tunnel_failover_reason"]
    if health_authenticated:
        if serving is None:
            raise _output_invalid(RuntimeOperationalOutputFailureCode.SERVING)
        readiness = _require_enum(
            readiness,
            _ALLOWED_READINESS,
            failure_code=RuntimeOperationalOutputFailureCode.READINESS_STATE,
        )
        proxy_status = _require_enum(
            proxy_status,
            _ALLOWED_PROXY_STATUS,
            failure_code=RuntimeOperationalOutputFailureCode.PROXY_STATUS,
        )
        tunnel_owner = _require_enum(
            tunnel_owner,
            _ALLOWED_TUNNEL_OWNERS,
            failure_code=RuntimeOperationalOutputFailureCode.TUNNEL_OWNER,
        )
        degradation = _require_enum(
            degradation,
            _ALLOWED_DEGRADATION,
            failure_code=RuntimeOperationalOutputFailureCode.DEGRADATION_REASON_CODE,
        )
        reverse_tunnel_freshness = _require_enum(
            reverse_tunnel_freshness,
            _ALLOWED_REVERSE_TUNNEL_FRESHNESS,
            failure_code=RuntimeOperationalOutputFailureCode.REVERSE_TUNNEL_FRESHNESS,
        )
        reverse_tunnel_active_transport = _require_enum(
            reverse_tunnel_active_transport,
            _ALLOWED_REVERSE_TUNNEL_TRANSPORT,
            failure_code=RuntimeOperationalOutputFailureCode.REVERSE_TUNNEL_ACTIVE_TRANSPORT,
        )
        reverse_tunnel_failover_reason = _require_enum(
            reverse_tunnel_failover_reason,
            _ALLOWED_REVERSE_TUNNEL_FAILOVER_REASON,
            failure_code=RuntimeOperationalOutputFailureCode.REVERSE_TUNNEL_FAILOVER_REASON,
        )
    else:
        if any(
            value != "unknown"
            for value in (
                readiness,
                values["serving"],
                proxy_status,
                values["cellular_route_ready"],
                values["proxy_bind_ready"],
                values["local_serving_ready"],
                tunnel_owner,
                degradation,
                values["reverse_tunnel_connected"],
                reverse_tunnel_freshness,
                reverse_tunnel_active_transport,
                reverse_tunnel_failover_reason,
            )
        ):
            raise _output_invalid(
                RuntimeOperationalOutputFailureCode.UNAUTHENTICATED_PAYLOAD_CONTRACT
            )

    return RuntimeOperationalObservation(
        watchdog_count=_parse_count(values["watchdog_count"]),
        runtime_supervisor_count=_parse_count(values["runtime_supervisor_count"]),
        host_daemon_count=_parse_count(values["host_daemon_count"]),
        sing_box_count=_parse_count(values["sing_box_count"]),
        health_api_authenticated=health_authenticated,
        readiness_state=readiness,
        serving=serving,
        proxy_status=proxy_status,
        cellular_route_ready=cellular_route_ready,
        proxy_bind_ready=proxy_bind_ready,
        local_serving_ready=local_serving_ready,
        tunnel_owner=tunnel_owner,
        degradation_reason_code=degradation,
        reverse_tunnel_connected=reverse_tunnel_connected,
        reverse_tunnel_freshness=reverse_tunnel_freshness,
        reverse_tunnel_active_transport=reverse_tunnel_active_transport,
        reverse_tunnel_failover_reason=reverse_tunnel_failover_reason,
    )


def observe_runtime_operational_health(
    serial: str, *, admin_token: str
) -> RuntimeOperationalObservation:
    phone_target._probe_root_capability(serial)
    result = phone_target._run_root_script(
        serial,
        _operational_script(admin_token),
        timeout=_ROOT_SCRIPT_TIMEOUT_SECONDS,
    )
    transport_failure_phase = phone_target._root_transport_failure_phase(result)
    if transport_failure_phase is not None:
        raise phone_target.PhoneTargetDiagnosticFailure(
            transport_failure_phase,
            _UNAVAILABLE,
        )
    if result.status != "completed":
        raise phone_target.PhoneTargetDiagnosticFailure(
            phone_target.PhoneFailurePhase.UNKNOWN,
            _UNAVAILABLE,
        )
    process_failure = _PROCESS_COUNT_FAILURE_BY_RETURN_CODE.get(result.returncode)
    if process_failure is not None:
        failure_phase, expected_last_phase = process_failure
        if _last_allowlisted_phase(result.stdout) != expected_last_phase:
            raise phone_target.PhoneTargetDiagnosticFailure(
                phone_target.PhoneFailurePhase.ROOT_SCRIPT_PROTOCOL_MISMATCH,
                _UNAVAILABLE,
            )
        raise RuntimeOperationalObservationUnavailable(
            _UNAVAILABLE,
            last_phase=failure_phase,
        )
    if result.stderr != b"":
        raise phone_target.PhoneTargetDiagnosticFailure(
            phone_target.PhoneFailurePhase.ROOT_SCRIPT_PROTOCOL_MISMATCH,
            _UNAVAILABLE,
        )
    if result.returncode != 0:
        raise phone_target.PhoneTargetDiagnosticFailure(
            phone_target.PhoneFailurePhase.ROOT_SCRIPT_NONZERO,
            _UNAVAILABLE,
        )
    phases, payload = _split_phase_markers(result.stdout)
    if phases != _PHASE_SEQUENCE:
        raise RuntimeOperationalObservationUnavailable(_UNAVAILABLE)
    return _parse_output(payload)
