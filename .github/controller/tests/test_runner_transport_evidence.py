from __future__ import annotations

import json
import tempfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runner_transport_evidence as evidence


def _layout(root: Path) -> tuple[Path, Path]:
    runner_temp = root / "_work" / "_temp"
    diag = root / "_diag"
    runner_temp.mkdir(parents=True)
    diag.mkdir()
    return runner_temp, diag


def _listener(diag: Path, *extra: str) -> None:
    lines = [
        "[2026-09-07T22:00:00.0000000Z INFO Runner] Runner version: '2.337.0'",
        "[2026-09-07T22:00:01.0000000Z INFO Runner] Listening for Jobs",
        *extra,
    ]
    (diag / "Runner_20260907-220000-utc.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_ready_session_has_bounded_identity_and_zero_errors() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runner_temp, diag = _layout(Path(raw))
        _listener(diag)
        value = evidence.collect_runner_transport_evidence(runner_temp=runner_temp, assignment_latency_ms=12_000)
        assert value["diagnostic_evidence_available"] is True
        assert value["diagnostics_truncated"] is False
        assert value["runner_version"] == "2.337.0"
        assert value["listener_session_started_at_utc"] == "2026-09-07T22:00:00Z"
        assert value["last_successful_session_establishment_at_utc"] == "2026-09-07T22:00:01Z"
        assert value["assignment_normal_slo_ms"] == 20_000
        assert value["assignment_bounded_slo_ms"] == 60_000
        assert value["assignment_slo_exceeded"] is False
        assert value["repeated_transport_error"] is False
        assert value["transport_degraded"] is False
        assert value["failure_classes"] == []
        assert all(item == 0 for item in value["error_counters"].values())
        assert evidence.classify_preflight(phone_ready=True, transport=value) == "READY"


def test_assignment_above_bounded_slo_is_transport_degraded() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runner_temp, diag = _layout(Path(raw))
        _listener(diag)
        value = evidence.collect_runner_transport_evidence(runner_temp=runner_temp, assignment_latency_ms=105_284)
        assert value["assignment_slo_exceeded"] is True
        assert value["transport_degraded"] is True
        assert evidence.classify_preflight(phone_ready=True, transport=value) == "TRANSPORT_DEGRADED"


def test_repeated_broker_tls_eof_is_counted_without_raw_log_content() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runner_temp, diag = _layout(Path(raw))
        _listener(
            diag,
            "[2026-09-07T22:01:00.0000000Z ERR BrokerServer] SSL connection could not be established; unexpected EOF; retrying with backoff https://example.invalid/?token=secret-one",
            "[2026-09-07T22:01:02.0000000Z ERR BrokerServer] zero bytes from the transport stream; retrying with backoff token=secret-two",
        )
        value = evidence.collect_runner_transport_evidence(runner_temp=runner_temp, assignment_latency_ms=5_000)
        counters = value["error_counters"]
        assert counters["broker_reconnect"] == 2
        assert counters["tls_error"] == 1
        assert counters["eof_error"] == 2
        assert counters["transport_error_lines"] == 2
        assert value["repeated_transport_error"] is True
        assert value["transport_degraded"] is True
        assert {"BROKER_RECONNECT", "TLS_ERROR", "EOF_ERROR"}.issubset(set(value["failure_classes"]))
        rendered = json.dumps(value, sort_keys=True)
        assert "example.invalid" not in rendered
        assert "secret-one" not in rendered
        assert "secret-two" not in rendered
        assert "token=" not in rendered


def test_worker_action_download_and_artifact_reset_are_classified() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runner_temp, diag = _layout(Path(raw))
        _listener(diag)
        (diag / "Worker_20260907-220100-utc.log").write_text(
            "\n".join(
                (
                    "[2026-09-07T22:01:00.0000000Z ERR Worker] Failed to resolve action download info. Error: The SSL connection could not be established",
                    "[2026-09-07T22:01:02.0000000Z ERR Worker] Failed to CreateArtifact: Unable to make request: ECONNRESET",
                )
            ) + "\n",
            encoding="utf-8",
        )
        value = evidence.collect_runner_transport_evidence(runner_temp=runner_temp, assignment_latency_ms=5_000)
        counters = value["error_counters"]
        assert counters["action_download_error"] == 1
        assert counters["artifact_transport_error"] == 1
        assert counters["tls_error"] == 1
        assert counters["connection_reset"] == 1
        assert counters["transport_error_lines"] == 2
        assert value["worker_files_scanned"] == 1
        assert value["transport_degraded"] is True
        assert {"ACTION_DOWNLOAD_TRANSPORT", "ARTIFACT_TRANSPORT", "TLS_ERROR", "CONNECTION_RESET"}.issubset(set(value["failure_classes"]))


def test_missing_diagnostics_fail_degraded_not_ready_by_default() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runner_temp = Path(raw) / "_work" / "_temp"
        runner_temp.mkdir(parents=True)
        value = evidence.collect_runner_transport_evidence(runner_temp=runner_temp, assignment_latency_ms=1_000)
        assert value["diagnostic_evidence_available"] is False
        assert value["transport_degraded"] is True
        assert value["failure_classes"] == ["DIAGNOSTIC_EVIDENCE_UNAVAILABLE"]
        assert evidence.classify_preflight(phone_ready=True, transport=value) == "TRANSPORT_DEGRADED"
        assert evidence.classify_preflight(phone_ready=False, transport=value) == "NOT_READY"


def test_bounded_diagnostic_window_marks_truncation_degraded() -> None:
    original = evidence.MAX_DIAGNOSTIC_BYTES
    evidence.MAX_DIAGNOSTIC_BYTES = 128
    try:
        with tempfile.TemporaryDirectory() as raw:
            runner_temp, diag = _layout(Path(raw))
            _listener(diag, "[2026-09-07T22:01:00.0000000Z INFO Runner] " + "x" * 512)
            value = evidence.collect_runner_transport_evidence(runner_temp=runner_temp, assignment_latency_ms=1_000)
            assert value["diagnostics_truncated"] is True
            assert value["transport_degraded"] is True
            assert "DIAGNOSTIC_WINDOW_TRUNCATED" in value["failure_classes"]
    finally:
        evidence.MAX_DIAGNOSTIC_BYTES = original


def main() -> int:
    tests = sorted(value for name, value in globals().items() if name.startswith("test_") and callable(value))
    for test in tests:
        test()
    print(f"RUNNER_TRANSPORT_EVIDENCE_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
