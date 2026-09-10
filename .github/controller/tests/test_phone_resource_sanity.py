from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"
PRODUCTION = ROOT / "production"


def load_controller():
    if str(CONTROLLER) not in sys.path:
        sys.path.insert(0, str(CONTROLLER))
    spec = importlib.util.spec_from_file_location(
        "phone_resource_sanity_acceptance",
        CONTROLLER / "phone_resource_sanity.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_adapter():
    if str(CONTROLLER) not in sys.path:
        sys.path.insert(0, str(CONTROLLER))
    spec = importlib.util.spec_from_file_location(
        "observe_phone_resource_sanity_acceptance",
        SCRIPTS / "observe_phone_resource_sanity.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _measured_output() -> bytes:
    lines = [
        "classification=MEASURED",
        "queue_clear=true",
        "process_set_exact=true",
        "process_identity_stable=true",
        "watchdog_count=1",
        "runtime_supervisor_count=1",
        "host_daemon_count=1",
        "sing_box_count=1",
        "sample_count=3",
        "exercise_request_count=12",
        "exercise_requests_attempted=12",
        "exercise_requests_succeeded=12",
        "recovery_wait_seconds=2",
        "bounded_local_exercise_completed=true",
    ]
    fields = (
        "memory_total_kib",
        "memory_available_kib",
        "data_total_kib",
        "data_available_kib",
        "runtime_rss_kib",
        "runtime_fd_count",
        "logs_kib",
        "runtime_cpu_ticks",
        "system_cpu_ticks",
    )
    samples = {
        "baseline": (
            8_000_000,
            4_000_000,
            64_000_000,
            32_000_000,
            100_000,
            120,
            4096,
            1000,
            100_000,
        ),
        "exercise": (
            8_000_000,
            3_900_000,
            64_000_000,
            31_999_000,
            102_000,
            122,
            4097,
            1025,
            100_500,
        ),
        "recovery": (
            8_000_000,
            4_100_000,
            64_000_000,
            31_999_500,
            100_500,
            120,
            4097,
            1030,
            102_500,
        ),
    }
    for prefix, values in samples.items():
        lines.extend(
            f"{prefix}_{field}={value}"
            for field, value in zip(fields, values)
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _busy_output() -> bytes:
    return b"""classification=BUSY
queue_clear=false
process_set_exact=unknown
process_identity_stable=unknown
watchdog_count=unknown
runtime_supervisor_count=unknown
host_daemon_count=unknown
sing_box_count=unknown
sample_count=0
exercise_request_count=12
exercise_requests_attempted=0
exercise_requests_succeeded=0
recovery_wait_seconds=2
bounded_local_exercise_completed=false
"""


def _progress_output(attempted: int, succeeded: int) -> bytes:
    return (
        f"exercise_requests_attempted={attempted}\n"
        f"exercise_requests_succeeded={succeeded}\n"
    ).encode("utf-8")


def test_script_is_bounded_read_only_exercise_with_safe_cpu_parser() -> None:
    module = load_controller()
    source = module._resource_script("safe-admin-token").decode("utf-8")
    assert len(source.encode("utf-8")) <= 16 * 1024
    for required in (
        "EXERCISE_REQUEST_COUNT=12",
        "RECOVERY_WAIT_SECONDS=2",
        "GET /v1/status",
        "current_job",
        "return 31",
        "return 32",
        "fail_with_request_progress",
        "exercise_requests_attempted=$((exercise_requests_attempted + 1))",
        '"$BB_BIN" sleep "$RECOVERY_WAIT_SECONDS"',
        "MemTotal:",
        "MemAvailable:",
        'df -Pk /data',
        "VmRSS:",
        '"/proc/$sample_pid/fd"/*',
        '"/proc/$sample_pid/stat"',
        'stat_rest="${stat_line##*) }"',
        'utime="${12}"',
        'stime="${13}"',
        'du -sk "$ROOT/logs"',
        "sample_count=3",
        "exercise_requests_attempted=%s",
        "bounded_local_exercise_completed=true",
    ):
        assert required in source, required
    assert source.count('"$BB_BIN" sleep "$RECOVERY_WAIT_SECONDS"') == 1
    for forbidden in (
        "POST ",
        "/v1/ip/rotate",
        "kill ",
        "reboot",
        "setprop sys.powerctl",
        "ln -s",
        "rm -",
        "cp ",
        "mv ",
        "chmod ",
        "service.sh",
        "adb install",
        "adb push",
        "backoff",
        "set -- $stat_line",
    ):
        assert forbidden not in source, forbidden


def test_parser_preserves_bounded_exercise_and_recovery_evidence() -> None:
    module = load_controller()
    observation = module._parse_output(_measured_output())
    bounded = observation.to_bounded_dict()
    assert bounded["classification"] == "MEASURED"
    assert bounded["queue_clear"] is True
    assert bounded["process_set_exact"] is True
    assert bounded["process_identity_stable"] is True
    assert bounded["sample_count"] == 3
    assert bounded["exercise_request_count"] == 12
    assert bounded["exercise_requests_attempted"] == 12
    assert bounded["exercise_requests_succeeded"] == 12
    assert bounded["recovery_wait_seconds"] == 2
    assert bounded["bounded_local_exercise_completed"] is True
    assert bounded["memory_available_basis_points"] == 5125
    assert bounded["data_available_basis_points"] == 4999
    assert bounded["minimum_memory_available_kib"] == 3_900_000
    assert bounded["peak_runtime_rss_kib"] == 102_000
    assert bounded["exercise_runtime_cpu_delta_ticks"] == 25
    assert bounded["exercise_system_cpu_delta_ticks"] == 500
    assert bounded["exercise_cpu_basis_points"] == 500
    assert bounded["recovery_cpu_basis_points"] == 25
    assert bounded["rss_exercise_delta_kib"] == 2000
    assert bounded["rss_recovery_delta_kib"] == 500
    assert bounded["fd_exercise_delta"] == 2
    assert bounded["fd_recovery_delta"] == 0
    assert bounded["rss_monotonic_growth"] is False
    assert bounded["fd_monotonic_growth"] is False
    assert bounded["synthetic_load_performed"] is True
    assert bounded["production_scale_load_performed"] is False
    assert bounded["external_network_traffic_performed"] is False
    assert bounded["performance_threshold_applied"] is False
    assert "pid" not in bounded and "pids" not in bounded
    assert "cmdline" not in bounded and "cmdlines" not in bounded
    serialized = json.dumps(bounded, sort_keys=True)
    assert "safe-admin-token" not in serialized
    assert '"current_job": "' not in serialized


def test_busy_queue_refuses_to_start_exercise() -> None:
    module = load_controller()
    bounded = module._parse_output(_busy_output()).to_bounded_dict()
    assert bounded["classification"] == "BUSY"
    assert bounded["queue_clear"] is False
    assert bounded["process_set_exact"] is None
    assert bounded["sample_count"] == 0
    assert bounded["exercise_requests_attempted"] == 0
    assert bounded["exercise_requests_succeeded"] == 0
    assert bounded["bounded_local_exercise_completed"] is False
    assert bounded["synthetic_load_performed"] is False
    assert bounded["resource_headroom_measured"] is False


def test_malformed_or_contradictory_output_fails_closed() -> None:
    module = load_controller()
    cases = (
        _measured_output() + b"raw_pid=123\n",
        _measured_output().replace(b"sample_count=3", b"sample_count=2"),
        _measured_output().replace(
            b"exercise_requests_attempted=12",
            b"exercise_requests_attempted=11",
        ),
        _measured_output().replace(
            b"exercise_requests_succeeded=12",
            b"exercise_requests_succeeded=11",
        ),
        _measured_output().replace(
            b"process_identity_stable=true",
            b"process_identity_stable=false",
        ),
        _measured_output().replace(
            b"exercise_memory_available_kib=3900000",
            b"exercise_memory_available_kib=9000000",
        ),
        _measured_output().replace(
            b"exercise_runtime_cpu_ticks=1025",
            b"exercise_runtime_cpu_ticks=999",
        ),
        _measured_output().replace(
            b"recovery_system_cpu_ticks=102500",
            b"recovery_system_cpu_ticks=100400",
        ),
        _busy_output().replace(
            b"process_set_exact=unknown",
            b"process_set_exact=true",
        ),
        _busy_output().replace(
            b"exercise_requests_attempted=0",
            b"exercise_requests_attempted=1",
        ),
    )
    for raw in cases:
        try:
            module._parse_output(raw)
        except module.PhoneResourceSanityUnavailable:
            pass
        else:
            raise AssertionError("malformed resource evidence unexpectedly accepted")


def _observe_nonzero(module, *, returncode: int, stdout: bytes):
    result = SimpleNamespace(
        status="completed",
        returncode=returncode,
        stdout=stdout,
        stderr=b"",
        stdout_truncated=False,
        stderr_truncated=False,
    )
    return patch.object(
        module.phone_target,
        "_probe_root_capability",
        return_value=None,
    ), patch.object(
        module.phone_target,
        "_run_root_script",
        return_value=result,
    ), patch.object(
        module.phone_target,
        "_root_transport_failure_phase",
        return_value=None,
    )


def test_sampler_failures_remain_bounded_without_parsing_raw_stdout() -> None:
    module = load_controller()
    for returncode, expected in (
        (22, "RESOURCE_PROCESS_SET_NOT_EXACT"),
        (28, "RESOURCE_CPU_SAMPLE_INVALID"),
        (29, "RESOURCE_PROCESS_IDENTITY_CHANGED"),
    ):
        patches = _observe_nonzero(
            module,
            returncode=returncode,
            stdout=b"sensitive raw sample",
        )
        with patches[0], patches[1], patches[2]:
            try:
                module.observe_phone_resource_sanity(
                    "registered-secret",
                    admin_token="admin-secret",
                )
            except module.PhoneResourceSanityUnavailable as exc:
                assert exc.failure_code == expected
                assert exc.exercise_requests_attempted is None
                assert exc.exercise_requests_succeeded is None
                assert "sensitive" not in str(exc)
            else:
                raise AssertionError("nonzero resource sampler unexpectedly accepted")


def test_request_failures_preserve_only_bounded_progress_and_classification() -> None:
    module = load_controller()
    for returncode, expected, attempted, succeeded in (
        (27, "RESOURCE_EXERCISE_REQUEST_FAILED", 12, 12),
        (30, "RESOURCE_QUEUE_CHANGED", 5, 4),
        (31, "HTTP_RESPONSE_UNAVAILABLE_OR_NON_200", 4, 3),
        (32, "STATUS_PAYLOAD_INVALID", 7, 6),
    ):
        patches = _observe_nonzero(
            module,
            returncode=returncode,
            stdout=_progress_output(attempted, succeeded),
        )
        with patches[0], patches[1], patches[2]:
            try:
                module.observe_phone_resource_sanity(
                    "registered-secret",
                    admin_token="admin-secret",
                )
            except module.PhoneResourceSanityUnavailable as exc:
                assert exc.failure_code == expected
                assert exc.exercise_requests_attempted == attempted
                assert exc.exercise_requests_succeeded == succeeded
                assert "registered-secret" not in str(exc)
                assert "admin-secret" not in str(exc)
            else:
                raise AssertionError("request diagnostic failure unexpectedly accepted")


def test_malformed_request_progress_fails_closed_without_raw_leak() -> None:
    module = load_controller()
    for raw in (
        b"sensitive raw status body",
        _progress_output(13, 12),
        _progress_output(3, 4),
        _progress_output(3, 2) + b"raw_status=secret\n",
    ):
        patches = _observe_nonzero(module, returncode=31, stdout=raw)
        with patches[0], patches[1], patches[2]:
            try:
                module.observe_phone_resource_sanity(
                    "registered-secret",
                    admin_token="admin-secret",
                )
            except module.PhoneResourceSanityUnavailable as exc:
                assert exc.failure_code == "RESOURCE_OUTPUT_MALFORMED"
                assert "sensitive" not in str(exc)
                assert "secret" not in str(exc)
            else:
                raise AssertionError("malformed request progress unexpectedly accepted")


def test_adapter_writes_secret_safe_measured_busy_and_unknown_evidence() -> None:
    controller = load_controller()
    adapter = load_adapter()
    measured = controller._parse_output(_measured_output())
    busy = controller._parse_output(_busy_output())
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        with patch.object(
            adapter,
            "observe_phone_resource_sanity",
            return_value=measured,
        ):
            payload = adapter.observe(
                target="phone-production",
                release_tag="v0.1.7",
                controller_revision="a" * 40,
                serial="registered-secret",
                admin_token="admin-secret",
                output=root / "measured.json",
            )
        assert payload["schema"] == "stage4-phone-resource-sanity.v2"
        assert payload["classification"] == "MEASURED"
        assert payload["observation"]["exercise_requests_attempted"] == 12
        assert payload["safety"]["synthetic_load_performed"] is True
        assert payload["safety"]["production_scale_load_performed"] is False
        text = (root / "measured.json").read_text(encoding="utf-8")
        assert "registered-secret" not in text and "admin-secret" not in text

        with patch.object(
            adapter,
            "observe_phone_resource_sanity",
            return_value=busy,
        ):
            payload = adapter.observe(
                target="phone-production",
                release_tag="v0.1.7",
                controller_revision="b" * 40,
                serial="registered-secret",
                admin_token="admin-secret",
                output=root / "busy.json",
            )
        assert payload["classification"] == "BUSY"
        assert payload["observation"]["exercise_requests_attempted"] == 0
        assert payload["safety"]["synthetic_load_performed"] is False

        failure = adapter.PhoneResourceSanityUnavailable(
            "STATUS_PAYLOAD_INVALID",
            exercise_requests_attempted=4,
            exercise_requests_succeeded=3,
        )
        with patch.object(
            adapter,
            "observe_phone_resource_sanity",
            side_effect=failure,
        ):
            payload = adapter.observe(
                target="phone-production",
                release_tag="v0.1.7",
                controller_revision="c" * 40,
                serial="registered-secret",
                admin_token="admin-secret",
                output=root / "unknown.json",
            )
        assert payload["classification"] == "UNKNOWN"
        assert payload["failure_code"] == "STATUS_PAYLOAD_INVALID"
        assert payload["safety"]["synthetic_load_performed"] is None
        assert payload["observation"]["exercise_request_count"] == 12
        assert payload["observation"]["exercise_requests_attempted"] == 4
        assert payload["observation"]["exercise_requests_succeeded"] == 3
        assert payload["observation"]["bounded_local_exercise_completed"] is None
        text = (root / "unknown.json").read_text(encoding="utf-8")
        assert "registered-secret" not in text and "admin-secret" not in text

        failure = adapter.PhoneResourceSanityUnavailable(
            "RESOURCE_CPU_SAMPLE_INVALID"
        )
        with patch.object(
            adapter,
            "observe_phone_resource_sanity",
            side_effect=failure,
        ):
            payload = adapter.observe(
                target="phone-production",
                release_tag="v0.1.7",
                controller_revision="d" * 40,
                serial="registered-secret",
                admin_token="admin-secret",
                output=root / "unknown-no-progress.json",
            )
        assert payload["observation"]["exercise_requests_attempted"] is None
        assert payload["observation"]["exercise_requests_succeeded"] is None


def test_registry_uses_independent_read_only_resource_operation() -> None:
    registry = json.loads(
        (PRODUCTION / "command-control-registry.json").read_text(
            encoding="utf-8"
        )
    )
    routes = [
        item
        for item in registry["routes"]
        if item.get("id") == "observe-phone-resource-sanity"
    ]
    assert len(routes) == 1
    route = routes[0]
    assert route["handler"] == "dispatch_workflow"
    assert route["operation"] == "observe-phone-resource-sanity"
    assert route["operation"] != "observe-phone-operational"
    assert route["operation_class"] == "OBSERVE"
    assert route["target_capability_policy"] == "phone-production-resource-sanity-observation"
    assert route["read_only"] is True and route["destructive"] is False
    assert route["allowed_targets"] == ["phone-production"]
    assert route["physical_domains"] == []
    assert route["ref"] == "main"
    assert route["ref_policy"] == "controller-main-exact"
    assert route["dispatch_inputs"] == {
        "release_tag": "release",
        "target": "target",
    }
    assert route["workflow"] == ".github/workflows/phone-resource-sanity-observation.yml"
    assert route["pattern"] == (
        r"^/observe-phone-resource-sanity "
        r"(?P<target>phone-production) (?P<release>v0\.1\.7)$"
    )


def test_resource_workflow_is_standalone_serialized_and_bounded() -> None:
    source = (WORKFLOWS / "phone-resource-sanity-observation.yml").read_text(
        encoding="utf-8"
    )
    for required in (
        "workflow_dispatch:",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-target-phone-production",
        "cancel-in-progress: false",
        "environment: phone-production",
        ".github/scripts/observe_phone_resource_sanity.py",
        "Run one bounded read-only local resource exercise",
        "stage4-phone-resource-sanity.v2",
        "stage4-phone-resource-sanity-${{ github.run_id }}-${{ github.run_attempt }}",
        "retention-days: 90",
        "sample_count') != 3",
        "exercise_request_count') != 12",
        "exercise_requests_attempted",
        "exercise_requests_succeeded",
        "recovery_wait_seconds') != 2",
        "synthetic_load_performed",
        "production_scale_load_performed",
        "external_network_traffic_performed",
        "performance_threshold_applied",
        "HTTP_RESPONSE_UNAVAILABLE_OR_NON_200",
        "STATUS_PAYLOAD_INVALID",
        "RESOURCE_CPU_SAMPLE_INVALID",
        "RESOURCE_PROCESS_IDENTITY_CHANGED",
        "RESOURCE_QUEUE_CHANGED",
    ):
        assert required in source, required
    for forbidden in (
        "issue_comment:",
        "workflow_call:",
        "run_phone_release_deployment.py",
        "dispatch_release_once",
        "dispatch_install_once",
        "systemctl restart",
        "service.sh",
        "adb install",
        "adb push",
        "/deploy ",
        "/retry-deploy ",
    ):
        assert forbidden not in source, forbidden


def main() -> int:
    tests = (
        test_script_is_bounded_read_only_exercise_with_safe_cpu_parser,
        test_parser_preserves_bounded_exercise_and_recovery_evidence,
        test_busy_queue_refuses_to_start_exercise,
        test_malformed_or_contradictory_output_fails_closed,
        test_sampler_failures_remain_bounded_without_parsing_raw_stdout,
        test_request_failures_preserve_only_bounded_progress_and_classification,
        test_malformed_request_progress_fails_closed_without_raw_leak,
        test_adapter_writes_secret_safe_measured_busy_and_unknown_evidence,
        test_registry_uses_independent_read_only_resource_operation,
        test_resource_workflow_is_standalone_serialized_and_bounded,
    )
    for test in tests:
        test()
    print(f"PHONE_RESOURCE_SANITY_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
