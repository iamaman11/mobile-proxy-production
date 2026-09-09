from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"
sys.path.insert(0, str(CONTROLLER))


def load_module():
    spec = importlib.util.spec_from_file_location(
        "phone_reboot_exercise_acceptance",
        SCRIPTS / "exercise_phone_reboot.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def operational(module, *, degradation: str = "reverse_tunnel_not_ready", local_ready: bool = True):
    return module.RuntimeOperationalObservation(
        watchdog_count=1,
        runtime_supervisor_count=1,
        host_daemon_count=1 if local_ready else 0,
        sing_box_count=1,
        health_api_authenticated=True,
        readiness_state="starting_proxy" if degradation != "none" else "healthy",
        serving=False if degradation != "none" else True,
        proxy_status="degraded" if degradation != "none" else "running",
        cellular_route_ready=True,
        proxy_bind_ready=True,
        local_serving_ready=True,
        tunnel_owner="first_party_reverse_tunnel",
        degradation_reason_code=degradation,
        reverse_tunnel_connected=False if degradation != "none" else True,
        reverse_tunnel_freshness="stale" if degradation != "none" else "fresh",
        reverse_tunnel_active_transport="none" if degradation != "none" else "tls_tcp",
        reverse_tunnel_failover_reason="connect_timeout" if degradation != "none" else "none",
    )


def completed(stdout: str, *, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_fixed_reboot_script_has_one_delayed_android_reboot_and_no_selector() -> None:
    module = load_module()
    script = module._fixed_reboot_script().decode("utf-8")
    assert script.count("setprop sys.powerctl reboot") == 1
    assert "nohup sh -c 'sleep 2; setprop sys.powerctl reboot'" in script
    assert script.count("stage4_reboot=mutation_dispatched") == 1
    assert "/data/adb/mobile-proxy-node/releases/v0.1.7" in script
    assert "$(getprop sys.boot_completed" in script
    source = (SCRIPTS / "exercise_phone_reboot.py").read_text(encoding="utf-8")
    for forbidden in (
        "--reboot-mode",
        "--reason",
        "--selector",
        "--process",
        "--signal",
        "--pid",
        "dispatch_release_once",
        "dispatch_install_once",
        "systemctl",
        "subprocess.run",
        "subprocess.Popen",
    ):
        assert forbidden not in source
    assert source.count("phone_target._run_root_script(") == 1
    assert "service.sh" not in source


def test_boot_state_observation_is_read_only_and_validated_without_evidence_identity() -> None:
    module = load_module()
    original = module.phone_target._read
    values = iter(
        (
            completed("11111111-2222-3333-4444-555555555555\n"),
            completed("1\n"),
        )
    )
    calls: list[tuple[str, tuple[str, ...], int]] = []

    def read(serial: str, arguments: list[str], *, timeout: int):
        calls.append((serial, tuple(arguments), timeout))
        return next(values)

    try:
        module.phone_target._read = read
        state = module._observe_boot_state("registered-phone")
    finally:
        module.phone_target._read = original

    assert state.boot_id == "11111111-2222-3333-4444-555555555555"
    assert state.boot_completed is True
    assert calls == [
        ("registered-phone", ("shell", "cat", "/proc/sys/kernel/random/boot_id"), 10),
        ("registered-phone", ("shell", "getprop", "sys.boot_completed"), 10),
    ]
    payload = module._base_payload(controller_revision="a" * 40, source_comment_id=123)
    assert "11111111-2222-3333-4444-555555555555" not in repr(payload)
    assert payload["safety"]["raw_boot_identity_recorded"] is False


def test_dispatch_requires_exact_completed_ack_and_never_infers_ambiguous_dispatch() -> None:
    module = load_module()
    original = module.phone_target._run_root_script
    try:
        cases = (
            (
                module.RootScriptResult(
                    status="completed",
                    returncode=0,
                    stdout=b"stage4_reboot=mutation_dispatched\n",
                    stderr=b"",
                ),
                ("DISPATCHED", None, True),
            ),
            (
                module.RootScriptResult(
                    status="completed",
                    returncode=40,
                    stdout=b"stage4_reboot=precondition_refused\n",
                    stderr=b"",
                ),
                ("REFUSED", "MUTATION_PRECONDITION_CHANGED", False),
            ),
            (
                module.RootScriptResult(
                    status="timeout",
                    returncode=None,
                    stdout=b"stage4_reboot=mutation_dispatched\n",
                    stderr=b"",
                ),
                ("UNKNOWN", "MUTATION_OUTCOME_UNKNOWN", None),
            ),
            (
                module.RootScriptResult(
                    status="completed",
                    returncode=0,
                    stdout=b"stage4_reboot=mutation_dispatched\nextra\n",
                    stderr=b"",
                ),
                ("UNKNOWN", "MUTATION_OUTPUT_AMBIGUOUS", None),
            ),
        )
        for result, expected in cases:
            module.phone_target._run_root_script = lambda serial, script, timeout, result=result: result
            observed = module._dispatch_fixed_reboot("registered-phone")
            assert (observed.outcome, observed.failure_code, observed.dispatched) == expected
    finally:
        module.phone_target._run_root_script = original


def test_wait_for_new_boot_requires_changed_identity_and_completed_android_boot() -> None:
    module = load_module()
    original_observe = module._observe_boot_state
    original_monotonic = module.time.monotonic
    original_sleep = module.time.sleep
    sequence = [
        module.BootState("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", True),
        module.PhoneTargetUnavailable("offline"),
        module.BootState("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", False),
        module.BootState("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", True),
    ]
    ticks = iter((0.0, 1.0, 2.0, 3.0, 4.0, 5.0))

    def observe(serial: str):
        item = sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    try:
        module._observe_boot_state = observe
        module.time.monotonic = lambda: next(ticks)
        module.time.sleep = lambda seconds: None
        state = module._wait_for_new_boot(
            "registered-phone",
            previous_boot_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )
    finally:
        module._observe_boot_state = original_observe
        module.time.monotonic = original_monotonic
        module.time.sleep = original_sleep

    assert state is not None
    assert state.boot_id == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert state.boot_completed is True


def test_topology_deferred_remains_separate_from_phone_local_reboot_readiness() -> None:
    module = load_module()
    value = operational(module)
    assert module._phone_local_ready(value) is True
    assert value.desired is False
    assert module._topology_disposition(value) == "TOPOLOGY_DEFERRED"


def test_phone_local_failure_remains_a_reboot_precondition_failure() -> None:
    module = load_module()
    value = operational(module, local_ready=False)
    assert module._phone_local_ready(value) is False
    assert module._topology_disposition(value) == "PHONE_LOCAL_NOT_READY"


def test_post_reboot_unknown_preserves_bounded_failure_domain_and_phase() -> None:
    module = load_module()

    class OutputCode:
        value = "OUTPUT_HEALTH_AUTH"

    cases = (
        (
            module.RuntimeOperationalObservationUnavailable(
                "observer unavailable",
                last_phase="process_count_start",
            ),
            (
                "POST_OPERATIONAL_OBSERVER_UNAVAILABLE",
                "RUNTIME_OPERATIONAL_OBSERVER",
                "process_count_start",
            ),
        ),
        (
            module.RuntimeOperationalOutputValidationFailure(OutputCode()),
            (
                "POST_OPERATIONAL_OUTPUT_INVALID",
                "RUNTIME_OPERATIONAL_OUTPUT",
                "OUTPUT_HEALTH_AUTH",
            ),
        ),
        (
            module.phone_target.PhoneTargetDiagnosticFailure(
                module.phone_target.PhoneFailurePhase.REGISTERED_DEVICE_NOT_DEVICE,
                "registered phone target is not in device state",
            ),
            (
                "POST_PHONE_TRANSPORT_UNAVAILABLE",
                "PHONE_TRANSPORT",
                "REGISTERED_DEVICE_NOT_DEVICE",
            ),
        ),
        (
            module.PhoneTargetUnavailable("opaque target failure"),
            (
                "POST_PHONE_TARGET_UNAVAILABLE",
                "PHONE_TARGET",
                None,
            ),
        ),
    )

    for error, expected in cases:
        observed = module._classify_post_operational_unavailable(error)
        assert (observed.failure_code, observed.failure_domain, observed.failure_phase) == expected

    source = (SCRIPTS / "exercise_phone_reboot.py").read_text(encoding="utf-8")
    assert "diagnostic = _classify_post_operational_unavailable(exc)" in source
    assert 'payload["failure_domain"] = diagnostic.failure_domain' in source
    assert 'payload["failure_phase"] = diagnostic.failure_phase' in source
    assert "failure_code=diagnostic.failure_code" in source


def test_post_operational_bound_reraises_last_machine_readable_transport_failure() -> None:
    module = load_module()
    original_observe = module.observe_runtime_operational_health
    original_monotonic = module.time.monotonic
    original_sleep = module.time.sleep
    error = module.phone_target.PhoneTargetDiagnosticFailure(
        module.phone_target.PhoneFailurePhase.ROOT_SCRIPT_TIMEOUT,
        "rooted runtime capability unavailable",
    )
    ticks = iter((0.0, float(module._POST_OPERATIONAL_BOUND_SECONDS)))

    def observe(serial: str, *, admin_token: str):
        raise error

    try:
        module.observe_runtime_operational_health = observe
        module.time.monotonic = lambda: next(ticks)
        module.time.sleep = lambda seconds: None
        try:
            module._observe_post_operational("registered-phone", admin_token="bounded-token")
        except module.phone_target.PhoneTargetDiagnosticFailure as exc:
            assert exc is error
            assert exc.phase == module.phone_target.PhoneFailurePhase.ROOT_SCRIPT_TIMEOUT
        else:
            raise AssertionError("post-operational observation should preserve the last diagnostic failure")
    finally:
        module.observe_runtime_operational_health = original_observe
        module.time.monotonic = original_monotonic
        module.time.sleep = original_sleep


def test_workflow_is_single_ingress_fixed_reboot_and_phone_serialized() -> None:
    source = (WORKFLOWS / "stage4-phone-reboot-exercise.yml").read_text(encoding="utf-8")
    for required in (
        "workflow_call:",
        "command != '/exercise-phone-reboot phone-production v0.1.7'",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-target-phone-production",
        "cancel-in-progress: false",
        "environment: phone-production",
        ".github/scripts/exercise_phone_reboot.py",
        "stage4-phone-reboot-exercise.v1",
        "STAGE4_PHONE_REBOOT_ACCEPTED classification=REBOOTED",
        "if-no-files-found: error",
    ):
        assert required in source

    github_token_line = "GITHUB_TOKEN: ${{ github.token }}"
    assert source.count(github_token_line) == 1
    admit_source, exercise_source = source.split("\n  exercise-phone:\n", 1)
    assert github_token_line in admit_source
    assert github_token_line not in exercise_source

    for forbidden in (
        "workflow_dispatch:",
        "issue_comment:",
        "run_phone_release_deployment.py",
        "dispatch_release_once",
        "dispatch_install_once",
        "systemctl restart",
        "runner-transport",
        "observe-runner-transport",
        "phone-transport-preflight",
        "phone-runtime-recovery.yml",
        "service.sh",
        "adb install",
        "adb push",
        "/deploy ",
        "/retry-deploy ",
    ):
        assert forbidden not in source


def test_evidence_safety_contract_records_no_raw_boot_or_secret_identity() -> None:
    module = load_module()
    value = module._base_payload(controller_revision="a" * 40, source_comment_id=123)
    safety = value["safety"]
    assert safety["phone_mutation_performed"] is False
    assert safety["phone_reboot_performed"] is False
    assert safety["runtime_restart_performed"] is False
    for field in (
        "deployment_created",
        "deployment_intent_created",
        "provider_access_performed",
        "provider_mutation_performed",
        "vm_access_performed",
        "vm_mutation_performed",
        "runner_proxy_mutation_performed",
        "bootstrap_reinvoked",
        "service_script_reinvoked",
        "arbitrary_reboot_selector_accepted",
        "arbitrary_process_selector_accepted",
        "raw_boot_identity_recorded",
        "process_ids_recorded",
        "process_cmdlines_recorded",
        "process_generation_tokens_recorded",
        "raw_device_identifier_recorded",
        "raw_config_recorded",
        "secret_values_recorded",
        "blind_retry_performed",
    ):
        assert safety[field] is False


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"PHONE_REBOOT_EXERCISE_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
