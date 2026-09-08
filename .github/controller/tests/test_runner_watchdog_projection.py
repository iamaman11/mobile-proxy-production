from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WSL = ROOT / "infra" / "runner-host" / "wsl"
CONTROLLER = ROOT / ".github" / "controller"
SCRIPTS = ROOT / ".github" / "scripts"


def _shell_function_body(source: str, name: str) -> str:
    marker = f"{name}() {{"
    assert marker in source
    return source.split(marker, 1)[1].split("\n}\n", 1)[0]


def _load_projection_diagnostic():
    path = SCRIPTS / "diagnose_runner_watchdog_projection.py"
    spec = importlib.util.spec_from_file_location("diagnose_runner_watchdog_projection_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_projection(path: Path, **overrides: object) -> None:
    value: dict[str, object] = {
        "schema": "runner-transport-health-projection.v1",
        "observed_at_epoch": 900,
        "expires_at_epoch": 1100,
        "decision": "HEALTHY",
        "cooldown_active": False,
        "restart_budget_remaining": 3,
        "post_restart_grace_active": False,
        "restart_count_window": 0,
    }
    value.update(overrides)
    path.write_text(json.dumps(value) + "\n", encoding="ascii")
    path.chmod(0o644)


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


def test_installer_has_explicit_safe_convergence_and_exact_verify_modes() -> None:
    source = (WSL / "install-runner-transport-health.sh").read_text(encoding="utf-8")
    required = (
        "--install' || \"$1\" == '--converge' || \"$1\" == '--verify'",
        "RUNNER_TRANSPORT_HEALTH_INSTALL_REQUIRES_ROOT",
        "RUNNER_TRANSPORT_HEALTH_DIRECTORY_OWNER_INVALID",
        "RUNNER_TRANSPORT_HEALTH_DIRECTORY_MODE_INVALID",
        "RUNNER_TRANSPORT_HEALTH_EXISTING_FILE_OWNER_INVALID",
        "RUNNER_TRANSPORT_HEALTH_EXISTING_FILE_MODE_INVALID",
        "cmp -s \"$SCRIPT_SOURCE\" \"$SCRIPT_TARGET\"",
        "cmp -s \"$SERVICE_SOURCE\" \"$SERVICE_TARGET\"",
        "cmp -s \"$TIMER_SOURCE\" \"$TIMER_TARGET\"",
        "install -d -o root -g root -m 0700 \"$STATE_DIR\"",
        "temporary=\"$(mktemp",
        "install -o root -g root -m \"$mode\" \"$source\" \"$temporary\"",
        "mv -f -- \"$temporary\" \"$target\"",
        "systemctl daemon-reload",
        "runner_restart_performed=false",
        "phone_access=false",
        "provider_access=false",
        "network_configuration_changed=false",
    )
    missing = [item for item in required if item not in source]
    assert missing == []
    for forbidden in (
        "systemctl restart mobile-proxy-phone-runner.service",
        "systemctl restart mobile-proxy-runner-transport-health.service",
        "systemctl start mobile-proxy-runner-transport-health.service",
        "systemctl stop mobile-proxy-runner-transport-health.timer",
        "systemctl restart mobile-proxy-runner-transport-health.timer",
        "chmod -R",
    ):
        assert forbidden not in source


def test_stage4_converge_mode_preserves_existing_active_timer_without_activation() -> None:
    source = (WSL / "install-runner-transport-health.sh").read_text(encoding="utf-8")
    body = _shell_function_body(source, "converge_existing")
    required = (
        'systemctl is-active --quiet "$RUNNER_SERVICE"',
        'systemctl is-enabled --quiet "$TIMER"',
        'systemctl is-active --quiet "$TIMER"',
        'systemctl is-active --quiet "$HEALTH_SERVICE"',
        "RUNNER_TRANSPORT_HEALTH_SERVICE_BUSY",
        "install_versioned_files",
        "RUNNER_TRANSPORT_HEALTH_TIMER_ENABLEMENT_CHANGED",
        "RUNNER_TRANSPORT_HEALTH_TIMER_ACTIVITY_CHANGED",
        "timer_activation_changed=false",
        "runner_restart_performed=false",
    )
    missing = [item for item in required if item not in body]
    assert missing == []
    forbidden = (
        "enable --now",
        "systemctl start",
        "systemctl stop",
        "systemctl restart",
    )
    present = [item for item in forbidden if item in body]
    assert present == []


def test_verify_mode_is_read_only_and_requires_exact_active_runtime_contract() -> None:
    source = (WSL / "install-runner-transport-health.sh").read_text(encoding="utf-8")
    body = _shell_function_body(source, "verify_runtime_contract")
    required = (
        "require_exact_installed_files",
        'systemctl is-active --quiet "$RUNNER_SERVICE"',
        'systemctl is-enabled --quiet "$TIMER"',
        'systemctl is-active --quiet "$TIMER"',
        'systemctl cat "$HEALTH_SERVICE"',
        'systemctl cat "$TIMER"',
        "runner_transport_health_installation=exact",
        "runner_transport_health_timer=active",
        "runner_restart_performed=false",
    )
    missing = [item for item in required if item not in body]
    assert missing == []
    forbidden = (
        "install ",
        "mv ",
        "daemon-reload",
        "enable --now",
        "systemctl start",
        "systemctl stop",
        "systemctl restart",
    )
    present = [item for item in forbidden if item in body]
    assert present == []


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


def test_projection_diagnostic_reports_trusted_projection_without_raw_values() -> None:
    diagnostic = _load_projection_diagnostic()
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        projection = directory / "observation.json"
        _write_projection(projection)
        reason, value = diagnostic.diagnose_projection(
            projection,
            now_epoch=1000,
            expected_owner_uid=os.getuid(),
        )
    assert reason == "NONE"
    assert value is not None
    assert value["decision"] == "HEALTHY"


def test_projection_diagnostic_distinguishes_stale_from_trust_and_shape_failures() -> None:
    diagnostic = _load_projection_diagnostic()
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        projection = directory / "observation.json"

        _write_projection(projection, expires_at_epoch=950)
        reason, value = diagnostic.diagnose_projection(
            projection,
            now_epoch=1000,
            expected_owner_uid=os.getuid(),
        )
        assert (reason, value) == ("PROJECTION_STALE", None)

        _write_projection(projection)
        projection.chmod(0o666)
        reason, value = diagnostic.diagnose_projection(
            projection,
            now_epoch=1000,
            expected_owner_uid=os.getuid(),
        )
        assert (reason, value) == ("FILE_MODE_INVALID", None)

        _write_projection(projection)
        payload = json.loads(projection.read_text(encoding="ascii"))
        payload["unexpected"] = True
        projection.write_text(json.dumps(payload) + "\n", encoding="ascii")
        projection.chmod(0o644)
        reason, value = diagnostic.diagnose_projection(
            projection,
            now_epoch=1000,
            expected_owner_uid=os.getuid(),
        )
        assert (reason, value) == ("FIELDS_INVALID", None)


def test_projection_diagnostic_is_read_only_and_bounded() -> None:
    source = (SCRIPTS / "diagnose_runner_watchdog_projection.py").read_text(encoding="utf-8")
    required = (
        "projection_rejection=",
        "projection_trusted=",
        "watchdog_decision=",
        "PARSER_INTERNAL_MISMATCH",
        "PROJECTION_STALE",
        "PARENT_OWNER_INVALID",
        "FILE_MODE_INVALID",
    )
    missing = [item for item in required if item not in source]
    assert missing == []
    forbidden = (
        "subprocess",
        "systemctl",
        "journalctl",
        "/var/lib/mobile-proxy-runner-health/state",
        "systemctl restart",
        "systemctl start",
        "systemctl stop",
        "adb ",
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
