from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT.parent / "scripts"
sys.path.insert(0, str(ROOT))

from phone_target import PhoneFailurePhase, PhoneTargetDiagnosticFailure  # noqa: E402

SERIAL = "registered-production-target-secret"


def load_preflight():
    spec = importlib.util.spec_from_file_location(
        "phone_transport_preflight_v3_acceptance",
        SCRIPTS / "phone_transport_preflight.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(module, *, failure_phase: PhoneFailurePhase = PhoneFailurePhase.NONE, degraded: bool = False):
    with tempfile.TemporaryDirectory() as raw:
        output = Path(raw) / "preflight.json"
        events: list[str] = []

        def step(name: str):
            def call(*_args, **_kwargs):
                events.append(name)
                expected = {
                    "adb_tooling": PhoneFailurePhase.ADB_TOOLING_UNAVAILABLE,
                    "registered_device_state": PhoneFailurePhase.REGISTERED_DEVICE_NOT_DEVICE,
                    "root_stdout_contract": {
                        PhoneFailurePhase.ADB_TRANSPORT_TIMEOUT,
                        PhoneFailurePhase.ROOT_SHELL_SPAWN_FAILED,
                        PhoneFailurePhase.ROOT_SCRIPT_TIMEOUT,
                        PhoneFailurePhase.ROOT_SCRIPT_OUTPUT_TRUNCATED,
                        PhoneFailurePhase.ROOT_SCRIPT_PROTOCOL_MISMATCH,
                        PhoneFailurePhase.ROOT_SCRIPT_NONZERO,
                        PhoneFailurePhase.UNKNOWN,
                    },
                    "root_stderr_exit_contract": PhoneFailurePhase.ROOT_SCRIPT_PROTOCOL_MISMATCH,
                }[name]
                matches = failure_phase in expected if isinstance(expected, set) else failure_phase is expected
                if matches:
                    raise PhoneTargetDiagnosticFailure(failure_phase, "raw-secret stderr https://example.invalid/")
                if name == "adb_tooling":
                    return "/usr/bin/adb"
                if name == "registered_device_state":
                    return "/usr/bin/adb"
                return None

            return call

        transport = {
            "artifact_upload_required": True,
            "transport_degraded": degraded,
            "failure_classes": ["TLS_ERROR"] if degraded else [],
            "error_counters": {"tls_error": 1 if degraded else 0},
        }
        with (
            mock.patch.dict(
                module.os.environ,
                {"ANDROID_PRODUCTION_SERIAL": SERIAL, "RUNNER_TEMP": raw},
                clear=False,
            ),
            mock.patch.object(module, "collect_runner_transport_evidence", return_value=transport),
            mock.patch.object(module, "_adb", side_effect=step("adb_tooling")),
            mock.patch.object(module, "_require_device", side_effect=step("registered_device_state")),
            mock.patch.object(module, "_probe_root_stdout_contract", side_effect=step("root_stdout_contract")),
            mock.patch.object(
                module,
                "_probe_root_stderr_exit_contract",
                side_effect=step("root_stderr_exit_contract"),
            ),
        ):
            rc = module.main(
                [
                    "--output",
                    str(output),
                    "--controller-revision",
                    "a" * 40,
                    "--source-comment-created-at",
                    datetime.now(timezone.utc).isoformat(),
                ]
            )
        return rc, json.loads(output.read_text(encoding="utf-8")), events


def test_ready_keeps_exact_s43_probe_order_and_v3_contract() -> None:
    module = load_preflight()
    rc, payload, events = _run(module)

    assert rc == 0
    assert events == [
        "adb_tooling",
        "registered_device_state",
        "root_stdout_contract",
        "root_stderr_exit_contract",
    ]
    assert payload["schema"] == "phone-transport-preflight.v3"
    assert payload["classification"] == "READY"
    assert payload["phone_failure_phase"] == "NONE"
    assert payload["execution_semantics"] == {
        "workflow_execution": "success",
        "phone_readiness": "READY",
        "automatic_recovery_performed": False,
    }
    assert payload["safety"]["phone_access_performed"] is True
    assert payload["safety"]["automatic_recovery_performed"] is False
    assert set(payload["timing_ms"]) == {
        "adb_tooling",
        "registered_device_state",
        "root_stdout_contract",
        "root_stderr_exit_contract",
        "total",
    }
    assert all(isinstance(payload["timing_ms"][key], int) for key in payload["timing_ms"])


def test_transport_degraded_is_phone_ready_with_no_phone_failure() -> None:
    module = load_preflight()
    rc, payload, events = _run(module, degraded=True)

    assert rc == 0
    assert len(events) == 4
    assert payload["classification"] == "TRANSPORT_DEGRADED"
    assert payload["phone_failure_phase"] == "NONE"
    assert payload["transport"]["failure_classes"] == ["TLS_ERROR"]


def test_every_typed_phone_failure_yields_not_ready_and_stops_later_probes() -> None:
    module = load_preflight()
    cases = (
        (PhoneFailurePhase.ADB_TOOLING_UNAVAILABLE, 1),
        (PhoneFailurePhase.REGISTERED_DEVICE_NOT_DEVICE, 2),
        (PhoneFailurePhase.ADB_TRANSPORT_TIMEOUT, 3),
        (PhoneFailurePhase.ROOT_SHELL_SPAWN_FAILED, 3),
        (PhoneFailurePhase.ROOT_SCRIPT_TIMEOUT, 3),
        (PhoneFailurePhase.ROOT_SCRIPT_OUTPUT_TRUNCATED, 3),
        (PhoneFailurePhase.ROOT_SCRIPT_PROTOCOL_MISMATCH, 3),
        (PhoneFailurePhase.ROOT_SCRIPT_NONZERO, 3),
        (PhoneFailurePhase.UNKNOWN, 3),
    )
    for phase, expected_events in cases:
        rc, payload, events = _run(module, failure_phase=phase, degraded=True)
        assert rc == 0
        assert len(events) == expected_events
        assert payload["classification"] == "NOT_READY"
        assert payload["phone_failure_phase"] == phase.value
        assert payload["transport"]["transport_degraded"] is True
        assert payload["execution_semantics"]["phone_readiness"] == "NOT_READY"


def test_stderr_contract_failure_is_protocol_mismatch_after_positive_probe() -> None:
    module = load_preflight()
    with tempfile.TemporaryDirectory() as raw:
        output = Path(raw) / "preflight.json"
        events: list[str] = []

        def ok(name, value=None):
            def call(*_args, **_kwargs):
                events.append(name)
                return value

            return call

        def fail_stderr(*_args, **_kwargs):
            events.append("root_stderr_exit_contract")
            raise PhoneTargetDiagnosticFailure(
                PhoneFailurePhase.ROOT_SCRIPT_PROTOCOL_MISMATCH,
                "raw-secret stderr",
            )

        with (
            mock.patch.dict(
                module.os.environ,
                {"ANDROID_PRODUCTION_SERIAL": SERIAL, "RUNNER_TEMP": raw},
                clear=False,
            ),
            mock.patch.object(
                module,
                "collect_runner_transport_evidence",
                return_value={"artifact_upload_required": True, "transport_degraded": False},
            ),
            mock.patch.object(module, "_adb", side_effect=ok("adb_tooling", "/usr/bin/adb")),
            mock.patch.object(
                module,
                "_require_device",
                side_effect=ok("registered_device_state", "/usr/bin/adb"),
            ),
            mock.patch.object(module, "_probe_root_stdout_contract", side_effect=ok("root_stdout_contract")),
            mock.patch.object(module, "_probe_root_stderr_exit_contract", side_effect=fail_stderr),
        ):
            assert module.main(
                [
                    "--output",
                    str(output),
                    "--controller-revision",
                    "b" * 40,
                    "--source-comment-created-at",
                    datetime.now(timezone.utc).isoformat(),
                ]
            ) == 0
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert events[-1] == "root_stderr_exit_contract"
        assert payload["phone_failure_phase"] == "ROOT_SCRIPT_PROTOCOL_MISMATCH"


def test_artifact_never_contains_serial_raw_output_url_or_secret_text() -> None:
    module = load_preflight()
    _, payload, _ = _run(module, failure_phase=PhoneFailurePhase.ROOT_SCRIPT_PROTOCOL_MISMATCH, degraded=True)
    rendered = json.dumps(payload, sort_keys=True)
    for forbidden in (
        SERIAL,
        "raw-secret",
        "stderr",
        "stdout",
        "example.invalid",
        "https://",
        "adb -s",
        "/data/adb/",
    ):
        assert forbidden not in rendered


def test_unexpected_programming_failure_is_not_converted_to_valid_not_ready() -> None:
    module = load_preflight()
    with tempfile.TemporaryDirectory() as raw:
        output = Path(raw) / "preflight.json"
        with (
            mock.patch.dict(
                module.os.environ,
                {"ANDROID_PRODUCTION_SERIAL": SERIAL, "RUNNER_TEMP": raw},
                clear=False,
            ),
            mock.patch.object(
                module,
                "collect_runner_transport_evidence",
                return_value={"artifact_upload_required": True, "transport_degraded": False},
            ),
            mock.patch.object(module, "_adb", side_effect=AssertionError("programming invariant")),
        ):
            try:
                module.main(
                    [
                        "--output",
                        str(output),
                        "--controller-revision",
                        "c" * 40,
                        "--source-comment-created-at",
                        datetime.now(timezone.utc).isoformat(),
                    ]
                )
            except AssertionError as exc:
                assert str(exc) == "programming invariant"
            else:
                raise AssertionError("programming failure was converted into diagnostic success")
        assert output.exists() is False


def test_preflight_does_not_introduce_second_adb_target_or_recovery_path() -> None:
    source = (SCRIPTS / "phone_transport_preflight.py").read_text(encoding="utf-8")
    assert "phone_transport_readiness" not in source
    assert "_registered_target_state" not in source
    assert "_ensure_adb_server" not in source
    for forbidden in (
        "start-server",
        "kill-server",
        "restart runner",
        "systemctl restart",
        "wsl --shutdown",
        "adb reboot",
        "/deploy",
        "/retry-deploy",
    ):
        assert forbidden not in source.lower()


def main() -> int:
    tests = sorted(
        (value for name, value in globals().items() if name.startswith("test_") and callable(value)),
        key=lambda item: item.__name__,
    )
    for test in tests:
        test()
    print(f"PHONE_TRANSPORT_PREFLIGHT_V3_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
