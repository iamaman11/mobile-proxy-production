from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import quarantine_phone_observer as observer  # noqa: E402
from quarantine_recovery import (  # noqa: E402
    QUARANTINED_INTENT_REF,
    QUARANTINED_REQUEST_ID,
    QUARANTINED_TERMINAL_REF,
    RECOVERY_INTENT_SCHEMA,
    RECOVERY_OPERATION,
    RECOVERY_PARENT_TERMINAL_REF,
    RECOVERY_RELEASE,
    RECOVERY_RELEASE_ID,
    RECOVERY_TARGET,
    RECOVERY_TERMINAL_SCHEMA,
    RECOVERY_UNKNOWN_TERMINAL_REF,
    QuarantineRecoveryError,
    recovery_semantic_id,
    validate_recovery_intent,
    validate_recovery_terminal,
)


def expect_error(fn) -> None:
    try:
        fn()
    except QuarantineRecoveryError:
        return
    raise AssertionError("expected QuarantineRecoveryError")


def reconciliation_id() -> str:
    return recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_UNKNOWN_TERMINAL_REF,
    )


def test_unknown_parent_has_distinct_exact_semantic_identity() -> None:
    initial = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
    )
    continuation = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_PARENT_TERMINAL_REF,
    )
    reconciliation = reconciliation_id()
    assert len({initial, continuation, reconciliation}) == 3
    expect_error(lambda: recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref="issue-comment:1",
    ))


def test_unknown_parent_cannot_create_mutation_intent() -> None:
    payload = {
        "schema": RECOVERY_INTENT_SCHEMA,
        "semantic_recovery_id": reconciliation_id(),
        "operation": RECOVERY_OPERATION,
        "execution_id": "gh-run:1:1",
        "controller_revision": "a" * 40,
        "target": RECOVERY_TARGET,
        "target_binding_id": "tb-hmac-sha256:" + "b" * 64,
        "product_release": RECOVERY_RELEASE,
        "release_id": RECOVERY_RELEASE_ID,
        "quarantined_request_id": QUARANTINED_REQUEST_ID,
        "quarantined_intent_ref": QUARANTINED_INTENT_REF,
        "quarantined_terminal_ref": QUARANTINED_TERMINAL_REF,
        "parent_recovery_terminal_ref": RECOVERY_UNKNOWN_TERMINAL_REF,
        "apk_exact": True,
        "inactive_runtime_exact": True,
        "current_before": "/data/adb/mobile-proxy-node/releases/old",
        "activation_may_reach_target": True,
        "blind_retry_allowed": False,
        "mutation_performed": False,
    }
    expect_error(lambda: validate_recovery_intent(payload))


def accepted_terminal() -> dict[str, object]:
    return {
        "schema": RECOVERY_TERMINAL_SCHEMA,
        "semantic_recovery_id": reconciliation_id(),
        "operation": RECOVERY_OPERATION,
        "execution_id": "gh-run:1:1",
        "controller_revision": "a" * 40,
        "target": RECOVERY_TARGET,
        "product_release": RECOVERY_RELEASE,
        "release_id": RECOVERY_RELEASE_ID,
        "quarantined_request_id": QUARANTINED_REQUEST_ID,
        "quarantined_terminal_ref": QUARANTINED_TERMINAL_REF,
        "parent_recovery_terminal_ref": RECOVERY_UNKNOWN_TERMINAL_REF,
        "recovery_intent_ref": None,
        "state": "ACCEPTED",
        "mutation_performed": False,
        "postcondition_verified": True,
        "blocking_predicate": None,
        "facts": {"postcondition": {}},
        "blind_retry_allowed": False,
    }


def test_unknown_reconciliation_terminal_is_structurally_read_only() -> None:
    payload = accepted_terminal()
    validate_recovery_terminal(payload)
    validate_recovery_terminal(dict(payload, state="REFUSED", blocking_predicate="known non-desired state"))
    expect_error(lambda: validate_recovery_terminal(dict(payload, mutation_performed=True)))
    expect_error(lambda: validate_recovery_terminal(dict(payload, postcondition_verified=False)))
    expect_error(lambda: validate_recovery_terminal(dict(payload, state="UNKNOWN", mutation_performed=True)))
    expect_error(lambda: validate_recovery_terminal(dict(payload, recovery_intent_ref="issue-comment:1")))


def _root_result(stdout: bytes):
    return SimpleNamespace(status="completed", returncode=0, stdout=stdout, stderr=b"")


def test_read_only_observer_proves_target_and_contains_no_mutation_primitives() -> None:
    expected = "1" * 64
    captured: list[bytes] = []
    old_files = observer._files
    old_run = observer._run_root_script
    try:
        observer._files = lambda release_root, required_paths: (("service.sh", Path("/tmp/service.sh"), expected),)
        def run(serial, script, timeout):
            captured.append(script)
            return _root_result((
                "target=present\n"
                "current=/data/adb/mobile-proxy-node/releases/v0.1.7\n"
                "h0=" + expected + "\n"
            ).encode())
        observer._run_root_script = run
        result = observer.observe_exact_inactive_runtime(
            serial="registered",
            release_root=Path("/tmp/release"),
            release_id="v0.1.7",
            required_paths=("service.sh",),
        )
    finally:
        observer._files = old_files
        observer._run_root_script = old_run
    assert result["desired"] is True
    assert result["current_relation"] == "target"
    assert result["inactive_exact_files_verified"] is True
    text = captured[0].decode()
    for forbidden in ("mkdir ", "cp ", "rm ", "mv ", "ln ", "kill ", "chmod ", "service.sh\n", "pm install", "am start"):
        assert forbidden not in text


def test_other_managed_current_is_bounded_not_raw() -> None:
    expected = "1" * 64
    old_files = observer._files
    old_run = observer._run_root_script
    try:
        observer._files = lambda release_root, required_paths: (("service.sh", Path("/tmp/service.sh"), expected),)
        observer._run_root_script = lambda serial, script, timeout: _root_result((
            "target=present\n"
            "current=/data/adb/mobile-proxy-node/releases/git-old\n"
            "h0=" + expected + "\n"
        ).encode())
        result = observer.observe_exact_inactive_runtime(
            serial="registered",
            release_root=Path("/tmp/release"),
            release_id="v0.1.7",
            required_paths=("service.sh",),
        )
    finally:
        observer._files = old_files
        observer._run_root_script = old_run
    assert result["current_relation"] == "other-managed"
    assert result["desired"] is False
    assert "git-old" not in repr(result)


def test_target_reconciliation_script_has_no_mutation_callsite() -> None:
    source = (ROOT.parent / "scripts" / "run_quarantine_reconciliation.py").read_text(encoding="utf-8")
    for forbidden in (
        "_activate(", "dispatch_release_once(", "dispatch_install_once(",
        "_stage_runtime(", "_materialize_inactive(", "RECOVERY_INTENT_SCHEMA",
    ):
        assert forbidden not in source
    for required in (
        "RECOVERY_UNKNOWN_TERMINAL_REF", "observe_exact_inactive_runtime(",
        '"durable_mutation_intent_created": False', '"mutation_performed": False',
    ):
        assert required in source


if __name__ == "__main__":
    tests = [name for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for name in tests:
        globals()[name]()
    print(f"UNKNOWN_RECONCILIATION_TESTS_OK count={len(tests)}")
