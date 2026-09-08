#!/usr/bin/env bash
set -euo pipefail

readonly RUNNER_SERVICE="mobile-proxy-phone-runner.service"
readonly REQUIRED_GROUP="plugdev"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SOURCE_DROPIN="${SCRIPT_DIR}/mobile-proxy-phone-runner-android-usb-permissions.conf"
readonly DROPIN_DIR="/etc/systemd/system/${RUNNER_SERVICE}.d"
readonly DROPIN_PATH="${DROPIN_DIR}/20-mobile-proxy-android-usb-permissions.conf"
readonly ACTIVATION_TIMEOUT_SECONDS=15

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ ${EUID} -eq 0 ]] || fail "installer must run as root"
command -v systemctl >/dev/null 2>&1 || fail "systemctl is required"
command -v getent >/dev/null 2>&1 || fail "getent is required"
command -v install >/dev/null 2>&1 || fail "install is required"
command -v awk >/dev/null 2>&1 || fail "awk is required"
command -v cut >/dev/null 2>&1 || fail "cut is required"
[[ -f "${SOURCE_DROPIN}" ]] || fail "versioned runner permission drop-in is missing"

getent group "${REQUIRED_GROUP}" >/dev/null 2>&1 || fail "required plugdev group does not exist"
systemctl cat "${RUNNER_SERVICE}" >/dev/null 2>&1 || fail "existing production runner service is missing"

runner_user="$(systemctl show --property=User --value "${RUNNER_SERVICE}")"
[[ -n "${runner_user}" ]] || fail "runner service User is empty"
[[ "${runner_user}" != "root" ]] || fail "runner service unexpectedly runs as root"

install -d -m 0755 "${DROPIN_DIR}"
install -m 0644 "${SOURCE_DROPIN}" "${DROPIN_PATH}"
systemctl daemon-reload

supplementary_groups="$(systemctl show --property=SupplementaryGroups --value "${RUNNER_SERVICE}")"
case " ${supplementary_groups} " in
  *" ${REQUIRED_GROUP} "*) ;;
  *) fail "runner service did not load required supplementary group" ;;
esac

systemctl restart --no-block "${RUNNER_SERVICE}"

deadline=$((SECONDS + ACTIVATION_TIMEOUT_SECONDS))
until systemctl is-active --quiet "${RUNNER_SERVICE}"; do
  (( SECONDS < deadline )) || fail "runner service did not become active within bounded activation window"
  sleep 1
done

main_pid="$(systemctl show --property=MainPID --value "${RUNNER_SERVICE}")"
[[ "${main_pid}" =~ ^[1-9][0-9]*$ ]] || fail "runner service has no live MainPID after restart"
[[ -r "/proc/${main_pid}/status" ]] || fail "runner service process credentials are unreadable"
plugdev_gid="$(getent group "${REQUIRED_GROUP}" | cut -d: -f3)"
[[ "${plugdev_gid}" =~ ^[0-9]+$ ]] || fail "plugdev group id is invalid"
process_groups="$(awk '/^Groups:/ {for (i=2; i<=NF; i++) printf "%s ", $i}' "/proc/${main_pid}/status")"
case " ${process_groups} " in
  *" ${plugdev_gid} "*) ;;
  *) fail "runner service process did not acquire required supplementary group" ;;
esac

printf 'runner_android_usb_permissions=active\n'
printf 'runner_service=%s\n' "${RUNNER_SERVICE}"
printf 'supplementary_group=%s\n' "${REQUIRED_GROUP}"
