from __future__ import annotations

from dataclasses import asdict, dataclass

import phone_target

_ALLOWED_READINESS = frozenset(
    {
        "booting",
        "waiting_tunnel",
        "waiting_cellular",
        "starting_proxy",
        "healthy",
        "recovering",
        "quarantined",
    }
)
_ALLOWED_PROXY_STATUS = frozenset({"running", "degraded"})
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
_MAX_PROCESS_COUNT = 8

_OPERATIONAL_SCRIPT = rb'''set -u
ROOT="/data/adb/mobile-proxy-node/current"
CONFIG="$ROOT/config/host-daemon.json"

count_cmdline() {
  needle="$1"
  count=0
  for cmdfile in /proc/[0-9]*/cmdline; do
    [ -r "$cmdfile" ] || continue
    cmd="$(tr '\000' ' ' < "$cmdfile" 2>/dev/null || true)"
    case "$cmd" in
      *"$needle"*) count=$((count + 1)) ;;
    esac
  done
  printf '%s' "$count"
}

watchdog_count="$(count_cmdline '/data/adb/mobile-proxy-node/logs/runtime-watchdog.sh')"
runtime_supervisor_count="$(count_cmdline '/data/adb/mobile-proxy-node/current/bin/runtime-supervisor')"
host_daemon_count="$(count_cmdline '/data/adb/mobile-proxy-node/current/bin/host-daemon')"
sing_box_count="$(count_cmdline '/data/adb/mobile-proxy-node/current/bin/sing-box')"

health_api_authenticated=false
readiness_state=unknown
serving=unknown
proxy_status=unknown
cellular_route_ready=unknown
proxy_bind_ready=unknown
local_serving_ready=unknown
tunnel_owner=unknown
degradation_reason_code=unknown

if [ -r "$CONFIG" ]; then
  listen="$(sed -n 's/^[[:space:]]*"listen"[[:space:]]*:[[:space:]]*"\([^"]*\)"[[:space:]]*,*[[:space:]]*$/\1/p' "$CONFIG" | head -n1)"
  admin_token="$(sed -n 's/^[[:space:]]*"admin_token"[[:space:]]*:[[:space:]]*"\([^"]*\)"[[:space:]]*,*[[:space:]]*$/\1/p' "$CONFIG" | head -n1)"
  case "$listen" in
    127.0.0.1:[0-9]*)
      port="${listen##*:}"
      ;;
    *)
      port=""
      ;;
  esac

  NC_MODE=""
  if [ -x /data/adb/magisk/busybox ]; then
    NC_BIN=/data/adb/magisk/busybox
    NC_MODE=busybox
  elif [ -x /debug_ramdisk/.magisk/busybox/busybox ]; then
    NC_BIN=/debug_ramdisk/.magisk/busybox/busybox
    NC_MODE=busybox
  elif command -v nc >/dev/null 2>&1; then
    NC_BIN="$(command -v nc)"
    NC_MODE=direct
  fi

  health_raw=""
  if [ -n "$port" ] && [ -n "$admin_token" ] && [ -n "$NC_MODE" ]; then
    if [ "$NC_MODE" = busybox ]; then
      health_raw="$(
        printf 'GET /v1/health HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer %s\r\nConnection: close\r\n\r\n' "$admin_token" |
          "$NC_BIN" nc -w 5 127.0.0.1 "$port" 2>/dev/null || true
      )"
    else
      health_raw="$(
        printf 'GET /v1/health HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer %s\r\nConnection: close\r\n\r\n' "$admin_token" |
          "$NC_BIN" -w 5 127.0.0.1 "$port" 2>/dev/null || true
      )"
    fi
  fi
  admin_token=''

  status_line="$(printf '%s\n' "$health_raw" | head -n1 | tr -d '\r')"
  case "$status_line" in
    'HTTP/1.1 200 '*|'HTTP/1.0 200 '*) health_api_authenticated=true ;;
  esac

  if [ "$health_api_authenticated" = true ]; then
    extract_string() {
      key="$1"
      printf '%s' "$health_raw" | sed -n "s/.*\"$key\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" | head -n1
    }
    extract_bool() {
      key="$1"
      value="$(printf '%s' "$health_raw" | sed -n "s/.*\"$key\"[[:space:]]*:[[:space:]]*\(true\|false\|null\).*/\1/p" | head -n1)"
      case "$value" in true|false) printf '%s' "$value" ;; *) printf 'unknown' ;; esac
    }
    readiness_state="$(extract_string readiness_state)"
    proxy_status="$(extract_string proxy_status)"
    serving="$(extract_bool serving)"
    cellular_route_ready="$(extract_bool cellular_route_ready)"
    proxy_bind_ready="$(extract_bool proxy_bind_ready)"
    local_serving_ready="$(extract_bool local_serving_ready)"
    tunnel_owner="$(extract_string tunnel_owner)"
    degradation_reason_code="$(extract_string degradation_reason_code)"
    [ -n "$readiness_state" ] || readiness_state=unknown
    [ -n "$proxy_status" ] || proxy_status=unknown
    [ -n "$tunnel_owner" ] || tunnel_owner=unknown
    [ -n "$degradation_reason_code" ] || degradation_reason_code=none
  fi
  health_raw=''
fi

printf 'watchdog_count=%s\n' "$watchdog_count"
printf 'runtime_supervisor_count=%s\n' "$runtime_supervisor_count"
printf 'host_daemon_count=%s\n' "$host_daemon_count"
printf 'sing_box_count=%s\n' "$sing_box_count"
printf 'health_api_authenticated=%s\n' "$health_api_authenticated"
printf 'readiness_state=%s\n' "$readiness_state"
printf 'serving=%s\n' "$serving"
printf 'proxy_status=%s\n' "$proxy_status"
printf 'cellular_route_ready=%s\n' "$cellular_route_ready"
printf 'proxy_bind_ready=%s\n' "$proxy_bind_ready"
printf 'local_serving_ready=%s\n' "$local_serving_ready"
printf 'tunnel_owner=%s\n' "$tunnel_owner"
printf 'degradation_reason_code=%s\n' "$degradation_reason_code"
'''


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
    def desired(self) -> bool:
        return (
            self.watchdog_count == 1
            and self.runtime_supervisor_count == 1
            and self.host_daemon_count == 1
            and self.sing_box_count == 1
            and self.health_api_authenticated
            and self.cellular_route_ready is True
            and self.proxy_bind_ready is True
            and self.local_serving_ready is True
        )

    def to_bounded_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.update(
            {
                "evaluated": True,
                "desired": self.desired,
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


def _parse_bool(raw: str) -> bool | None:
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "unknown":
        return None
    raise phone_target.PhoneTargetUnavailable("runtime operational observation is malformed")


def _parse_count(raw: str) -> int:
    if not raw.isdigit():
        raise phone_target.PhoneTargetUnavailable("runtime operational observation is malformed")
    value = int(raw)
    if value > _MAX_PROCESS_COUNT:
        raise phone_target.PhoneTargetUnavailable("runtime operational observation is malformed")
    return value


def _require_enum(raw: str, allowed: frozenset[str]) -> str:
    if raw not in allowed:
        raise phone_target.PhoneTargetUnavailable("runtime operational observation is malformed")
    return raw


def _parse_output(raw: bytes) -> RuntimeOperationalObservation:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise phone_target.PhoneTargetUnavailable("runtime operational observation is malformed") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            raise phone_target.PhoneTargetUnavailable("runtime operational observation is malformed")
        key, value = line.split("=", 1)
        if key in values:
            raise phone_target.PhoneTargetUnavailable("runtime operational observation is malformed")
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
        raise phone_target.PhoneTargetUnavailable("runtime operational observation is malformed")

    health_authenticated = _parse_bool(values["health_api_authenticated"])
    if health_authenticated is None:
        raise phone_target.PhoneTargetUnavailable("runtime operational observation is malformed")

    readiness = values["readiness_state"]
    proxy_status = values["proxy_status"]
    tunnel_owner = values["tunnel_owner"]
    degradation = values["degradation_reason_code"]
    if health_authenticated:
        readiness = _require_enum(readiness, _ALLOWED_READINESS)
        proxy_status = _require_enum(proxy_status, _ALLOWED_PROXY_STATUS)
        tunnel_owner = _require_enum(tunnel_owner, _ALLOWED_TUNNEL_OWNERS)
        degradation = _require_enum(degradation, _ALLOWED_DEGRADATION)
    else:
        if any(value != "unknown" for value in (readiness, proxy_status, tunnel_owner, degradation)):
            raise phone_target.PhoneTargetUnavailable("runtime operational observation is malformed")

    return RuntimeOperationalObservation(
        watchdog_count=_parse_count(values["watchdog_count"]),
        runtime_supervisor_count=_parse_count(values["runtime_supervisor_count"]),
        host_daemon_count=_parse_count(values["host_daemon_count"]),
        sing_box_count=_parse_count(values["sing_box_count"]),
        health_api_authenticated=health_authenticated,
        readiness_state=readiness,
        serving=_parse_bool(values["serving"]),
        proxy_status=proxy_status,
        cellular_route_ready=_parse_bool(values["cellular_route_ready"]),
        proxy_bind_ready=_parse_bool(values["proxy_bind_ready"]),
        local_serving_ready=_parse_bool(values["local_serving_ready"]),
        tunnel_owner=tunnel_owner,
        degradation_reason_code=degradation,
    )


def observe_runtime_operational_health(serial: str) -> RuntimeOperationalObservation:
    phone_target._probe_root_capability(serial)
    result = phone_target._run_root_script(serial, _OPERATIONAL_SCRIPT, timeout=20)
    if result.status != "completed" or result.returncode != 0 or result.stderr != b"":
        raise phone_target.PhoneTargetUnavailable("runtime operational observation is unavailable")
    return _parse_output(result.stdout)
