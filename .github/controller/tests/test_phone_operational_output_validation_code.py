from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"

if str(CONTROLLER) not in sys.path:
    sys.path.insert(0, str(CONTROLLER))

import runtime_operational_observer as runtime_observer  # noqa: E402


def load_script():
    spec = importlib.util.spec_from_file_location(
        "phone_operational_output_validation_acceptance",
        SCRIPTS / "observe_phone_operational.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload(**overrides: str) -> bytes:
    values = {
        "watchdog_count": "1",
        "runtime_supervisor_count": "1",
        "host_daemon_count": "1",
        "sing_box_count": "1",
        "health_api_authenticated": "true",
        "readiness_state": "healthy",
        "serving": "true",
        "proxy_status": "running",
        "cellular_route_ready": "true",
        "proxy_bind_ready": "true",
        "local_serving_ready": "true",
        "tunnel_owner": "first_party_reverse_tunnel",
        "degradation_reason_code": "none",
    }
    values.update(overrides)
    return ("\n".join(f"{key}={value}" for key, value in values.items()) + "\n").encode()


def _assert_code(call, expected: runtime_observer.RuntimeOperationalOutputFailureCode) -> None:
    with pytest.raises(runtime_observer.RuntimeOperationalOutputValidationFailure) as captured:
        call()
    assert captured.value.code is expected
    assert str(captured.value) == runtime_observer._MALFORMED


def test_phase_protocol_and_encoding_failures_are_typed() -> None:
    _assert_code(
        lambda: runtime_observer._split_phase_markers(b"\xff"),
        runtime_observer.RuntimeOperationalOutputFailureCode.STREAM_ENCODING,
    )
    _assert_code(
        lambda: runtime_observer._split_phase_markers(b"stage4_phase=raw_detail\n"),
        runtime_observer.RuntimeOperationalOutputFailureCode.PHASE_PROTOCOL,
    )
    _assert_code(
        lambda: runtime_observer._parse_output(b"\xff"),
        runtime_observer.RuntimeOperationalOutputFailureCode.PAYLOAD_ENCODING,
    )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({"watchdog_count": "9"}, "PROCESS_COUNT"),
        ({"health_api_authenticated": "unknown"}, "HEALTH_AUTH"),
        ({"serving": "unknown"}, "SERVING"),
        ({"cellular_route_ready": "invalid"}, "CELLULAR_ROUTE_READY"),
        ({"proxy_bind_ready": "invalid"}, "PROXY_BIND_READY"),
        ({"local_serving_ready": "invalid"}, "LOCAL_SERVING_READY"),
        ({"readiness_state": "invalid"}, "READINESS_STATE"),
        ({"proxy_status": "invalid"}, "PROXY_STATUS"),
        ({"tunnel_owner": "invalid"}, "TUNNEL_OWNER"),
        ({"degradation_reason_code": "invalid"}, "DEGRADATION_REASON_CODE"),
    ),
)
def test_field_contract_failures_are_typed(
    overrides: dict[str, str], expected: str
) -> None:
    _assert_code(
        lambda: runtime_observer._parse_output(_payload(**overrides)),
        runtime_observer.RuntimeOperationalOutputFailureCode[expected],
    )


def test_payload_structure_and_unauthenticated_contract_are_typed() -> None:
    _assert_code(
        lambda: runtime_observer._parse_output(b"watchdog_count=1\n"),
        runtime_observer.RuntimeOperationalOutputFailureCode.PAYLOAD_STRUCTURE,
    )
    _assert_code(
        lambda: runtime_observer._parse_output(
            _payload(
                health_api_authenticated="false",
                readiness_state="healthy",
                serving="unknown",
                proxy_status="unknown",
                cellular_route_ready="unknown",
                proxy_bind_ready="unknown",
                local_serving_ready="unknown",
                tunnel_owner="unknown",
                degradation_reason_code="unknown",
            )
        ),
        runtime_observer.RuntimeOperationalOutputFailureCode.UNAUTHENTICATED_PAYLOAD_CONTRACT,
    )


def test_typed_failure_projects_only_bounded_code(tmp_path: Path) -> None:
    module = load_script()

    def fail(serial: str, *, admin_token: str):
        raise module.RuntimeOperationalOutputValidationFailure(
            module.runtime_observer.RuntimeOperationalOutputFailureCode.TUNNEL_OWNER
        )

    module.observe_runtime_operational_health = fail
    output = tmp_path / "operational.json"
    value = module.observe(
        target="phone-production",
        release_tag="v0.1.7",
        controller_revision="f" * 40,
        serial="registered-phone-secret-identifier",
        admin_token="stage4-admin-token-secret-value",
        output=output,
    )
    assert value["classification"] == "UNKNOWN"
    assert value["failure_code"] == "PHONE_TARGET_UNAVAILABLE"
    assert value["observer_failure_phase"] == "OUTPUT_VALIDATION"
    assert value["observer_failure_code"] == "OUTPUT_TUNNEL_OWNER"
    assert "phone_failure_phase" not in value
    text = output.read_text(encoding="utf-8")
    assert "registered-phone-secret-identifier" not in text
    assert "stage4-admin-token-secret-value" not in text
    terminal = module._bounded_terminal(value)
    assert "observer_failure_phase=OUTPUT_VALIDATION" in terminal
    assert "observer_failure_code=OUTPUT_TUNNEL_OWNER" in terminal


def test_observer_failure_code_is_fail_closed() -> None:
    module = load_script()
    invalid = (
        {
            "classification": "UNKNOWN",
            "failure_code": "PHONE_TARGET_UNAVAILABLE",
            "observer_failure_phase": "OUTPUT_VALIDATION",
            "observer_failure_code": "RAW_VALUE",
        },
        {
            "classification": "UNKNOWN",
            "failure_code": "PHONE_TARGET_UNAVAILABLE",
            "observer_failure_phase": "BINDING_VALIDATION",
            "observer_failure_code": "OUTPUT_TUNNEL_OWNER",
        },
        {
            "classification": "UNKNOWN",
            "failure_code": "PHONE_TARGET_UNAVAILABLE",
            "phone_failure_phase": "REGISTERED_DEVICE_NOT_DEVICE",
            "observer_failure_phase": "OUTPUT_VALIDATION",
            "observer_failure_code": "OUTPUT_TUNNEL_OWNER",
        },
    )
    for payload in invalid:
        with pytest.raises(ValueError):
            module._bounded_terminal(payload)


def test_workflow_independently_validates_output_failure_code() -> None:
    source = (WORKFLOWS / "phone-operational-observation.yml").read_text(encoding="utf-8")
    assert "observer_failure_code = value.get('observer_failure_code')" in source
    assert "'OUTPUT_TUNNEL_OWNER'" in source
    assert "observer_failure_phase != 'OUTPUT_VALIDATION'" in source
    assert "observer_failure_code={observer_failure_code}" in source
