from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass

import phone_target

_ROOT = "/data/adb/mobile-proxy-node"
_STATUS_HOST = "127.0.0.1"
_STATUS_PORT = 8088
_STATUS_TIMEOUT_SECONDS = 3
_MAX_STATUS_RESPONSE_BYTES = 8 * 1024
_ROOT_SCRIPT_TIMEOUT_SECONDS = 25
_EXERCISE_REQUEST_COUNT = 12
_RECOVERY_WAIT_SECONDS = 2
_MAX_KIB = 1_000_000_000_000
_MAX_FD_COUNT = 1_000_000
_MAX_CPU_TICKS = 1_000_000_000_000_000
_UNAVAILABLE = "phone resource sanity observation is unavailable"

_RETURN_CODE_FAILURE = {
    21: "RESOURCE_QUEUE_STATUS_UNAVAILABLE",
    22: "RESOURCE_PROCESS_SET_NOT_EXACT",
    23: "RESOURCE_MEMORY_SAMPLE_INVALID",
    24: "RESOURCE_FILESYSTEM_SAMPLE_INVALID",
    25: "RESOURCE_PROCESS_SAMPLE_INVALID",
    26: "RESOURCE_LOG_SAMPLE_INVALID",
    27: "RESOURCE_EXERCISE_REQUEST_FAILED",
    28: "RESOURCE_CPU_SAMPLE_INVALID",
    29: "RESOURCE_PROCESS_IDENTITY_CHANGED",
    30: "RESOURCE_QUEUE_CHANGED",
    31: "HTTP_RESPONSE_UNAVAILABLE_OR_NON_200",
    32: "STATUS_PAYLOAD_INVALID",
}
_PROGRESS_RETURN_CODES = frozenset({27, 30, 31, 32})

_SAMPLE_FIELDS = (
    "memory_total_kib",
    "memory_available_kib",
    "data_total_kib",
    "data_available_kib",
    "runtime_rss_kib",
    "runtime_fd_count",
    "logs_kib",
    "runtime_cpu_ticks",
    "system_cpu_ticks",
)


class PhoneResourceSanityUnavailable(phone_target.PhoneTargetUnavailable):
    def __init__(
        self,
        failure_code: str,
        *,
        exercise_requests_attempted: int | None = None,
        exercise_requests_succeeded: int | None = None,
    ) -> None:
        super().__init__(_UNAVAILABLE)
        self.failure_code = failure_code
        self.exercise_requests_attempted = exercise_requests_attempted
        self.exercise_requests_succeeded = exercise_requests_succeeded


@dataclass(frozen=True)
class PhoneResourceSanityObservation:
    classification: str
    queue_clear: bool
    process_set_exact: bool | None
    process_identity_stable: bool | None
    watchdog_count: int | None
    runtime_supervisor_count: int | None
    host_daemon_count: int | None
    sing_box_count: int | None
    sample_count: int
    exercise_request_count: int
    exercise_requests_attempted: int
    exercise_requests_succeeded: int
    recovery_wait_seconds: int
    bounded_local_exercise_completed: bool
    baseline_memory_total_kib: int | None = None
    baseline_memory_available_kib: int | None = None
    baseline_data_total_kib: int | None = None
    baseline_data_available_kib: int | None = None
    baseline_runtime_rss_kib: int | None = None
    baseline_runtime_fd_count: int | None = None
    baseline_logs_kib: int | None = None
    baseline_runtime_cpu_ticks: int | None = None
    baseline_system_cpu_ticks: int | None = None
    exercise_memory_total_kib: int | None = None
    exercise_memory_available_kib: int | None = None
    exercise_data_total_kib: int | None = None
    exercise_data_available_kib: int | None = None
    exercise_runtime_rss_kib: int | None = None
    exercise_runtime_fd_count: int | None = None
    exercise_logs_kib: int | None = None
    exercise_runtime_cpu_ticks: int | None = None
    exercise_system_cpu_ticks: int | None = None
    recovery_memory_total_kib: int | None = None
    recovery_memory_available_kib: int | None = None
    recovery_data_total_kib: int | None = None
    recovery_data_available_kib: int | None = None
    recovery_runtime_rss_kib: int | None = None
    recovery_runtime_fd_count: int | None = None
    recovery_logs_kib: int | None = None
    recovery_runtime_cpu_ticks: int | None = None
    recovery_system_cpu_ticks: int | None = None
    mode: str = "read_only"

    @property
    def memory_available_basis_points(self) -> int | None:
        if (
            self.recovery_memory_total_kib in (None, 0)
            or self.recovery_memory_available_kib is None
        ):
            return None
        return (
            self.recovery_memory_available_kib * 10_000
            // self.recovery_memory_total_kib
        )

    @property
    def data_available_basis_points(self) -> int | None:
        if (
            self.recovery_data_total_kib in (None, 0)
            or self.recovery_data_available_kib is None
        ):
            return None
        return self.recovery_data_available_kib * 10_000 // self.recovery_data_total_kib

    @property
    def memory_headroom_to_runtime_rss_milli(self) -> int | None:
        if (
            self.recovery_memory_available_kib is None
            or self.recovery_runtime_rss_kib in (None, 0)
        ):
            return None
        return (
            self.recovery_memory_available_kib * 1000
            // self.recovery_runtime_rss_kib
        )

    @property
    def minimum_memory_available_kib(self) -> int | None:
        values = (
            self.baseline_memory_available_kib,
            self.exercise_memory_available_kib,
            self.recovery_memory_available_kib,
        )
        if any(value is None for value in values):
            return None
        return min(value for value in values if value is not None)

    @property
    def minimum_data_available_kib(self) -> int | None:
        values = (
            self.baseline_data_available_kib,
            self.exercise_data_available_kib,
            self.recovery_data_available_kib,
        )
        if any(value is None for value in values):
            return None
        return min(value for value in values if value is not None)

    @property
    def peak_runtime_rss_kib(self) -> int | None:
        values = (
            self.baseline_runtime_rss_kib,
            self.exercise_runtime_rss_kib,
            self.recovery_runtime_rss_kib,
        )
        if any(value is None for value in values):
            return None
        return max(value for value in values if value is not None)

    @property
    def peak_runtime_fd_count(self) -> int | None:
        values = (
            self.baseline_runtime_fd_count,
            self.exercise_runtime_fd_count,
            self.recovery_runtime_fd_count,
        )
        if any(value is None for value in values):
            return None
        return max(value for value in values if value is not None)

    def _delta(self, newer: int | None, older: int | None) -> int | None:
        if newer is None or older is None:
            return None
        return newer - older

    @property
    def exercise_runtime_cpu_delta_ticks(self) -> int | None:
        return self._delta(
            self.exercise_runtime_cpu_ticks,
            self.baseline_runtime_cpu_ticks,
        )

    @property
    def exercise_system_cpu_delta_ticks(self) -> int | None:
        return self._delta(
            self.exercise_system_cpu_ticks,
            self.baseline_system_cpu_ticks,
        )

    @property
    def exercise_cpu_basis_points(self) -> int | None:
        runtime_delta = self.exercise_runtime_cpu_delta_ticks
        system_delta = self.exercise_system_cpu_delta_ticks
        if (
            runtime_delta is None
            or system_delta in (None, 0)
            or runtime_delta < 0
            or system_delta < 0
        ):
            return None
        return runtime_delta * 10_000 // system_delta

    @property
    def recovery_runtime_cpu_delta_ticks(self) -> int | None:
        return self._delta(
            self.recovery_runtime_cpu_ticks,
            self.exercise_runtime_cpu_ticks,
        )

    @property
    def recovery_system_cpu_delta_ticks(self) -> int | None:
        return self._delta(
            self.recovery_system_cpu_ticks,
            self.exercise_system_cpu_ticks,
        )

    @property
    def recovery_cpu_basis_points(self) -> int | None:
        runtime_delta = self.recovery_runtime_cpu_delta_ticks
        system_delta = self.recovery_system_cpu_delta_ticks
        if (
            runtime_delta is None
            or system_delta in (None, 0)
            or runtime_delta < 0
            or system_delta < 0
        ):
            return None
        return runtime_delta * 10_000 // system_delta

    @property
    def rss_exercise_delta_kib(self) -> int | None:
        return self._delta(
            self.exercise_runtime_rss_kib,
            self.baseline_runtime_rss_kib,
        )

    @property
    def rss_recovery_delta_kib(self) -> int | None:
        return self._delta(
            self.recovery_runtime_rss_kib,
            self.baseline_runtime_rss_kib,
        )

    @property
    def fd_exercise_delta(self) -> int | None:
        return self._delta(
            self.exercise_runtime_fd_count,
            self.baseline_runtime_fd_count,
        )

    @property
    def fd_recovery_delta(self) -> int | None:
        return self._delta(
            self.recovery_runtime_fd_count,
            self.baseline_runtime_fd_count,
        )

    @property
    def log_exercise_delta_kib(self) -> int | None:
        return self._delta(self.exercise_logs_kib, self.baseline_logs_kib)

    @property
    def log_recovery_delta_kib(self) -> int | None:
        return self._delta(self.recovery_logs_kib, self.baseline_logs_kib)

    @property
    def rss_monotonic_growth(self) -> bool | None:
        values = (
            self.baseline_runtime_rss_kib,
            self.exercise_runtime_rss_kib,
            self.recovery_runtime_rss_kib,
        )
        if any(value is None for value in values):
            return None
        baseline, exercise, recovery = values
        assert baseline is not None and exercise is not None and recovery is not None
        return baseline < exercise < recovery

    @property
    def fd_monotonic_growth(self) -> bool | None:
        values = (
            self.baseline_runtime_fd_count,
            self.exercise_runtime_fd_count,
            self.recovery_runtime_fd_count,
        )
        if any(value is None for value in values):
            return None
        baseline, exercise, recovery = values
        assert baseline is not None and exercise is not None and recovery is not None
        return baseline < exercise < recovery

    @property
    def log_monotonic_growth(self) -> bool | None:
        values = (
            self.baseline_logs_kib,
            self.exercise_logs_kib,
            self.recovery_logs_kib,
        )
        if any(value is None for value in values):
            return None
        baseline, exercise, recovery = values
        assert baseline is not None and exercise is not None and recovery is not None
        return baseline < exercise < recovery

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
                "minimum_memory_available_kib": self.minimum_memory_available_kib,
                "minimum_data_available_kib": self.minimum_data_available_kib,
                "peak_runtime_rss_kib": self.peak_runtime_rss_kib,
                "peak_runtime_fd_count": self.peak_runtime_fd_count,
                "exercise_runtime_cpu_delta_ticks": self.exercise_runtime_cpu_delta_ticks,
                "exercise_system_cpu_delta_ticks": self.exercise_system_cpu_delta_ticks,
                "exercise_cpu_basis_points": self.exercise_cpu_basis_points,
                "recovery_runtime_cpu_delta_ticks": self.recovery_runtime_cpu_delta_ticks,
                "recovery_system_cpu_delta_ticks": self.recovery_system_cpu_delta_ticks,
                "recovery_cpu_basis_points": self.recovery_cpu_basis_points,
                "rss_exercise_delta_kib": self.rss_exercise_delta_kib,
                "rss_recovery_delta_kib": self.rss_recovery_delta_kib,
                "fd_exercise_delta": self.fd_exercise_delta,
                "fd_recovery_delta": self.fd_recovery_delta,
                "log_exercise_delta_kib": self.log_exercise_delta_kib,
                "log_recovery_delta_kib": self.log_recovery_delta_kib,
                "rss_monotonic_growth": self.rss_monotonic_growth,
                "fd_monotonic_growth": self.fd_monotonic_growth,
                "log_monotonic_growth": self.log_monotonic_growth,
                "synthetic_load_performed": self.bounded_local_exercise_completed,
                "production_scale_load_performed": False,
                "external_network_traffic_performed": False,
                "performance_threshold_applied": False,
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
EXERCISE_REQUEST_COUNT={_EXERCISE_REQUEST_COUNT}
RECOVERY_WAIT_SECONDS={_RECOVERY_WAIT_SECONDS}
exercise_requests_attempted=0
exercise_requests_succeeded=0

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

read_queue_state() {{
  status_raw="$(
    printf 'GET /v1/status HTTP/1.1\\r\\nHost: localhost\\r\\nAuthorization: Bearer %s\\r\\nConnection: close\\r\\n\\r\\n' "$ADMIN_TOKEN" |
      run_timeout {_STATUS_TIMEOUT_SECONDS} "$BB_BIN" nc -w {_STATUS_TIMEOUT_SECONDS} {_STATUS_HOST} {_STATUS_PORT} 2>/dev/null |
      "$BB_BIN" head -c {_MAX_STATUS_RESPONSE_BYTES} || true
  )"
  status_line="$(printf '%s\\n' "$status_raw" | "$BB_BIN" head -n 1 | "$BB_BIN" tr -d '\\r')"
  case "$status_line" in
    'HTTP/1.1 200 '*|'HTTP/1.0 200 '*) ;;
    *) status_raw=''; return 31 ;;
  esac
  if printf '%s' "$status_raw" | "$BB_BIN" grep -Eq '\"current_job\"[[:space:]]*:[[:space:]]*null'; then
    status_raw=''
    printf 'clear'
    return 0
  fi
  if printf '%s' "$status_raw" | "$BB_BIN" grep -Eq '\"current_job\"[[:space:]]*:[[:space:]]*\"[^\"]+\"'; then
    status_raw=''
    printf 'busy'
    return 0
  fi
  status_raw=''
  return 32
}}

fail_with_request_progress() {{
  failure_rc="$1"
  ADMIN_TOKEN=''
  printf 'exercise_requests_attempted=%s\\n' "$exercise_requests_attempted"
  printf 'exercise_requests_succeeded=%s\\n' "$exercise_requests_succeeded"
  exit "$failure_rc"
}}

find_processes() {{
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
    if [ "$watchdog_match" -eq 1 ]; then watchdog_count=$((watchdog_count + 1)); watchdog_pid="$pid"; fi
    if [ "$runtime_supervisor_match" -eq 1 ]; then runtime_supervisor_count=$((runtime_supervisor_count + 1)); runtime_supervisor_pid="$pid"; fi
    if [ "$host_daemon_match" -eq 1 ]; then host_daemon_count=$((host_daemon_count + 1)); host_daemon_pid="$pid"; fi
    if [ "$sing_box_match" -eq 1 ]; then sing_box_count=$((sing_box_count + 1)); sing_box_pid="$pid"; fi
  done
  [ "$readable_processes" -gt 0 ] || return 22
  [ "$watchdog_count" -eq 1 ] || return 22
  [ "$runtime_supervisor_count" -eq 1 ] || return 22
  [ "$host_daemon_count" -eq 1 ] || return 22
  [ "$sing_box_count" -eq 1 ] || return 22
  return 0
}}

process_cpu_ticks() {{
  sample_pid="$1"
  stat_line="$("$BB_BIN" cat "/proc/$sample_pid/stat" 2>/dev/null)" || return 1
  case "$stat_line" in
    *') '*) ;;
    *) return 1 ;;
  esac
  stat_rest="${{stat_line##*) }}"
  set -- $stat_rest
  [ "$#" -ge 13 ] || return 1
  utime="${{12}}"
  stime="${{13}}"
  is_uint "$utime" || return 1
  is_uint "$stime" || return 1
  printf '%s' $((utime + stime))
}}

collect_snapshot() {{
  find_processes
  rc=$?
  [ "$rc" -eq 0 ] || return "$rc"

  memory_total_kib=''
  memory_available_kib=''
  while read -r key value unit rest; do
    case "$key" in
      MemTotal:) [ "$unit" = kB ] || return 23; memory_total_kib="$value" ;;
      MemAvailable:) [ "$unit" = kB ] || return 23; memory_available_kib="$value" ;;
    esac
  done < /proc/meminfo
  is_uint "$memory_total_kib" || return 23
  is_uint "$memory_available_kib" || return 23
  [ "$memory_total_kib" -gt 0 ] || return 23
  [ "$memory_available_kib" -le "$memory_total_kib" ] || return 23

  df_line="$("$BB_BIN" df -Pk /data 2>/dev/null | "$BB_BIN" tail -n 1)"
  set -- $df_line
  [ "$#" -ge 6 ] || return 24
  data_total_kib="$2"
  data_available_kib="$4"
  is_uint "$data_total_kib" || return 24
  is_uint "$data_available_kib" || return 24
  [ "$data_total_kib" -gt 0 ] || return 24
  [ "$data_available_kib" -le "$data_total_kib" ] || return 24

  runtime_rss_kib=0
  runtime_fd_count=0
  runtime_cpu_ticks=0
  for sample_pid in "$watchdog_pid" "$runtime_supervisor_pid" "$host_daemon_pid" "$sing_box_pid"; do
    rss_kib=''
    while read -r key value unit rest; do
      if [ "$key" = 'VmRSS:' ]; then
        [ "$unit" = kB ] || return 25
        rss_kib="$value"
        break
      fi
    done < "/proc/$sample_pid/status" || return 25
    is_uint "$rss_kib" || return 25
    fd_count=0
    for fd_path in "/proc/$sample_pid/fd"/*; do
      [ "$fd_path" = "/proc/$sample_pid/fd/*" ] && continue
      fd_count=$((fd_count + 1))
    done
    cpu_ticks="$(process_cpu_ticks "$sample_pid")" || return 28
    is_uint "$cpu_ticks" || return 28
    runtime_rss_kib=$((runtime_rss_kib + rss_kib))
    runtime_fd_count=$((runtime_fd_count + fd_count))
    runtime_cpu_ticks=$((runtime_cpu_ticks + cpu_ticks))
  done
  [ "$runtime_rss_kib" -gt 0 ] || return 25
  [ "$runtime_fd_count" -gt 0 ] || return 25

  read -r cpu_label cpu_user cpu_nice cpu_system cpu_idle cpu_iowait cpu_irq cpu_softirq cpu_steal cpu_rest < /proc/stat || return 28
  [ "$cpu_label" = cpu ] || return 28
  cpu_iowait="${{cpu_iowait:-0}}"
  cpu_irq="${{cpu_irq:-0}}"
  cpu_softirq="${{cpu_softirq:-0}}"
  cpu_steal="${{cpu_steal:-0}}"
  for cpu_value in "$cpu_user" "$cpu_nice" "$cpu_system" "$cpu_idle" "$cpu_iowait" "$cpu_irq" "$cpu_softirq" "$cpu_steal"; do
    is_uint "$cpu_value" || return 28
  done
  system_cpu_ticks=$((cpu_user + cpu_nice + cpu_system + cpu_idle + cpu_iowait + cpu_irq + cpu_softirq + cpu_steal))

  logs_line="$("$BB_BIN" du -sk "$ROOT/logs" 2>/dev/null)" || return 26
  set -- $logs_line
  [ "$#" -ge 1 ] || return 26
  logs_kib="$1"
  is_uint "$logs_kib" || return 26
  return 0
}}

queue_state="$(read_queue_state)"
queue_rc=$?
[ "$queue_rc" -eq 0 ] || fail_with_request_progress "$queue_rc"
if [ "$queue_state" = busy ]; then
  ADMIN_TOKEN=''
  printf 'classification=BUSY\\n'
  printf 'queue_clear=false\\n'
  printf 'process_set_exact=unknown\\n'
  printf 'process_identity_stable=unknown\\n'
  printf 'watchdog_count=unknown\\n'
  printf 'runtime_supervisor_count=unknown\\n'
  printf 'host_daemon_count=unknown\\n'
  printf 'sing_box_count=unknown\\n'
  printf 'sample_count=0\\n'
  printf 'exercise_request_count=%s\\n' "$EXERCISE_REQUEST_COUNT"
  printf 'exercise_requests_attempted=0\\n'
  printf 'exercise_requests_succeeded=0\\n'
  printf 'recovery_wait_seconds=%s\\n' "$RECOVERY_WAIT_SECONDS"
  printf 'bounded_local_exercise_completed=false\\n'
  exit 0
fi

collect_snapshot
snapshot_rc=$?
[ "$snapshot_rc" -eq 0 ] || exit "$snapshot_rc"
baseline_watchdog_pid="$watchdog_pid"
baseline_runtime_supervisor_pid="$runtime_supervisor_pid"
baseline_host_daemon_pid="$host_daemon_pid"
baseline_sing_box_pid="$sing_box_pid"
baseline_memory_total_kib="$memory_total_kib"
baseline_memory_available_kib="$memory_available_kib"
baseline_data_total_kib="$data_total_kib"
baseline_data_available_kib="$data_available_kib"
baseline_runtime_rss_kib="$runtime_rss_kib"
baseline_runtime_fd_count="$runtime_fd_count"
baseline_logs_kib="$logs_kib"
baseline_runtime_cpu_ticks="$runtime_cpu_ticks"
baseline_system_cpu_ticks="$system_cpu_ticks"

while [ "$exercise_requests_attempted" -lt "$EXERCISE_REQUEST_COUNT" ]; do
  exercise_requests_attempted=$((exercise_requests_attempted + 1))
  queue_state="$(read_queue_state)"
  queue_rc=$?
  [ "$queue_rc" -eq 0 ] || fail_with_request_progress "$queue_rc"
  [ "$queue_state" = clear ] || fail_with_request_progress 30
  exercise_requests_succeeded=$((exercise_requests_succeeded + 1))
done

collect_snapshot
snapshot_rc=$?
[ "$snapshot_rc" -eq 0 ] || exit "$snapshot_rc"
[ "$watchdog_pid" = "$baseline_watchdog_pid" ] || exit 29
[ "$runtime_supervisor_pid" = "$baseline_runtime_supervisor_pid" ] || exit 29
[ "$host_daemon_pid" = "$baseline_host_daemon_pid" ] || exit 29
[ "$sing_box_pid" = "$baseline_sing_box_pid" ] || exit 29
exercise_memory_total_kib="$memory_total_kib"
exercise_memory_available_kib="$memory_available_kib"
exercise_data_total_kib="$data_total_kib"
exercise_data_available_kib="$data_available_kib"
exercise_runtime_rss_kib="$runtime_rss_kib"
exercise_runtime_fd_count="$runtime_fd_count"
exercise_logs_kib="$logs_kib"
exercise_runtime_cpu_ticks="$runtime_cpu_ticks"
exercise_system_cpu_ticks="$system_cpu_ticks"

"$BB_BIN" sleep "$RECOVERY_WAIT_SECONDS" || fail_with_request_progress 27
queue_state="$(read_queue_state)"
queue_rc=$?
[ "$queue_rc" -eq 0 ] || fail_with_request_progress "$queue_rc"
[ "$queue_state" = clear ] || fail_with_request_progress 30

collect_snapshot
snapshot_rc=$?
[ "$snapshot_rc" -eq 0 ] || exit "$snapshot_rc"
[ "$watchdog_pid" = "$baseline_watchdog_pid" ] || exit 29
[ "$runtime_supervisor_pid" = "$baseline_runtime_supervisor_pid" ] || exit 29
[ "$host_daemon_pid" = "$baseline_host_daemon_pid" ] || exit 29
[ "$sing_box_pid" = "$baseline_sing_box_pid" ] || exit 29
recovery_memory_total_kib="$memory_total_kib"
recovery_memory_available_kib="$memory_available_kib"
recovery_data_total_kib="$data_total_kib"
recovery_data_available_kib="$data_available_kib"
recovery_runtime_rss_kib="$runtime_rss_kib"
recovery_runtime_fd_count="$runtime_fd_count"
recovery_logs_kib="$logs_kib"
recovery_runtime_cpu_ticks="$runtime_cpu_ticks"
recovery_system_cpu_ticks="$system_cpu_ticks"

[ "$baseline_memory_total_kib" -eq "$exercise_memory_total_kib" ] || exit 23
[ "$baseline_memory_total_kib" -eq "$recovery_memory_total_kib" ] || exit 23
[ "$baseline_data_total_kib" -eq "$exercise_data_total_kib" ] || exit 24
[ "$baseline_data_total_kib" -eq "$recovery_data_total_kib" ] || exit 24
[ "$exercise_runtime_cpu_ticks" -ge "$baseline_runtime_cpu_ticks" ] || exit 28
[ "$recovery_runtime_cpu_ticks" -ge "$exercise_runtime_cpu_ticks" ] || exit 28
[ "$exercise_system_cpu_ticks" -gt "$baseline_system_cpu_ticks" ] || exit 28
[ "$recovery_system_cpu_ticks" -gt "$exercise_system_cpu_ticks" ] || exit 28

ADMIN_TOKEN=''
printf 'classification=MEASURED\\n'
printf 'queue_clear=true\\n'
printf 'process_set_exact=true\\n'
printf 'process_identity_stable=true\\n'
printf 'watchdog_count=%s\\n' "$watchdog_count"
printf 'runtime_supervisor_count=%s\\n' "$runtime_supervisor_count"
printf 'host_daemon_count=%s\\n' "$host_daemon_count"
printf 'sing_box_count=%s\\n' "$sing_box_count"
printf 'sample_count=3\\n'
printf 'exercise_request_count=%s\\n' "$EXERCISE_REQUEST_COUNT"
printf 'exercise_requests_attempted=%s\\n' "$exercise_requests_attempted"
printf 'exercise_requests_succeeded=%s\\n' "$exercise_requests_succeeded"
printf 'recovery_wait_seconds=%s\\n' "$RECOVERY_WAIT_SECONDS"
printf 'bounded_local_exercise_completed=true\\n'
printf 'baseline_memory_total_kib=%s\\n' "$baseline_memory_total_kib"
printf 'baseline_memory_available_kib=%s\\n' "$baseline_memory_available_kib"
printf 'baseline_data_total_kib=%s\\n' "$baseline_data_total_kib"
printf 'baseline_data_available_kib=%s\\n' "$baseline_data_available_kib"
printf 'baseline_runtime_rss_kib=%s\\n' "$baseline_runtime_rss_kib"
printf 'baseline_runtime_fd_count=%s\\n' "$baseline_runtime_fd_count"
printf 'baseline_logs_kib=%s\\n' "$baseline_logs_kib"
printf 'baseline_runtime_cpu_ticks=%s\\n' "$baseline_runtime_cpu_ticks"
printf 'baseline_system_cpu_ticks=%s\\n' "$baseline_system_cpu_ticks"
printf 'exercise_memory_total_kib=%s\\n' "$exercise_memory_total_kib"
printf 'exercise_memory_available_kib=%s\\n' "$exercise_memory_available_kib"
printf 'exercise_data_total_kib=%s\\n' "$exercise_data_total_kib"
printf 'exercise_data_available_kib=%s\\n' "$exercise_data_available_kib"
printf 'exercise_runtime_rss_kib=%s\\n' "$exercise_runtime_rss_kib"
printf 'exercise_runtime_fd_count=%s\\n' "$exercise_runtime_fd_count"
printf 'exercise_logs_kib=%s\\n' "$exercise_logs_kib"
printf 'exercise_runtime_cpu_ticks=%s\\n' "$exercise_runtime_cpu_ticks"
printf 'exercise_system_cpu_ticks=%s\\n' "$exercise_system_cpu_ticks"
printf 'recovery_memory_total_kib=%s\\n' "$recovery_memory_total_kib"
printf 'recovery_memory_available_kib=%s\\n' "$recovery_memory_available_kib"
printf 'recovery_data_total_kib=%s\\n' "$recovery_data_total_kib"
printf 'recovery_data_available_kib=%s\\n' "$recovery_data_available_kib"
printf 'recovery_runtime_rss_kib=%s\\n' "$recovery_runtime_rss_kib"
printf 'recovery_runtime_fd_count=%s\\n' "$recovery_runtime_fd_count"
printf 'recovery_logs_kib=%s\\n' "$recovery_logs_kib"
printf 'recovery_runtime_cpu_ticks=%s\\n' "$recovery_runtime_cpu_ticks"
printf 'recovery_system_cpu_ticks=%s\\n' "$recovery_system_cpu_ticks"
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


def _parse_failure_progress(raw: bytes) -> tuple[int, int]:
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
    if set(values) != {
        "exercise_requests_attempted",
        "exercise_requests_succeeded",
    }:
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
    attempted = _parse_uint(
        values["exercise_requests_attempted"], maximum=_EXERCISE_REQUEST_COUNT
    )
    succeeded = _parse_uint(
        values["exercise_requests_succeeded"], maximum=_EXERCISE_REQUEST_COUNT
    )
    if attempted is None or succeeded is None or succeeded > attempted:
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
    return attempted, succeeded


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

    common = {
        "classification",
        "queue_clear",
        "process_set_exact",
        "process_identity_stable",
        "watchdog_count",
        "runtime_supervisor_count",
        "host_daemon_count",
        "sing_box_count",
        "sample_count",
        "exercise_request_count",
        "exercise_requests_attempted",
        "exercise_requests_succeeded",
        "recovery_wait_seconds",
        "bounded_local_exercise_completed",
    }
    classification = values.get("classification")
    if classification not in {"MEASURED", "BUSY"}:
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
    expected = set(common)
    if classification == "MEASURED":
        for prefix in ("baseline", "exercise", "recovery"):
            expected.update(f"{prefix}_{field}" for field in _SAMPLE_FIELDS)
    if set(values) != expected:
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")

    queue_clear = _parse_bool(values["queue_clear"])
    process_set_exact = _parse_bool(values["process_set_exact"])
    process_identity_stable = _parse_bool(values["process_identity_stable"])
    counts = (
        _parse_uint(values["watchdog_count"], maximum=8),
        _parse_uint(values["runtime_supervisor_count"], maximum=8),
        _parse_uint(values["host_daemon_count"], maximum=8),
        _parse_uint(values["sing_box_count"], maximum=8),
    )
    sample_count = _parse_uint(values["sample_count"], maximum=3)
    request_count = _parse_uint(
        values["exercise_request_count"], maximum=_EXERCISE_REQUEST_COUNT
    )
    requests_attempted = _parse_uint(
        values["exercise_requests_attempted"], maximum=_EXERCISE_REQUEST_COUNT
    )
    requests_succeeded = _parse_uint(
        values["exercise_requests_succeeded"], maximum=_EXERCISE_REQUEST_COUNT
    )
    recovery_wait = _parse_uint(
        values["recovery_wait_seconds"], maximum=_RECOVERY_WAIT_SECONDS
    )
    exercise_completed = _parse_bool(values["bounded_local_exercise_completed"])
    if None in (
        queue_clear,
        sample_count,
        request_count,
        requests_attempted,
        requests_succeeded,
        recovery_wait,
        exercise_completed,
    ):
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")

    if classification == "BUSY":
        if (
            queue_clear is not False
            or process_set_exact is not None
            or process_identity_stable is not None
            or counts != (None, None, None, None)
            or sample_count != 0
            or request_count != _EXERCISE_REQUEST_COUNT
            or requests_attempted != 0
            or requests_succeeded != 0
            or recovery_wait != _RECOVERY_WAIT_SECONDS
            or exercise_completed is not False
        ):
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
        return PhoneResourceSanityObservation(
            classification="BUSY",
            queue_clear=False,
            process_set_exact=None,
            process_identity_stable=None,
            watchdog_count=None,
            runtime_supervisor_count=None,
            host_daemon_count=None,
            sing_box_count=None,
            sample_count=0,
            exercise_request_count=_EXERCISE_REQUEST_COUNT,
            exercise_requests_attempted=0,
            exercise_requests_succeeded=0,
            recovery_wait_seconds=_RECOVERY_WAIT_SECONDS,
            bounded_local_exercise_completed=False,
        )

    if (
        queue_clear is not True
        or process_set_exact is not True
        or process_identity_stable is not True
        or counts != (1, 1, 1, 1)
        or sample_count != 3
        or request_count != _EXERCISE_REQUEST_COUNT
        or requests_attempted != _EXERCISE_REQUEST_COUNT
        or requests_succeeded != _EXERCISE_REQUEST_COUNT
        or recovery_wait != _RECOVERY_WAIT_SECONDS
        or exercise_completed is not True
    ):
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")

    parsed: dict[str, int] = {}
    for prefix in ("baseline", "exercise", "recovery"):
        for field in _SAMPLE_FIELDS:
            if field == "runtime_fd_count":
                maximum = _MAX_FD_COUNT
            elif field.endswith("cpu_ticks"):
                maximum = _MAX_CPU_TICKS
            else:
                maximum = _MAX_KIB
            value = _parse_uint(values[f"{prefix}_{field}"], maximum=maximum)
            if value is None:
                raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
            parsed[f"{prefix}_{field}"] = value
        if parsed[f"{prefix}_memory_total_kib"] <= 0:
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
        if (
            parsed[f"{prefix}_memory_available_kib"]
            > parsed[f"{prefix}_memory_total_kib"]
        ):
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
        if parsed[f"{prefix}_data_total_kib"] <= 0:
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
        if (
            parsed[f"{prefix}_data_available_kib"]
            > parsed[f"{prefix}_data_total_kib"]
        ):
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")
        if (
            parsed[f"{prefix}_runtime_rss_kib"] <= 0
            or parsed[f"{prefix}_runtime_fd_count"] <= 0
        ):
            raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")

    if not (
        parsed["baseline_memory_total_kib"]
        == parsed["exercise_memory_total_kib"]
        == parsed["recovery_memory_total_kib"]
        and parsed["baseline_data_total_kib"]
        == parsed["exercise_data_total_kib"]
        == parsed["recovery_data_total_kib"]
        and parsed["baseline_runtime_cpu_ticks"]
        <= parsed["exercise_runtime_cpu_ticks"]
        <= parsed["recovery_runtime_cpu_ticks"]
        and parsed["baseline_system_cpu_ticks"]
        < parsed["exercise_system_cpu_ticks"]
        < parsed["recovery_system_cpu_ticks"]
    ):
        raise PhoneResourceSanityUnavailable("RESOURCE_OUTPUT_MALFORMED")

    return PhoneResourceSanityObservation(
        classification="MEASURED",
        queue_clear=True,
        process_set_exact=True,
        process_identity_stable=True,
        watchdog_count=1,
        runtime_supervisor_count=1,
        host_daemon_count=1,
        sing_box_count=1,
        sample_count=3,
        exercise_request_count=_EXERCISE_REQUEST_COUNT,
        exercise_requests_attempted=_EXERCISE_REQUEST_COUNT,
        exercise_requests_succeeded=_EXERCISE_REQUEST_COUNT,
        recovery_wait_seconds=_RECOVERY_WAIT_SECONDS,
        bounded_local_exercise_completed=True,
        **parsed,
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
            if result.returncode in _PROGRESS_RETURN_CODES:
                attempted, succeeded = _parse_failure_progress(result.stdout)
                raise PhoneResourceSanityUnavailable(
                    failure,
                    exercise_requests_attempted=attempted,
                    exercise_requests_succeeded=succeeded,
                )
            raise PhoneResourceSanityUnavailable(failure)
        raise phone_target.PhoneTargetDiagnosticFailure(
            phone_target.PhoneFailurePhase.ROOT_SCRIPT_NONZERO,
            _UNAVAILABLE,
        )
    return _parse_output(result.stdout)
