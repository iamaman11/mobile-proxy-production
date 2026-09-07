#!/usr/bin/env bash
set -euo pipefail

readonly RUNNER_SERVICE="mobile-proxy-phone-runner.service"
readonly RUNNER_DIR="/opt/mobile-proxy-production-runner"
readonly STATE_DIR="/var/lib/mobile-proxy-runner-health"
readonly STATE_FILE="${STATE_DIR}/state"
readonly STALE_SECONDS=300
readonly WINDOW_MINUTES=6
readonly FAILURE_THRESHOLD=3
readonly COOLDOWN_SECONDS=900
readonly MAX_RESTARTS_PER_HOUR=3

log() { logger -t mobile-proxy-runner-health -- "$1"; }

state_value() {
  local key="$1" fallback="$2" value
  if [[ ! -r "$STATE_FILE" ]]; then printf '%s\n' "$fallback"; return; fi
  value="$(awk -F= -v key="$key" '$1 == key { print $2; exit }' "$STATE_FILE" 2>/dev/null || true)"
  if [[ "$value" =~ ^[0-9]+$ ]]; then printf '%s\n' "$value"; else printf '%s\n' "$fallback"; fi
}

write_state() {
  local last_restart="$1" window_started="$2" restarts="$3" temporary
  install -d -m 0700 "$STATE_DIR"
  temporary="$(mktemp "${STATE_DIR}/state.XXXXXX")"
  umask 077
  printf 'last_restart=%s\nwindow_started=%s\nrestarts=%s\n' "$last_restart" "$window_started" "$restarts" > "$temporary"
  mv -f "$temporary" "$STATE_FILE"
}

latest_listener_log() {
  find "${RUNNER_DIR}/_diag" -maxdepth 1 -type f -name 'Runner_*.log' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-
}

now="$(date +%s)"
if ! systemctl is-active --quiet "$RUNNER_SERVICE"; then
  log "runner_service_not_active; systemd restart policy owns recovery"
  exit 0
fi
listener_log="$(latest_listener_log)"
if [[ -z "$listener_log" || ! -r "$listener_log" ]]; then
  log "listener_log_unavailable; no_restart"
  exit 0
fi
listener_mtime="$(stat -c %Y "$listener_log" 2>/dev/null || printf '0')"
if (( now - listener_mtime <= STALE_SECONDS )) && tail -n 300 "$listener_log" | grep -Eqi 'Listening for Jobs|Session created|Connected to the broker'; then
  exit 0
fi
tls_failures="$(journalctl -u "$RUNNER_SERVICE" --since "${WINDOW_MINUTES} minutes ago" --no-pager 2>/dev/null | grep -Eic 'SSL connection could not be established|unexpected EOF|0 bytes|BrokerServer' || true)"
if (( tls_failures < FAILURE_THRESHOLD )); then
  log "listener_not_fresh_without_sustained_tls_failures; no_restart"
  exit 0
fi
last_restart="$(state_value last_restart 0)"
window_started="$(state_value window_started "$now")"
restarts="$(state_value restarts 0)"
if (( now - window_started >= 3600 )); then window_started="$now"; restarts=0; fi
if (( now - last_restart < COOLDOWN_SECONDS )); then log "restart_cooldown_active; no_restart"; exit 0; fi
if (( restarts >= MAX_RESTARTS_PER_HOUR )); then log "restart_rate_limit_active; no_restart"; exit 0; fi
log "sustained_broker_tls_failure_and_stale_listener; restarting_existing_runner_service"
systemctl restart "$RUNNER_SERVICE"
write_state "$now" "$window_started" "$((restarts + 1))"
