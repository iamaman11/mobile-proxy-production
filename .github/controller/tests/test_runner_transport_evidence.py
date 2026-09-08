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
        "[RUNNER 2026-09-07 22:00:00Z INFO Runner] Runner version: '2.337.0'",
        "[RUNNER 2026-09-07 22:00:01Z INFO Terminal] WRITE LINE: 2026-09-07 22:00:01Z: Listening for Jobs",
        *extra,
    ]
    (diag / "Runner_20260907-220000-utc.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _worker(diag: Path, name: str, *lines: str) -> None:
    (diag / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_runner_timestamp_parser_accepts_real_space_and_iso_t_formats() -> None:
    actual = evidence._parse_timestamp("[RUNNER 2026-09-07 22:00:00Z INFO Runner] message")
    iso = evidence._parse_timestamp("[2026-09-07T22:00:00.123456Z INFO Runner] message")
    assert actual is not None and actual.isoformat() == "2026-09-07T22:00:00+00:00"
    assert iso is not None and iso.isoformat() == "2026-09-07T22:00:00.123456+00:00"


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
            "[RUNNER 2026-09-07 22:01:00Z ERR BrokerServer] SSL connection could not be established; unexpected EOF; retrying with backoff https://example.invalid/?token=secret-one",
            "[RUNNER 2026-09-07 22:01:02Z ERR BrokerServer] zero bytes from the transport stream; retrying with backoff token=secret-two",
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
        _worker(
            diag,
            "Worker_20260907-220100-utc.log",
            "[WORKER 2026-09-07 22:01:00Z ERR Worker] Failed to resolve action download info. Error: The SSL connection could not be established",
            "[WORKER 2026-09-07 22:01:02Z ERR Worker] Failed to CreateArtifact: Unable to make request: ECONNRESET",
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


def test_workers_before_current_listener_session_are_excluded() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runner_temp, diag = _layout(Path(raw))
        _listener(diag)
        _worker(
            diag,
            "Worker_20260907-210000-utc.log",
            "[WORKER 2026-09-07 21:00:00Z ERR Worker] Failed to CreateArtifact: Unable to make request: ECONNRESET",
        )
        _worker(
            diag,
            "Worker_20260907-220100-utc.log",
            "[WORKER 2026-09-07 22:01:00Z INFO Worker] healthy current-session worker",
        )
        value = evidence.collect_runner_transport_evidence(runner_temp=runner_temp, assignment_latency_ms=5_000)
        assert value["worker_files_scanned"] == 1
        assert value["error_counters"]["artifact_transport_error"] == 0
        assert value["error_counters"]["connection_reset"] == 0


def test_unresolved_listener_window_does_not_scan_historical_workers() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runner_temp, diag = _layout(Path(raw))
        (diag / "Runner_unknown.log").write_text("Runner version: '2.337.0'\nListening for Jobs\n", encoding="utf-8")
        _worker(
            diag,
            "Worker_20260907-210000-utc.log",
            "[WORKER 2026-09-07 21:00:00Z ERR Worker] Failed to CreateArtifact: Unable to make request: ECONNRESET",
        )
        value = evidence.collect_runner_transport_evidence(runner_temp=runner_temp, assignment_latency_ms=5_000)
        assert value["listener_session_started_at_utc"] is None
        assert value["worker_files_scanned"] == 0
        assert value["error_counters"]["artifact_transport_error"] == 0
        assert "SESSION_WINDOW_UNRESOLVED" in value["failure_classes"]
        assert value["transport_degraded"] is True


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
            _listener(diag, "[RUNNER 2026-09-07 22:01:00Z INFO Runner] " + "x" * 512)
            value = evidence.collect_runner_transport_evidence(runner_temp=runner_temp, assignment_latency_ms=1_000)
            assert value["diagnostics_truncated"] is True
            assert value["transport_degraded"] is True
            assert "DIAGNOSTIC_WINDOW_TRUNCATED" in value["failure_classes"]
    finally:
        evidence.MAX_DIAGNOSTIC_BYTES = original


def main() -> int:
    tests = sorted(
        (value for name, value in globals().items() if name.startswith("test_") and callable(value)),
        key=lambda item: item.__name__,
    )
    for test in tests:
        test()
    print(f"RUNNER_TRANSPORT_EVIDENCE_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
