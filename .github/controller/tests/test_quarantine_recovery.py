#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from durable_release_identity import durable_release_identity  # noqa: E402
from quarantine_recovery import (  # noqa: E402
    QUARANTINED_INTENT_REF,
    QUARANTINED_REQUEST_ID,
    QUARANTINED_TERMINAL_REF,
    RECOVERY_INTENT_HEADING,
    RECOVERY_INTENT_SCHEMA,
    RECOVERY_OPERATION,
    RECOVERY_PARENT_INTENT_REF,
    RECOVERY_PARENT_TERMINAL_REF,
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

RECONCILE_SCRIPT = ROOT.parent / "scripts" / "reconcile_quarantine_unknown.py"
reconcile_spec = importlib.util.spec_from_file_location("reconcile_quarantine_unknown", RECONCILE_SCRIPT)
reconcile_runner = importlib.util.module_from_spec(reconcile_spec)
assert reconcile_spec.loader is not None
reconcile_spec.loader.exec_module(reconcile_runner)

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


def initial_semantic_id() -> str:
    return recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
    )


def activation_semantic_id() -> str:
    return recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_PARENT_TERMINAL_REF,
    )


def reconciliation_semantic_id() -> str:
    return recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_UNKNOWN_TERMINAL_REF,
    )


def base_intent() -> dict[str, object]:
    return {
        "schema": RECOVERY_INTENT_SCHEMA,
        "semantic_recovery_id": initial_semantic_id(),
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


def base_terminal() -> dict[str, object]:
    return {
        "schema": RECOVERY_TERMINAL_SCHEMA,
        "semantic_recovery_id": initial_semantic_id(),
        "operation": RECOVERY_OPERATION,
        "execution_id": "gh-run:1:1",
        "controller_revision": "a" * 40,
        "target": RECOVERY_TARGET,
        "product_release": RECOVERY_RELEASE,
        "release_id": RECOVERY_RELEASE_ID,
        "quarantined_request_id": QUARANTINED_REQUEST_ID,
        "quarantined_terminal_ref": QUARANTINED_TERMINAL_REF,
        "recovery_intent_ref": "issue-comment:1",
        "state": "ACCEPTED",
        "mutation_performed": True,
        "postcondition_verified": True,
        "blocking_predicate": None,
        "facts": {"postcondition": {}},
        "blind_retry_allowed": False,
    }


def test_semantic_identity_is_exact_and_stable() -> None:
    assert initial_semantic_id() == initial_semantic_id()
    assert initial_semantic_id().startswith("recovery-sha256:")
    expect_error(lambda: recovery_semantic_id(
        target=RECOVERY_TARGET,
        release="v0.1.8",
        quarantined_request_id=QUARANTINED_REQUEST_ID,
    ))


def test_three_bounded_semantic_generations_are_distinct_and_parent_bound() -> None:
    identities = {initial_semantic_id(), activation_semantic_id(), reconciliation_semantic_id()}
    assert len(identities) == 3
    assert all(item.startswith("recovery-sha256:") for item in identities)
    expect_error(lambda: recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref="issue-comment:1",
    ))


def test_activation_payload_requires_exact_parent_hash_binding() -> None:
    intent = base_intent()
    intent["parent_recovery_terminal_ref"] = RECOVERY_PARENT_TERMINAL_REF
    intent["semantic_recovery_id"] = activation_semantic_id()
    validate_recovery_intent(intent)
    wrong_id = dict(intent, semantic_recovery_id=initial_semantic_id())
    expect_error(lambda: validate_recovery_intent(wrong_id))

    terminal = base_terminal()
    terminal["parent_recovery_terminal_ref"] = RECOVERY_PARENT_TERMINAL_REF
    terminal["semantic_recovery_id"] = activation_semantic_id()
    validate_recovery_terminal(terminal)
    wrong_terminal_id = dict(terminal, semantic_recovery_id=initial_semantic_id())
    expect_error(lambda: validate_recovery_terminal(wrong_terminal_id))


def test_reconciliation_terminal_is_parent_bound_and_has_no_intent_or_mutation() -> None:
    payload = reconcile_runner._terminal(
        semantic_id=reconciliation_semantic_id(),
        execution_id="gh-run:2:1",
        controller_revision="a" * 40,
        state="ACCEPTED",
        postcondition_verified=True,
        facts={"postcondition": {}, "reconciliation_mode": "read_only"},
        reason=None,
    )
    assert payload["parent_recovery_terminal_ref"] == RECOVERY_UNKNOWN_TERMINAL_REF
    assert payload["recovery_intent_ref"] is None
    assert payload["mutation_performed"] is False
    validate_recovery_terminal(payload)


def test_intent_requires_exact_no_blind_retry_boundary() -> None:
    payload = base_intent()
    validate_recovery_intent(payload)
    broken = dict(payload)
    broken["blind_retry_allowed"] = True
    expect_error(lambda: validate_recovery_intent(broken))


def test_terminal_contract_distinguishes_pre_and_post_intent_states() -> None:
    accepted = base_terminal()
    validate_recovery_terminal(accepted)
    refused = dict(accepted, state="REFUSED", mutation_performed=False, postcondition_verified=False, recovery_intent_ref=None)
    validate_recovery_terminal(refused)
    unknown = dict(accepted, state="UNKNOWN", mutation_performed=True, postcondition_verified=False)
    validate_recovery_terminal(unknown)
    bad = dict(accepted, state="UNKNOWN", mutation_performed=False, postcondition_verified=False)
    expect_error(lambda: validate_recovery_terminal(bad))


def test_prepare_requires_exact_unknown_parent_and_admits_only_reconciliation_child() -> None:
    first_intent_payload = base_intent()
    first_terminal_payload = dict(
        base_terminal(),
        state="QUARANTINED",
        mutation_performed=True,
        postcondition_verified=True,
        recovery_intent_ref=RECOVERY_PARENT_INTENT_REF,
        blocking_predicate="recovery postcondition mismatch",
    )
    unknown_intent_payload = dict(
        base_intent(),
        semantic_recovery_id=activation_semantic_id(),
        parent_recovery_terminal_ref=RECOVERY_PARENT_TERMINAL_REF,
        execution_id="gh-run:2:1",
    )
    unknown_terminal_payload = dict(
        base_terminal(),
        semantic_recovery_id=activation_semantic_id(),
        parent_recovery_terminal_ref=RECOVERY_PARENT_TERMINAL_REF,
        execution_id="gh-run:2:1",
        recovery_intent_ref=RECOVERY_UNKNOWN_INTENT_REF,
        state="UNKNOWN",
        mutation_performed=True,
        postcondition_verified=False,
        blocking_predicate="activation outcome is unknown",
    )
    records = {
        RECOVERY_INTENT_HEADING: [
            SimpleNamespace(ref=RECOVERY_PARENT_INTENT_REF, payload=first_intent_payload),
            SimpleNamespace(ref=RECOVERY_UNKNOWN_INTENT_REF, payload=unknown_intent_payload),
        ],
        RECOVERY_TERMINAL_HEADING: [
            SimpleNamespace(ref=RECOVERY_PARENT_TERMINAL_REF, payload=first_terminal_payload),
            SimpleNamespace(ref=RECOVERY_UNKNOWN_TERMINAL_REF, payload=unknown_terminal_payload),
        ],
    }

    class Evidence:
        def list_records(self, heading):
            return list(records.get(heading, []))

    parent, semantic_id = prepare_recovery._validate_recovery_lineage(Evidence())
    assert parent.ref == RECOVERY_UNKNOWN_TERMINAL_REF
    assert semantic_id == reconciliation_semantic_id()

    records[RECOVERY_TERMINAL_HEADING][1] = SimpleNamespace(
        ref=RECOVERY_UNKNOWN_TERMINAL_REF,
        payload=dict(unknown_terminal_payload, state="QUARANTINED", postcondition_verified=True),
    )
    expect_error(lambda: prepare_recovery._validate_recovery_lineage(Evidence()))


def test_quarantined_terminal_identity_uses_schema_owned_runtime_fact() -> None:
    identity = SimpleNamespace(
        tag=RECOVERY_RELEASE,
        release_id=RECOVERY_RELEASE_ID,
        source_sha="a" * 40,
        artifact_digest="b3:" + "b" * 64,
        phone_runtime_artifact_name=f"mobile-proxy-phone-production-runtime-{RECOVERY_RELEASE}.tar.gz",
        phone_runtime_artifact_digest="b3:" + "c" * 64,
        phone_runtime_inventory_path="phone-production-runtime/components.json",
        phone_runtime_inventory_digest="b3:" + "d" * 64,
    )
    durable = durable_release_identity(identity, target=RECOVERY_TARGET)
    terminal = {
        key: durable[key]
        for key in ("target", "product_release", "release_id", "release_source_sha", "artifact_digest")
    }
    terminal["facts"] = {"release_admission": dict(durable)}
    assert prepare_recovery._terminal_matches_release_identity(terminal, identity) is True

    runtime_mismatch = dict(durable)
    runtime_mismatch["phone_runtime_artifact_digest"] = "b3:" + "e" * 64
    broken_runtime = dict(terminal)
    broken_runtime["facts"] = {"release_admission": runtime_mismatch}
    assert prepare_recovery._terminal_matches_release_identity(broken_runtime, identity) is False


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
    assert observed["inactive_exact_files_verified"] is True
    assert observed["current_managed"] is True
    assert observed["desired"] is False


def test_reconciliation_observer_proves_desired_without_mutation_script() -> None:
    expected = "1" * 64
    captured: list[bytes] = []
    old_files = reconcile_runner._files
    old_run = reconcile_runner._run_root_script
    try:
        reconcile_runner._files = lambda release_root, required_paths: (("service.sh", Path("/tmp/service.sh"), expected),)
        def run(serial, script, timeout):
            captured.append(script)
            return _fake_root_result(
                ("target=present\ncurrent=/data/adb/mobile-proxy-node/releases/v0.1.7\nh0=" + expected + "\n").encode()
            )
        reconcile_runner._run_root_script = run
        observed = reconcile_runner._root_runtime_observation(
            serial="registered",
            release_root=Path("/tmp/release"),
            release_id=RECOVERY_RELEASE,
            required_paths=("service.sh",),
        )
    finally:
        reconcile_runner._files = old_files
        reconcile_runner._run_root_script = old_run
    assert observed["desired"] is True
    text = captured[0].decode()
    for forbidden in ("mkdir ", "cp ", "rm ", "mv ", "ln ", "kill ", "chmod ", "service.sh\n"):
        assert forbidden not in text


def test_reconciliation_observer_classifies_old_current_as_known_not_desired() -> None:
    expected = "1" * 64
    old_files = reconcile_runner._files
    old_run = reconcile_runner._run_root_script
    try:
        reconcile_runner._files = lambda release_root, required_paths: (("service.sh", Path("/tmp/service.sh"), expected),)
        reconcile_runner._run_root_script = lambda serial, script, timeout: _fake_root_result(
            ("target=present\ncurrent=/data/adb/mobile-proxy-node/releases/old\nh0=" + expected + "\n").encode()
        )
        observed = reconcile_runner._root_runtime_observation(
            serial="registered",
            release_root=Path("/tmp/release"),
            release_id=RECOVERY_RELEASE,
            required_paths=("service.sh",),
        )
    finally:
        reconcile_runner._files = old_files
        reconcile_runner._run_root_script = old_run
    assert observed["inactive_exact_files_verified"] is True
    assert observed["current_managed"] is True
    assert observed["desired"] is False


if __name__ == "__main__":
    tests = [name for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for name in tests:
        globals()[name]()
    print(f"QUARANTINE_RECOVERY_TESTS_OK count={len(tests)}")
