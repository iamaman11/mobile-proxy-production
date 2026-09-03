#!/usr/bin/env python3
"""Hosted safety tests for the filesystem scratch Kernel seam and recovery."""

from __future__ import annotations

from dataclasses import dataclass
import json
import pathlib
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import filesystem_scratch_edges as edges
import filesystem_scratch_transaction_ports as scratch_ports


@dataclass(frozen=True)
class FakeTerminalRecord:
    operation_id: str
    target: str
    transaction_id: str
    affected_domain_generations: dict[str, str]
    evidence: tuple[object, ...]
    derived: dict[str, object]
    lifecycle_state: str
    control_request_id: str
    authority_cursor: str
    desired_generation: str


class FakeTransaction:
    class DispatchOutcomeUnknown(RuntimeError):
        pass

    class DispatchReceipt:
        def __init__(self, source_ref: str) -> None:
            self.source_ref = source_ref

    class PostconditionProof:
        def __init__(self, passed: bool, source_ref: str) -> None:
            self.passed = passed
            self.source_ref = source_ref

    TerminalRecord = FakeTerminalRecord


@dataclass(frozen=True)
class FakePhaseEvidence:
    step_id: str
    status: str
    transaction_id: str
    source_ref: str
    authority: str = "CONTROL"
    lifecycle: str = "CURRENT"


class FakeOperation:
    PASSED = "PASSED"
    DISPATCHED = "DISPATCHED"
    PhaseEvidence = FakePhaseEvidence


class MemoryComments:
    def __init__(self) -> None:
        self.comments: list[dict[str, object]] = []
        self.next_id = 1001

    def list_comments(self):
        return list(self.comments)

    def create_comment(self, body: str) -> int:
        comment_id = self.next_id
        self.next_id += 1
        self.comments.append(
            {
                "id": comment_id,
                "body": body,
                "user": {"login": "github-actions[bot]"},
            }
        )
        return comment_id


def request():
    return SimpleNamespace(
        scratch_ref="/data/local/tmp/mobile-proxy-kernel-" + "a" * 32,
        payload_ref="payload/gen-sha256:" + "b" * 64,
    )


def completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def make_ports(comments: MemoryComments):
    control_request_id = "req-sha256:" + "c" * 64
    desired_generation = "gen-sha256:" + "d" * 64
    transaction_id = (
        "physical-tx-v1:"
        + "c" * 64
        + ":android.filesystem-scratch-roundtrip.v1:"
        + "d" * 64
    )
    scratch_ref = "/data/local/tmp/mobile-proxy-kernel-" + "e" * 32
    context = scratch_ports.ScratchTransactionContext(
        canonical_sha="a" * 40,
        canonical_quality_run_id=1,
        private_sha="b" * 40,
        request_ref="https://github.com/iamaman11/mobile-proxy-production/issues/1#issuecomment-1",
        control_request_id=control_request_id,
        authority_cursor="issue179-comment-5531491187",
        desired_generation=desired_generation,
        transaction_id=transaction_id,
        scratch_ref=scratch_ref,
        payload_ref="payload/" + desired_generation,
        serial="registered-device",
        target_binding_key="k" * 32,
        workflow_run_id=1,
        workflow_run_attempt=1,
        recovery_kernel_sha="f" * 40,
        recovery_kernel_quality_run_id=2,
    )
    ports = scratch_ports.PrivateScratchTransactionPorts(
        transaction_module=FakeTransaction,
        operation_module=FakeOperation,
        control_module=SimpleNamespace(),
        preflight_module=SimpleNamespace(),
        comments=comments,
        context=context,
    )
    return ports, context


def test_dispatch_success_and_single_transport_edge() -> None:
    calls = []

    def fake(serial: str, script: str, *, timeout: int):
        calls.append((serial, script, timeout))
        return completed(0, "SCRATCH_ROUNDTRIP_V1:OK")

    with patch.object(edges, "_adb_shell", side_effect=fake):
        receipt = edges.AdbScratchRoundtripEdge("device", FakeTransaction).scratch_roundtrip_once(request())
    require(receipt.source_ref == "adb-dispatch:filesystem-scratch-roundtrip", "dispatch receipt differs")
    require(len(calls) == 1, "atomic dispatch must cross the controller->phone transport exactly once")
    script = calls[0][1]
    for required in ("mkdir", "printf '%s'", "cp", "actual=$(cat", "rmdir", "SCRATCH_ROUNDTRIP_V1:"):
        require(required in script, f"dispatch script missing bounded roundtrip primitive: {required}")
    require("/data/local/tmp/mobile-proxy-kernel-" in script, "dispatch escaped bounded scratch namespace")


def test_bounded_dispatch_failure_never_persists_raw_output() -> None:
    secret = "RAW_DEVICE_OR_SECRET_SHOULD_NOT_ESCAPE"
    with patch.object(
        edges,
        "_adb_shell",
        return_value=completed(23, "SCRATCH_ROUNDTRIP_V1:COPY_FAILED", secret),
    ):
        try:
            edges.AdbScratchRoundtripEdge("device", FakeTransaction).scratch_roundtrip_once(request())
        except FakeTransaction.DispatchOutcomeUnknown as error:
            message = str(error)
            require(message == "scratch dispatch bounded_stage=COPY_FAILED", "bounded stage differs")
            require(secret not in message, "raw stderr escaped into dispatch error")
        else:
            raise AssertionError("bounded dispatch failure was not UNKNOWN")


def test_unrecognized_dispatch_output_is_generic_unknown() -> None:
    secret = "unexpected raw output"
    with patch.object(edges, "_adb_shell", return_value=completed(1, secret, "raw stderr")):
        try:
            edges.AdbScratchRoundtripEdge("device", FakeTransaction).scratch_roundtrip_once(request())
        except FakeTransaction.DispatchOutcomeUnknown as error:
            require(secret not in str(error), "unrecognized raw stdout escaped into dispatch error")
        else:
            raise AssertionError("nonzero physical dispatch was not classified UNKNOWN")


def test_dispatch_timeout_is_unknown() -> None:
    with patch.object(edges, "_adb_shell", side_effect=subprocess.TimeoutExpired("adb", 30)):
        try:
            edges.AdbScratchRoundtripEdge("device", FakeTransaction).scratch_roundtrip_once(request())
        except FakeTransaction.DispatchOutcomeUnknown:
            pass
        else:
            raise AssertionError("dispatch timeout was not classified UNKNOWN")


def test_observer_is_independent_and_read_only() -> None:
    calls = []

    def fake(serial: str, script: str, *, timeout: int):
        calls.append(script)
        return completed(0, "ABSENT")

    with patch.object(edges, "_adb_shell", side_effect=fake):
        proof = edges.AdbScratchAbsenceObserver("device", FakeTransaction).observe_scratch_roundtrip(request())
    require(proof.passed is True, "absent namespace must satisfy postcondition")
    require(len(calls) == 1, "postcondition observer must be one independent transport edge")
    for forbidden in ("mkdir", "rm -", "rmdir", "cp ", ">"):
        require(forbidden not in calls[0], f"postcondition observer contains mutation primitive: {forbidden}")


def test_present_namespace_fails_postcondition_without_mutation() -> None:
    with patch.object(edges, "_adb_shell", return_value=completed(0, "PRESENT")):
        proof = edges.AdbScratchAbsenceObserver("device", FakeTransaction).observe_scratch_roundtrip(request())
    require(proof.passed is False, "present scratch namespace must fail postcondition")


def test_recovery_dispatch_binding_has_no_adb_surface() -> None:
    calls = []
    with patch.object(edges, "_adb_shell", side_effect=lambda *a, **k: calls.append((a, k))):
        try:
            edges.RecoveryDispatchForbiddenEdge(FakeTransaction).scratch_roundtrip_once(request())
        except edges.ScratchEdgeFailure:
            pass
        else:
            raise AssertionError("recovery dispatch edge did not fail closed")
    require(calls == [], "recovery dispatch edge reached ADB")


def test_durable_intent_reconstructs_may_have_dispatched_state() -> None:
    comments = MemoryComments()
    ports, context = make_ports(comments)
    intent = SimpleNamespace(
        operation_id=scratch_ports.OPERATION_ID,
        target=scratch_ports.TARGET,
        transaction_id=context.transaction_id,
        dispatch_step_id="scratch_roundtrip",
        mutation_subject_ref=context.scratch_ref,
        affected_domain_generations={scratch_ports.FILESYSTEM_DOMAIN: context.transaction_id},
        control_request_id=context.control_request_id,
        authority_cursor=context.authority_cursor,
        desired_generation=context.desired_generation,
        preflight_observation_refs=("issue-comment:999",),
    )

    intent_ref = ports.persist_mutation_intent(intent)
    evidence = ports.load_existing_evidence()

    require(intent_ref == "issue-comment:1001", "durable intent reference differs")
    require(len(comments.comments) == 1, "intent persistence must create exactly one durable CONTROL record")
    require(
        [(item.step_id, item.status) for item in evidence]
        == [
            ("resolve_authority", "PASSED"),
            ("mutation_scope", "PASSED"),
            ("phone_access_boundary", "PASSED"),
            ("mutation_intent", "PASSED"),
            ("scratch_roundtrip", "DISPATCHED"),
        ],
        "intent-only recovery must conservatively reconstruct may-have-dispatched evidence",
    )
    require(all(item.source_ref == intent_ref for item in evidence), "reconstructed evidence ref differs")


def test_recovery_terminal_is_separate_bounded_and_nonduplicable() -> None:
    comments = MemoryComments()
    ports, context = make_ports(comments)
    evidence = (
        FakePhaseEvidence("resolve_authority", "PASSED", context.transaction_id, "a"),
        FakePhaseEvidence("mutation_scope", "PASSED", context.transaction_id, "b"),
        FakePhaseEvidence("phone_access_boundary", "PASSED", context.transaction_id, "c"),
        FakePhaseEvidence("mutation_intent", "PASSED", context.transaction_id, "d"),
        FakePhaseEvidence("scratch_roundtrip", "DISPATCHED", context.transaction_id, "d"),
    )
    primary = FakeTerminalRecord(
        operation_id=scratch_ports.OPERATION_ID,
        target=scratch_ports.TARGET,
        transaction_id=context.transaction_id,
        affected_domain_generations={scratch_ports.FILESYSTEM_DOMAIN: context.transaction_id},
        evidence=evidence,
        derived={"state": "UNKNOWN_EXECUTION_OUTCOME", "next_step": "recovery_observe"},
        lifecycle_state="UNKNOWN",
        control_request_id=context.control_request_id,
        authority_cursor=context.authority_cursor,
        desired_generation=context.desired_generation,
    )
    primary_ref = ports.persist_terminal(primary)
    loaded = ports.load_terminal_by_ref(primary_ref)
    require(loaded.transaction_id == context.transaction_id, "loaded terminal transaction differs")
    recovered_evidence = evidence + (
        FakePhaseEvidence(
            "recovery_observe",
            "PASSED",
            context.transaction_id,
            "adb-observation:filesystem-scratch-absent",
        ),
    )
    recovered = FakeTerminalRecord(
        operation_id=scratch_ports.OPERATION_ID,
        target=scratch_ports.TARGET,
        transaction_id=context.transaction_id,
        affected_domain_generations={scratch_ports.FILESYSTEM_DOMAIN: context.transaction_id},
        evidence=recovered_evidence,
        derived={"state": "RECOVERED"},
        lifecycle_state="QUARANTINED",
        control_request_id=context.control_request_id,
        authority_cursor=context.authority_cursor,
        desired_generation=context.desired_generation,
    )
    recovery_ref = ports.persist_recovery_terminal(
        recovered,
        prior_terminal_ref=primary_ref,
        recovery_error=None,
    )
    require(recovery_ref == "issue-comment:1002", "recovery terminal ref differs")
    payload = scratch_ports._parse(comments.comments[-1], scratch_ports.RECOVERY_HEADING)
    require(payload is not None, "recovery terminal payload missing")
    require(payload["prior_terminal_ref"] == primary_ref, "recovery causal ref differs")
    require(payload["blind_retry_allowed"] is False, "recovery permits retry")
    require(payload["phone_mutation_performed"] is False, "recovery claims phone mutation")
    require(payload["cleanup_performed"] is False, "recovery claims cleanup")
    require(payload["original_transaction_accepted"] is False, "recovery accepted original")
    require(payload["raw_device_identifier_recorded"] is False, "recovery records raw device id")
    require(payload["recovery_observation_ref"] == "adb-observation:filesystem-scratch-absent", "observation ref differs")
    try:
        ports.persist_recovery_terminal(recovered, prior_terminal_ref=primary_ref, recovery_error=None)
    except scratch_ports.ScratchTransactionIntegrationFailure:
        pass
    else:
        raise AssertionError("duplicate recovery terminal for one prior terminal was accepted")


def test_recovery_terminal_rejects_acceptance_and_unbounded_observation() -> None:
    comments = MemoryComments()
    ports, context = make_ports(comments)
    bad = FakeTerminalRecord(
        operation_id=scratch_ports.OPERATION_ID,
        target=scratch_ports.TARGET,
        transaction_id=context.transaction_id,
        affected_domain_generations={scratch_ports.FILESYSTEM_DOMAIN: context.transaction_id},
        evidence=(FakePhaseEvidence("recovery_observe", "PASSED", context.transaction_id, "raw-adb-output"),),
        derived={"state": "ACCEPTED"},
        lifecycle_state="ACCEPTED",
        control_request_id=context.control_request_id,
        authority_cursor=context.authority_cursor,
        desired_generation=context.desired_generation,
    )
    try:
        ports.persist_recovery_terminal(bad, prior_terminal_ref="issue-comment:9", recovery_error=None)
    except scratch_ports.ScratchTransactionIntegrationFailure:
        pass
    else:
        raise AssertionError("recovery accepted original transaction")


def test_static_kernel_only_path() -> None:
    entry = (HERE / "run_filesystem_scratch_transaction.py").read_text(encoding="utf-8")
    recovery = (HERE / "run_filesystem_scratch_recovery.py").read_text(encoding="utf-8")
    ports = (HERE / "filesystem_scratch_transaction_ports.py").read_text(encoding="utf-8")
    edge = (HERE / "filesystem_scratch_edges.py").read_text(encoding="utf-8")
    require("TransactionRunner().run" not in entry or "runner.run(" in entry, "entrypoint does not invoke Universal TransactionRunner")
    require("runner.run(" in entry, "entrypoint does not invoke Universal TransactionRunner")
    require("recover_observe" in entry, "same-run UNKNOWN recovery is not automatic")
    require("RecoveryDispatchForbiddenEdge" in entry, "same-run recovery lacks dispatch-fail-closed binding")
    require("FilesystemScratchRoundtripBinding" in entry, "entrypoint does not use canonical scratch binding")
    require("run_android_filesystem_certification.py" not in entry, "legacy composite certification entered Kernel path")
    require("recover_observe" in recovery, "resume entrypoint does not use canonical recovery path")
    require("RecoveryDispatchForbiddenEdge" in recovery, "resume recovery lacks dispatch-fail-closed binding")
    require(".run(" not in recovery, "resume recovery contains primary transaction run surface")
    require("production-phone-global-mutation" in ports, "private mutation ports lost global scope")
    require('"blind_retry_allowed": False' in ports, "private evidence does not forbid blind retry")
    require('"phone_mutation_performed": False' in ports, "recovery evidence does not prove read-only")
    require('"cleanup_performed": False' in ports, "recovery evidence does not prove no cleanup")
    require(edge.count("subprocess.run(") == 1, "ADB transport helper must remain centralized in one subprocess call site")
    require("result.stderr" not in edge, "raw stderr is consumed by durable dispatch classifier")


def main() -> int:
    test_dispatch_success_and_single_transport_edge()
    test_bounded_dispatch_failure_never_persists_raw_output()
    test_unrecognized_dispatch_output_is_generic_unknown()
    test_dispatch_timeout_is_unknown()
    test_observer_is_independent_and_read_only()
    test_present_namespace_fails_postcondition_without_mutation()
    test_recovery_dispatch_binding_has_no_adb_surface()
    test_durable_intent_reconstructs_may_have_dispatched_state()
    test_recovery_terminal_is_separate_bounded_and_nonduplicable()
    test_recovery_terminal_rejects_acceptance_and_unbounded_observation()
    test_static_kernel_only_path()
    print("FILESYSTEM_SCRATCH_TRANSACTION_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
