#!/usr/bin/env python3
"""Thin private TransactionPorts for the canonical filesystem scratch transaction."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import copy
from dataclasses import dataclass
import re
from typing import Any, Iterator

from apk_transaction_ports import (
    CANONICAL_REPOSITORY,
    OBSERVER_ID,
    OBSERVER_SCOPE,
    PRIVATE_REPOSITORY,
    SESSION_SCOPE,
    TARGET,
    TARGET_SCOPE,
    IssueCommentClient,
    _body,
    _parse,
    _phase_dict,
    derive_target_binding_id,
)

OPERATION_ID = "android.filesystem-scratch-roundtrip.v1"
SEMANTIC_OPERATION = "phone-filesystem-certification"
FILESYSTEM_DOMAIN = "domain/filesystem"
INTENT_HEADING = "## CONTROL FILESYSTEM SCRATCH MUTATION INTENT"
TERMINAL_HEADING = "## CONTROL FILESYSTEM SCRATCH TRANSACTION RESULT"
RECOVERY_HEADING = "## CONTROL FILESYSTEM SCRATCH RECOVERY RESULT"
BOUNDARY_RAW_HEADING = "## CONTROL FILESYSTEM SCRATCH SAME-TRANSACTION PHONE ACCESS RAW"
BOUNDARY_FACT_HEADING = "## CONTROL FILESYSTEM SCRATCH SAME-TRANSACTION PHONE ACCESS FACT"

_SHA = re.compile(r"^[0-9a-f]{40}$")
_REQUEST = re.compile(r"^req-sha256:[0-9a-f]{64}$")
_GENERATION = re.compile(r"^gen-sha256:[0-9a-f]{64}$")
_CURSOR = re.compile(r"^issue179-comment-[1-9][0-9]*$")
_TRANSACTION = re.compile(
    r"^physical-tx-v1:[0-9a-f]{64}:android\.filesystem-scratch-roundtrip\.v1:[0-9a-f]{64}$"
)
_SCRATCH = re.compile(r"^/data/local/tmp/mobile-proxy-kernel-[0-9a-f]{32}$")
_PAYLOAD = re.compile(r"^payload/gen-sha256:[0-9a-f]{64}$")
_ISSUE_REF = re.compile(r"^issue-comment:([1-9][0-9]*)$")
_RECOVERY_OBSERVATION_REFS = frozenset(
    {
        "adb-observation:filesystem-scratch-absent",
        "adb-observation:filesystem-scratch-present",
    }
)


class ScratchTransactionIntegrationFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class ScratchTransactionContext:
    canonical_sha: str
    canonical_quality_run_id: int
    private_sha: str
    request_ref: str
    control_request_id: str
    authority_cursor: str
    desired_generation: str
    transaction_id: str
    scratch_ref: str
    payload_ref: str
    serial: str
    target_binding_key: str
    workflow_run_id: int
    workflow_run_attempt: int
    mutation_scope_ref: str = "github-actions:production-phone-global-mutation"
    recovery_kernel_sha: str = ""
    recovery_kernel_quality_run_id: int = 0


class PrivateScratchTransactionPorts:
    def __init__(
        self,
        *,
        transaction_module: Any,
        operation_module: Any,
        control_module: Any,
        preflight_module: Any,
        comments: IssueCommentClient,
        context: ScratchTransactionContext,
    ) -> None:
        self.transaction = transaction_module
        self.operation = operation_module
        self.control = control_module
        self.preflight = preflight_module
        self.comments = comments
        self.context = context
        self.target_binding_id = derive_target_binding_id(
            context.serial,
            context.target_binding_key,
        )
        self._validate_context()

    def _validate_context(self) -> None:
        c = self.context
        if _SHA.fullmatch(c.canonical_sha) is None:
            raise ScratchTransactionIntegrationFailure("canonical SHA is invalid")
        if _SHA.fullmatch(c.private_sha) is None:
            raise ScratchTransactionIntegrationFailure("private SHA is invalid")
        if c.canonical_quality_run_id <= 0 or c.workflow_run_id <= 0 or c.workflow_run_attempt <= 0:
            raise ScratchTransactionIntegrationFailure("run identity is invalid")
        if not c.request_ref or any(ch.isspace() for ch in c.request_ref):
            raise ScratchTransactionIntegrationFailure("request reference is invalid")
        if _REQUEST.fullmatch(c.control_request_id) is None:
            raise ScratchTransactionIntegrationFailure("control request identity is invalid")
        if _CURSOR.fullmatch(c.authority_cursor) is None:
            raise ScratchTransactionIntegrationFailure("authority cursor is invalid")
        if _GENERATION.fullmatch(c.desired_generation) is None:
            raise ScratchTransactionIntegrationFailure("desired generation is invalid")
        if _TRANSACTION.fullmatch(c.transaction_id) is None:
            raise ScratchTransactionIntegrationFailure("physical transaction identity is invalid")
        if _SCRATCH.fullmatch(c.scratch_ref) is None:
            raise ScratchTransactionIntegrationFailure("scratch mutation scope is invalid")
        if _PAYLOAD.fullmatch(c.payload_ref) is None:
            raise ScratchTransactionIntegrationFailure("scratch payload identity is invalid")
        if c.mutation_scope_ref != "github-actions:production-phone-global-mutation":
            raise ScratchTransactionIntegrationFailure("global production-phone mutation scope is not represented")
        if c.recovery_kernel_sha:
            if _SHA.fullmatch(c.recovery_kernel_sha) is None or c.recovery_kernel_quality_run_id <= 0:
                raise ScratchTransactionIntegrationFailure("recovery Kernel authority is invalid")
        elif c.recovery_kernel_quality_run_id != 0:
            raise ScratchTransactionIntegrationFailure("recovery Kernel authority is incomplete")

    def _persist(self, heading: str, payload: Mapping[str, Any]) -> str:
        return f"issue-comment:{self.comments.create_comment(_body(heading, payload))}"

    def _base_record(self, evidence_type: str) -> dict[str, Any]:
        return {
            "format_version": 1,
            "evidence_type": evidence_type,
            "authority": "CONTROL",
            "lifecycle": "CURRENT",
            "private_repository": PRIVATE_REPOSITORY,
            "private_sha": self.context.private_sha,
            "canonical_repository": CANONICAL_REPOSITORY,
            "canonical_sha": self.context.canonical_sha,
            "canonical_quality_run_id": self.context.canonical_quality_run_id,
            "request_ref": self.context.request_ref,
            "control_request_id": self.context.control_request_id,
            "authority_cursor": self.context.authority_cursor,
            "desired_generation": self.context.desired_generation,
            "operation_id": OPERATION_ID,
            "operation_transaction_id": self.context.transaction_id,
            "target": TARGET,
            "target_binding_id": self.target_binding_id,
            "scratch_ref": self.context.scratch_ref,
            "payload_ref": self.context.payload_ref,
            "raw_device_identifier_recorded": False,
        }

    def resolve_authority(self, request: object, contract: Any):
        semantic = getattr(request, "semantic_request", None)
        authorized = (
            contract.operation_id == OPERATION_ID
            and contract.target == TARGET
            and getattr(request, "scratch_ref", None) == self.context.scratch_ref
            and getattr(request, "payload_ref", None) == self.context.payload_ref
            and getattr(semantic, "request_id", None) == self.context.control_request_id
            and getattr(semantic, "authority_cursor", None) == self.context.authority_cursor
            and getattr(semantic, "desired_generation", None) == self.context.desired_generation
            and getattr(semantic, "operation", None) == SEMANTIC_OPERATION
        )
        source_ref = (
            f"authority:{self.context.canonical_sha}:quality:{self.context.canonical_quality_run_id}:"
            f"request:{self.context.request_ref}"
        )
        return self.transaction.AuthorityProof(authorized, source_ref)

    @contextmanager
    def acquire_mutation_scope(self, target: str, transaction_id: str) -> Iterator[str]:
        if target != TARGET or transaction_id != self.context.transaction_id:
            raise ScratchTransactionIntegrationFailure("scratch mutation scope identity differs")
        yield self.context.mutation_scope_ref

    def prove_same_transaction_boundary(self, contract: Any, transaction_id: str):
        if contract.operation_id != OPERATION_ID or transaction_id != self.context.transaction_id:
            raise ScratchTransactionIntegrationFailure("scratch boundary transaction differs")
        session_id = (
            f"scratch-tx-session:{self.context.workflow_run_id}:"
            f"{self.context.workflow_run_attempt}:{transaction_id}"
        )
        observation_ref = (
            f"scratch-boundary:{self.context.workflow_run_id}:"
            f"{self.context.workflow_run_attempt}:{transaction_id}"
        )
        report = self.preflight.build_report(
            self.context.canonical_sha,
            target_binding_id=self.target_binding_id,
            session_id=session_id,
            observation_ref=observation_ref,
            transaction_id=transaction_id,
        )
        if (
            not isinstance(report, Mapping)
            or report.get("accepted") is not True
            or report.get("mode") != "read_only"
            or report.get("mutation_performed") is not False
            or report.get("raw_device_identifier_recorded") is not False
        ):
            raise ScratchTransactionIntegrationFailure("transaction-bound phone probe is not admissible")
        facts = report.get("observed_facts")
        if not isinstance(facts, list) or len(facts) != 1 or not isinstance(facts[0], Mapping):
            raise ScratchTransactionIntegrationFailure("transaction-bound phone fact set differs")
        raw_fact = dict(facts[0])
        raw_record = self._base_record("SCRATCH_SAME_TRANSACTION_PHONE_ACCESS_RAW") | {
            "fact": raw_fact,
            "phone_mutation_performed": False,
        }
        raw_ref = self._persist(BOUNDARY_RAW_HEADING, raw_record)
        promoted = copy.deepcopy(raw_fact)
        promoted["persisted"] = True
        reverted = copy.deepcopy(promoted)
        reverted["persisted"] = False
        if reverted != raw_fact:
            raise ScratchTransactionIntegrationFailure("fact promotion changed physical truth")
        promoted_ref = self._persist(
            BOUNDARY_FACT_HEADING,
            self._base_record("PROMOTED_OBSERVED_FACT")
            | {
                "raw_evidence_ref": raw_ref,
                "fact": promoted,
                "promotion_changes_only_persistence": True,
                "phone_mutation_performed": False,
            },
        )
        dependencies = tuple(
            self.control.FactDependency(item["scope"], item["identity"])
            for item in promoted["dependencies"]
        )
        fact = self.control.ObservedFact(
            subject=promoted["subject"],
            predicate=promoted["predicate"],
            value=True,
            target=TARGET,
            observation_ref=promoted_ref,
            source_ref=promoted["source_ref"],
            dependencies=dependencies,
            authority="CONTROL",
            persisted=True,
        )
        current_context = {
            TARGET_SCOPE: self.target_binding_id,
            OBSERVER_SCOPE: OBSERVER_ID,
            SESSION_SCOPE: session_id,
            f"transaction/{transaction_id}": transaction_id,
        }
        return self.transaction.BoundaryProof(fact=fact, current_context=current_context)

    def persist_mutation_intent(self, intent: Any) -> str:
        expected_generation = {FILESYSTEM_DOMAIN: self.context.transaction_id}
        if (
            intent.operation_id != OPERATION_ID
            or intent.target != TARGET
            or intent.transaction_id != self.context.transaction_id
            or intent.dispatch_step_id != "scratch_roundtrip"
            or intent.mutation_subject_ref != self.context.scratch_ref
            or dict(intent.affected_domain_generations) != expected_generation
            or intent.control_request_id != self.context.control_request_id
            or intent.authority_cursor != self.context.authority_cursor
            or intent.desired_generation != self.context.desired_generation
        ):
            raise ScratchTransactionIntegrationFailure("scratch mutation intent differs from canonical contract")
        payload = self._base_record("MUTATION_DISPATCH_INTENT") | {
            "workflow_run_id": self.context.workflow_run_id,
            "workflow_run_attempt": self.context.workflow_run_attempt,
            "destructive_step_id": "scratch_roundtrip",
            "mutation_subject_ref": self.context.scratch_ref,
            "affected_domain_generations": expected_generation,
            "preflight_observation_refs": list(intent.preflight_observation_refs),
            "dispatch_may_reach_target": True,
            "adapter_invocation_allowed_only_after_persistence": True,
            "blind_retry_allowed": False,
            "phone_mutation_performed": False,
        }
        return self._persist(INTENT_HEADING, payload)

    def persist_terminal(self, record: Any) -> str:
        if (
            record.operation_id != OPERATION_ID
            or record.target != TARGET
            or record.transaction_id != self.context.transaction_id
            or record.control_request_id != self.context.control_request_id
            or record.authority_cursor != self.context.authority_cursor
            or record.desired_generation != self.context.desired_generation
        ):
            raise ScratchTransactionIntegrationFailure("scratch terminal identity differs")
        generations = dict(record.affected_domain_generations)
        if generations not in ({}, {FILESYSTEM_DOMAIN: self.context.transaction_id}):
            raise ScratchTransactionIntegrationFailure("scratch terminal filesystem generation differs")
        payload = self._base_record("FILESYSTEM_SCRATCH_TRANSACTION_TERMINAL") | {
            "affected_domain_generations": generations,
            "evidence": [_phase_dict(item) for item in record.evidence],
            "derived": dict(record.derived),
            "lifecycle_state": record.lifecycle_state,
            "blind_retry_allowed": False,
        }
        return self._persist(TERMINAL_HEADING, payload)

    def _phase_rows(self, payload: Mapping[str, Any]) -> tuple[Any, ...]:
        rows = payload.get("evidence")
        if not isinstance(rows, list) or not rows:
            raise ScratchTransactionIntegrationFailure("trusted scratch terminal evidence is missing")
        result = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("transaction_id") != self.context.transaction_id:
                raise ScratchTransactionIntegrationFailure("trusted scratch terminal phase differs")
            result.append(
                self.operation.PhaseEvidence(
                    row["step_id"],
                    row["status"],
                    row["transaction_id"],
                    row["source_ref"],
                    row.get("authority", "CONTROL"),
                    row.get("lifecycle", "CURRENT"),
                )
            )
        return tuple(result)

    def _validate_terminal_payload(self, payload: Mapping[str, Any]) -> None:
        if (
            payload.get("operation_id") != OPERATION_ID
            or payload.get("operation_transaction_id") != self.context.transaction_id
            or payload.get("control_request_id") != self.context.control_request_id
            or payload.get("authority_cursor") != self.context.authority_cursor
            or payload.get("desired_generation") != self.context.desired_generation
            or payload.get("canonical_sha") != self.context.canonical_sha
            or payload.get("canonical_quality_run_id") != self.context.canonical_quality_run_id
            or payload.get("target") != TARGET
            or payload.get("target_binding_id") != self.target_binding_id
            or payload.get("scratch_ref") != self.context.scratch_ref
            or payload.get("payload_ref") != self.context.payload_ref
            or payload.get("blind_retry_allowed") is not False
            or payload.get("raw_device_identifier_recorded") is not False
        ):
            raise ScratchTransactionIntegrationFailure("trusted scratch terminal differs")

    def _terminal_record(self, payload: Mapping[str, Any]):
        self._validate_terminal_payload(payload)
        generations = payload.get("affected_domain_generations")
        if not isinstance(generations, Mapping):
            raise ScratchTransactionIntegrationFailure("trusted scratch terminal generations differ")
        evidence = self._phase_rows(payload)
        derived = payload.get("derived")
        lifecycle = payload.get("lifecycle_state")
        if not isinstance(derived, Mapping) or not isinstance(lifecycle, str):
            raise ScratchTransactionIntegrationFailure("trusted scratch terminal classification differs")
        return self.transaction.TerminalRecord(
            operation_id=OPERATION_ID,
            target=TARGET,
            transaction_id=self.context.transaction_id,
            affected_domain_generations=dict(generations),
            evidence=evidence,
            derived=dict(derived),
            lifecycle_state=lifecycle,
            control_request_id=self.context.control_request_id,
            authority_cursor=self.context.authority_cursor,
            desired_generation=self.context.desired_generation,
        )

    def load_terminal_by_ref(self, prior_terminal_ref: str):
        match = _ISSUE_REF.fullmatch(prior_terminal_ref)
        if match is None:
            raise ScratchTransactionIntegrationFailure("prior terminal reference is invalid")
        wanted = int(match.group(1))
        matches = []
        for comment in self.comments.list_comments():
            if comment.get("id") != wanted:
                continue
            payload = _parse(comment, TERMINAL_HEADING)
            if payload is None:
                payload = _parse(comment, RECOVERY_HEADING)
            if payload is not None:
                matches.append(payload)
        if len(matches) != 1:
            raise ScratchTransactionIntegrationFailure("exact prior scratch terminal is unavailable or ambiguous")
        return self._terminal_record(matches[0])

    def persist_recovery_terminal(
        self,
        record: Any,
        *,
        prior_terminal_ref: str,
        recovery_error: str | None,
    ) -> str:
        if _ISSUE_REF.fullmatch(prior_terminal_ref) is None:
            raise ScratchTransactionIntegrationFailure("recovery prior terminal reference is invalid")
        if (
            record.operation_id != OPERATION_ID
            or record.target != TARGET
            or record.transaction_id != self.context.transaction_id
            or record.control_request_id != self.context.control_request_id
            or record.authority_cursor != self.context.authority_cursor
            or record.desired_generation != self.context.desired_generation
        ):
            raise ScratchTransactionIntegrationFailure("scratch recovery terminal identity differs")
        if dict(record.affected_domain_generations) != {FILESYSTEM_DOMAIN: self.context.transaction_id}:
            raise ScratchTransactionIntegrationFailure("scratch recovery generation differs")
        state = record.derived.get("state")
        if state not in {"UNKNOWN_EXECUTION_OUTCOME", "RECOVERED", "QUARANTINED"}:
            raise ScratchTransactionIntegrationFailure("scratch recovery classification differs")
        if record.lifecycle_state == "ACCEPTED" or state == "ACCEPTED":
            raise ScratchTransactionIntegrationFailure("recovery cannot accept original scratch transaction")

        existing = []
        for comment in self.comments.list_comments():
            payload = _parse(comment, RECOVERY_HEADING)
            if (
                payload is not None
                and payload.get("operation_transaction_id") == self.context.transaction_id
                and payload.get("prior_terminal_ref") == prior_terminal_ref
            ):
                existing.append(payload)
        if existing:
            raise ScratchTransactionIntegrationFailure("recovery terminal already exists for this prior terminal")

        observation_ref = None
        if record.evidence:
            last = record.evidence[-1]
            if getattr(last, "step_id", None) == "recovery_observe":
                observation_ref = getattr(last, "source_ref", None)
                if observation_ref not in _RECOVERY_OBSERVATION_REFS:
                    raise ScratchTransactionIntegrationFailure("recovery observation source is not bounded")
        if recovery_error is None and state in {"RECOVERED", "QUARANTINED"} and observation_ref is None:
            raise ScratchTransactionIntegrationFailure("classified recovery lacks bounded observation")

        payload = self._base_record("FILESYSTEM_SCRATCH_RECOVERY_TERMINAL") | {
            "recovery_kernel_sha": self.context.recovery_kernel_sha or self.context.canonical_sha,
            "recovery_kernel_quality_run_id": (
                self.context.recovery_kernel_quality_run_id or self.context.canonical_quality_run_id
            ),
            "workflow_run_id": self.context.workflow_run_id,
            "workflow_run_attempt": self.context.workflow_run_attempt,
            "prior_terminal_ref": prior_terminal_ref,
            "affected_domain_generations": dict(record.affected_domain_generations),
            "evidence": [_phase_dict(item) for item in record.evidence],
            "derived": dict(record.derived),
            "lifecycle_state": record.lifecycle_state,
            "recovery_observation_ref": observation_ref,
            "recovery_error_class": None if recovery_error is None else "OBSERVATION_UNAVAILABLE",
            "blind_retry_allowed": False,
            "phone_mutation_performed": False,
            "cleanup_performed": False,
            "original_transaction_accepted": False,
        }
        return self._persist(RECOVERY_HEADING, payload)

    def _terminal_evidence(self, payload: Mapping[str, Any]) -> tuple[Any, ...]:
        self._validate_terminal_payload(payload)
        return self._phase_rows(payload)

    def load_existing_evidence(self) -> tuple[Any, ...]:
        intents: list[tuple[int, dict[str, Any]]] = []
        terminals: list[dict[str, Any]] = []
        for comment in self.comments.list_comments():
            comment_id = comment.get("id")
            if not isinstance(comment_id, int) or comment_id <= 0:
                continue
            intent = _parse(comment, INTENT_HEADING)
            if intent is not None and intent.get("operation_transaction_id") == self.context.transaction_id:
                intents.append((comment_id, intent))
            terminal = _parse(comment, TERMINAL_HEADING)
            if terminal is not None and terminal.get("operation_transaction_id") == self.context.transaction_id:
                terminals.append(terminal)
        if len(intents) > 1 or len(terminals) > 1:
            raise ScratchTransactionIntegrationFailure("duplicate durable scratch transaction evidence exists")
        if terminals:
            return self._terminal_evidence(terminals[0])
        if not intents:
            return ()
        comment_id, intent = intents[0]
        if (
            intent.get("operation_id") != OPERATION_ID
            or intent.get("control_request_id") != self.context.control_request_id
            or intent.get("destructive_step_id") != "scratch_roundtrip"
            or intent.get("blind_retry_allowed") is not False
        ):
            raise ScratchTransactionIntegrationFailure("trusted scratch mutation intent differs")
        ref = f"issue-comment:{comment_id}"
        tx = self.context.transaction_id
        return (
            self.operation.PhaseEvidence("resolve_authority", self.operation.PASSED, tx, ref),
            self.operation.PhaseEvidence("mutation_scope", self.operation.PASSED, tx, ref),
            self.operation.PhaseEvidence("phone_access_boundary", self.operation.PASSED, tx, ref),
            self.operation.PhaseEvidence("mutation_intent", self.operation.PASSED, tx, ref),
            self.operation.PhaseEvidence("scratch_roundtrip", self.operation.DISPATCHED, tx, ref),
        )


def context_from_prior_terminal(
    comments: IssueCommentClient,
    prior_terminal_ref: str,
    *,
    private_sha: str,
    serial: str,
    target_binding_key: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    recovery_kernel_sha: str,
    recovery_kernel_quality_run_id: int,
) -> ScratchTransactionContext:
    match = _ISSUE_REF.fullmatch(prior_terminal_ref)
    if match is None:
        raise ScratchTransactionIntegrationFailure("prior terminal reference is invalid")
    wanted = int(match.group(1))
    payloads = []
    for comment in comments.list_comments():
        if comment.get("id") != wanted:
            continue
        payload = _parse(comment, TERMINAL_HEADING)
        if payload is None:
            payload = _parse(comment, RECOVERY_HEADING)
        if payload is not None:
            payloads.append(payload)
    if len(payloads) != 1:
        raise ScratchTransactionIntegrationFailure("prior scratch terminal is unavailable or ambiguous")
    payload = payloads[0]
    required_strings = {
        "canonical_sha": _SHA,
        "control_request_id": _REQUEST,
        "authority_cursor": _CURSOR,
        "desired_generation": _GENERATION,
        "operation_transaction_id": _TRANSACTION,
        "scratch_ref": _SCRATCH,
        "payload_ref": _PAYLOAD,
    }
    for field, pattern in required_strings.items():
        if pattern.fullmatch(str(payload.get(field, ""))) is None:
            raise ScratchTransactionIntegrationFailure(f"prior terminal {field} is invalid")
    if payload.get("operation_id") != OPERATION_ID or payload.get("target") != TARGET:
        raise ScratchTransactionIntegrationFailure("prior terminal operation/target differs")
    if payload.get("blind_retry_allowed") is not False or payload.get("raw_device_identifier_recorded") is not False:
        raise ScratchTransactionIntegrationFailure("prior terminal safety flags differ")
    target_binding_id = derive_target_binding_id(serial, target_binding_key)
    if payload.get("target_binding_id") != target_binding_id:
        raise ScratchTransactionIntegrationFailure("prior terminal target binding differs")
    quality = payload.get("canonical_quality_run_id")
    request_ref = payload.get("request_ref")
    if not isinstance(quality, int) or quality <= 0:
        raise ScratchTransactionIntegrationFailure("prior terminal Quality authority differs")
    if not isinstance(request_ref, str) or not request_ref or any(ch.isspace() for ch in request_ref):
        raise ScratchTransactionIntegrationFailure("prior terminal request reference differs")
    return ScratchTransactionContext(
        canonical_sha=str(payload["canonical_sha"]),
        canonical_quality_run_id=quality,
        private_sha=private_sha,
        request_ref=request_ref,
        control_request_id=str(payload["control_request_id"]),
        authority_cursor=str(payload["authority_cursor"]),
        desired_generation=str(payload["desired_generation"]),
        transaction_id=str(payload["operation_transaction_id"]),
        scratch_ref=str(payload["scratch_ref"]),
        payload_ref=str(payload["payload_ref"]),
        serial=serial,
        target_binding_key=target_binding_key,
        workflow_run_id=workflow_run_id,
        workflow_run_attempt=workflow_run_attempt,
        recovery_kernel_sha=recovery_kernel_sha,
        recovery_kernel_quality_run_id=recovery_kernel_quality_run_id,
    )
