# Production runner host resilience

This directory owns only the host-side dependencies of the existing private
GitHub Actions runner. It deliberately does not own a phone release, ADB
server lifecycle, USB device identity, runner registration, or deployment.

There are two small supervisors plus one service-scoped permission contract.

* `windows/mobile-proxy-usb-bridge.ps1` runs as the existing owner-logon
  Scheduled Task. It owns one allowlisted USBIPD device and preserves its
  USBIPD auto-attach session across WSL restarts. It never invokes ADB, never
  detaches a device, and never changes the phone.
* `wsl/runner-transport-health.*` is a root-owned systemd timer. It observes
  the existing runner and local diagnostic logs. It restarts that existing
  service only after a five-minute listener outage with three recent
  broker-TLS failures, a fifteen-minute cooldown, and a three-per-hour limit.
* `wsl/mobile-proxy-phone-runner-android-usb-permissions.conf` is a systemd
  drop-in for the existing production runner service. It grants only the
  supplementary `plugdev` group needed to write an already-udev-managed
  `root:plugdev` Android USB device node. It does not add a global user-group
  membership, device-specific rule, permissive `0666` mode, or ADB behavior.

Windows owns USB. WSL owns the outbound GitHub runner session and the
service-scoped Linux device permission required for that runner to use the
already attached device. A normal GitHub job owns the per-job ADB server and
all device observation or mutation.

The bridge uses the current usbipd-win 5.x command contract: the distribution
is the optional value of `--wsl`, not a `--distribution` flag. Every 15 seconds
the task reads local USBIPD state and attaches only the allowlisted device if
its WSL client is absent. This is deliberately used instead of usbipd's
event-driven auto-attach loop, which can survive a full WSL shutdown without
noticing that its prior client disappeared. The policy test locks this argv
shape and ownership boundary.

The bridge keeps a bounded local event log under its ProgramData directory. It
contains only generic bridge states such as `attach_failed`; it intentionally
never persists USB inventory, command output, device identifiers, or exception
text.

## Bootstrap after merge

Run these versioned installers from a trusted local administrator session:

```powershell
Set-Location \\wsl.localhost\Ubuntu\home\bose\projects\mobile-proxy-production
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\infra\runner-host\windows\install-mobile-proxy-usb-bridge.ps1
```

```bash
cd /home/bose/projects/mobile-proxy-production
sudo ./infra/runner-host/wsl/install-runner-transport-health.sh
sudo bash ./infra/runner-host/wsl/install-runner-android-usb-permissions.sh
```

The Windows installer updates only the named `MobileProxyUsbBridge` task and
its versioned script, preserving its existing owner-interactive principal. The
transport-health installer adds only a separate health timer and state
directory; it does not modify `mobile-proxy-phone-runner.service`. The Android
USB permission installer adds only a systemd drop-in to that already existing
runner service, reloads systemd, and performs one bounded restart of the same
service so the supplementary group is active. It never re-registers the
runner, changes labels, changes udev rules, or invokes ADB.

Use GitHub's canonical read-only phone observation command to prove the full
path. These host facilities never replace Controller release, intent, lock,
and postcondition contracts.

## Existing Windows proxy for the WSL runner

Controller #169 reproduced TLS termination using Git under the existing runner
identity on the direct WSL path. The same Git with the existing Windows proxy
passed the bounded comparison. The interactive shell already uses this proxy;
the runner service must configure its own process tree explicitly.

GitHub Actions Runner supports service proxy configuration through the
runner-root `.env` file loaded by `Runner.Listener` at startup. The Controller
therefore uses that runner-owned boundary instead of relying on a dynamic
systemd `EnvironmentFile`. `wsl/mobile-proxy-phone-runner-proxy.conf` adds only
a root `ExecStartPre` to the existing runner service. Immediately before each
listener activation, `prepare-runner-proxy-environment.py` resolves the current
private IPv4 default gateway and atomically reconciles one bounded
`mobile-proxy-runner-proxy` marker block in the existing runner `.env`.

The helper preserves all unrelated `.env` lines plus the existing file owner,
group and mode. It refuses a missing/unsafe/symlinked runner `.env`, malformed
owned markers, unmanaged occurrences of any proxy key it owns, and
missing/ambiguous/invalid routes. Lowercase and uppercase HTTP/HTTPS/SOCKS proxy
variables agree; loopback traffic remains local. The helper never prints proxy
values. Existing runner `ExecStart`, service identity, USB permissions, TLS
verification and .NET settings are preserved.

This design intentionally supersedes the #195 separate prerequisite-unit /
systemd-EnvironmentFile generation. Installation fails closed while that old
prepare unit is still installed, so migration is explicit: roll back the exact
older generation first, then install this generation. The installer adds only
the exact helper and runner drop-in, reloads systemd, and never restarts the
runner automatically:

```bash
sudo bash infra/runner-host/wsl/install-runner-proxy.sh --install
```

After confirming the runner is idle, one controlled restart of the existing
service activates the new generation. Acceptance observes the exact
`Runner.Listener run --startuptype service` descendant rather than assuming the
systemd MainPID wrapper is the listener. Require proxy-presence in that exact
listener, a fresh successfully established listener session, one governed
`/observe-runner-transport` with complete healthy evidence, and then the
existing `/phone-transport-preflight`. Do not treat service liveness or a
wrapper process environment as acceptance. No global Git proxy configuration
is required: runner jobs inherit the listener's environment.

Rollback from the same revision first removes only its owned marker block from
the existing runner `.env`, then removes only the exact matching helper and
drop-in. It refuses to overwrite/remove another revision and reloads systemd
without restarting the service:

```bash
sudo bash infra/runner-host/wsl/install-runner-proxy.sh --rollback
```

An independently admitted idle restart is required for the running listener to
lose the removed proxy environment. The original runner service, unrelated
`.env` settings and all other drop-ins remain intact.

The existing Windows edge-platform lifecycle is owner-logon based and manages
remote tunnels. This change does not start/reconfigure that platform, change its
selector, provision a VM, or claim unattended pre-logon availability. A `direct`
selector label may identify a remote tunnel with direct VM egress, not local
Windows direct internet. If that upstream is unavailable the runner's proxy
connections fail; the installation does not bypass it or acquire provider/recovery
authority. Independent Windows/USB lifecycle acceptance remains in Stage 4.
