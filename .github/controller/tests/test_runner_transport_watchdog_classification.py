from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
CONTROLLER = ROOT / "controller"
sys.path.insert(0, str(CONTROLLER))


def _observer():
    name = "observe_runner_transport_watchdog_classification_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / "observe_runner_transport.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runner() -> dict[str, object]:
    return {
        "service_state": "ACTIVE",
        "runner_activity": "BUSY",
    }


def _session() -> dict[str, object]:
    return {"evidence_complete": True}


def _environment() -> dict[str, object]:
    return {
        "service_environment_observed": True,
        "observer_environment_matches_presence": True,
    }


def _transport(*, tls_errors: int = 0) -> dict[str, object]:
    return {
        "error_counters": {
            "broker_reconnect": 0,
            "runserver_reconnect": 0,
            "tls_error": tls_errors,
            "eof_error": 0,
            "connection_reset": 0,
            "action_download_error": 0,
            "artifact_transport_error": 0,
            "transport_error_lines": tls_errors,
        }
    }


def _active() -> dict[str, object]:
    return {
        "roles": [
            {"role": "GITHUB_API", "final_result": "SUCCESS"},
            {"role": "REPOSITORY_TRANSPORT", "final_result": "SUCCESS"},
            {"role": "ACTION_DOWNLOAD", "final_result": "SUCCESS"},
            {"role": "ACTIONS_RESULTS", "final_result": "SUCCESS"},
        ],
        "retry_observed": False,
        "budget_exhausted": False,
    }


def _watchdog(
    decision: str,
    *,
    cooldown: bool = False,
    grace: bool = False,
    restart_count: int = 0,
) -> dict[str, object]:
    return {
        "timer_state": "ACTIVE",
        "state_readable": True,
        "decision": decision,
        "cooldown_active": cooldown,
        "restart_budget_remaining": 3,
        "post_restart_grace_active": grace,
        "restart_count_window": restart_count,
    }


def _classify(watchdog: dict[str, object], *, tls_errors: int = 0) -> str:
    observer = _observer()
    classification, _ = observer._classification(
        runner=_runner(),
        session=_session(),
        transport=_transport(tls_errors=tls_errors),
        environment_presence=_environment(),
        watchdog=watchdog,
        active=_active(),
    )
    return classification


def test_neutral_observe_is_context_not_transport_degradation() -> None:
    assert _classify(_watchdog("OBSERVE")) == "HEALTHY"


def test_observe_with_cooldown_remains_degraded() -> None:
    assert _classify(_watchdog("OBSERVE", cooldown=True)) == "TRANSPORT_DEGRADED"


def test_observe_with_post_restart_grace_remains_degraded() -> None:
    assert _classify(_watchdog("OBSERVE", grace=True)) == "TRANSPORT_DEGRADED"


def test_observe_with_restart_history_remains_degraded() -> None:
    assert _classify(_watchdog("OBSERVE", restart_count=1)) == "TRANSPORT_DEGRADED"


def test_restart_eligible_remains_degraded() -> None:
    assert _classify(_watchdog("RESTART_ELIGIBLE")) == "TRANSPORT_DEGRADED"


def test_rate_limited_remains_degraded() -> None:
    assert _classify(_watchdog("RATE_LIMITED")) == "TRANSPORT_DEGRADED"


def test_real_transport_symptom_still_degrades_neutral_observe() -> None:
    assert _classify(_watchdog("OBSERVE"), tls_errors=1) == "TRANSPORT_DEGRADED"


def main() -> int:
    tests = sorted(
        (value for name, value in globals().items() if name.startswith("test_") and callable(value)),
        key=lambda item: item.__name__,
    )
    for test in tests:
        test()
    print(f"RUNNER_TRANSPORT_WATCHDOG_CLASSIFICATION_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
