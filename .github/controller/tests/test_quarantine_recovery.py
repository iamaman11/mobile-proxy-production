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


def initial_semantic_id() -> str:
    return recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
    )


def continuation_semantic_id() -> str:
    return recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_PARENT_TERMINAL_REF,
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
    first = initial_semantic_id()
    second = initial_semantic_id()
    assert first == second and first.startswith("recovery-sha256:")
    expect_error(lambda: recovery_semantic_id(
        target=RECOVERY_TARGET, release="v0.1.8",
        quarantined_request_id=QUARANTINED_REQUEST_ID,
    ))


def test_continuation_semantic_identity_is_distinct_and_parent_bound() -> None:
    initial = initial_semantic_id()
    continuation = continuation_semantic_id()
    assert continuation.startswith("recovery-sha256:")
    assert continuation != initial
    assert continuation == continuation_semantic_id()
    expect_error(lambda: recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref="issue-comment:1",
    ))


def test_continuation_payload_requires_exact_parent_hash_binding() -> None:
    intent = base_intent()
    intent["parent_recovery_terminal_ref"] = RECOVERY_PARENT_TERMINAL_REF
    intent["semantic_recovery_id"] = continuation_semantic_id()
    validate_recovery_intent(intent)
    wrong_parent = dict(intent, parent_recovery_terminal_ref="issue-comment:1")
    expect_error(lambda: validate_recovery_intent(wrong_parent))
    wrong_id = dict(intent, semantic_recovery_id=initial_semantic_id())
    expect_error(lambda: validate_recovery_intent(wrong_id))

    terminal = base_terminal()
    terminal["parent_recovery_terminal_ref"] = RECOVERY_PARENT_TERMINAL_REF
    terminal["semantic_recovery_id"] = continuation_semantic_id()
    validate_recovery_terminal(terminal)
    wrong_terminal_id = dict(terminal, semantic_recovery_id=initial_semantic_id())
    expect_error(lambda: validate_recovery_terminal(wrong_terminal_id))


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


def test_parent_recovery_validation_is_exact_and_bounded() -> None:
    parent_intent_payload = base_intent()
    parent_terminal_payload = dict(
        base_terminal(),
        state="QUARANTINED",
        mutation_performed=True,
        postcondition_verified=True,
        recovery_intent_ref=RECOVERY_PARENT_INTENT_REF,
        blocking_predicate="recovery postcondition mismatch",
    )
    intent_record = SimpleNamespace(ref=RECOVERY_PARENT_INTENT_REF, payload=parent_intent_payload)
    terminal_record = SimpleNamespace(ref=RECOVERY_PARENT_TERMINAL_REF, payload=parent_terminal_payload)

    class Evidence:
        def list_records(self, heading):
            if heading == RECOVERY_INTENT_HEADING:
                return [intent_record]
            if heading == RECOVERY_TERMINAL_HEADING:
                return [terminal_record]
            return []

    parent_intent, parent_terminal = prepare_recovery._validated_parent_recovery(Evidence())
    assert parent_intent.ref == RECOVERY_PARENT_INTENT_REF
    assert parent_terminal.ref == RECOVERY_PARENT_TERMINAL_REF

    wrong_terminal = SimpleNamespace(ref="issue-comment:1", payload=parent_terminal_payload)

    class WrongEvidence(Evidence):
        def list_records(self, heading):
            if heading == RECOVERY_TERMINAL_HEADING:
                return [wrong_terminal]
            return super().list_records(heading)

    expect_error(lambda: prepare_recovery._validated_parent_recovery(WrongEvidence()))


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

    broken_source = dict(terminal)
    broken_source["release_source_sha"] = "f" * 40
    assert prepare_recovery._terminal_matches_release_identity(broken_source, identity) is False


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


def test_inactive_runtime_hash_mismatch_fails_exact_classification() -> None:
    expected = "1" * 64
    old_files = recovery_runner._files
    old_run = recovery_runner._run_root_script
    try:
        recovery_runner._files = lambda release_root, required_paths: (("service.sh", Path("/tmp/service.sh"), expected),)
        recovery_runner._run_root_script = lambda serial, script, timeout: _fake_root_result(
            ("target=present\ncurrent=/data/adb/mobile-proxy-node/releases/old\nh0=" + "2" * 64 + "\n").encode()
        )
        observed = recovery_runner._root_runtime_observation(
            serial="registered", release_root=Path("/tmp/release"), release_id=RECOVERY_RELEASE,
            required_paths=("service.sh",),
        )
    finally:
        recovery_runner._files = old_files
        recovery_runner._run_root_script = old_run
    assert observed["inactive_exact_files_verified"] is False
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
    for forbidden in ("mkdir ", "cp ", "rm ", "mv ", "ln ", "kill ", "chmod ", "service.sh\n"):
        assert forbidden not in text


if __name__ == "__main__":
    tests = [name for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for name in tests:
        globals()[name]()
    print(f"QUARANTINE_RECOVERY_TESTS_OK count={len(tests)}")
