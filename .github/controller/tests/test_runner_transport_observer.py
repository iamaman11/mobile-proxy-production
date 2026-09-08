from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
SCRIPTS = ROOT / "scripts"
PRODUCTION = ROOT / "production"

sys.path.insert(0, str(CONTROLLER))

import runner_transport_evidence as evidence
import runner_transport_probe as probe


def _load_observer():
    name = "observe_runner_transport"
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / "observe_runner_transport.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _policy() -> dict[str, object]:
    return probe.load_policy(PRODUCTION / "runner-transport-policy.json")


def _layout(root: Path) -> tuple[Path, Path]:
    runner_temp = root / "_work" / "_temp"
    diag = root / "_diag"
    runner_temp.mkdir(parents=True)
    diag.mkdir()
    return runner_temp, diag


def _healthy_legacy() -> dict[str, object]:
    return {
        "diagnostic_window": "current_listener_session",
        "diagnostic_evidence_available": True,
        "diagnostics_truncated": False,
        "runner_version": "2.337.0",
        "listener_session_started_at_utc": "2026-09-08T19:00:23Z",
        "last_successful_session_establishment_at_utc": "2026-09-08T19:00:27Z",
        "listener_files_scanned": 1,
        "worker_files_scanned": 1,
        "error_counters": {
            "broker_reconnect": 0,
            "runserver_reconnect": 0,
            "tls_error": 0,
            "eof_error": 0,
            "connection_reset": 0,
            "action_download_error": 0,
            "artifact_transport_error": 0,
            "transport_error_lines": 0,
        },
        "context_counters": {"expected_local_poll_cancellation": 0},
        "failure_classes": [],
        "assignment_normal_slo_ms": 20000,
        "assignment_bounded_slo_ms": 60000,
        "assignment_slo_exceeded": False,
        "repeated_transport_error": False,
        "transport_degraded": False,
    }


def _runner() -> dict[str, object]:
    return {
        "service_state": "ACTIVE",
        "service_substate_observed": True,
        "runner_activity": "BUSY",
        "service_environment_observed": True,
    }


def _environment() -> dict[str, object]:
    return {
        "proxy_present": True,
        "custom_ca_present": False,
        "git_tls_override_present": False,
        "runtime_tls_override_present": False,
        "source": "SERVICE_PROCESS",
        "service_environment_observed": True,
        "observer_environment_matches_presence": True,
    }


def _watchdog() -> dict[str, object]:
    return {
        "timer_state": "ACTIVE",
        "state_readable": True,
        "decision": "UNKNOWN",
        "cooldown_active": False,
        "restart_budget_remaining": 3,
        "post_restart_grace_active": False,
        "restart_count_window": 0,
    }


def _active(result: str = "SUCCESS", *, retry: bool = False) -> dict[str, object]:
    roles = []
    for destination in _policy()["destinations"]:
        attempts = [{"attempt": 1, "result": result, "duration_ms": 5}]
        if retry:
            attempts = [
                {"attempt": 1, "result": "TLS_FAILURE", "duration_ms": 5},
                {"attempt": 2, "result": "SUCCESS", "duration_ms": 5},
            ]
        roles.append({"role": destination["role"], "attempts": attempts, "final_result": attempts[-1]["result"]})
    return {"roles": roles, "retry_observed": retry, "budget_exhausted": False}


def test_policy_is_exact_bounded_and_has_no_wildcard_destination() -> None:
    value = _policy()
    assert value["active_probe_budget_seconds"] == 120
    assert value["attempt_timeout_seconds"] == 10
    assert value["max_destinations"] == 5
    assert value["max_attempts_per_destination"] == 3
    assert value["max_concurrency"] == 2
    assert value["tls_verification_required"] is True
    destinations = value["destinations"]
    assert [item["role"] for item in destinations] == [
        "GITHUB_API", "REPOSITORY_TRANSPORT", "ACTION_DOWNLOAD", "ACTIONS_RESULTS",
    ]
    assert all("*" not in item["url"] for item in destinations)
    assert {item["url"] for item in destinations} == {
        "https://api.github.com/",
        "https://github.com/",
        "https://codeload.github.com/",
        "https://results-receiver.actions.githubusercontent.com/",
    }


def test_policy_refuses_wildcards_and_inconsistent_budget() -> None:
    value = _policy()
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "policy.json"
        changed = json.loads(json.dumps(value))
        changed["destinations"][0]["url"] = "https://*.actions.githubusercontent.com/"
        path.write_text(json.dumps(changed), encoding="utf-8")
        try:
            probe.load_policy(path)
        except ValueError:
            pass
        else:
            raise AssertionError("wildcard runner transport destination accepted")

        changed = json.loads(json.dumps(value))
        changed["active_probe_budget_seconds"] = 30
        path.write_text(json.dumps(changed), encoding="utf-8")
        try:
            probe.load_policy(path)
        except ValueError:
            pass
        else:
            raise AssertionError("inconsistent runner transport budget accepted")


def test_active_probe_preserves_failure_before_success_without_internal_curl_retry() -> None:
    value = _policy()
    calls: dict[str, int] = {}
    commands: list[tuple[str, ...]] = []

    def fake_runner(command, timeout, environment):
        command = tuple(command)
        commands.append(command)
        url = command[-1]
        calls[url] = calls.get(url, 0) + 1
        if url == "https://api.github.com/" and calls[url] == 1:
            return 35, ""
        return 0, ""

    observed = probe.run_active_probes(value, runner=fake_runner, environment={"HTTPS_PROXY": "secret-value"})
    api = next(item for item in observed["roles"] if item["role"] == "GITHUB_API")
    assert [item["result"] for item in api["attempts"]] == ["TLS_FAILURE", "SUCCESS"]
    assert api["final_result"] == "SUCCESS"
    assert observed["retry_observed"] is True
    assert all("--retry" in command and command[command.index("--retry") + 1] == "0" for command in commands)
    assert all("--proto" in command and command[command.index("--proto") + 1] == "=https" for command in commands)
    assert all("--insecure" not in command and "-k" not in command and "--location" not in command for command in commands)


def test_actual_service_environment_is_reduced_to_presence_only() -> None:
    with tempfile.TemporaryDirectory() as raw:
        proc = Path(raw)
        environ = proc / "42" / "environ"
        environ.parent.mkdir()
        environ.write_bytes(b"HTTPS_PROXY=http://secret.invalid:17890\0NO_PROXY=localhost\0SECRET_TOKEN=do-not-record\0")

        def fake_runner(command, timeout, environment):
            prop = next(item for item in command if item.startswith("--property="))
            values = {
                "--property=ActiveState": "active\n",
                "--property=SubState": "running\n",
                "--property=MainPID": "42\n",
            }
            return 0, values[prop]

        runner, presence = probe.collect_runner_runtime(
            runner=fake_runner,
            environment={"GITHUB_ACTIONS": "true", "HTTPS_PROXY": "another-secret"},
            proc_root=proc,
        )
        assert runner["service_state"] == "ACTIVE"
        assert runner["runner_activity"] == "BUSY"
        assert presence["proxy_present"] is True
        assert presence["service_environment_observed"] is True
        rendered = json.dumps({"runner": runner, "presence": presence}, sort_keys=True)
        assert "secret.invalid" not in rendered
        assert "do-not-record" not in rendered
        assert "another-secret" not in rendered


def test_watchdog_unreadable_state_is_unknown_without_action() -> None:
    def fake_runner(command, timeout, environment):
        return 0, "active\n"

    with tempfile.TemporaryDirectory() as raw:
        result = probe.collect_watchdog(
            runner=fake_runner,
            state_path=Path(raw) / "missing",
            now_epoch=1000,
        )
    assert result["timer_state"] == "ACTIVE"
    assert result["state_readable"] is False
    assert result["decision"] == "UNKNOWN"
    assert result["cooldown_active"] is None


def test_watchdog_rate_limit_is_read_only_derived_state() -> None:
    def fake_runner(command, timeout, environment):
        return 0, "active\n"

    with tempfile.TemporaryDirectory() as raw:
        state = Path(raw) / "state"
        state.write_text("last_restart=950\nwindow_started=500\nrestarts=3\n", encoding="ascii")
        result = probe.collect_watchdog(runner=fake_runner, state_path=state, now_epoch=1000)
    assert result["decision"] == "RATE_LIMITED"
    assert result["restart_budget_remaining"] == 0
    assert result["cooldown_active"] is True
    assert result["post_restart_grace_active"] is True


def test_expected_local_poll_cancellation_is_explicit_context_not_transport_error() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runner_temp, diag = _layout(Path(raw))
        (diag / "Runner_current.log").write_text(
            "\n".join([
                "[RUNNER 2026-09-08 19:00:23Z INFO Runner] Runner version: '2.337.0'",
                "[RUNNER 2026-09-08 19:00:27Z INFO Terminal] Listening for Jobs",
                "[RUNNER 2026-09-08 19:04:17Z INFO BrokerMessageListener] OnJobStatus local token CancellationToken",
                "[RUNNER 2026-09-08 19:04:18Z WARN BrokerServer] retry with backoff after SocketException 125 operation canceled",
                "[RUNNER 2026-09-08 19:04:19Z INFO BrokerMessageListener] local token cancellation completed",
            ]) + "\n",
            encoding="utf-8",
        )
        observed = evidence.collect_runner_transport_evidence(
            runner_temp=runner_temp,
            assignment_latency_ms=0,
            include_context=True,
        )
    assert observed["context_counters"]["expected_local_poll_cancellation"] == 1
    assert observed["error_counters"]["broker_reconnect"] == 0
    assert observed["error_counters"]["transport_error_lines"] == 0


def test_healthy_requires_complete_current_session_and_zero_retries() -> None:
    observer = _load_observer()
    legacy = _healthy_legacy()
    saved = (
        observer.collect_runner_transport_evidence,
        observer.collect_runner_runtime,
        observer.collect_watchdog,
        observer.run_active_probes,
    )
    try:
        observer.collect_runner_transport_evidence = lambda **kwargs: dict(legacy)
        observer.collect_runner_runtime = lambda: (_runner(), _environment())
        observer.collect_watchdog = lambda: _watchdog()
        observer.run_active_probes = lambda policy: _active()
        value = observer.observe(controller_revision="a" * 40, runner_temp=Path("."))
        observer.validate_observation(value)
        assert value["classification"] == "HEALTHY"
        assert value["failure_domain"] == "UNKNOWN"
        assert value["timing"]["broker_assignment"]["value_ms"] is None
        assert "BROKER_ASSIGNMENT_TIMESTAMP_UNAVAILABLE" in value["evidence_limitations"]
        assert value["safety"]["network_probe_performed"] is True
        assert value["safety"]["automatic_recovery_performed"] is False
    finally:
        (
            observer.collect_runner_transport_evidence,
            observer.collect_runner_runtime,
            observer.collect_watchdog,
            observer.run_active_probes,
        ) = saved


def test_incomplete_session_cannot_be_healthy() -> None:
    observer = _load_observer()
    legacy = _healthy_legacy()
    legacy["diagnostics_truncated"] = True
    legacy["failure_classes"] = ["DIAGNOSTIC_WINDOW_TRUNCATED"]
    session = observer._session_view(legacy, now=observer._utc_now())
    classification, limitations = observer._classification(
        runner=_runner(),
        session=session,
        transport={"error_counters": legacy["error_counters"]},
        environment_presence=_environment(),
        watchdog=_watchdog(),
        active=_active(),
    )
    assert classification == "TRANSPORT_UNAVAILABLE"
    assert "CURRENT_SESSION_EVIDENCE_INCOMPLETE" in limitations


def test_retry_after_failure_is_degraded_even_when_final_probe_succeeds() -> None:
    observer = _load_observer()
    legacy = _healthy_legacy()
    session = observer._session_view(legacy, now=observer._utc_now())
    classification, _ = observer._classification(
        runner=_runner(),
        session=session,
        transport={"error_counters": legacy["error_counters"]},
        environment_presence=_environment(),
        watchdog=_watchdog(),
        active=_active(retry=True),
    )
    assert classification == "TRANSPORT_DEGRADED"


def test_dns_failure_is_degraded_with_narrow_failure_domain() -> None:
    observer = _load_observer()
    legacy = _healthy_legacy()
    session = observer._session_view(legacy, now=observer._utc_now())
    active = _active("DNS_FAILURE")
    classification, _ = observer._classification(
        runner=_runner(),
        session=session,
        transport={"error_counters": legacy["error_counters"]},
        environment_presence=_environment(),
        watchdog=_watchdog(),
        active=active,
    )
    assert classification == "TRANSPORT_DEGRADED"
    assert observer._failure_domain(_runner(), active) == "DNS_RESOLUTION"


def test_observation_validator_rejects_raw_url_in_artifact() -> None:
    observer = _load_observer()
    legacy = _healthy_legacy()
    saved = (
        observer.collect_runner_transport_evidence,
        observer.collect_runner_runtime,
        observer.collect_watchdog,
        observer.run_active_probes,
    )
    try:
        observer.collect_runner_transport_evidence = lambda **kwargs: dict(legacy)
        observer.collect_runner_runtime = lambda: (_runner(), _environment())
        observer.collect_watchdog = lambda: _watchdog()
        observer.run_active_probes = lambda policy: _active()
        value = observer.observe(controller_revision="b" * 40, runner_temp=Path("."))
        value["environment_presence"]["debug"] = "https://secret.invalid/path"
        try:
            observer.validate_observation(value)
        except AssertionError:
            pass
        else:
            raise AssertionError("raw URL escaped runner transport artifact validation")
    finally:
        (
            observer.collect_runner_transport_evidence,
            observer.collect_runner_runtime,
            observer.collect_watchdog,
            observer.run_active_probes,
        ) = saved


def test_observer_sources_have_no_phone_recovery_or_network_mutation_surface() -> None:
    combined = "\n".join(
        (CONTROLLER / "runner_transport_probe.py").read_text(encoding="utf-8").lower(),
        (SCRIPTS / "observe_runner_transport.py").read_text(encoding="utf-8").lower(),
    )
    forbidden = (
        "adb ", "adb-", "systemctl restart", "service restart", "svc.sh restart",
        "config.sh remove", "ip route add", "ip route replace", "resolv.conf", "iptables",
        "nft add", "netsh ", "deployment intent", "recover-runner-transport",
    )
    present = [item for item in forbidden if item in combined]
    assert present == []


def main() -> int:
    tests = sorted(
        (value for name, value in globals().items() if name.startswith("test_") and callable(value)),
        key=lambda item: item.__name__,
    )
    for test in tests:
        test()
    print(f"RUNNER_TRANSPORT_OBSERVER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
