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

import runtime_operational_observer as observer  # noqa: E402


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _root_result(*, returncode: int, stdout: bytes):
    return observer.phone_target.RootScriptResult(
        status="completed",
        returncode=returncode,
        stdout=stdout,
        stderr=b"",
    )


def _install_result(monkeypatch: pytest.MonkeyPatch, result) -> None:
    monkeypatch.setattr(observer.phone_target, "_probe_root_capability", lambda serial: None)
    monkeypatch.setattr(
        observer.phone_target,
        "_run_root_script",
        lambda serial, script, timeout: result,
    )


def test_process_count_failure_mapping_is_bounded_and_does_not_expand_observation() -> None:
    script = observer._operational_script("stage4-admin-token-safe-value")

    assert observer._PROCESS_COUNT_TIMEOUT_SECONDS == 5
    assert observer._ROOT_SCRIPT_TIMEOUT_SECONDS == 15
    assert b'case "$process_count_status" in' in script
    assert b"124|143)" in script
    assert b"exit 31" in script
    assert b"exit 32" in script
    assert b"exit 33" in script
    assert b"/proc/[0-9]*" in script
    assert b"process_ids_recorded" not in script
    assert b"process_cmdlines_recorded" not in script


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected_phase"),
    (
        (
            31,
            b"stage4_phase=busybox_selected\n"
            b"stage4_phase=process_count_start\n"
            b"stage4_phase=process_count_done\n",
            "process_count_no_readable_proc",
        ),
        (
            32,
            b"stage4_phase=busybox_selected\n"
            b"stage4_phase=process_count_start\n",
            "process_count_timeout",
        ),
        (
            33,
            b"stage4_phase=busybox_selected\n"
            b"stage4_phase=process_count_start\n",
            "process_count_execution_failed",
        ),
    ),
)
def test_process_count_failures_project_only_allowlisted_reason(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: bytes,
    expected_phase: str,
) -> None:
    _install_result(monkeypatch, _root_result(returncode=returncode, stdout=stdout))

    with pytest.raises(observer.RuntimeOperationalObservationUnavailable) as captured:
        observer.observe_runtime_operational_health("registered-phone", admin_token="safe-token")

    assert str(captured.value) == observer._UNAVAILABLE
    assert captured.value.last_phase == expected_phase
    assert expected_phase in observer._ALLOWED_FAILURE_PHASES


def test_process_count_reason_requires_matching_phase_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_result(
        monkeypatch,
        _root_result(
            returncode=32,
            stdout=(
                b"stage4_phase=busybox_selected\n"
                b"stage4_phase=process_count_start\n"
                b"stage4_phase=process_count_done\n"
            ),
        ),
    )

    with pytest.raises(observer.phone_target.PhoneTargetDiagnosticFailure) as captured:
        observer.observe_runtime_operational_health("registered-phone", admin_token="safe-token")

    assert captured.value.phase is observer.phone_target.PhoneFailurePhase.ROOT_SCRIPT_PROTOCOL_MISMATCH


def test_root_transport_timeout_uses_existing_phone_target_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_result(
        monkeypatch,
        observer.phone_target.RootScriptResult(
            status="timeout",
            returncode=None,
            stdout=(
                b"stage4_phase=busybox_selected\n"
                b"stage4_phase=process_count_start\n"
            ),
            stderr=b"",
        ),
    )

    with pytest.raises(observer.phone_target.PhoneTargetDiagnosticFailure) as captured:
        observer.observe_runtime_operational_health("registered-phone", admin_token="safe-token")

    assert captured.value.phase is observer.phone_target.PhoneFailurePhase.ROOT_SCRIPT_TIMEOUT


def test_unclassified_root_nonzero_fails_closed_as_existing_phone_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_result(
        monkeypatch,
        _root_result(
            returncode=99,
            stdout=b"stage4_phase=busybox_selected\n",
        ),
    )

    with pytest.raises(observer.phone_target.PhoneTargetDiagnosticFailure) as captured:
        observer.observe_runtime_operational_health("registered-phone", admin_token="safe-token")

    assert captured.value.phase is observer.phone_target.PhoneFailurePhase.ROOT_SCRIPT_NONZERO


def test_malformed_output_keeps_independent_output_validation_class() -> None:
    with pytest.raises(observer.RuntimeOperationalOutputValidationFailure) as captured:
        observer._split_phase_markers(b"stage4_phase=not_allowlisted\n")

    assert captured.value.code is observer.RuntimeOperationalOutputFailureCode.PHASE_PROTOCOL


def test_standalone_artifact_projects_process_timeout_without_raw_detail(tmp_path: Path) -> None:
    module = _load_script(
        "stage4_failure_projection_observer",
        SCRIPTS / "observe_phone_operational.py",
    )

    def fail(serial: str, *, admin_token: str):
        raise module.RuntimeOperationalObservationUnavailable(
            module.runtime_observer._UNAVAILABLE,
            last_phase="process_count_timeout",
        )

    module.observe_runtime_operational_health = fail
    output = tmp_path / "operational.json"
    value = module.observe(
        target="phone-production",
        release_tag="v0.1.7",
        controller_revision="a" * 40,
        serial="registered-phone-secret-identifier",
        admin_token="stage4-admin-token-secret-value",
        output=output,
    )

    assert value["classification"] == "UNKNOWN"
    assert value["failure_code"] == "RUNTIME_OPERATIONAL_OBSERVATION_UNAVAILABLE"
    assert value["failure_phase"] == "process_count_timeout"
    assert "failure_phase=process_count_timeout" in module._bounded_terminal(value)
    text = output.read_text(encoding="utf-8")
    assert "registered-phone-secret-identifier" not in text
    assert "stage4-admin-token-secret-value" not in text


def test_reboot_classifier_preserves_process_timeout_as_bounded_failure_phase() -> None:
    module = _load_script(
        "stage4_failure_projection_reboot",
        SCRIPTS / "exercise_phone_reboot.py",
    )
    failure = module._classify_post_operational_unavailable(
        module.RuntimeOperationalObservationUnavailable(
            observer._UNAVAILABLE,
            last_phase="process_count_timeout",
        )
    )

    assert failure.failure_code == "POST_OPERATIONAL_OBSERVER_UNAVAILABLE"
    assert failure.failure_domain == "RUNTIME_OPERATIONAL_OBSERVER"
    assert failure.failure_phase == "process_count_timeout"


def test_workflow_independently_allowlists_new_bounded_process_failure_phases() -> None:
    source = (WORKFLOWS / "phone-operational-observation.yml").read_text(encoding="utf-8")

    for phase in (
        "process_count_timeout",
        "process_count_no_readable_proc",
        "process_count_execution_failed",
    ):
        assert f"'{phase}'" in source
