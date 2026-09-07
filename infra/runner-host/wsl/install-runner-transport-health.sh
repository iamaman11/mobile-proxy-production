#!/usr/bin/env bash
set -euo pipefail
if (( EUID != 0 )); then
  printf '%s\n' 'Run as root: sudo ./infra/runner-host/wsl/install-runner-transport-health.sh' >&2
  exit 1
fi
source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -d -m 0755 /usr/local/lib/mobile-proxy-runner-health
install -m 0755 "$source_dir/runner-transport-health.sh" /usr/local/lib/mobile-proxy-runner-health/runner-transport-health.sh
install -m 0644 "$source_dir/mobile-proxy-runner-transport-health.service" /etc/systemd/system/mobile-proxy-runner-transport-health.service
install -m 0644 "$source_dir/mobile-proxy-runner-transport-health.timer" /etc/systemd/system/mobile-proxy-runner-transport-health.timer
install -d -m 0700 /var/lib/mobile-proxy-runner-health
systemctl daemon-reload
systemctl enable --now mobile-proxy-runner-transport-health.timer
printf '%s\n' 'Mobile Proxy runner transport health timer installed.'
