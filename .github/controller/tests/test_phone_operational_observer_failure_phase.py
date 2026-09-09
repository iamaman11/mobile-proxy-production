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
        "phone_operational_observer_failure_phase_acceptance",
        SCRIPTS / "observe_phone_operational.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _observe_failure(module, tmp_path: Path, message: str) -> tuple[dict[str, object], str]:
    def fail(serial: str, *, admin_token: str):
        raise module.PhoneTargetUnavailable(
            f"{message}" if message in {module.runtime_observer._UNAVAILABLE, module.runtime_observer._MALFORMED}
            else message
        )

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


def test_binding_validation_is_bounded_observer_phase(tmp_path: Path) -> None:
    module = load_script()
    value, text = _observe_failure(module, tmp_path, module.runtime_observer._UNAVAILABLE)

    assert value["classification"] == "UNKNOWN"
    assert value["failure_code"] == "PHONE_TARGET_UNAVAILABLE"
    assert value["observer_failure_phase"] == "BINDING_VALIDATION"
    assert "phone_failure_phase" not in value
    assert "failure_phase" not in value
    assert "registered-phone-secret-identifier" not in text
    assert "stage4-admin-token-secret-value" not in text
    terminal = module._bounded_terminal(value)
    assert "observer_failure_phase=BINDING_VALIDATION" in terminal


def test_output_validation_is_bounded_observer_phase(tmp_path: Path) -> None:
    module = load_script()
    value, text = _observe_failure(module, tmp_path, module.runtime_observer._MALFORMED)

    assert value["classification"] == "UNKNOWN"
    assert value["failure_code"] == "PHONE_TARGET_UNAVAILABLE"
    assert value["observer_failure_phase"] == "OUTPUT_VALIDATION"
    assert "phone_failure_phase" not in value
    assert "failure_phase" not in value
    assert "registered-phone-secret-identifier" not in text
    assert "stage4-admin-token-secret-value" not in text
    terminal = module._bounded_terminal(value)
    assert "observer_failure_phase=OUTPUT_VALIDATION" in terminal


def test_unrecognized_phone_target_failure_remains_generic(tmp_path: Path) -> None:
    module = load_script()
    value, text = _observe_failure(
        module,
        tmp_path,
        "unsafe raw detail registered-phone-secret-identifier stage4-admin-token-secret-value",
    )

    assert value["classification"] == "UNKNOWN"
    assert value["failure_code"] == "PHONE_TARGET_UNAVAILABLE"
    assert "observer_failure_phase" not in value
    assert "unsafe raw detail" not in text
    assert "registered-phone-secret-identifier" not in text
    assert "stage4-admin-token-secret-value" not in text


def test_observer_phase_is_fail_closed_and_separate_from_phone_phase() -> None:
    module = load_script()
    invalid_payloads = (
        {
            "classification": "UNKNOWN",
            "failure_code": "PHONE_TARGET_UNAVAILABLE",
            "observer_failure_phase": "RAW_OUTPUT",
        },
        {
            "classification": "UNKNOWN",
            "failure_code": "RUNTIME_OPERATIONAL_OBSERVATION_UNAVAILABLE",
            "observer_failure_phase": "OUTPUT_VALIDATION",
        },
        {
            "classification": "UNKNOWN",
            "failure_code": "PHONE_TARGET_UNAVAILABLE",
            "phone_failure_phase": "REGISTERED_DEVICE_NOT_DEVICE",
            "observer_failure_phase": "OUTPUT_VALIDATION",
        },
    )
    for payload in invalid_payloads:
        try:
            module._bounded_terminal(payload)
        except ValueError:
            pass
        else:
            raise AssertionError("observer failure metadata must fail closed")


def test_workflow_validates_and_projects_observer_failure_phase() -> None:
    source = (WORKFLOWS / "phone-operational-observation.yml").read_text(encoding="utf-8")
    assert "observer_failure_phase = value.get('observer_failure_phase')" in source
    assert "'BINDING_VALIDATION'" in source
    assert "'OUTPUT_VALIDATION'" in source
    assert "observer_failure_phase={observer_failure_phase}" in source
    assert "phone target and observer failure phases overlap" in source
