from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"


def load_script():
    if str(CONTROLLER) not in sys.path:
        sys.path.insert(0, str(CONTROLLER))
    spec = importlib.util.spec_from_file_location(
        "phone_operational_target_failure_phase_acceptance",
        SCRIPTS / "observe_phone_operational.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _observe_failure(module, tmp_path: Path, exc: Exception) -> tuple[dict[str, object], str]:
    def fail(serial: str, *, admin_token: str):
        raise exc

    module.observe_runtime_operational_health = fail
    output = tmp_path / "operational.json"
    value = module.observe(
        target="phone-production",
        release_tag="v0.1.7",
        controller_revision="e" * 40,
        serial="registered-phone-secret-identifier",
        admin_token="stage4-admin-token-secret-value",
        output=output,
    )
    return value, output.read_text(encoding="utf-8")


def test_diagnostic_phone_target_failure_preserves_only_bounded_phase(tmp_path: Path) -> None:
    module = load_script()
    value, text = _observe_failure(
        module,
        tmp_path,
        module.PhoneTargetDiagnosticFailure(
            module.PhoneFailurePhase.REGISTERED_DEVICE_NOT_DEVICE,
            "unsafe raw detail registered-phone-secret-identifier stage4-admin-token-secret-value",
        ),
    )

    assert value["classification"] == "UNKNOWN"
    assert value["failure_code"] == "PHONE_TARGET_UNAVAILABLE"
    assert value["phone_failure_phase"] == "REGISTERED_DEVICE_NOT_DEVICE"
    assert "failure_phase" not in value
    assert "unsafe raw detail" not in text
    assert "registered-phone-secret-identifier" not in text
    assert "stage4-admin-token-secret-value" not in text

    terminal = module._bounded_terminal(value)
    assert "failure_code=PHONE_TARGET_UNAVAILABLE" in terminal
    assert "phone_failure_phase=REGISTERED_DEVICE_NOT_DEVICE" in terminal
    assert "unsafe raw detail" not in terminal


def test_all_existing_diagnostic_failure_phases_are_explicitly_allowlisted(tmp_path: Path) -> None:
    module = load_script()
    expected = {
        "ADB_TOOLING_UNAVAILABLE",
        "ADB_TRANSPORT_TIMEOUT",
        "REGISTERED_DEVICE_NOT_DEVICE",
        "ROOT_SHELL_SPAWN_FAILED",
        "ROOT_SCRIPT_TIMEOUT",
        "ROOT_SCRIPT_OUTPUT_TRUNCATED",
        "ROOT_SCRIPT_PROTOCOL_MISMATCH",
        "ROOT_SCRIPT_NONZERO",
        "UNKNOWN",
    }
    assert module._ALLOWED_PHONE_FAILURE_PHASES == expected

    for index, name in enumerate(sorted(expected)):
        phase = module.PhoneFailurePhase(name)
        value, _ = _observe_failure(
            module,
            tmp_path,
            module.PhoneTargetDiagnosticFailure(phase, f"raw-{index}"),
        )
        assert value["phone_failure_phase"] == name


def test_generic_phone_target_failure_remains_unphased(tmp_path: Path) -> None:
    module = load_script()
    value, text = _observe_failure(
        module,
        tmp_path,
        module.PhoneTargetUnavailable("unbounded transport detail must not be copied"),
    )
    assert value["failure_code"] == "PHONE_TARGET_UNAVAILABLE"
    assert "phone_failure_phase" not in value
    assert "unbounded transport detail" not in text


def test_phone_failure_phase_is_semantically_separate_and_fail_closed() -> None:
    module = load_script()
    invalid = (
        {
            "classification": "UNKNOWN",
            "failure_code": "PHONE_TARGET_UNAVAILABLE",
            "phone_failure_phase": "raw-adb-stderr",
        },
        {
            "classification": "UNKNOWN",
            "failure_code": "RUNTIME_OPERATIONAL_OBSERVATION_UNAVAILABLE",
            "phone_failure_phase": "REGISTERED_DEVICE_NOT_DEVICE",
        },
        {
            "classification": "UNKNOWN",
            "failure_code": "PHONE_TARGET_UNAVAILABLE",
            "failure_phase": "process_count_start",
        },
    )
    for payload in invalid:
        try:
            module._bounded_terminal(payload)
        except ValueError:
            pass
        else:
            raise AssertionError("cross-domain or unbounded failure metadata must fail closed")


def test_workflow_independently_validates_phone_target_failure_phase() -> None:
    source = (WORKFLOWS / "phone-operational-observation.yml").read_text(encoding="utf-8")
    assert "phone_failure_phase = value.get('phone_failure_phase')" in source
    assert "failure_code != 'PHONE_TARGET_UNAVAILABLE'" in source
    assert "phone_failure_phase={phone_failure_phase}" in source
    for phase in (
        "ADB_TOOLING_UNAVAILABLE",
        "ADB_TRANSPORT_TIMEOUT",
        "REGISTERED_DEVICE_NOT_DEVICE",
        "ROOT_SHELL_SPAWN_FAILED",
        "ROOT_SCRIPT_TIMEOUT",
        "ROOT_SCRIPT_OUTPUT_TRUNCATED",
        "ROOT_SCRIPT_PROTOCOL_MISMATCH",
        "ROOT_SCRIPT_NONZERO",
        "UNKNOWN",
    ):
        assert phase in source
