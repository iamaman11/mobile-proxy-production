from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"
SHA = "832c8b010efee97a6f5c9c587b766acbe65dd453"


def _load(name: str, path: Path):
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_route_is_exact_main_bound_read_only_and_allowlisted() -> None:
    router = _load("issue_command_router", SCRIPTS / "issue_command_router.py")
    route = router.classify(
        repository="iamaman11/mobile-proxy-production",
        issue_number=1,
        author="iamaman11",
        command="/diagnose-phone-target phone-production v0.1.7",
        event_sha=SHA,
        current_main_sha=SHA,
        run_attempt=1,
    )
    assert route.route_id == "phone-target-observation-diagnostic"
    assert route.handler == "dispatch_workflow"
    assert route.operation_class == "DIAGNOSTIC"
    assert route.read_only is True and route.destructive is False
    assert route.ref == "main"
    assert route.target == "phone-production"
    assert route.release_tag == "v0.1.7"
    assert route.idempotency_policy == "single-run-attempt"
    assert json.loads(route.arguments_json) == {"release": "v0.1.7", "target": "phone-production"}

    dispatcher = _load("dispatch_allowlisted_workflow", SCRIPTS / "dispatch_allowlisted_workflow.py")
    workflow, ref, inputs = dispatcher.build_dispatch(
        route.route_id,
        route.arguments_json,
    )
    assert workflow == "phone-target-observation-diagnostic.yml"
    assert ref == "main"
    assert inputs == {"release_tag": "v0.1.7", "target": "phone-production"}

    for bad in (
        "/diagnose-phone-target vm-production v0.1.7",
        "/diagnose-phone-target phone-production 0.1.7",
        "/diagnose-phone-target phone-production v0.1.7 extra",
        "/diagnose-phone-target phone-production v0.1.7;echo",
    ):
        try:
            router.classify(
                repository="iamaman11/mobile-proxy-production",
                issue_number=1,
                author="iamaman11",
                command=bad,
                event_sha=SHA,
                current_main_sha=SHA,
                run_attempt=1,
            )
        except router.RouteRefused:
            pass
        else:
            raise AssertionError(f"unsafe diagnostic command accepted: {bad}")


def test_workflow_is_serialized_read_only_and_bounded() -> None:
    source = (WORKFLOWS / "phone-target-observation-diagnostic.yml").read_text(encoding="utf-8")
    required = (
        "workflow_dispatch:",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-target-phone-production",
        "cancel-in-progress: false",
        "environment: phone-production",
        "ANDROID_PRODUCTION_SERIAL: ${{ secrets.ANDROID_PRODUCTION_SERIAL }}",
        ".github/scripts/phone_target_observation_diagnostic.py",
        "PHONE_TARGET_OBSERVATION_EVIDENCE_ACCEPTED",
        "phone_mutation=false",
        "runner_mutation=false",
    )
    missing = [token for token in required if token not in source]
    assert not missing, missing
    forbidden = (
        "adb push",
        "adb install",
        "pm install",
        "rm -rf /data",
        "mkdir /data",
        "chmod 0",
        "mv -f",
        "service.sh",
        "runtime-supervisor",
        "MOBILE_PROXY_ADMIN_TOKEN",
        "PUBLIC_DEPLOYMENTS_TOKEN",
        "workflow_run:",
        "issue_comment:",
    )
    present = [token for token in forbidden if token in source]
    assert not present, present


def _diagnostic_with(fake_phone_target: ModuleType):
    old = sys.modules.get("phone_target")
    sys.modules["phone_target"] = fake_phone_target
    try:
        spec = importlib.util.spec_from_file_location(
            "phone_target_observation_diagnostic_test_instance",
            SCRIPTS / "phone_target_observation_diagnostic.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            sys.modules.pop("phone_target", None)
        else:
            sys.modules["phone_target"] = old


def _fake_phone_target(root_scripts: list[SimpleNamespace]) -> ModuleType:
    fake = ModuleType("phone_target")

    class PhoneTargetUnavailable(RuntimeError):
        pass

    fake.PhoneTargetUnavailable = PhoneTargetUnavailable
    fake._ROOT = "/data/adb/mobile-proxy-node"
    fake._adb = lambda: "/usr/bin/adb"
    fake._run = lambda command, timeout: SimpleNamespace(returncode=0, stdout="device\n", stderr="")
    fake._read = lambda serial, args: SimpleNamespace(returncode=0, stdout="", stderr="")
    calls: list[str] = []

    def run_root_script(serial, script):
        calls.append(script)
        if not root_scripts:
            raise AssertionError("unexpected extra root probe")
        return root_scripts.pop(0)

    fake._run_root_script = run_root_script
    fake.calls = calls
    return fake


def test_diagnostic_separates_transport_predicates_and_combined_probe() -> None:
    fake = _fake_phone_target([
        SimpleNamespace(returncode=0, stdout="stdin-ok\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="absent\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="absent\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="target=absent\ncurrent=absent\n", stderr=""),
    ])
    diagnostic = _diagnostic_with(fake)
    report = diagnostic.diagnose(serial="serial", release_id="v0.1.7")
    assert report["device_state"] == "DEVICE"
    assert report["root_capability"] == "PASS"
    assert report["stdin_shell"] == "STDIN-OK"
    assert report["target_predicate"] == "ABSENT"
    assert report["current_predicate"] == "ABSENT"
    assert report["current_symlink_resolution"] == "NOT_APPLICABLE"
    assert report["combined_layout_probe"] == "PASS"
    assert len(fake.calls) == 4
    diagnostic._validate_bounded_report(report)


def test_diagnostic_maps_shell_syntax_without_recording_raw_stderr() -> None:
    raw = "/system/bin/sh: syntax error: unexpected 'then'"
    fake = _fake_phone_target([
        SimpleNamespace(returncode=0, stdout="stdin-ok\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="absent\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="absent\n", stderr=""),
        SimpleNamespace(returncode=2, stdout="", stderr=raw),
    ])
    diagnostic = _diagnostic_with(fake)
    report = diagnostic.diagnose(serial="serial", release_id="v0.1.7")
    assert report["combined_layout_probe"] == "SHELL_SYNTAX"
    encoded = json.dumps(report, sort_keys=True)
    assert raw not in encoded
    assert "/system/bin/sh" not in encoded
    assert report["raw_stderr_recorded"] is False
    assert report["raw_root_output_recorded"] is False


def test_symlink_resolution_is_independently_classified() -> None:
    fake = _fake_phone_target([
        SimpleNamespace(returncode=0, stdout="stdin-ok\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="absent\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="symlink\n", stderr=""),
        SimpleNamespace(returncode=127, stdout="", stderr="readlink: not found"),
        SimpleNamespace(returncode=127, stdout="", stderr="readlink: not found"),
    ])
    diagnostic = _diagnostic_with(fake)
    report = diagnostic.diagnose(serial="serial", release_id="v0.1.7")
    assert report["current_predicate"] == "SYMLINK"
    assert report["current_symlink_resolution"] == "COMMAND_NOT_FOUND"
    assert report["combined_layout_probe"] == "COMMAND_NOT_FOUND"
    assert report["phone_mutation_performed"] is False
    assert report["filesystem_contents_recorded"] is False
    assert "/data/adb/mobile-proxy-node" not in json.dumps(report)


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"PHONE_TARGET_OBSERVATION_DIAGNOSTIC_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
