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
    return b"""classification=MEASURED
queue_clear=true
process_set_exact=true
watchdog_count=1
runtime_supervisor_count=1
host_daemon_count=1
sing_box_count=1
memory_total_kib=8000000
memory_available_kib=4000000
data_total_kib=64000000
data_available_kib=32000000
runtime_rss_kib=100000
runtime_fd_count=120
logs_kib=4096
sample_count=1
"""


def _busy_output() -> bytes:
    return b"""classification=BUSY
queue_clear=false
process_set_exact=unknown
watchdog_count=unknown
runtime_supervisor_count=unknown
host_daemon_count=unknown
sing_box_count=unknown
memory_total_kib=unknown
memory_available_kib=unknown
data_total_kib=unknown
data_available_kib=unknown
runtime_rss_kib=unknown
runtime_fd_count=unknown
logs_kib=unknown
sample_count=0
"""


def test_script_is_single_snapshot_read_only_and_headroom_focused() -> None:
    module = load_controller()
    source = module._resource_script("safe-admin-token").decode("utf-8")
    assert len(source.encode("utf-8")) <= 16 * 1024
    for required in (
        "GET /v1/status",
        "current_job",
        "MemTotal:",
        "MemAvailable:",
        'df -Pk /data',
        "VmRSS:",
        '"/proc/$sample_pid/fd"/*',
        'du -sk "$ROOT/logs"',
        "sample_count=1",
        "process_set_exact=true",
    ):
        assert required in source, required
    for forbidden in (
        "GET /v1/health",
        "POST ",
        "/v1/ip/rotate",
        " sleep ",
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
        "/proc/$pid/stat",
        "${14}",
        "${15}",
        "${22}",
    ):
        assert forbidden not in source, forbidden


def test_parser_preserves_only_bounded_aggregate_headroom() -> None:
    module = load_controller()
    observation = module._parse_output(_measured_output())
    bounded = observation.to_bounded_dict()
    assert bounded["classification"] == "MEASURED"
    assert bounded["queue_clear"] is True
    assert bounded["process_set_exact"] is True
    assert bounded["sample_count"] == 1
    assert bounded["memory_available_basis_points"] == 5000
    assert bounded["data_available_basis_points"] == 5000
    assert bounded["memory_headroom_to_runtime_rss_milli"] == 40000
    assert bounded["queue_authority"] == "product-v1-status-current-job"
    assert bounded["performance_threshold_applied"] is False
    assert bounded["synthetic_load_performed"] is False
    assert "pid" not in bounded and "pids" not in bounded
    assert "cmdline" not in bounded and "cmdlines" not in bounded
    serialized = json.dumps(bounded, sort_keys=True)
    assert "safe-admin-token" not in serialized
    assert '"current_job": "' not in serialized


def test_busy_queue_refuses_to_mix_resource_sample_with_active_operation() -> None:
    module = load_controller()
    observation = module._parse_output(_busy_output())
    bounded = observation.to_bounded_dict()
    assert bounded["classification"] == "BUSY"
    assert bounded["queue_clear"] is False
    assert bounded["process_set_exact"] is None
    assert bounded["sample_count"] == 0
    assert bounded["resource_headroom_measured"] is False
    assert bounded["memory_available_kib"] is None
    assert bounded["runtime_rss_kib"] is None


def test_malformed_or_contradictory_output_fails_closed() -> None:
    module = load_controller()
    cases = (
        _measured_output() + b"raw_pid=123\n",
        _measured_output().replace(b"sample_count=1", b"sample_count=0"),
        _measured_output().replace(b"watchdog_count=1", b"watchdog_count=2"),
        _measured_output().replace(b"memory_available_kib=4000000", b"memory_available_kib=9000000"),
        _measured_output().replace(b"runtime_fd_count=120", b"runtime_fd_count=0"),
        _busy_output().replace(b"process_set_exact=unknown", b"process_set_exact=true"),
    )
    for raw in cases:
        try:
            module._parse_output(raw)
        except module.PhoneResourceSanityUnavailable:
            pass
        else:
            raise AssertionError("malformed resource evidence unexpectedly accepted")


def test_transport_and_sampler_failures_remain_bounded() -> None:
    module = load_controller()

    result = SimpleNamespace(
        status="completed",
        returncode=22,
        stdout=b"sensitive raw sample",
        stderr=b"",
        stdout_truncated=False,
        stderr_truncated=False,
    )
    with patch.object(module.phone_target, "_probe_root_capability", return_value=None), patch.object(
        module.phone_target, "_run_root_script", return_value=result
    ):
        try:
            module.observe_phone_resource_sanity("registered-secret", admin_token="admin-secret")
        except module.PhoneResourceSanityUnavailable as exc:
            assert exc.failure_code == "RESOURCE_PROCESS_SET_NOT_EXACT"
            assert "sensitive" not in str(exc)
        else:
            raise AssertionError("nonzero resource sampler unexpectedly accepted")


def test_adapter_writes_secret_safe_measured_and_unknown_evidence() -> None:
    controller = load_controller()
    adapter = load_adapter()
    measured = controller._parse_output(_measured_output())
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        with patch.object(adapter, "observe_phone_resource_sanity", return_value=measured):
            payload = adapter.observe(
                target="phone-production",
                release_tag="v0.1.7",
                controller_revision="a" * 40,
                serial="registered-secret",
                admin_token="admin-secret",
                output=root / "measured.json",
            )
        assert payload["classification"] == "MEASURED"
        text = (root / "measured.json").read_text(encoding="utf-8")
        assert "registered-secret" not in text and "admin-secret" not in text

        failure = adapter.PhoneResourceSanityUnavailable("RESOURCE_MEMORY_SAMPLE_INVALID")
        with patch.object(adapter, "observe_phone_resource_sanity", side_effect=failure):
            payload = adapter.observe(
                target="phone-production",
                release_tag="v0.1.7",
                controller_revision="b" * 40,
                serial="registered-secret",
                admin_token="admin-secret",
                output=root / "unknown.json",
            )
        assert payload["classification"] == "UNKNOWN"
        assert payload["failure_code"] == "RESOURCE_MEMORY_SAMPLE_INVALID"
        assert payload["observation"]["resource_headroom_measured"] is False
        text = (root / "unknown.json").read_text(encoding="utf-8")
        assert "registered-secret" not in text and "admin-secret" not in text


def test_registry_uses_existing_read_only_extension_point_only() -> None:
    registry = json.loads((PRODUCTION / "command-control-registry.json").read_text(encoding="utf-8"))
    routes = [item for item in registry["routes"] if item.get("id") == "observe-phone-resource-sanity"]
    assert len(routes) == 1
    route = routes[0]
    assert route["handler"] == "dispatch_workflow"
    assert route["operation"] == "observe-phone-operational"
    assert route["operation_class"] == "OBSERVE"
    assert route["read_only"] is True and route["destructive"] is False
    assert route["allowed_targets"] == ["phone-production"]
    assert route["physical_domains"] == []
    assert route["ref"] == "main"
    assert route["ref_policy"] == "controller-main-exact"
    assert route["dispatch_inputs"] == {"release_tag": "release", "target": "target"}
    assert route["workflow"] == ".github/workflows/phone-resource-sanity-observation.yml"
    assert route["pattern"] == r"^/observe-phone-resource-sanity (?P<target>phone-production) (?P<release>v0\.1\.7)$"


def test_resource_workflow_is_standalone_serialized_and_read_only() -> None:
    source = (WORKFLOWS / "phone-resource-sanity-observation.yml").read_text(encoding="utf-8")
    for required in (
        "workflow_dispatch:",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-target-phone-production",
        "cancel-in-progress: false",
        "environment: phone-production",
        ".github/scripts/observe_phone_resource_sanity.py",
        "stage4-phone-resource-sanity.v1",
        "stage4-phone-resource-sanity-${{ github.run_id }}-${{ github.run_attempt }}",
        "retention-days: 90",
        "performance_threshold_applied",
        "synthetic_load_performed",
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
        test_script_is_single_snapshot_read_only_and_headroom_focused,
        test_parser_preserves_only_bounded_aggregate_headroom,
        test_busy_queue_refuses_to_mix_resource_sample_with_active_operation,
        test_malformed_or_contradictory_output_fails_closed,
        test_transport_and_sampler_failures_remain_bounded,
        test_adapter_writes_secret_safe_measured_and_unknown_evidence,
        test_registry_uses_existing_read_only_extension_point_only,
        test_resource_workflow_is_standalone_serialized_and_read_only,
    )
    for test in tests:
        test()
    print(f"PHONE_RESOURCE_SANITY_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
