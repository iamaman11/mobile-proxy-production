from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windows_usb_bridge_evidence import (  # noqa: E402
    collect_host_usb_bridge_evidence,
    not_evaluated_host_usb_bridge_evidence,
)

NOW = datetime(2026, 9, 9, 20, 0, 30, tzinfo=timezone.utc)


def _write(path: Path, message: str, *, timestamp: str = "2026-09-09 20:00:00Z") -> None:
    path.write_text(f"{timestamp} mobile-proxy-usb-bridge {message}\n", encoding="utf-8")


def test_not_evaluated_contract_is_explicit() -> None:
    assert not_evaluated_host_usb_bridge_evidence() == {
        "classification": "NOT_EVALUATED",
        "failure_category": None,
        "freshness": "NOT_EVALUATED",
        "age_seconds": None,
    }


def test_missing_log_is_unavailable_without_guessing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        value = collect_host_usb_bridge_evidence(path=Path(raw) / "missing.log", now=NOW)
    assert value == {
        "classification": "EVIDENCE_UNAVAILABLE",
        "failure_category": None,
        "freshness": "UNKNOWN",
        "age_seconds": None,
    }


def test_fresh_device_not_present_is_decision_grade() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "bridge.log"
        _write(path, "approved_usb_device_not_present; retrying")
        value = collect_host_usb_bridge_evidence(path=path, now=NOW)
    assert value == {
        "classification": "USB_DEVICE_NOT_PRESENT",
        "failure_category": None,
        "freshness": "FRESH",
        "age_seconds": 30,
    }


def test_fresh_attach_failure_reduces_to_allowlisted_category() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "bridge.log"
        _write(path, "bridge_attach_firewall_blocked_Sensitive_Exception_line123; retrying")
        value = collect_host_usb_bridge_evidence(path=path, now=NOW)
    assert value == {
        "classification": "BRIDGE_FAILURE",
        "failure_category": "attach_firewall_blocked",
        "freshness": "FRESH",
        "age_seconds": 30,
    }
    rendered = json.dumps(value, sort_keys=True)
    assert "Sensitive_Exception" not in rendered
    assert "line123" not in rendered
    assert str(path) not in rendered


def test_fresh_attach_success_is_event_not_phone_state() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "bridge.log"
        _write(path, "allowlisted_usb_attached_to_wsl")
        value = collect_host_usb_bridge_evidence(path=path, now=NOW)
    assert value["classification"] == "ATTACH_SUCCEEDED"
    assert value["freshness"] == "FRESH"
    assert value["failure_category"] is None


def test_stale_event_drops_historical_cause() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "bridge.log"
        _write(
            path,
            "bridge_attach_windows_device_busy_SensitiveException_line99; retrying",
            timestamp="2026-09-09 19:58:00Z",
        )
        value = collect_host_usb_bridge_evidence(path=path, now=NOW)
    assert value == {
        "classification": "EVIDENCE_STALE",
        "failure_category": None,
        "freshness": "STALE",
        "age_seconds": 150,
    }


def test_malformed_latest_line_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "bridge.log"
        _write(path, "approved_usb_device_not_present; retrying")
        with path.open("a", encoding="utf-8") as handle:
            handle.write("raw secret https://example.invalid/ device-id\n")
        value = collect_host_usb_bridge_evidence(path=path, now=NOW)
    assert value == {
        "classification": "EVIDENCE_INVALID",
        "failure_category": None,
        "freshness": "UNKNOWN",
        "age_seconds": None,
    }


def test_oversized_log_fails_closed_without_parsing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "bridge.log"
        path.write_text("x" * 65537, encoding="utf-8")
        value = collect_host_usb_bridge_evidence(path=path, now=NOW)
    assert value["classification"] == "EVIDENCE_INVALID"
    assert value["age_seconds"] is None


def test_future_clock_beyond_small_skew_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "bridge.log"
        _write(
            path,
            "bridge_started",
            timestamp="2026-09-09 20:01:00Z",
        )
        value = collect_host_usb_bridge_evidence(path=path, now=NOW)
    assert value["classification"] == "EVIDENCE_INVALID"
    assert value["freshness"] == "UNKNOWN"


def main() -> int:
    tests = sorted(
        (value for name, value in globals().items() if name.startswith("test_") and callable(value)),
        key=lambda item: item.__name__,
    )
    for test in tests:
        test()
    print(f"WINDOWS_USB_BRIDGE_EVIDENCE_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
