#!/usr/bin/env bash
set -euo pipefail

readonly RUNNER_SERVICE='mobile-proxy-phone-runner.service'
readonly PREPARE_UNIT='mobile-proxy-runner-proxy-prepare.service'
readonly SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly LIB_DIR='/usr/local/lib/mobile-proxy-runner-proxy'
readonly HELPER='prepare-runner-proxy-environment.py'
readonly DROPIN_DIR="/etc/systemd/system/${RUNNER_SERVICE}.d"
readonly DROPIN="${DROPIN_DIR}/40-mobile-proxy-proxy.conf"
readonly SOURCE_DROPIN="${SOURCE_DIR}/mobile-proxy-phone-runner-proxy.conf"
readonly INSTALLED_PREPARE_UNIT="/etc/systemd/system/${PREPARE_UNIT}"
readonly SOURCE_PREPARE_UNIT="${SOURCE_DIR}/${PREPARE_UNIT}"

fail() { printf '%s\n' "$1" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || fail 'RUNNER_PROXY_INSTALL_REQUIRES_ROOT'
[[ $# -eq 1 && ( "$1" == '--install' || "$1" == '--rollback' ) ]] || fail 'usage: install-runner-proxy.sh --install|--rollback'
for tool in systemctl install cmp python3; do
  command -v "$tool" >/dev/null || fail 'RUNNER_PROXY_REQUIRED_TOOL_UNAVAILABLE'
done
[[ -x /usr/sbin/ip ]] || fail 'RUNNER_PROXY_IP_TOOL_UNAVAILABLE'
[[ -f "${SOURCE_DIR}/${HELPER}" && -f "$SOURCE_DROPIN" && -f "$SOURCE_PREPARE_UNIT" ]] || fail 'RUNNER_PROXY_VERSIONED_SOURCE_UNAVAILABLE'
systemctl cat "$RUNNER_SERVICE" >/dev/null 2>&1 || fail 'RUNNER_PROXY_EXISTING_SERVICE_UNAVAILABLE'
runner_user="$(systemctl show --property=User --value "$RUNNER_SERVICE")"
[[ -n "$runner_user" && "$runner_user" != 'root' ]] || fail 'RUNNER_PROXY_SERVICE_IDENTITY_INVALID'

# Additive first installation, or exact idempotent re-installation only. Never
# overwrite another revision/unmanaged file, including during rollback.
for directory in "$LIB_DIR" "$DROPIN_DIR"; do
  [[ ! -L "$directory" ]] || fail 'RUNNER_PROXY_DIRECTORY_SYMLINK_REFUSED'
done
for target in "$DROPIN" "${LIB_DIR}/${HELPER}" "$INSTALLED_PREPARE_UNIT"; do
  [[ ! -L "$target" ]] || fail 'RUNNER_PROXY_FILE_SYMLINK_REFUSED'
done
if [[ -e "$DROPIN" ]]; then
  cmp -s "$SOURCE_DROPIN" "$DROPIN" || fail 'RUNNER_PROXY_EXISTING_DROPIN_DIFFERS'
fi
if [[ -e "${LIB_DIR}/${HELPER}" ]]; then
  cmp -s "${SOURCE_DIR}/${HELPER}" "${LIB_DIR}/${HELPER}" || fail 'RUNNER_PROXY_EXISTING_HELPER_DIFFERS'
fi
if [[ -e "$INSTALLED_PREPARE_UNIT" ]]; then
  cmp -s "$SOURCE_PREPARE_UNIT" "$INSTALLED_PREPARE_UNIT" || fail 'RUNNER_PROXY_EXISTING_PREPARE_UNIT_DIFFERS'
fi

if [[ "$1" == '--install' ]]; then
  install -d -o root -g root -m 0755 "$LIB_DIR" "$DROPIN_DIR"
  install -o root -g root -m 0644 "${SOURCE_DIR}/${HELPER}" "${LIB_DIR}/${HELPER}"
  install -o root -g root -m 0644 "$SOURCE_PREPARE_UNIT" "$INSTALLED_PREPARE_UNIT"
  install -o root -g root -m 0644 "$SOURCE_DROPIN" "$DROPIN"
  systemctl daemon-reload
  printf '%s\n' 'runner_proxy_config=installed' 'runner_restart_performed=false'
else
  # Exact-file equality was checked above. Existing service/other drop-ins are retained.
  /usr/bin/python3 -I -c 'from pathlib import Path; Path("/etc/systemd/system/mobile-proxy-phone-runner.service.d/40-mobile-proxy-proxy.conf").unlink(missing_ok=True); Path("/usr/local/lib/mobile-proxy-runner-proxy/prepare-runner-proxy-environment.py").unlink(missing_ok=True); Path("/etc/systemd/system/mobile-proxy-runner-proxy-prepare.service").unlink(missing_ok=True)'
  systemctl daemon-reload
  printf '%s\n' 'runner_proxy_config=rolled_back' 'runner_restart_performed=false'
fi
