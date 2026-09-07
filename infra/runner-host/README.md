# Production runner host resilience

This directory owns only the host-side dependencies of the existing private
GitHub Actions runner. It deliberately does not own a phone release, ADB
server lifecycle, USB device identity, runner registration, or deployment.

There are two small supervisors.

* `windows/mobile-proxy-usb-bridge.ps1` runs as the existing owner-logon
  Scheduled Task. It owns one allowlisted USBIPD device and preserves its
  USBIPD auto-attach session across WSL restarts. It never invokes ADB, never
  detaches a device, and never changes the phone.
* `wsl/runner-transport-health.*` is a root-owned systemd timer. It observes
  the existing runner and local diagnostic logs. It restarts that existing
  service only after a five-minute listener outage with three recent
  broker-TLS failures, a fifteen-minute cooldown, and a three-per-hour limit.

Windows owns USB. WSL owns the outbound GitHub runner session. A normal GitHub
job owns the per-job ADB server and all device observation or mutation.

The bridge uses the current usbipd-win 5.x command contract: the distribution
is the optional value of `--wsl`, not a `--distribution` flag. Every 15 seconds
the task reads local USBIPD state and attaches only the allowlisted device if
its WSL client is absent. This is deliberately used instead of usbipd's
event-driven auto-attach loop, which can survive a full WSL shutdown without
noticing that its prior client disappeared. The policy test locks this argv
shape and ownership boundary.

## Bootstrap after merge

Run these versioned installers from a trusted local administrator session:

```powershell
Set-Location \\wsl.localhost\Ubuntu\home\bose\projects\mobile-proxy-production
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\infra\runner-host\windows\install-mobile-proxy-usb-bridge.ps1
```

```bash
cd /home/bose/projects/mobile-proxy-production
sudo ./infra/runner-host/wsl/install-runner-transport-health.sh
```

The Windows installer updates only the named `MobileProxyUsbBridge` task and
its versioned script, preserving its existing owner-interactive principal. The
Linux installer adds only a separate health timer and state directory; it does
not modify `mobile-proxy-phone-runner.service`.

Use GitHub's canonical read-only phone observation command to prove the full
path. These host facilities never replace Controller release, intent, lock,
and postcondition contracts.
