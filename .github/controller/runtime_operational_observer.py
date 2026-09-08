from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass

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
_PHASE_SEQUENCE = (
    "busybox_selected",
    "process_count_start",
    "process_count_done",
    "health_transport_start",
    "health_transport_done",
    "health_parse_done",
)
_ALLOWED_PHASES = frozenset(_PHASE_SEQUENCE)
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
        if last_phase is not None and last_phase not in _ALLOWED_PHASES:
            raise ValueError("runtime operational failure phase is not allowlisted")
        self.last_phase = last_phase


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

WATCHDOG_NEEDLE='/data/adb/mobile-proxy-node/logs/runtime-watchdog.sh'
RUNTIME_SUPERVISOR_NEEDLE='/data/adb/mobile-proxy-node/current/bin/runtime-supervisor'
HOST_DAEMON_NEEDLE='/data/adb/mobile-proxy-node/current/bin/host-daemon'
SING_BOX_NEEDLE='/data/adb/mobile-proxy-node/current/bin/sing-box'
export BB_BIN WATCHDOG_NEEDLE RUNTIME_SUPERVISOR_NEEDLE HOST_DAEMON_NEEDLE SING_BOX_NEEDLE
printf '{_PHASE_PREFIX}process_count_start\\n'
process_counts="$(
  "$BB_BIN" timeout -t {_PROCESS_COUNT_TIMEOUT_SECONDS} "$BB_BIN" sh -s <<'STAGE4_PROCESS_COUNT'
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
  if [ "$process_count_status" -eq 26 ]; then
    printf '{_PHASE_PREFIX}process_count_done\\n'
  fi
  exit 21
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

health_raw=""
printf '{_PHASE_PREFIX}health_transport_start\\n'
health_raw="$(
  printf 'GET /v1/health HTTP/1.1\\r\\nHost: localhost\\r\\nAuthorization: Bearer %s\\r\\nConnection: close\\r\\n\\r\\n' "$ADMIN_TOKEN" |
    "$BB_BIN" timeout -t {_HEALTH_TRANSPORT_TIMEOUT_SECONDS} "$BB_BIN" nc -w {_HEALTH_TRANSPORT_TIMEOUT_SECONDS} {_HEALTH_HOST} {_HEALTH_PORT} 2>/dev/null |
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
    value="$(printf '%s' "$health_raw" | sed -n "s/.*\\\"$key\\\"[[:space:]]*:[[:space:]]*\\(true\\|false\\|null\\).*/\\1/p" | head -n1)"
    case "$value" in true|false) printf '%s' "$value" ;; *) printf 'unknown' ;; esac
  }}
  readiness_state="$(extract_string readiness_state)"
  proxy_status="$(extract_string proxy_status)"
  serving="$(extract_bool serving)"
  cellular_route_ready="$(extract_bool cellular_route_ready)"
  proxy_bind_ready="$(extract_bool proxy_bind_ready)"
  local_serving_ready="$(extract_bool local_serving_ready)"
  tunnel_owner="$(extract_string tunnel_owner)"
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
'''.encode("utf-8")


def _split_phase_markers(raw: bytes) -> tuple[tuple[str, ...], bytes]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise phone_target.PhoneTargetUnavailable(_MALFORMED) from exc
    phases: list[str] = []
    payload: list[str] = []
    for line in lines:
        if line.startswith(_PHASE_PREFIX):
            phase = line[len(_PHASE_PREFIX) :]
            if phase not in _ALLOWED_PHASES:
                raise phone_target.PhoneTargetUnavailable(_MALFORMED)
            phases.append(phase)
        else:
            payload.append(line)
    if tuple(phases) != _PHASE_SEQUENCE[: len(phases)]:
        raise phone_target.PhoneTargetUnavailable(_MALFORMED)
    body = ("\n".join(payload) + ("\n" if payload else "")).encode("utf-8")
    return tuple(phases), body


def _last_allowlisted_phase(raw: bytes) -> str | None:
    try:
        phases, _ = _split_phase_markers(raw)
    except phone_target.PhoneTargetUnavailable:
        return None
    return phases[-1] if phases else None


def _parse_bool(raw: str) -> bool | None:
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "unknown":
        return None
    raise phone_target.PhoneTargetUnavailable(_MALFORMED)


def _parse_count(raw: str) -> int:
    if not raw.isdigit():
        raise phone_target.PhoneTargetUnavailable(_MALFORMED)
    value = int(raw)
    if value > _MAX_PROCESS_COUNT:
        raise phone_target.PhoneTargetUnavailable(_MALFORMED)
    return value


def _require_enum(raw: str, allowed: frozenset[str]) -> str:
    if raw not in allowed:
        raise phone_target.PhoneTargetUnavailable(_MALFORMED)
    return raw


def _parse_output(raw: bytes) -> RuntimeOperationalObservation:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise phone_target.PhoneTargetUnavailable(_MALFORMED) from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            raise phone_target.PhoneTargetUnavailable(_MALFORMED)
        key, value = line.split("=", 1)
        if key in values:
            raise phone_target.PhoneTargetUnavailable(_MALFORMED)
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
    }
    if set(values) != expected:
        raise phone_target.PhoneTargetUnavailable(_MALFORMED)

    health_authenticated = _parse_bool(values["health_api_authenticated"])
    if health_authenticated is None:
        raise phone_target.PhoneTargetUnavailable(_MALFORMED)

    serving = _parse_bool(values["serving"])
    cellular_route_ready = _parse_bool(values["cellular_route_ready"])
    proxy_bind_ready = _parse_bool(values["proxy_bind_ready"])
    local_serving_ready = _parse_bool(values["local_serving_ready"])
    readiness = values["readiness_state"]
    proxy_status = values["proxy_status"]
    tunnel_owner = values["tunnel_owner"]
    degradation = values["degradation_reason_code"]
    if health_authenticated:
        if serving is None:
            raise phone_target.PhoneTargetUnavailable(_MALFORMED)
        readiness = _require_enum(readiness, _ALLOWED_READINESS)
        proxy_status = _require_enum(proxy_status, _ALLOWED_PROXY_STATUS)
        tunnel_owner = _require_enum(tunnel_owner, _ALLOWED_TUNNEL_OWNERS)
        degradation = _require_enum(degradation, _ALLOWED_DEGRADATION)
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
            )
        ):
            raise phone_target.PhoneTargetUnavailable(_MALFORMED)

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
    if result.status != "completed" or result.returncode != 0 or result.stderr != b"":
        raise RuntimeOperationalObservationUnavailable(
            _UNAVAILABLE,
            last_phase=_last_allowlisted_phase(result.stdout),
        )
    phases, payload = _split_phase_markers(result.stdout)
    if phases != _PHASE_SEQUENCE:
        raise RuntimeOperationalObservationUnavailable(_UNAVAILABLE)
    return _parse_output(payload)