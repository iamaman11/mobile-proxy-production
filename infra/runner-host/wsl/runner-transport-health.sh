#!/usr/bin/env bash
set -euo pipefail

readonly RUNNER_SERVICE="mobile-proxy-phone-runner.service"
readonly RUNNER_DIR="/opt/mobile-proxy-production-runner"
readonly STATE_DIR="/var/lib/mobile-proxy-runner-health"
readonly STATE_FILE="${STATE_DIR}/state"
readonly PROJECTION_DIR="/run/mobile-proxy-runner-health"
readonly PROJECTION_FILE="${PROJECTION_DIR}/observation.json"
readonly PROJECTION_SCHEMA="runner-transport-health-projection.v1"
readonly PROJECTION_TTL_SECONDS=180
readonly POST_RESTART_GRACE_SECONDS=300
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

write_projection() {
  local base_decision="$1" last_restart window_started restarts restart_count cooldown grace remaining decision temporary expires
  case "$base_decision" in
    HEALTHY|OBSERVE|RESTART_ELIGIBLE|RATE_LIMITED|UNKNOWN) ;;
    *) return 1 ;;
  esac

  last_restart="$(state_value last_restart 0)"
  window_started="$(state_value window_started "$now")"
  restarts="$(state_value restarts 0)"
  if (( now - window_started >= 3600 )); then
    restart_count=0
  else
    restart_count="$restarts"
  fi

  cooldown=false
  grace=false
  if (( last_restart > 0 && now >= last_restart )); then
    if (( now - last_restart < COOLDOWN_SECONDS )); then cooldown=true; fi
    if (( now - last_restart < POST_RESTART_GRACE_SECONDS )); then grace=true; fi
  fi
  remaining=$((MAX_RESTARTS_PER_HOUR - restart_count))
  if (( remaining < 0 )); then remaining=0; fi

  decision="$base_decision"
  if [[ "$base_decision" != UNKNOWN ]]; then
    if (( remaining == 0 )); then
      decision=RATE_LIMITED
    elif [[ "$cooldown" == true || "$grace" == true ]]; then
      decision=OBSERVE
    fi
  fi

  expires=$((now + PROJECTION_TTL_SECONDS))
  install -d -m 0755 "$PROJECTION_DIR"
  temporary="$(mktemp "${PROJECTION_DIR}/observation.XXXXXX")"
  printf '{"schema":"%s","observed_at_epoch":%s,"expires_at_epoch":%s,"decision":"%s","cooldown_active":%s,"restart_budget_remaining":%s,"post_restart_grace_active":%s,"restart_count_window":%s}\n' \
    "$PROJECTION_SCHEMA" "$now" "$expires" "$decision" "$cooldown" "$remaining" "$grace" "$restart_count" > "$temporary"
  chmod 0644 "$temporary"
  mv -f "$temporary" "$PROJECTION_FILE"
}

finish_with_projection() {
  local decision="$1"
  write_projection "$decision"
  exit 0
}

latest_listener_log() {
  find "${RUNNER_DIR}/_diag" -maxdepth 1 -type f -name 'Runner_*.log' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-
}

now="$(date +%s)"
if ! systemctl is-active --quiet "$RUNNER_SERVICE"; then
  log "runner_service_not_active; systemd restart policy owns recovery"
  finish_with_projection UNKNOWN
fi
listener_log="$(latest_listener_log)"
if [[ -z "$listener_log" || ! -r "$listener_log" ]]; then
  log "listener_log_unavailable; no_restart"
  finish_with_projection UNKNOWN
fi
listener_mtime="$(stat -c %Y "$listener_log" 2>/dev/null || printf '0')"
if (( now - listener_mtime <= STALE_SECONDS )) && tail -n 300 "$listener_log" | grep -Eqi 'Listening for Jobs|Session created|Connected to the broker'; then
  finish_with_projection HEALTHY
fi
tls_failures="$(journalctl -u "$RUNNER_SERVICE" --since "${WINDOW_MINUTES} minutes ago" --no-pager 2>/dev/null | grep -Eic 'SSL connection could not be established|unexpected EOF|0 bytes|BrokerServer' || true)"
if (( tls_failures < FAILURE_THRESHOLD )); then
  log "listener_not_fresh_without_sustained_tls_failures; no_restart"
  finish_with_projection OBSERVE
fi
last_restart="$(state_value last_restart 0)"
window_started="$(state_value window_started "$now")"
restarts="$(state_value restarts 0)"
if (( now - window_started >= 3600 )); then window_started="$now"; restarts=0; fi
if (( now - last_restart < COOLDOWN_SECONDS )); then
  log "restart_cooldown_active; no_restart"
  finish_with_projection OBSERVE
fi
if (( restarts >= MAX_RESTARTS_PER_HOUR )); then
  log "restart_rate_limit_active; no_restart"
  finish_with_projection RATE_LIMITED
fi
log "sustained_broker_tls_failure_and_stale_listener; restarting_existing_runner_service"
write_projection RESTART_ELIGIBLE
if systemctl restart "$RUNNER_SERVICE"; then
  write_state "$now" "$window_started" "$((restarts + 1))"
  write_projection OBSERVE
else
  write_projection UNKNOWN
  exit 1
fi
