from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WSL = ROOT / "infra" / "runner-host" / "wsl"
CONTROLLER = ROOT / ".github" / "controller"


def test_watchdog_keeps_private_governor_state_and_publishes_only_projection() -> None:
    source = (WSL / "runner-transport-health.sh").read_text(encoding="utf-8")
    required = (
        'readonly STATE_DIR="/var/lib/mobile-proxy-runner-health"',
        'readonly STATE_FILE="${STATE_DIR}/state"',
        'readonly PROJECTION_DIR="/run/mobile-proxy-runner-health"',
        'readonly PROJECTION_FILE="${PROJECTION_DIR}/observation.json"',
        'readonly PROJECTION_SCHEMA="runner-transport-health-projection.v1"',
        'readonly PROJECTION_TTL_SECONDS=180',
        'install -d -m 0700 "$STATE_DIR"',
        'install -d -m 0755 "$PROJECTION_DIR"',
        'chmod 0644 "$temporary"',
        'mv -f "$temporary" "$PROJECTION_FILE"',
        'finish_with_projection HEALTHY',
        'write_projection RESTART_ELIGIBLE',
        'write_projection OBSERVE',
        'write_projection UNKNOWN',
        'systemctl restart "$RUNNER_SERVICE"',
    )
    missing = [item for item in required if item not in source]
    assert missing == []
    forbidden = (
        'chmod 0755 "$STATE_DIR"',
        'chmod 0755 "$STATE_FILE"',
        'chmod 0644 "$STATE_FILE"',
        'install -d -m 0755 "$STATE_DIR"',
        'cp "$STATE_FILE" "$PROJECTION_FILE"',
        'cat "$STATE_FILE" > "$PROJECTION_FILE"',
    )
    present = [item for item in forbidden if item in source]
    assert present == []


def test_systemd_owns_ephemeral_readable_projection_directory_without_relaxing_private_state() -> None:
    source = (WSL / "mobile-proxy-runner-transport-health.service").read_text(encoding="utf-8")
    required = (
        "User=root",
        "Group=root",
        "ProtectSystem=strict",
        "RuntimeDirectory=mobile-proxy-runner-health",
        "RuntimeDirectoryMode=0755",
        "RuntimeDirectoryPreserve=yes",
        "ReadWritePaths=/var/lib/mobile-proxy-runner-health /run/mobile-proxy-runner-health",
    )
    missing = [item for item in required if item not in source]
    assert missing == []


def test_installer_preserves_private_state_and_does_not_run_recovery_service_directly() -> None:
    source = (WSL / "install-runner-transport-health.sh").read_text(encoding="utf-8")
    assert "install -d -m 0700 /var/lib/mobile-proxy-runner-health" in source
    assert "systemctl daemon-reload" in source
    assert "systemctl enable --now mobile-proxy-runner-transport-health.timer" in source
    for forbidden in (
        "systemctl restart mobile-proxy-phone-runner.service",
        "systemctl restart mobile-proxy-runner-transport-health.service",
        "systemctl start mobile-proxy-runner-transport-health.service",
        "chmod -R",
    ):
        assert forbidden not in source


def test_runner_observer_reads_only_bounded_projection_and_owns_no_restart_thresholds() -> None:
    source = (CONTROLLER / "runner_transport_probe.py").read_text(encoding="utf-8")
    required = (
        'WATCHDOG_PROJECTION = Path("/run/mobile-proxy-runner-health/observation.json")',
        'WATCHDOG_PROJECTION_SCHEMA = "runner-transport-health-projection.v1"',
        '"HEALTHY", "OBSERVE", "RESTART_ELIGIBLE", "RATE_LIMITED", "UNKNOWN"',
        "_parse_watchdog_projection",
    )
    missing = [item for item in required if item not in source]
    assert missing == []
    forbidden = (
        "/var/lib/mobile-proxy-runner-health/state",
        "WATCHDOG_COOLDOWN_SECONDS",
        "WATCHDOG_WINDOW_SECONDS",
        "WATCHDOG_MAX_RESTARTS",
        "WATCHDOG_POST_RESTART_GRACE_SECONDS",
        "systemctl restart",
    )
    present = [item for item in forbidden if item in source]
    assert present == []


def main() -> int:
    tests = sorted(
        (value for name, value in globals().items() if name.startswith("test_") and callable(value)),
        key=lambda item: item.__name__,
    )
    for test in tests:
        test()
    print(f"RUNNER_WATCHDOG_PROJECTION_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
