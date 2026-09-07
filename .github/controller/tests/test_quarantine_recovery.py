#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quarantine_recovery import (  # noqa: E402
    QUARANTINED_INTENT_REF,
    QUARANTINED_REQUEST_ID,
    QUARANTINED_TERMINAL_REF,
    RECOVERY_INTENT_HEADING,
    RECOVERY_INTENT_SCHEMA,
    RECOVERY_OPERATION,
    RECOVERY_PARENT_INTENT_REF,
    RECOVERY_PARENT_TERMINAL_REF,
    RECOVERY_RECONCILED_REFUSED_TERMINAL_REF,
    RECOVERY_RELEASE,
    RECOVERY_RELEASE_ID,
    RECOVERY_TARGET,
    RECOVERY_TERMINAL_HEADING,
    RECOVERY_TERMINAL_SCHEMA,
    RECOVERY_UNKNOWN_INTENT_REF,
    RECOVERY_UNKNOWN_TERMINAL_REF,
    QuarantineRecoveryError,
    recovery_semantic_id,
    validate_recovery_intent,
    validate_recovery_terminal,
)

SCRIPT = ROOT.parent / "scripts" / "run_quarantine_recovery.py"
spec = importlib.util.spec_from_file_location("run_quarantine_recovery", SCRIPT)
recovery_runner = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(recovery_runner)

PREPARE_SCRIPT = ROOT.parent / "scripts" / "prepare_quarantine_recovery.py"
prepare_spec = importlib.util.spec_from_file_location("prepare_quarantine_recovery", PREPARE_SCRIPT)
prepare_recovery = importlib.util.module_from_spec(prepare_spec)
assert prepare_spec.loader is not None
prepare_spec.loader.exec_module(prepare_recovery)


def expect_error(fn) -> None:
    try:
        fn()
    except QuarantineRecoveryError:
        return
    raise AssertionError("expected QuarantineRecoveryError")


def semantic(parent: str | None = None) -> str:
    return recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=parent,
    )


def intent_payload(*, parent: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": RECOVERY_INTENT_SCHEMA,
        "semantic_recovery_id": semantic(parent),
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
        "apk_exact": True,
        "inactive_runtime_exact": True,
        "current_before": "/data/adb/mobile-proxy-node/releases/old",
        "activation_may_reach_target": True,
        "blind_retry_allowed": False,
        "mutation_performed": False,
    }
    if parent is not None:
        payload["parent_recovery_terminal_ref"] = parent
    return payload


def terminal_payload(
    *, parent: str | None = None, state: str = "ACCEPTED", mutation: bool = True,
    verified: bool = True, intent_ref: str | None = "issue-comment:1", facts: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": RECOVERY_TERMINAL_SCHEMA,
        "semantic_recovery_id": semantic(parent),
        "operation": RECOVERY_OPERATION,
        "execution_id": "gh-run:1:1",
        "controller_revision": "a" * 40,
        "target": RECOVERY_TARGET,
        "product_release": RECOVERY_RELEASE,
        "release_id": RECOVERY_RELEASE_ID,
        "quarantined_request_id": QUARANTINED_REQUEST_ID,
        "quarantined_terminal_ref": QUARANTINED_TERMINAL_REF,
        "recovery_intent_ref": intent_ref,
        "state": state,
        "mutation_performed": mutation,
        "postcondition_verified": verified,
        "blocking_predicate": None if state == "ACCEPTED" else "bounded test state",
        "facts": facts if facts is not None else {"postcondition": {}},
        "blind_retry_allowed": False,
    }
    if parent is not None:
        payload["parent_recovery_terminal_ref"] = parent
    return payload


def reconciled_facts(*, current_relation: str = "other-managed") -> dict[str, object]:
    return {
        "postcondition": {
            "apk": {"desired": True, "exact_artifact_verified": True},
            "runtime": {
                "target_release_exists": True,
                "inactive_exact_files_verified": True,
                "mismatch_count": 0,
                "mismatch_files": [],
                "current_relation": current_relation,
            },
            "target_binding_matches_original_intent": True,
        }
    }


def exact_lineage_records():
    initial_intent = SimpleNamespace(ref=RECOVERY_PARENT_INTENT_REF, payload=intent_payload())
    initial_terminal = SimpleNamespace(
        ref=RECOVERY_PARENT_TERMINAL_REF,
        payload=terminal_payload(
            state="QUARANTINED", mutation=True, verified=True,
            intent_ref=RECOVERY_PARENT_INTENT_REF,
        ),
    )
    continuation_intent = SimpleNamespace(
        ref=RECOVERY_UNKNOWN_INTENT_REF,
        payload=intent_payload(parent=RECOVERY_PARENT_TERMINAL_REF),
    )
    continuation_terminal = SimpleNamespace(
        ref=RECOVERY_UNKNOWN_TERMINAL_REF,
        payload=terminal_payload(
            parent=RECOVERY_PARENT_TERMINAL_REF,
            state="UNKNOWN", mutation=True, verified=False,
            intent_ref=RECOVERY_UNKNOWN_INTENT_REF,
        ),
    )
    reconciliation_terminal = SimpleNamespace(
        ref=RECOVERY_RECONCILED_REFUSED_TERMINAL_REF,
        payload=terminal_payload(
            parent=RECOVERY_UNKNOWN_TERMINAL_REF,
            state="REFUSED", mutation=False, verified=True,
            intent_ref=None, facts=reconciled_facts(),
        ),
    )
    return initial_intent, initial_terminal, continuation_intent, continuation_terminal, reconciliation_terminal


class Evidence:
    def __init__(self, *, bad_reconciliation: bool = False):
        records = list(exact_lineage_records())
        if bad_reconciliation:
            records[-1] = SimpleNamespace(
                ref=RECOVERY_RECONCILED_REFUSED_TERMINAL_REF,
                payload=terminal_payload(
                    parent=RECOVERY_UNKNOWN_TERMINAL_REF,
                    state="REFUSED", mutation=False, verified=True,
                    intent_ref=None, facts=reconciled_facts(current_relation="target"),
                ),
            )
        self.intents = [records[0], records[2]]
        self.terminals = [records[1], records[3], records[4]]

    def list_records(self, heading):
        if heading == RECOVERY_INTENT_HEADING:
            return self.intents
        if heading == RECOVERY_TERMINAL_HEADING:
            return self.terminals
        return []


def test_all_stage3_recovery_generations_have_distinct_semantic_identity() -> None:
    ids = {
        semantic(),
        semantic(RECOVERY_PARENT_TERMINAL_REF),
        semantic(RECOVERY_UNKNOWN_TERMINAL_REF),
        semantic(RECOVERY_RECONCILED_REFUSED_TERMINAL_REF),
    }
    assert len(ids) == 4
    expect_error(lambda: semantic("issue-comment:1"))


def test_unknown_parent_is_read_only_but_reconciled_refused_parent_can_form_new_intent() -> None:
    expect_error(lambda: validate_recovery_intent(intent_payload(parent=RECOVERY_UNKNOWN_TERMINAL_REF)))
    final_intent = intent_payload(parent=RECOVERY_RECONCILED_REFUSED_TERMINAL_REF)
    validate_recovery_intent(final_intent)
    assert final_intent["semantic_recovery_id"] == semantic(RECOVERY_RECONCILED_REFUSED_TERMINAL_REF)


def test_reconciled_refused_terminal_contract_is_read_only_and_verified() -> None:
    payload = terminal_payload(
        parent=RECOVERY_UNKNOWN_TERMINAL_REF,
        state="REFUSED", mutation=False, verified=True,
        intent_ref=None, facts=reconciled_facts(),
    )
    validate_recovery_terminal(payload)
    expect_error(lambda: validate_recovery_terminal(dict(payload, mutation_performed=True)))
    expect_error(lambda: validate_recovery_terminal(dict(payload, postcondition_verified=False)))


def test_prepare_binds_exact_full_lineage_and_known_safe_reconciliation() -> None:
    parent = prepare_recovery._validated_parent_recovery(Evidence())
    assert parent.ref == RECOVERY_RECONCILED_REFUSED_TERMINAL_REF
    assert parent.payload["state"] == "REFUSED"
    expect_error(lambda: prepare_recovery._validated_parent_recovery(Evidence(bad_reconciliation=True)))


def test_target_runner_revalidates_exact_reconciled_parent_under_lock() -> None:
    class RunnerEvidence:
        def list_records(self, heading):
            records = exact_lineage_records()
            if heading == RECOVERY_INTENT_HEADING:
                return []
            if heading == RECOVERY_TERMINAL_HEADING:
                return [records[-1]]
            return []

    parent = recovery_runner._validated_parent_recovery(RunnerEvidence())
    assert parent.ref == RECOVERY_RECONCILED_REFUSED_TERMINAL_REF


def test_final_terminal_is_bound_to_reconciled_parent() -> None:
    payload = recovery_runner._terminal(
        semantic_id=semantic(RECOVERY_RECONCILED_REFUSED_TERMINAL_REF),
        execution_id="gh-run:1:1",
        controller_revision="a" * 40,
        state="ACCEPTED",
        mutation_performed=True,
        postcondition_verified=True,
        facts={"postcondition": {}},
        intent_ref="issue-comment:99",
        reason=None,
    )
    assert payload["parent_recovery_terminal_ref"] == RECOVERY_RECONCILED_REFUSED_TERMINAL_REF
    validate_recovery_terminal(payload)


def _fake_root_result(stdout: bytes):
    return SimpleNamespace(status="completed", returncode=0, stdout=stdout, stderr=b"")


def test_inactive_runtime_hashes_are_root_observed_while_current_is_old() -> None:
    expected = "1" * 64
    old_files = recovery_runner._files
    old_run = recovery_runner._run_root_script
    try:
        recovery_runner._files = lambda release_root, required_paths: (("service.sh", Path("/tmp/service.sh"), expected),)
        recovery_runner._run_root_script = lambda serial, script, timeout: _fake_root_result(
            ("target=present\ncurrent=/data/adb/mobile-proxy-node/releases/old\nh0=" + expected + "\n").encode()
        )
        observed = recovery_runner._root_runtime_observation(
            serial="registered", release_root=Path("/tmp/release"), release_id=RECOVERY_RELEASE,
            required_paths=("service.sh",),
        )
    finally:
        recovery_runner._files = old_files
        recovery_runner._run_root_script = old_run
    assert observed["target_release_exists"] is True
    assert observed["inactive_exact_files_verified"] is True
    assert observed["current_managed"] is True
    assert observed["desired"] is False


def test_root_observer_script_contains_no_mutation_primitive() -> None:
    expected = "1" * 64
    captured: list[bytes] = []
    old_files = recovery_runner._files
    old_run = recovery_runner._run_root_script
    try:
        recovery_runner._files = lambda release_root, required_paths: (("service.sh", Path("/tmp/service.sh"), expected),)
        def run(serial, script, timeout):
            captured.append(script)
            return _fake_root_result(("target=present\ncurrent=/data/adb/mobile-proxy-node/releases/old\nh0=" + expected + "\n").encode())
        recovery_runner._run_root_script = run
        recovery_runner._root_runtime_observation(
            serial="registered", release_root=Path("/tmp/release"), release_id=RECOVERY_RELEASE,
            required_paths=("service.sh",),
        )
    finally:
        recovery_runner._files = old_files
        recovery_runner._run_root_script = old_run
    text = captured[0].decode()
    for forbidden in ("mkdir ", "cp ", "rm ", "mv ", "ln ", "kill ", "chmod ", "pm install"):
        assert forbidden not in text


def test_runner_source_reobserves_completed_activation_failure_but_not_transport_unknown() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    unknown_catch = source.index("except PhoneTargetMutationOutcomeUnknown:")
    known_catch = source.index("except PhoneTargetUnavailable as exc:")
    post_observe = source.index("apk_post = observe(")
    assert unknown_catch < known_catch < post_observe
    assert 'activation_state = "completed_failure"' in source
    assert 'state="UNKNOWN"' in source
    assert 'state="ACCEPTED" if accepted else "QUARANTINED"' in source


if __name__ == "__main__":
    tests = [name for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for name in tests:
        globals()[name]()
    print(f"QUARANTINE_RECOVERY_TESTS_OK count={len(tests)}")
