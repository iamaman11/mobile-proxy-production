#!/usr/bin/env python3
"""Thin private TransactionPorts for the first canonical filesystem scratch transaction."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import copy
from dataclasses import dataclass
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
BOUNDARY_RAW_HEADING = "## CONTROL FILESYSTEM SCRATCH SAME-TRANSACTION PHONE ACCESS RAW"
BOUNDARY_FACT_HEADING = "## CONTROL FILESYSTEM SCRATCH SAME-TRANSACTION PHONE ACCESS FACT"


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
        if len(c.canonical_sha) != 40 or any(ch not in "0123456789abcdef" for ch in c.canonical_sha):
            raise ScratchTransactionIntegrationFailure("canonical SHA is invalid")
        if len(c.private_sha) != 40 or any(ch not in "0123456789abcdef" for ch in c.private_sha):
            raise ScratchTransactionIntegrationFailure("private SHA is invalid")
        if c.canonical_quality_run_id <= 0 or c.workflow_run_id <= 0 or c.workflow_run_attempt <= 0:
            raise ScratchTransactionIntegrationFailure("run identity is invalid")
        if not c.request_ref or any(ch.isspace() for ch in c.request_ref):
            raise ScratchTransactionIntegrationFailure("request reference is invalid")
        if not c.control_request_id.startswith("req-sha256:"):
            raise ScratchTransactionIntegrationFailure("control request identity is invalid")
        if not c.authority_cursor.startswith("issue179-comment-"):
            raise ScratchTransactionIntegrationFailure("authority cursor is invalid")
        if not c.desired_generation.startswith("gen-sha256:"):
            raise ScratchTransactionIntegrationFailure("desired generation is invalid")
        if not c.transaction_id.startswith("physical-tx-v1:"):
            raise ScratchTransactionIntegrationFailure("physical transaction identity is invalid")
        if not c.scratch_ref.startswith("/data/local/tmp/mobile-proxy-kernel-"):
            raise ScratchTransactionIntegrationFailure("scratch mutation scope is invalid")
        if not c.payload_ref.startswith("payload/gen-sha256:"):
            raise ScratchTransactionIntegrationFailure("scratch payload identity is invalid")
        if c.mutation_scope_ref != "github-actions:production-phone-global-mutation":
            raise ScratchTransactionIntegrationFailure("global production-phone mutation scope is not represented")

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

    def _terminal_evidence(self, payload: Mapping[str, Any]) -> tuple[Any, ...]:
        if (
            payload.get("operation_id") != OPERATION_ID
            or payload.get("operation_transaction_id") != self.context.transaction_id
            or payload.get("control_request_id") != self.context.control_request_id
            or payload.get("blind_retry_allowed") is not False
        ):
            raise ScratchTransactionIntegrationFailure("trusted scratch terminal differs")
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
