from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass

import phone_target

_ROOT = "/data/adb/mobile-proxy-node"
_STATUS_HOST = "127.0.0.1"
_STATUS_PORT = 8088
_STATUS_TIMEOUT_SECONDS = 3
_MAX_STATUS_RESPONSE_BYTES = 8 * 1024
_ROOT_SCRIPT_TIMEOUT_SECONDS = 15
_MAX_KIB = 1_000_000_000_000
_MAX_FD_COUNT = 1_000_000
_UNAVAILABLE = "phone resource sanity observation is unavailable"

_RETURN_CODE_FAILURE = {
    21: "RESOURCE_QUEUE_STATUS_UNAVAILABLE",
    22: "RESOURCE_PROCESS_SET_NOT_EXACT",
    23: "RESOURCE_MEMORY_SAMPLE_INVALID",
    24: "RESOURCE_FILESYSTEM_SAMPLE_INVALID",
    25: "RESOURCE_PROCESS_SAMPLE_INVALID",
    26: "RESOURCE_LOG_SAMPLE_INVALID",
}


class PhoneResourceSanityUnavailable(phone_target.PhoneTargetUnavailable):
    def __init__(self, failure_code: str) -> None:
        super().__init__(_UNAVAILABLE)
        self.failure_code = failure_code


@dataclass(frozen=True)
class PhoneResourceSanityObservation:
    classification: str
    queue_clear: bool
    process_set_exact: bool | None
    watchdog_count: int | None
    runtime_supervisor_count: int | None
    host_daemon_count: int | None
    sing_box_count: int | None
    memory_total_kib: int | None
    memory_available_kib: int | None
    data_total_kib: int | None
    data_available_kib: int | None
    runtime_rss_kib: int | None
    runtime_fd_count: int | None
    logs_kib: int | None
    sample_count: int
    mode: str = "read_only"

    @property
    def memory_available_basis_points(self) -> int | None:
        if self.memory_total_kib is None or self.memory_available_kib is None:
            return None
        return self.memory_available_kib * 10_000 // self.memory_total_kib

    @property
    def data_available_basis_points(self) -> int | None:
        if self.data_total_kib is None or self.data_available_kib is None:
            return None
        return self.data_available_kib * 10_000 // self.data_total_kib

    @property
    def memory_headroom_to_runtime_rss_milli(self) -> int | None:
        if self.memory_available_kib is None or self.runtime_rss_kib in (None, 0):
            return None
        return self.memory_available_kib * 1000 // self.runtime_rss_kib

    def to_bounded_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.update(
            {
                "evaluated": True,
                "queue_authority": "product-v1-status-current-job",
                "resource_headroom_measured": self.classification == "MEASURED",
                "memory_available_basis_points": self.memory_available_basis_points,
                "data_available_basis_points": self.data_available_basis_points,
                "memory_headroom_to_runtime_rss_milli": self.memory_headroom_to_runtime_rss_milli,
                "performance_threshold_applied": False,
                "synthetic_load_performed": False,
                "process_ids_recorded": False,
                "process_cmdlines_recorded": False,
                "raw_status_json_recorded": False,
                "raw_config_recorded": False,
                "secret_values_recorded": False,
                "provider_access_performed": False,
                "phone_mutation_performed": False,
            }
        )
        return value


def _validate_admin_token(raw: str) -> str:
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > 4096
        or any(character in raw for character in "\r\n\x00")
        or any(ord(character) < 32 for character in raw)
    ):
        raise PhoneResourceSanityUnavailable("RESOURCE_BINDING_INVALID")
    return raw


def _resource_script(admin_token: str) -> bytes:
    token = shlex.quote(_validate_admin_token(admin_token))
    return f'''set -u
ROOT={shlex.quote(_ROOT)}
ADMIN_TOKEN={token}

BB_BIN=""
if [ -x /data/adb/magisk/busybox ]; then
  BB_BIN=/data/adb/magisk/busybox
elif [ -x /debug_ramdisk/.magisk/busybox/busybox ]; then
  BB_BIN=/debug_ramdisk/.magisk/busybox/busybox
fi
[ -n "$BB_BIN" ] || exit 20

TIMEOUT_STYLE=""
if "$BB_BIN" timeout 1 "$BB_BIN" true >/dev/null 2>&1; then
  TIMEOUT_STYLE=positional
elif "$BB_BIN" timeout -t 1 "$BB_BIN" true >/dev/null 2>&1; then
  TIMEOUT_STYLE=legacy
fi
[ -n "$TIMEOUT_STYLE" ] || exit 20

run_timeout() {{
  timeout_seconds="$1"
  shift
  if [ "$TIMEOUT_STYLE" = positional ]; then
    "$BB_BIN" timeout "$timeout_seconds" "$@"
  else
    "$BB_BIN" timeout -t "$timeout_seconds" "$@"
  fi
}}

is_uint() {{
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}}

status_raw="$(
  printf 'GET /v1/status HTTP/1.1\\r\\nHost: localhost\\r\\nAuthorization: Bearer %s\\r\\nConnection: close\\r\\n\\r\\n' "$ADMIN_TOKEN" |
    run_timeout {_STATUS_TIMEOUT_SECONDS} "$BB_BIN" nc -w {_STATUS_TIMEOUT_SECONDS} {_STATUS_HOST} {_STATUS_PORT} 2>/dev/null |
    "$BB_BIN" head -c {_MAX_STATUS_RESPONSE_BYTES} || true
)"
ADMIN_TOKEN=''
status_line="$(printf '%s\\n' "$status_raw" | "$BB_BIN" head -n 1 | "$BB_BIN" tr -d '\\r')"
case "$status_line" in
  'HTTP/1.1 200 '*|'HTTP/1.0 200 '*) ;;
  *) status_raw=''; exit 21 ;;
esac
if printf '%s' "$status_raw" | "$BB_BIN" grep -Eq '\"current_job\"[[:space:]]*:[[:space:]]*null'; then
  queue_clear=true
elif printf '%s' "$status_raw" | "$BB_BIN" grep -Eq '\"current_job\"[[:space:]]*:[[:space:]]*\"[^\"]+\"'; then
  queue_clear=false
else
  status_raw=''
  exit 21
fi
status_raw=''

if [ "$queue_clear" != true ]; then
  printf 'classification=BUSY\\n'
  printf 'queue_clear=false\\n'
  printf 'process_set_exact=unknown\\n'
  printf 'watchdog_count=unknown\\n'
  printf 'runtime_supervisor_count=unknown\\n'
  printf 'host_daemon_count=unknown\\n'
  printf 'sing_box_count=unknown\\n'
  printf 'memory_total_kib=unknown\\n'
  printf 'memory_available_kib=unknown\\n'
  printf 'data_total_kib=unknown\\n'
  printf 'data_available_kib=unknown\\n'
  printf 'runtime_rss_kib=unknown\\n'
  printf 'runtime_fd_count=unknown\\n'
  printf 'logs_kib=unknown\\n'
  printf 'sample_count=0\\n'
  exit 0
fi

WATCHDOG_NEEDLE="$ROOT/logs/runtime-watchdog.sh"
RUNTIME_SUPERVISOR_NEEDLE="$ROOT/current/bin/runtime-supervisor"
HOST_DAEMON_NEEDLE="$ROOT/current/bin/host-daemon"
SING_BOX_NEEDLE="$ROOT/current/bin/sing-box"
watchdog_count=0
runtime_supervisor_count=0
host_daemon_count=0
sing_box_count=0
watchdog_pid=''
runtime_supervisor_pid=''
host_daemon_pid=''
sing_box_pid=''
readable_processes=0

for process_dir in /proc/[0-9]*; do
  cmdline_path="$process_dir/cmdline"
  [ -r "$cmdline_path" ] || continue
  {{ exec 3<"$cmdline_path"; }} 2>/dev/null || continue
  readable_processes=$((readable_processes + 1))
  watchdog_match=0
  runtime_supervisor_match=0
  host_daemon_match=0
  sing_box_match=0
  while IFS= read -r -d '' process_arg <&3; do
    case "$process_arg" in
      "$WATCHDOG_NEEDLE") watchdog_match=1 ;;
      "$RUNTIME_SUPERVISOR_NEEDLE") runtime_supervisor_match=1 ;;
      "$HOST_DAEMON_NEEDLE") host_daemon_match=1 ;;
      "$SING_BOX_NEEDLE") sing_box_match=1 ;;
    esac
  done
  exec 3<&-
  pid="${{process_dir##*/}}"
  if [ "$watchdog_match" -eq 1 ]; then
    watchdog_count=$((watchdog_count + 1)); watchdog_pid="$pid"
  fi
  if [ "$runtime_supervisor_match" -eq 1 ]; then
    runtime_supervisor_count=$((runtime_supervisor_count + 1)); runtime_supervisor_pid="$pid"
  fi
  if [ "$host_daemon_match" -eq 1 ]; then
    host_daemon_count=$((host_daemon_count + 1)); host_daemon_pid="$pid"
  fi
  if [ "$sing_box_match" -eq 1 ]; then
    sing_box_count=$((sing_box_count + 1)); sing_box_pid="$pid"
  fi
done
[ "$readable_processes" -gt 0 ] || exit 22
[ "$watchdog_count" -eq 1 ] || exit 22
[ "$runtime_supervisor_count" -eq 1 ] || exit 22
[ "$host_daemon_count" -eq 1 ] || exit 22
[ "$sing_box_count" -eq 1 ] || exit 22

memory_total_kib=''
memory_available_kib=''
while read -r key value unit rest; do
  case "$key" in
    MemTotal:)
      [ "$unit" = kB ] || exit 23
      memory_total_kib="$value"
      ;;
    MemAvailable:)
      [ "$unit" = kB ] || exit 23
      memory_available_kib="$value"
      ;;
  esac
done < /proc/meminfo
is_uint "$memory_total_kib" || exit 23
is_uint "$memory_available_kib" || exit 23
[ "$memory_total_kib" -gt 0 ] || exit 23
[ "$memory_available_kib" -le "$memory_total_kib" ] || exit 23

df_line="$("$BB_BIN" df -Pk /data 2>/dev/null | "$BB_BIN" tail -n 1)"
set -- $df_line
[ "$#" -ge 6 ] || exit 24
data_total_kib="$2"
data_available_kib="$4"
is_uint "$data_total_kib" || exit 24
is_uint "$data_available_kib" || exit 24
[ "$data_total_kib" -gt 0 ] || exit 24
[ "$data_available_kib" -le "$data_total_kib" ] || exit 24

process_metrics() {{
  sample_pid="$1"
  rss_kib=''
  while read -r key value unit rest; do
    if [ "$key" = 'VmRSS:' ]; then
      [ "$unit" = kB ] || return 1
      rss_kib="$value"
      break
    fi
  done < "/proc/$sample_pid/status" || return 1
  is_uint "$rss_kib" || return 1
  fd_count=0
  for fd_path in "/proc/$sample_pid/fd"/*; do
    [ "$fd_path" = "/proc/$sample_pid/fd/*" ] && continue
    fd_count=$((fd_count + 1))
  done
  printf '%s %s' "$rss_kib" "$fd_count"
}}

runtime_rss_kib=0
runtime_fd_count=0
for sample_pid in "$watchdog_pid" "$runtime_supervisor_pid" "$host_daemon_pid" "$sing_box_pid"; do
  metrics="$(process_metrics "$sample_pid")" || exit 25
  set -- $metrics
  [ "$#" -eq 2 ] || exit 25
  is_uint "$1" || exit 25
  is_uint "$2" || exit 25
  runtime_rss_kib=$((runtime_rss_kib + $1))
  runtime_fd_count=$((runtime_fd_count + $2))
done
[ "$runtime_rss_kib" -gt 0 ] || exit 25

logs_line="$("$BB_BIN" du -sk "$ROOT/logs" 2>/dev/null)" || exit 26
set -- $logs_line
[ "$#" -ge 1 ] || exit 26
logs_kib="$1"
is_uint "$logs_kib" || exit 26

printf 'classification=MEASURED\\n'
printf 'queue_clear=true\\n'
printf 'process_set_exact=true\\n'
printf 'watchdog_count=%s\\n' "$watchdog_count"
printf 'runtime_supervisor_count=%s\\n' "$runtime_supervisor_count"
printf 'host_daemon_count=%s\\n' "$host_daemon_count"
printf 'sing_box_count=%s\\n' "$sing_box_count"
printf 'memory_total_kib=%s\\n' "$memory_total_kib"
printf 'memory_available_kib=%s\\n' "$memory_available_kib"
printf 'data_total_kib=%s\\n' "$data_total_kib"
printf 'data_available_kib=%s\\n' "$data_available_kib"
printf 'runtime_rss_kib=%s\\n' "$runtime_rss_kib"
printf 'runtime_fd_count=%s\\n' "$runtime_fd_count"
printf 'logs_kib=%s\\n' "$logs_kib"
printf 'sample_count=1\\n'
'''.encode("utf-8")


def _parse_bool(raw: str) -> bool | None:
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "unknown":
        return None
    raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")


def _parse_uint(raw: str, *, maximum: int = _MAX_KIB) -> int | None:
    if raw == "unknown":
        return None
    if not raw.isdigit():
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
    value = int(raw)
    if value > maximum:
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
    return value


def _parse_output(raw: bytes) -> PhoneResourceSanityObservation:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
        key, value = line.split("=", 1)
        if not key or key in values:
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
        values[key] = value
    expected = {
        "classification",
        "queue_clear",
        "process_set_exact",
        "watchdog_count",
        "runtime_supervisor_count",
        "host_daemon_count",
        "sing_box_count",
        "memory_total_kib",
        "memory_available_kib",
        "data_total_kib",
        "data_available_kib",
        "runtime_rss_kib",
        "runtime_fd_count",
        "logs_kib",
        "sample_count",
    }
    if set(values) != expected:
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")

    classification = values["classification"]
    if classification not in {"MEASURED", "BUSY"}:
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
    queue_clear = _parse_bool(values["queue_clear"])
    if queue_clear is None:
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
    process_set_exact = _parse_bool(values["process_set_exact"])
    counts = (
        _parse_uint(values["watchdog_count"], maximum=8),
        _parse_uint(values["runtime_supervisor_count"], maximum=8),
        _parse_uint(values["host_daemon_count"], maximum=8),
        _parse_uint(values["sing_box_count"], maximum=8),
    )
    memory_total = _parse_uint(values["memory_total_kib"])
    memory_available = _parse_uint(values["memory_available_kib"])
    data_total = _parse_uint(values["data_total_kib"])
    data_available = _parse_uint(values["data_available_kib"])
    runtime_rss = _parse_uint(values["runtime_rss_kib"])
    runtime_fd = _parse_uint(values["runtime_fd_count"], maximum=_MAX_FD_COUNT)
    logs_kib = _parse_uint(values["logs_kib"])
    sample_count = _parse_uint(values["sample_count"], maximum=1)
    if sample_count is None:
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")

    if classification == "BUSY":
        if queue_clear or process_set_exact is not None or sample_count != 0:
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
        if any(value is not None for value in (*counts, memory_total, memory_available, data_total, data_available, runtime_rss, runtime_fd, logs_kib)):
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
    else:
        if queue_clear is not True or process_set_exact is not True or sample_count != 1:
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
        if counts != (1, 1, 1, 1):
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
        required = (memory_total, memory_available, data_total, data_available, runtime_rss, runtime_fd, logs_kib)
        if any(value is None for value in required):
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
        assert memory_total is not None and memory_available is not None
        assert data_total is not None and data_available is not None
        assert runtime_rss is not None and runtime_fd is not None
        if memory_total <= 0 or memory_available > memory_total or data_total <= 0 or data_available > data_total:
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
        if runtime_rss <= 0 or runtime_fd <= 0:
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")

    return PhoneResourceSanityObservation(
        classification=classification,
        queue_clear=queue_clear,
        process_set_exact=process_set_exact,
        watchdog_count=counts[0],
        runtime_supervisor_count=counts[1],
        host_daemon_count=counts[2],
        sing_box_count=counts[3],
        memory_total_kib=memory_total,
        memory_available_kib=memory_available,
        data_total_kib=data_total,
        data_available_kib=data_available,
        runtime_rss_kib=runtime_rss,
        runtime_fd_count=runtime_fd,
        logs_kib=logs_kib,
        sample_count=sample_count,
    )


def observe_phone_resource_sanity(
    serial: str, *, admin_token: str
) -> PhoneResourceSanityObservation:
    phone_target._probe_root_capability(serial)
    result = phone_target._run_root_script(
        serial,
        _resource_script(admin_token),
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
    if result.stderr != b"":
        raise phone_target.PhoneTargetDiagnosticFailure(
            phone_target.PhoneFailurePhase.ROOT_SCRIPT_PROTOCOL_MISMATCH,
            _UNAVAILABLE,
        )
    if result.returncode != 0:
        failure = _RETURN_CODE_FAILURE.get(result.returncode)
        if failure is not None:
            raise PhoneResourceSanityUnavailable(failure)
        raise phone_target.PhoneTargetDiagnosticFailure(
            phone_target.PhoneFailurePhase.ROOT_SCRIPT_NONZERO,
            _UNAVAILABLE,
        )
    return _parse_output(result.stdout)
