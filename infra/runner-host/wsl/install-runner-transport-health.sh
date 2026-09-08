#!/usr/bin/env bash
set -euo pipefail

readonly RUNNER_SERVICE='mobile-proxy-phone-runner.service'
readonly HEALTH_SERVICE='mobile-proxy-runner-transport-health.service'
readonly TIMER='mobile-proxy-runner-transport-health.timer'
readonly SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly LIB_DIR='/usr/local/lib/mobile-proxy-runner-health'
readonly STATE_DIR='/var/lib/mobile-proxy-runner-health'
readonly SYSTEMD_DIR='/etc/systemd/system'
readonly PROJECTION_DIR='/run/mobile-proxy-runner-health'
readonly PROJECTION_FILE="${PROJECTION_DIR}/observation.json"
readonly SCRIPT_SOURCE="${SOURCE_DIR}/runner-transport-health.sh"
readonly SERVICE_SOURCE="${SOURCE_DIR}/${HEALTH_SERVICE}"
readonly TIMER_SOURCE="${SOURCE_DIR}/${TIMER}"
readonly SCRIPT_TARGET="${LIB_DIR}/runner-transport-health.sh"
readonly SERVICE_TARGET="${SYSTEMD_DIR}/${HEALTH_SERVICE}"
readonly TIMER_TARGET="${SYSTEMD_DIR}/${TIMER}"

temporary_files=()
cleanup() {
  local path
  for path in "${temporary_files[@]}"; do
    [[ -n "$path" && -e "$path" ]] && rm -f -- "$path"
  done
}
trap cleanup EXIT

fail() { printf '%s\n' "$1" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || fail 'RUNNER_TRANSPORT_HEALTH_INSTALL_REQUIRES_ROOT'
[[ $# -eq 1 && ( "$1" == '--install' || "$1" == '--converge' || "$1" == '--verify' ) ]] \
  || fail 'usage: install-runner-transport-health.sh --install|--converge|--verify'
readonly MODE="$1"

for tool in systemctl install cmp stat mktemp mv rm; do
  command -v "$tool" >/dev/null || fail 'RUNNER_TRANSPORT_HEALTH_REQUIRED_TOOL_UNAVAILABLE'
done
for source in "$SCRIPT_SOURCE" "$SERVICE_SOURCE" "$TIMER_SOURCE"; do
  [[ -f "$source" && ! -L "$source" ]] || fail 'RUNNER_TRANSPORT_HEALTH_VERSIONED_SOURCE_INVALID'
done
systemctl cat "$RUNNER_SERVICE" >/dev/null 2>&1 || fail 'RUNNER_TRANSPORT_HEALTH_RUNNER_SERVICE_UNAVAILABLE'

require_secure_root_dir() {
  local path="$1" owner mode
  [[ -d "$path" && ! -L "$path" ]] || fail 'RUNNER_TRANSPORT_HEALTH_DIRECTORY_TRUST_INVALID'
  owner="$(stat -c '%u' "$path")"
  mode="$(stat -c '%a' "$path")"
  [[ "$owner" == '0' ]] || fail 'RUNNER_TRANSPORT_HEALTH_DIRECTORY_OWNER_INVALID'
  (( (8#$mode & 022) == 0 )) || fail 'RUNNER_TRANSPORT_HEALTH_DIRECTORY_MODE_INVALID'
}

require_secure_root_file_if_present() {
  local path="$1" owner mode
  if [[ -e "$path" || -L "$path" ]]; then
    [[ -f "$path" && ! -L "$path" ]] || fail 'RUNNER_TRANSPORT_HEALTH_EXISTING_FILE_TRUST_INVALID'
    owner="$(stat -c '%u' "$path")"
    mode="$(stat -c '%a' "$path")"
    [[ "$owner" == '0' ]] || fail 'RUNNER_TRANSPORT_HEALTH_EXISTING_FILE_OWNER_INVALID'
    (( (8#$mode & 022) == 0 )) || fail 'RUNNER_TRANSPORT_HEALTH_EXISTING_FILE_MODE_INVALID'
  fi
}

require_exact_installed_files() {
  require_secure_root_dir "$LIB_DIR"
  require_secure_root_dir "$STATE_DIR"
  require_secure_root_dir "$SYSTEMD_DIR"
  require_secure_root_file_if_present "$SCRIPT_TARGET"
  require_secure_root_file_if_present "$SERVICE_TARGET"
  require_secure_root_file_if_present "$TIMER_TARGET"
  [[ -f "$SCRIPT_TARGET" && -f "$SERVICE_TARGET" && -f "$TIMER_TARGET" ]] \
    || fail 'RUNNER_TRANSPORT_HEALTH_INSTALLED_FILE_MISSING'
  cmp -s "$SCRIPT_SOURCE" "$SCRIPT_TARGET" || fail 'RUNNER_TRANSPORT_HEALTH_SCRIPT_BYTES_DIFFER'
  cmp -s "$SERVICE_SOURCE" "$SERVICE_TARGET" || fail 'RUNNER_TRANSPORT_HEALTH_SERVICE_BYTES_DIFFER'
  cmp -s "$TIMER_SOURCE" "$TIMER_TARGET" || fail 'RUNNER_TRANSPORT_HEALTH_TIMER_BYTES_DIFFER'
}

atomic_install() {
  local source="$1" target="$2" mode="$3" directory temporary
  directory="$(dirname -- "$target")"
  temporary="$(mktemp "${directory}/.$(basename -- "$target").XXXXXX")"
  temporary_files+=("$temporary")
  install -o root -g root -m "$mode" "$source" "$temporary"
  mv -f -- "$temporary" "$target"
}

prepare_target_directories() {
  if [[ -e "$LIB_DIR" || -L "$LIB_DIR" ]]; then require_secure_root_dir "$LIB_DIR"; fi
  if [[ -e "$STATE_DIR" || -L "$STATE_DIR" ]]; then require_secure_root_dir "$STATE_DIR"; fi
  require_secure_root_dir "$SYSTEMD_DIR"
  for target in "$SCRIPT_TARGET" "$SERVICE_TARGET" "$TIMER_TARGET"; do
    require_secure_root_file_if_present "$target"
  done
  install -d -o root -g root -m 0755 "$LIB_DIR"
  install -d -o root -g root -m 0700 "$STATE_DIR"
}

install_versioned_files() {
  prepare_target_directories
  atomic_install "$SCRIPT_SOURCE" "$SCRIPT_TARGET" 0755
  atomic_install "$SERVICE_SOURCE" "$SERVICE_TARGET" 0644
  atomic_install "$TIMER_SOURCE" "$TIMER_TARGET" 0644
  systemctl daemon-reload
  require_exact_installed_files
}

verify_runtime_contract() {
  require_exact_installed_files
  systemctl is-active --quiet "$RUNNER_SERVICE" || fail 'RUNNER_TRANSPORT_HEALTH_RUNNER_NOT_ACTIVE'
  systemctl is-enabled --quiet "$TIMER" || fail 'RUNNER_TRANSPORT_HEALTH_TIMER_NOT_ENABLED'
  systemctl is-active --quiet "$TIMER" || fail 'RUNNER_TRANSPORT_HEALTH_TIMER_NOT_ACTIVE'
  systemctl cat "$HEALTH_SERVICE" >/dev/null 2>&1 || fail 'RUNNER_TRANSPORT_HEALTH_SERVICE_UNIT_UNAVAILABLE'
  systemctl cat "$TIMER" >/dev/null 2>&1 || fail 'RUNNER_TRANSPORT_HEALTH_TIMER_UNIT_UNAVAILABLE'
  printf '%s\n' \
    'runner_transport_health_installation=exact' \
    'runner_transport_health_timer=active' \
    'runner_restart_performed=false' \
    'phone_access=false' \
    'provider_access=false' \
    'network_configuration_changed=false'
}

converge_existing() {
  systemctl is-active --quiet "$RUNNER_SERVICE" || fail 'RUNNER_TRANSPORT_HEALTH_RUNNER_NOT_ACTIVE_BEFORE_CONVERGENCE'
  systemctl is-enabled --quiet "$TIMER" || fail 'RUNNER_TRANSPORT_HEALTH_TIMER_NOT_ENABLED_BEFORE_CONVERGENCE'
  systemctl is-active --quiet "$TIMER" || fail 'RUNNER_TRANSPORT_HEALTH_TIMER_NOT_ACTIVE_BEFORE_CONVERGENCE'
  if systemctl is-active --quiet "$HEALTH_SERVICE"; then
    fail 'RUNNER_TRANSPORT_HEALTH_SERVICE_BUSY'
  fi
  install_versioned_files
  systemctl is-enabled --quiet "$TIMER" || fail 'RUNNER_TRANSPORT_HEALTH_TIMER_ENABLEMENT_CHANGED'
  systemctl is-active --quiet "$TIMER" || fail 'RUNNER_TRANSPORT_HEALTH_TIMER_ACTIVITY_CHANGED'
  printf '%s\n' \
    'runner_transport_health_convergence=exact' \
    'timer_activation_changed=false' \
    'runner_restart_performed=false' \
    'phone_access=false' \
    'provider_access=false' \
    'network_configuration_changed=false'
}

case "$MODE" in
  --verify)
    verify_runtime_contract
    ;;
  --converge)
    # Stage-4 host convergence path: the existing active timer is preserved.
    # No timer/service start/stop/restart and no runner restart is performed.
    converge_existing
    ;;
  --install)
    # Explicit first-install/repair mode retains the historical activation behavior.
    # Stage-4 projection convergence must use --converge, not this mode.
    install_versioned_files
    systemctl enable --now "$TIMER"
    printf '%s\n' \
      'runner_transport_health_installation=installed' \
      'timer_activation_requested=true' \
      'runner_restart_performed=false' \
      'phone_access=false' \
      'provider_access=false' \
      'network_configuration_changed=false'
    ;;
esac
