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
runner-root `.env` file loaded by `Runner.Listener` at startup. That file is an
optional Runner input: absence is a valid clean state. The Controller therefore
uses the runner-owned `.env` boundary instead of a dynamic systemd
`EnvironmentFile`. `wsl/mobile-proxy-phone-runner-proxy.conf` adds only a root
`ExecStartPre` to the existing runner service. Immediately before each listener
activation, `prepare-runner-proxy-environment.py` resolves the current private
IPv4 default gateway and atomically reconciles one bounded
`mobile-proxy-runner-proxy` marker block in the runner `.env`.

For an existing `.env`, the helper preserves every unrelated line plus existing
owner, group and mode. If `.env` is absent, `--check` validates the prospective
transition without creating anything and `--apply` atomically creates `.env`
with RunnerRoot owner/group and mode `0600`. Unsafe/symlinked/group- or
world-writable existing state, malformed owned markers, unmanaged occurrences
of proxy keys it owns, and missing/ambiguous/invalid routes remain fail-closed.
Lowercase and uppercase HTTP/HTTPS/SOCKS proxy variables agree; loopback traffic
remains local. The helper never prints proxy values. Existing runner `ExecStart`,
service identity, USB permissions, TLS verification and .NET settings are
preserved.

This design supersedes the #195 separate prerequisite-unit /
systemd-EnvironmentFile generation. Installation fails closed while that old
prepare unit is still installed, so the generations never coexist. The
migration transaction is intentionally ordered:

1. validate the exact RunnerRoot, current route and prospective `.env` change
   with read-only `--check`;
2. install the exact helper and runner drop-in;
3. atomically apply the owned `.env` block before any restart;
4. reload systemd;
5. return with `runner_restart_performed=false`.

If a fresh installation fails during apply/reload, the installer attempts to
remove the just-installed generation and its owned `.env` block before
returning failure. A partially present helper/drop-in pair is rejected instead
of guessed or repaired implicitly.

For migration from #195, roll back the exact old generation once. If that
rollback has already completed and the legacy prepare unit is absent, do not
repeat it: install only the accepted new generation.

```bash
sudo bash infra/runner-host/wsl/install-runner-proxy.sh --install
```

After successful install and after confirming the runner is idle, one
separately controlled restart of the existing service activates the already
materialized `.env` generation. Acceptance observes the exact
`Runner.Listener run --startuptype service` descendant rather than assuming the
systemd MainPID wrapper is the listener. Require proxy-presence in that exact
listener, a fresh successfully established listener session, one governed
`/observe-runner-transport` with complete healthy evidence, and then the
existing `/phone-transport-preflight`. Do not treat service liveness or a
wrapper process environment as acceptance. No global Git proxy configuration
is required: runner jobs inherit the listener's environment.

Rollback from the same revision first removes only its owned marker block from
the Runner `.env`, then removes only the exact matching helper and drop-in. It
refuses another revision or partial generation and reloads systemd without
restarting the service:

```bash
sudo bash infra/runner-host/wsl/install-runner-proxy.sh --rollback
```

An independently admitted idle restart is required for the running listener to
lose the removed proxy environment. The original runner service, unrelated
`.env` settings and all other drop-ins remain intact. If `.env` was created from
an absent state and contains no unrelated settings, rollback leaves an empty,
secure runner-owned `.env`; Runner treats empty and absent `.env` equivalently.

The existing Windows edge-platform lifecycle is owner-logon based and manages
remote tunnels. This change does not start/reconfigure that platform, change its
selector, provision a VM, or claim unattended pre-logon availability. A `direct`
selector label may identify a remote tunnel with direct VM egress, not local
Windows direct internet. If that upstream is unavailable the runner's proxy
connections fail; the installation does not bypass it or acquire provider/recovery
authority. Independent Windows/USB lifecycle acceptance remains in Stage 4.
