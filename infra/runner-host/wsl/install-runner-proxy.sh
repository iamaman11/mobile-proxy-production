#!/usr/bin/env bash
set -euo pipefail

readonly RUNNER_SERVICE='mobile-proxy-phone-runner.service'
readonly LEGACY_PREPARE_UNIT='/etc/systemd/system/mobile-proxy-runner-proxy-prepare.service'
readonly SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly LIB_DIR='/usr/local/lib/mobile-proxy-runner-proxy'
readonly HELPER='prepare-runner-proxy-environment.py'
readonly DROPIN_DIR="/etc/systemd/system/${RUNNER_SERVICE}.d"
readonly DROPIN="${DROPIN_DIR}/40-mobile-proxy-proxy.conf"
readonly SOURCE_DROPIN="${SOURCE_DIR}/mobile-proxy-phone-runner-proxy.conf"

fail() { printf '%s\n' "$1" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || fail 'RUNNER_PROXY_INSTALL_REQUIRES_ROOT'
[[ $# -eq 1 && ( "$1" == '--install' || "$1" == '--rollback' ) ]] || fail 'usage: install-runner-proxy.sh --install|--rollback'
for tool in systemctl install cmp python3; do
  command -v "$tool" >/dev/null || fail 'RUNNER_PROXY_REQUIRED_TOOL_UNAVAILABLE'
done
[[ -x /usr/sbin/ip ]] || fail 'RUNNER_PROXY_IP_TOOL_UNAVAILABLE'
[[ -f "${SOURCE_DIR}/${HELPER}" && -f "$SOURCE_DROPIN" ]] || fail 'RUNNER_PROXY_VERSIONED_SOURCE_UNAVAILABLE'
systemctl cat "$RUNNER_SERVICE" >/dev/null 2>&1 || fail 'RUNNER_PROXY_EXISTING_SERVICE_UNAVAILABLE'
runner_user="$(systemctl show --property=User --value "$RUNNER_SERVICE")"
[[ -n "$runner_user" && "$runner_user" != 'root' ]] || fail 'RUNNER_PROXY_SERVICE_IDENTITY_INVALID'

# This revision supersedes the #195 prerequisite-unit generation. Require its
# exact rollback before install instead of silently coexisting with stale
# infrastructure owned by another accepted revision.
if [[ "$1" == '--install' && -e "$LEGACY_PREPARE_UNIT" ]]; then
  fail 'RUNNER_PROXY_LEGACY_PREPARE_UNIT_PRESENT'
fi

# Additive first installation, or exact idempotent re-installation only. Never
# overwrite another revision/unmanaged file, including during rollback.
for directory in "$LIB_DIR" "$DROPIN_DIR"; do
  [[ ! -L "$directory" ]] || fail 'RUNNER_PROXY_DIRECTORY_SYMLINK_REFUSED'
done
for target in "$DROPIN" "${LIB_DIR}/${HELPER}"; do
  [[ ! -L "$target" ]] || fail 'RUNNER_PROXY_FILE_SYMLINK_REFUSED'
done
if [[ -e "$DROPIN" ]]; then
  cmp -s "$SOURCE_DROPIN" "$DROPIN" || fail 'RUNNER_PROXY_EXISTING_DROPIN_DIFFERS'
fi
if [[ -e "${LIB_DIR}/${HELPER}" ]]; then
  cmp -s "${SOURCE_DIR}/${HELPER}" "${LIB_DIR}/${HELPER}" || fail 'RUNNER_PROXY_EXISTING_HELPER_DIFFERS'
fi

if [[ "$1" == '--install' ]]; then
  install -d -o root -g root -m 0755 "$LIB_DIR" "$DROPIN_DIR"
  install -o root -g root -m 0644 "${SOURCE_DIR}/${HELPER}" "${LIB_DIR}/${HELPER}"
  install -o root -g root -m 0644 "$SOURCE_DROPIN" "$DROPIN"
  systemctl daemon-reload
  printf '%s\n' 'runner_proxy_config=installed' 'runner_restart_performed=false'
else
  # Remove only the marker block owned by this revision before removing its
  # exact files. The running listener keeps its current environment until a
  # separately admitted idle restart.
  /usr/bin/python3 -I -B "${LIB_DIR}/${HELPER}" --remove >/dev/null || fail 'RUNNER_PROXY_ENV_ROLLBACK_FAILED'
  /usr/bin/python3 -I -c 'from pathlib import Path; Path("/etc/systemd/system/mobile-proxy-phone-runner.service.d/40-mobile-proxy-proxy.conf").unlink(missing_ok=True); Path("/usr/local/lib/mobile-proxy-runner-proxy/prepare-runner-proxy-environment.py").unlink(missing_ok=True)'
  systemctl daemon-reload
  printf '%s\n' 'runner_proxy_config=rolled_back' 'runner_restart_performed=false'
fi
