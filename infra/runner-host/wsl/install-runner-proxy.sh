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
runner_workdir="$(systemctl show --property=WorkingDirectory --value "$RUNNER_SERVICE")"
[[ "$runner_workdir" == /* && -d "$runner_workdir" && ! -L "$runner_workdir" ]] || fail 'RUNNER_PROXY_WORKING_DIRECTORY_INVALID'

# This generation supersedes the #195 prerequisite-unit EnvironmentFile path.
# Never silently coexist with that older accepted generation.
if [[ "$1" == '--install' && -e "$LEGACY_PREPARE_UNIT" ]]; then
  fail 'RUNNER_PROXY_LEGACY_PREPARE_UNIT_PRESENT'
fi

for directory in "$LIB_DIR" "$DROPIN_DIR"; do
  [[ ! -L "$directory" ]] || fail 'RUNNER_PROXY_DIRECTORY_SYMLINK_REFUSED'
done
for target in "$DROPIN" "${LIB_DIR}/${HELPER}"; do
  [[ ! -L "$target" ]] || fail 'RUNNER_PROXY_FILE_SYMLINK_REFUSED'
done

helper_present=false
dropin_present=false
[[ -e "${LIB_DIR}/${HELPER}" ]] && helper_present=true
[[ -e "$DROPIN" ]] && dropin_present=true
[[ "$helper_present" == "$dropin_present" ]] || fail 'RUNNER_PROXY_PARTIAL_GENERATION_PRESENT'

generation_preexisting=false
if [[ "$helper_present" == true ]]; then
  cmp -s "${SOURCE_DIR}/${HELPER}" "${LIB_DIR}/${HELPER}" || fail 'RUNNER_PROXY_EXISTING_HELPER_DIFFERS'
  cmp -s "$SOURCE_DROPIN" "$DROPIN" || fail 'RUNNER_PROXY_EXISTING_DROPIN_DIFFERS'
  generation_preexisting=true
fi

remove_generation_files() {
  /usr/bin/python3 -I -c 'from pathlib import Path; Path("/etc/systemd/system/mobile-proxy-phone-runner.service.d/40-mobile-proxy-proxy.conf").unlink(missing_ok=True); Path("/usr/local/lib/mobile-proxy-runner-proxy/prepare-runner-proxy-environment.py").unlink(missing_ok=True)'
}

rollback_fresh_install() {
  [[ "$generation_preexisting" == false ]] || return 0
  local failed=0
  if [[ -f "${LIB_DIR}/${HELPER}" ]]; then
    (
      cd -- "$runner_workdir"
      /usr/bin/python3 -I -B "${LIB_DIR}/${HELPER}" --remove >/dev/null
    ) || failed=1
  fi
  remove_generation_files || failed=1
  systemctl daemon-reload >/dev/null 2>&1 || failed=1
  [[ $failed -eq 0 ]]
}

if [[ "$1" == '--install' ]]; then
  # Validate the prospective transition before any write. The accepted Runner
  # contract permits .env to be absent; --check must therefore validate both
  # ABSENT and SAFE_EXISTING without creating or changing the file.
  (
    cd -- "$runner_workdir"
    /usr/bin/python3 -I -B "${SOURCE_DIR}/${HELPER}" --check >/dev/null
  ) || fail 'RUNNER_PROXY_PREINSTALL_CHECK_FAILED'

  install -d -o root -g root -m 0755 "$LIB_DIR" "$DROPIN_DIR"
  install -o root -g root -m 0644 "${SOURCE_DIR}/${HELPER}" "${LIB_DIR}/${HELPER}"
  install -o root -g root -m 0644 "$SOURCE_DROPIN" "$DROPIN"

  # Materialize the exact Runner-supported .env generation before any later
  # controlled restart. This is atomic inside the helper and exposes no values.
  if ! (
    cd -- "$runner_workdir"
    /usr/bin/python3 -I -B "${LIB_DIR}/${HELPER}" --apply >/dev/null
  ); then
    rollback_fresh_install || fail 'RUNNER_PROXY_INSTALL_ROLLBACK_FAILED'
    fail 'RUNNER_PROXY_ENV_APPLY_FAILED'
  fi

  if ! systemctl daemon-reload; then
    rollback_fresh_install || fail 'RUNNER_PROXY_INSTALL_ROLLBACK_FAILED'
    fail 'RUNNER_PROXY_SYSTEMD_RELOAD_FAILED'
  fi
  printf '%s\n' 'runner_proxy_config=installed' 'runner_proxy_env=applied' 'runner_restart_performed=false'
else
  [[ "$generation_preexisting" == true ]] || fail 'RUNNER_PROXY_GENERATION_NOT_INSTALLED'

  # Remove only this generation's owned marker block before removing exact
  # helper/drop-in files. The running listener keeps its current process
  # environment until a separately admitted idle restart.
  (
    cd -- "$runner_workdir"
    /usr/bin/python3 -I -B "${LIB_DIR}/${HELPER}" --remove >/dev/null
  ) || fail 'RUNNER_PROXY_ENV_ROLLBACK_FAILED'
  remove_generation_files
  systemctl daemon-reload
  printf '%s\n' 'runner_proxy_config=rolled_back' 'runner_restart_performed=false'
fi
