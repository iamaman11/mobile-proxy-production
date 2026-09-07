from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WINDOWS = ROOT / "infra/runner-host/windows/mobile-proxy-usb-bridge.ps1"
WINDOWS_INSTALLER = ROOT / "infra/runner-host/windows/install-mobile-proxy-usb-bridge.ps1"
WATCHDOG = ROOT / "infra/runner-host/wsl/runner-transport-health.sh"
UNIT = ROOT / "infra/runner-host/wsl/mobile-proxy-runner-transport-health.service"
TIMER = ROOT / "infra/runner-host/wsl/mobile-proxy-runner-transport-health.timer"

def test_windows_bridge_has_one_allowlisted_usbipd_ownership_boundary() -> None:
    script = WINDOWS.read_text(encoding="utf-8").lower()
    assert "validatepattern('^\\d+-\\d+$')" in script
    assert "validatepattern('^[0-9a-fa-f]{4}:[0-9a-fa-f]{4}$')" in script
    assert "usbipd.exe bind --busid $busid" in script
    assert "usbipd.exe attach --wsl $distro --busid $busid --auto-attach --unplugged" in script
    assert "attach --wsl --distribution" not in script
    for forbidden in ("adb get-state", "adb kill-server", "adb.exe", "udevadm", "usbipd.exe detach"):
        assert forbidden not in script

def test_windows_installer_updates_only_existing_named_task() -> None:
    script = WINDOWS_INSTALLER.read_text(encoding="utf-8")
    assert "MobileProxyUsbBridge" in script
    assert "Get-ScheduledTask" in script
    assert "refusing to create" in script
    assert "Set-ScheduledTask -TaskName $TaskName -Action $action" in script

def test_watchdog_is_bounded_and_never_reconfigures_runner_or_phone() -> None:
    script = WATCHDOG.read_text(encoding="utf-8")
    assert "readonly STALE_SECONDS=300" in script
    assert "readonly FAILURE_THRESHOLD=3" in script
    assert "readonly COOLDOWN_SECONDS=900" in script
    assert "readonly MAX_RESTARTS_PER_HOUR=3" in script
    assert 'systemctl restart "$RUNNER_SERVICE"' in script
    for forbidden in ("config.sh", "registration", "usbipd", "adb ", "adb\n", "curl ", "iptables"):
        assert forbidden not in script.lower()

def test_watchdog_runs_as_a_root_owned_timer_with_hardening() -> None:
    unit = UNIT.read_text(encoding="utf-8")
    timer = TIMER.read_text(encoding="utf-8")
    assert "User=root" in unit
    assert "ProtectSystem=strict" in unit
    assert "OnUnitActiveSec=1min" in timer
    assert "Persistent=true" in timer
