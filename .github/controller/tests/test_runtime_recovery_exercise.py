from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"
sys.path.insert(0, str(CONTROLLER))


def load_module():
    spec = importlib.util.spec_from_file_location(
        "runtime_recovery_exercise_acceptance",
        SCRIPTS / "exercise_runtime_recovery.py",
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


def test_fixed_scenario_has_one_non_parameterized_child_termination() -> None:
    module = load_module()
    script = module._fixed_recovery_script().decode("utf-8")
    assert script.count('kill -TERM "$host_pid"') == 1
    assert "/data/adb/mobile-proxy-node/releases/v0.1.7/bin/host-daemon" in script
    assert "/data/adb/mobile-proxy-node/releases/v0.1.7/bin/runtime-supervisor" in script
    assert 'stage4_recovery=mutation_dispatched' in script
    assert 'stage4_recovery=generation_changed' in script
    assert 'stage4_recovery=owner_changed' in script
    assert "while [ \"$i\" -lt 15 ]" in script
    source = (SCRIPTS / "exercise_runtime_recovery.py").read_text(encoding="utf-8")
    for forbidden in ("--process", "--signal", "--pid", "dispatch_release_once", "dispatch_install_once"):
        assert forbidden not in source


def test_topology_deferred_does_not_fail_phone_local_lifecycle_precondition() -> None:
    module = load_module()
    value = operational(module)
    assert module._phone_local_ready(value) is True
    assert value.desired is False
    assert module._topology_disposition(value) == "TOPOLOGY_DEFERRED"


def test_phone_local_failure_remains_distinct_from_topology() -> None:
    module = load_module()
    value = operational(module, local_ready=False)
    assert module._phone_local_ready(value) is False
    assert module._topology_disposition(value) == "PHONE_LOCAL_NOT_READY"


def test_mutation_result_requires_exact_bounded_protocol() -> None:
    module = load_module()
    original = module.phone_target._run_root_script
    try:
        cases = (
            (
                module.RootScriptResult(
                    status="completed",
                    returncode=0,
                    stdout=(
                        b"stage4_recovery=mutation_dispatched\n"
                        b"stage4_recovery=generation_changed\n"
                    ),
                    stderr=b"",
                ),
                ("RECOVERED", True, True, True),
            ),
            (
                module.RootScriptResult(
                    status="completed",
                    returncode=40,
                    stdout=b"stage4_recovery=precondition_refused\n",
                    stderr=b"",
                ),
                ("REFUSED", False, False, None),
            ),
            (
                module.RootScriptResult(
                    status="completed",
                    returncode=41,
                    stdout=b"stage4_recovery=mutation_dispatched\n",
                    stderr=b"",
                ),
                ("NOT_RECOVERED", True, False, True),
            ),
            (
                module.RootScriptResult(
                    status="completed",
                    returncode=42,
                    stdout=(
                        b"stage4_recovery=mutation_dispatched\n"
                        b"stage4_recovery=owner_changed\n"
                    ),
                    stderr=b"",
                ),
                ("OWNER_CHANGED", True, False, False),
            ),
        )
        for result, expected in cases:
            module.phone_target._run_root_script = lambda serial, script, timeout, result=result: result
            observed = module._run_fixed_mutation("registered-phone")
            assert (
                observed.outcome,
                observed.dispatched,
                observed.recovery_observed,
                observed.owner_generation_stable,
            ) == expected
    finally:
        module.phone_target._run_root_script = original


def test_ambiguous_mutation_transport_stops_without_inferred_success() -> None:
    module = load_module()
    original = module.phone_target._run_root_script
    try:
        module.phone_target._run_root_script = lambda serial, script, timeout: module.RootScriptResult(
            status="timeout",
            returncode=None,
            stdout=b"stage4_recovery=mutation_dispatched\n",
            stderr=b"",
        )
        observed = module._run_fixed_mutation("registered-phone")
        assert observed.outcome == "UNKNOWN"
        assert observed.failure_code == "MUTATION_OUTCOME_UNKNOWN"
        assert observed.dispatched is None
        assert observed.recovery_observed is None
    finally:
        module.phone_target._run_root_script = original


def test_workflow_is_single_ingress_fixed_mutation_and_global_phone_serialized() -> None:
    source = (WORKFLOWS / "phone-runtime-recovery.yml").read_text(encoding="utf-8")
    for required in (
        "workflow_call:",
        "command != '/exercise-runtime-recovery phone-production v0.1.7'",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-target-phone-production",
        "cancel-in-progress: false",
        "environment: phone-production",
        ".github/scripts/exercise_runtime_recovery.py",
        "stage4-runtime-recovery-exercise.v1",
        "STAGE4_RUNTIME_RECOVERY_ACCEPTED classification=RECOVERED",
        "if-no-files-found: error",
    ):
        assert required in source
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
        "adb install",
        "adb push",
        "/deploy ",
        "/retry-deploy ",
    ):
        assert forbidden not in source


def test_evidence_safety_contract_never_records_process_identity_or_secrets() -> None:
    module = load_module()
    value = module._base_payload(controller_revision="a" * 40, source_comment_id=123)
    safety = value["safety"]
    assert safety["phone_mutation_performed"] is False
    for field in (
        "deployment_created",
        "deployment_intent_created",
        "provider_access_performed",
        "provider_mutation_performed",
        "vm_access_performed",
        "vm_mutation_performed",
        "runner_proxy_mutation_performed",
        "runtime_restart_performed",
        "phone_reboot_performed",
        "arbitrary_process_selector_accepted",
        "process_ids_recorded",
        "process_cmdlines_recorded",
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
    print(f"RUNTIME_RECOVERY_EXERCISE_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
