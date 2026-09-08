from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT.parent / "scripts"
sys.path.insert(0, str(ROOT))

import android_target as ANDROID  # noqa: E402
import phone_transport_readiness as READINESS  # noqa: E402
from phone_target import PhoneTargetUnavailable  # noqa: E402

SERIAL = "registered-device-1"


def load_preflight():
    spec = importlib.util.spec_from_file_location(
        "phone_transport_preflight_acceptance",
        SCRIPTS / "phone_transport_preflight.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _completed() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, "", "")


def test_readiness_returns_bounded_registered_target_leaf_without_root_probe() -> None:
    with (
        mock.patch.object(READINESS, "_adb", return_value="/usr/bin/adb"),
        mock.patch.object(READINESS, "_ensure_adb_server"),
        mock.patch.object(READINESS, "_registered_target_state", return_value="no_permissions"),
        mock.patch.object(READINESS, "_require_device") as strict_state,
        mock.patch.object(READINESS, "_probe_root_capability") as root_probe,
    ):
        result = READINESS.probe_registered_phone_transport(SERIAL)

    assert result.ready is False
    assert result.failure_phase == "registered_device_state"
    assert result.failure_code == "REGISTERED_DEVICE_NOT_DEVICE"
    assert result.target_state == "no_permissions"
    assert "registered_device_state" in result.timing_ms
    strict_state.assert_not_called()
    root_probe.assert_not_called()


def test_readiness_requires_strict_get_state_then_existing_root_contract() -> None:
    with (
        mock.patch.object(READINESS, "_adb", return_value="/usr/bin/adb"),
        mock.patch.object(READINESS, "_ensure_adb_server"),
        mock.patch.object(READINESS, "_registered_target_state", return_value="device"),
        mock.patch.object(READINESS, "_require_device", return_value="/usr/bin/adb") as strict_state,
        mock.patch.object(READINESS, "_probe_root_capability") as root_probe,
    ):
        result = READINESS.probe_registered_phone_transport(SERIAL)

    assert result.ready is True
    assert result.failure_phase is None
    assert result.failure_code is None
    assert result.target_state is None
    strict_state.assert_called_once_with(SERIAL)
    root_probe.assert_called_once_with(SERIAL)


def test_readiness_distinguishes_transport_timeout_without_raw_error() -> None:
    timeout = subprocess.TimeoutExpired(["adb", "devices"], 15)
    inventory_error = ANDROID.AndroidObservationUnavailable("Android read-only observation transport failed")
    inventory_error.__cause__ = timeout

    with (
        mock.patch.object(READINESS, "_adb", return_value="/usr/bin/adb"),
        mock.patch.object(READINESS, "_ensure_adb_server"),
        mock.patch.object(READINESS, "_registered_target_state", side_effect=inventory_error),
    ):
        result = READINESS.probe_registered_phone_transport(SERIAL)

    assert result.ready is False
    assert result.failure_phase == "registered_device_state"
    assert result.failure_code == "ADB_TRANSPORT_TIMEOUT"
    assert result.target_state is None


def test_readiness_root_failure_is_one_safe_leaf() -> None:
    with (
        mock.patch.object(READINESS, "_adb", return_value="/usr/bin/adb"),
        mock.patch.object(READINESS, "_ensure_adb_server"),
        mock.patch.object(READINESS, "_registered_target_state", return_value="device"),
        mock.patch.object(READINESS, "_require_device", return_value="/usr/bin/adb"),
        mock.patch.object(
            READINESS,
            "_probe_root_capability",
            side_effect=PhoneTargetUnavailable("rooted runtime capability unavailable"),
        ),
    ):
        result = READINESS.probe_registered_phone_transport(SERIAL)

    assert result.ready is False
    assert result.failure_phase == "root_contract"
    assert result.failure_code == "ROOT_CONTRACT_FAILED"
    assert result.target_state is None


def _run_preflight_with(readiness: READINESS.PhoneTransportReadiness) -> dict[str, object]:
    module = load_preflight()
    with tempfile.TemporaryDirectory() as raw:
        output = Path(raw) / "preflight.json"
        with (
            mock.patch.dict(
                module.os.environ,
                {"ANDROID_PRODUCTION_SERIAL": SERIAL, "RUNNER_TEMP": raw},
                clear=False,
            ),
            mock.patch.object(module, "collect_runner_transport_evidence", return_value={}),
            mock.patch.object(module, "probe_registered_phone_transport", return_value=readiness),
            mock.patch.object(module, "classify_preflight", return_value="READY"),
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
        assert rc == 0
        return json.loads(output.read_text(encoding="utf-8"))


def test_preflight_artifact_publishes_only_bounded_target_failure_evidence() -> None:
    payload = _run_preflight_with(
        READINESS.PhoneTransportReadiness(
            ready=False,
            timing_ms={"registered_device_state": 7},
            failure_phase="registered_device_state",
            failure_code="REGISTERED_DEVICE_NOT_DEVICE",
            target_state="no_permissions",
        )
    )

    assert payload["classification"] == "NOT_READY"
    assert payload["failure_phase"] == "registered_device_state"
    assert payload["failure_code"] == "REGISTERED_DEVICE_NOT_DEVICE"
    assert payload["target_state"] == "no_permissions"
    assert payload["safety"]["phone_access_performed"] is False
    encoded = json.dumps(payload, sort_keys=True)
    assert SERIAL not in encoded
    for forbidden in ("stderr", "stdout", "cmdline", "device_identifier"):
        assert forbidden not in encoded.lower()


def test_preflight_marks_phone_access_when_root_contract_was_attempted() -> None:
    payload = _run_preflight_with(
        READINESS.PhoneTransportReadiness(
            ready=False,
            timing_ms={"strict_get_state": 2, "root_contract": 3},
            failure_phase="root_contract",
            failure_code="ROOT_CONTRACT_FAILED",
        )
    )

    assert payload["classification"] == "NOT_READY"
    assert payload["failure_phase"] == "root_contract"
    assert payload["failure_code"] == "ROOT_CONTRACT_FAILED"
    assert "target_state" not in payload
    assert payload["safety"]["phone_access_performed"] is True


def test_ready_preflight_has_no_failure_leaf() -> None:
    payload = _run_preflight_with(
        READINESS.PhoneTransportReadiness(
            ready=True,
            timing_ms={
                "adb_tooling": 1,
                "adb_server": 1,
                "registered_device_state": 1,
                "strict_get_state": 1,
                "root_contract": 1,
            },
        )
    )

    assert payload["classification"] == "READY"
    assert "failure_phase" not in payload
    assert "failure_code" not in payload
    assert "target_state" not in payload
    assert payload["safety"]["phone_access_performed"] is True
