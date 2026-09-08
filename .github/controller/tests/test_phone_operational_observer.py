from __future__ import annotations

import importlib.util
import json
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
        "phone_operational_observer_acceptance",
        SCRIPTS / "observe_phone_operational.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeOperational:
    def __init__(self, desired: bool) -> None:
        self.desired = desired

    def to_bounded_dict(self) -> dict[str, object]:
        return {
            "evaluated": True,
            "desired": self.desired,
            "mode": "read_only",
            "runtime_supervisor_count": 1,
            "host_daemon_count": 1,
            "sing_box_count": 1,
            "health_api_authenticated": True,
            "readiness_state": "healthy" if self.desired else "waiting_cellular",
            "serving": self.desired,
            "cellular_route_ready": self.desired,
            "localhost_only": True,
            "raw_health_json_recorded": False,
            "raw_config_recorded": False,
            "secret_values_recorded": False,
            "process_ids_recorded": False,
            "process_cmdlines_recorded": False,
            "provider_access_performed": False,
            "phone_mutation_performed": False,
        }


def _observe(module, tmp_path: Path, *, desired: bool) -> tuple[dict[str, object], str]:
    serial = "registered-phone-secret-identifier"
    token = "stage4-admin-token-secret-value"
    module.observe_runtime_operational_health = lambda serial_value, *, admin_token: FakeOperational(desired)
    output = tmp_path / "operational.json"
    value = module.observe(
        target="phone-production",
        release_tag="v0.1.7",
        controller_revision="a" * 40,
        serial=serial,
        admin_token=token,
        output=output,
    )
    return value, output.read_text(encoding="utf-8")


def test_ready_and_degraded_are_independent_from_exact_release_state(tmp_path: Path) -> None:
    module = load_script()
    for desired, expected in ((True, "READY"), (False, "DEGRADED")):
        value, text = _observe(module, tmp_path, desired=desired)
        assert value["classification"] == expected
        assert value["safety"]["exact_phone_release_state_observed"] is False
        assert value["safety"]["phone_mutation_performed"] is False
        assert value["safety"]["provider_access_performed"] is False
        assert "registered-phone-secret-identifier" not in text
        assert "stage4-admin-token-secret-value" not in text
        assert "failure_code" not in value
        assert "failure_phase" not in value


def test_runtime_unavailable_is_valid_bounded_unknown(tmp_path: Path) -> None:
    module = load_script()

    def fail(serial: str, *, admin_token: str):
        raise module.RuntimeOperationalObservationUnavailable(
            "runtime operational observation is unavailable",
            last_phase="process_count_start",
        )

    module.observe_runtime_operational_health = fail
    output = tmp_path / "operational.json"
    value = module.observe(
        target="phone-production",
        release_tag="v0.1.7",
        controller_revision="b" * 40,
        serial="registered-phone-secret-identifier",
        admin_token="stage4-admin-token-secret-value",
        output=output,
    )
    assert value["classification"] == "UNKNOWN"
    assert value["failure_code"] == "RUNTIME_OPERATIONAL_OBSERVATION_UNAVAILABLE"
    assert value["failure_phase"] == "process_count_start"
    assert value["observation"]["evaluated"] is False
    text = output.read_text(encoding="utf-8")
    assert "registered-phone-secret-identifier" not in text
    assert "stage4-admin-token-secret-value" not in text


def test_generic_phone_target_failure_does_not_copy_raw_exception(tmp_path: Path) -> None:
    module = load_script()

    def fail(serial: str, *, admin_token: str):
        raise module.PhoneTargetUnavailable(
            "unsafe raw transport detail registered-phone-secret-identifier stage4-admin-token-secret-value"
        )

    module.observe_runtime_operational_health = fail
    output = tmp_path / "operational.json"
    value = module.observe(
        target="phone-production",
        release_tag="v0.1.7",
        controller_revision="c" * 40,
        serial="registered-phone-secret-identifier",
        admin_token="stage4-admin-token-secret-value",
        output=output,
    )
    assert value["classification"] == "UNKNOWN"
    assert value["failure_code"] == "PHONE_TARGET_UNAVAILABLE"
    text = output.read_text(encoding="utf-8")
    assert "unsafe raw transport detail" not in text
    assert "registered-phone-secret-identifier" not in text
    assert "stage4-admin-token-secret-value" not in text


def test_identity_and_revision_are_fail_closed(tmp_path: Path) -> None:
    module = load_script()
    for target, release, revision in (
        ("vm-production", "v0.1.7", "a" * 40),
        ("phone-production", "v0.1.8", "a" * 40),
        ("phone-production", "v0.1.7", "main"),
    ):
        try:
            module.observe(
                target=target,
                release_tag=release,
                controller_revision=revision,
                serial="registered-phone",
                admin_token="safe-token",
                output=tmp_path / "invalid.json",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid operational identity must fail closed")


def test_workflow_is_standalone_read_only_and_does_not_enrich_release_observation() -> None:
    source = (WORKFLOWS / "phone-operational-observation.yml").read_text(encoding="utf-8")
    for required in (
        "workflow_call:",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-target-phone-production",
        "cancel-in-progress: false",
        ".github/scripts/observe_phone_operational.py",
        "stage4-phone-operational-observation.v1",
        "READY', 'DEGRADED', 'UNKNOWN",
        "exact_phone_release_state_observed",
        "phone_mutation_performed",
        "provider_mutation_performed",
    ):
        assert required in source
    for forbidden in (
        "workflow_dispatch:",
        "issue_comment:",
        ".github/scripts/observe_phone_release.py",
        "enrich_phone_runtime_operational.py",
        "run_phone_release_deployment.py",
        "/deploy ",
        "/retry-deploy ",
        "adb ",
    ):
        assert forbidden not in source


def test_evidence_schema_contains_only_bounded_unknown_failure_metadata(tmp_path: Path) -> None:
    module = load_script()
    module.observe_runtime_operational_health = lambda serial, *, admin_token: FakeOperational(True)
    output = tmp_path / "operational.json"
    module.observe(
        target="phone-production",
        release_tag="v0.1.7",
        controller_revision="d" * 40,
        serial="registered-phone",
        admin_token="safe-token",
        output=output,
    )
    value = json.loads(output.read_text(encoding="utf-8"))
    assert set(value) == {
        "schema",
        "target",
        "release_tag",
        "controller_revision",
        "classification",
        "observation",
        "safety",
    }
