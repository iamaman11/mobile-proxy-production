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
        "runtime_restart_exercise_acceptance",
        SCRIPTS / "exercise_runtime_restart.py",
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


def test_fixed_scenario_terminates_only_supervisor_and_proves_stack_generation() -> None:
    module = load_module()
    script = module._fixed_restart_script().decode("utf-8")
    assert script.count('kill -TERM "$supervisor_pid"') == 1
    assert 'kill -TERM "$host_pid"' not in script
    assert 'kill -TERM "$sing_box_pid"' not in script
    assert "/data/adb/mobile-proxy-node/releases/v0.1.7/bin/runtime-supervisor" in script
    assert "/data/adb/mobile-proxy-node/releases/v0.1.7/bin/host-daemon" in script
    assert "/data/adb/mobile-proxy-node/releases/v0.1.7/bin/sing-box" in script
    assert "/data/adb/mobile-proxy-node/logs/runtime-watchdog.pid" in script
    assert 'stage4_restart=mutation_dispatched' in script
    assert 'stage4_restart=stack_rehydrated' in script
    assert 'stage4_restart=watchdog_changed' in script
    assert 'stage4_restart=ownership_changed' in script
    assert 'process_starttime()' in script
    assert 'printf \'%s\' "${22}"' in script
    assert 'printf \'%s\' "$22"' not in script
    assert 'process_parent()' in script
    assert 'process_generation()' in script
    assert 'while [ "$i" -lt 30 ]' in script
    source = (SCRIPTS / "exercise_runtime_restart.py").read_text(encoding="utf-8")
    for forbidden in (
        "--process",
        "--signal",
        "--pid",
        "dispatch_release_once",
        "dispatch_install_once",
        "systemctl",
    ):
        assert forbidden not in source


def test_topology_deferred_remains_separate_from_phone_local_restart_readiness() -> None:
    module = load_module()
    value = operational(module)
    assert module._phone_local_ready(value) is True
    assert value.desired is False
    assert module._topology_disposition(value) == "TOPOLOGY_DEFERRED"


def test_phone_local_failure_remains_a_restart_precondition_failure() -> None:
    module = load_module()
    value = operational(module, local_ready=False)
    assert module._phone_local_ready(value) is False
    assert module._topology_disposition(value) == "PHONE_LOCAL_NOT_READY"


def test_restart_result_requires_exact_bounded_protocol() -> None:
    module = load_module()
    original = module.phone_target._run_root_script
    try:
        cases = (
            (
                module.RootScriptResult(
                    status="completed",
                    returncode=0,
                    stdout=(
                        b"stage4_restart=mutation_dispatched\n"
                        b"stage4_restart=stack_rehydrated\n"
                    ),
                    stderr=b"",
                ),
                ("RESTARTED", True, True, True, True, True, True),
            ),
            (
                module.RootScriptResult(
                    status="completed",
                    returncode=40,
                    stdout=b"stage4_restart=precondition_refused\n",
                    stderr=b"",
                ),
                ("REFUSED", False, None, None, None, None, None),
            ),
            (
                module.RootScriptResult(
                    status="completed",
                    returncode=41,
                    stdout=b"stage4_restart=mutation_dispatched\n",
                    stderr=b"",
                ),
                ("NOT_REHYDRATED", True, True, None, None, None, None),
            ),
            (
                module.RootScriptResult(
                    status="completed",
                    returncode=42,
                    stdout=(
                        b"stage4_restart=mutation_dispatched\n"
                        b"stage4_restart=watchdog_changed\n"
                    ),
                    stderr=b"",
                ),
                ("WATCHDOG_CHANGED", True, False, None, None, None, False),
            ),
            (
                module.RootScriptResult(
                    status="completed",
                    returncode=43,
                    stdout=(
                        b"stage4_restart=mutation_dispatched\n"
                        b"stage4_restart=ownership_changed\n"
                    ),
                    stderr=b"",
                ),
                ("OWNERSHIP_CHANGED", True, True, True, None, None, False),
            ),
        )
        for result, expected in cases:
            module.phone_target._run_root_script = lambda serial, script, timeout, result=result: result
            observed = module._run_fixed_restart("registered-phone")
            assert (
                observed.outcome,
                observed.dispatched,
                observed.watchdog_generation_stable,
                observed.supervisor_generation_changed,
                observed.host_generation_changed,
                observed.sing_box_generation_changed,
                observed.ownership_reestablished,
            ) == expected
    finally:
        module.phone_target._run_root_script = original


def test_ambiguous_restart_transport_stops_without_inferred_dispatch() -> None:
    module = load_module()
    original = module.phone_target._run_root_script
    try:
        module.phone_target._run_root_script = lambda serial, script, timeout: module.RootScriptResult(
            status="timeout",
            returncode=None,
            stdout=b"stage4_restart=mutation_dispatched\n",
            stderr=b"",
        )
        observed = module._run_fixed_restart("registered-phone")
        assert observed.outcome == "UNKNOWN"
        assert observed.failure_code == "MUTATION_OUTCOME_UNKNOWN"
        assert observed.dispatched is None
        assert observed.watchdog_generation_stable is None
        assert observed.supervisor_generation_changed is None
    finally:
        module.phone_target._run_root_script = original


def test_post_observation_prefers_latest_valid_snapshot_over_stale_transport_error() -> None:
    module = load_module()
    original_observer = module.observe_runtime_operational_health
    original_monotonic = module.time.monotonic
    original_sleep = module.time.sleep
    original_bound = module._POST_OBSERVATION_BOUND_SECONDS
    snapshot = operational(module, local_ready=False)
    sequence: list[object] = [module.PhoneTargetUnavailable("transient"), snapshot]
    ticks = iter((0.0, 0.0, 1.0))

    def observe(serial: str, *, admin_token: str):
        item = sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    try:
        module.observe_runtime_operational_health = observe
        module.time.monotonic = lambda: next(ticks)
        module.time.sleep = lambda seconds: None
        module._POST_OBSERVATION_BOUND_SECONDS = 1
        assert module._observe_post_operational("registered-phone", admin_token="token") is snapshot
    finally:
        module.observe_runtime_operational_health = original_observer
        module.time.monotonic = original_monotonic
        module.time.sleep = original_sleep
        module._POST_OBSERVATION_BOUND_SECONDS = original_bound


def test_workflow_is_single_ingress_fixed_restart_and_phone_serialized() -> None:
    source = (WORKFLOWS / "stage4-runtime-restart-exercise.yml").read_text(encoding="utf-8")
    for required in (
        "workflow_call:",
        "command != '/exercise-runtime-restart phone-production v0.1.7'",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-target-phone-production",
        "cancel-in-progress: false",
        "environment: phone-production",
        ".github/scripts/exercise_runtime_restart.py",
        "stage4-runtime-restart-exercise.v1",
        "STAGE4_RUNTIME_RESTART_ACCEPTED classification=RESTARTED",
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
        "phone-runtime-recovery.yml",
        "service.sh",
        "adb install",
        "adb push",
        "/deploy ",
        "/retry-deploy ",
    ):
        assert forbidden not in source


def test_evidence_safety_contract_records_no_process_or_secret_identity() -> None:
    module = load_module()
    value = module._base_payload(controller_revision="a" * 40, source_comment_id=123)
    safety = value["safety"]
    assert safety["phone_mutation_performed"] is False
    assert safety["runtime_restart_performed"] is False
    for field in (
        "deployment_created",
        "deployment_intent_created",
        "provider_access_performed",
        "provider_mutation_performed",
        "vm_access_performed",
        "vm_mutation_performed",
        "runner_proxy_mutation_performed",
        "phone_reboot_performed",
        "bootstrap_reinvoked",
        "service_script_reinvoked",
        "arbitrary_process_selector_accepted",
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
    print(f"RUNTIME_RESTART_EXERCISE_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
