#!/usr/bin/env python3
"""Private GitHub-backed TransactionPorts for canonical APK transactions.

The canonical transaction reducer/kernel, phone observer, APK binding and APK
executor remain authoritative. This module owns only private execution inputs and
synchronous bounded CONTROL evidence on private Issue #1.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import copy
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
from typing import Any, Iterator, Protocol
import urllib.error
import urllib.request

PRIVATE_REPOSITORY = "iamaman11/mobile-proxy-production"
CANONICAL_REPOSITORY = "iamaman11/mobile-proxy"
CONTROL_ISSUE = 1
TRUSTED_BOT = "github-actions[bot]"
APK_OPERATION = "android.apk-install.v1"
TARGET = "android-production"
TARGET_SCOPE = "target/android-production"
OBSERVER_SCOPE = "observer/phone-access"
OBSERVER_ID = "android.phone-access-observer.v2"
SESSION_SCOPE = "session/android-production"
PACKAGE_DOMAIN = "domain/package"
INTENT_HEADING = "## CONTROL APK MUTATION INTENT"
TERMINAL_HEADING = "## CONTROL APK TRANSACTION RESULT"
BOUNDARY_RAW_HEADING = "## CONTROL APK SAME-TRANSACTION PHONE ACCESS RAW"
BOUNDARY_FACT_HEADING = "## CONTROL APK SAME-TRANSACTION PHONE ACCESS FACT"

_SHA = re.compile(r"^[0-9a-f]{40}$")
_TX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_ARTIFACT = re.compile(r"^b3:[0-9a-f]{64}$")
_TARGET_BINDING = re.compile(r"^tb-hmac-sha256:[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,191}$")


class ApkTransactionIntegrationFailure(RuntimeError):
    pass


class IssueCommentClient(Protocol):
    def list_comments(self) -> list[Mapping[str, Any]]: ...
    def create_comment(self, body: str) -> int: ...


@dataclass(frozen=True)
class RestIssueCommentClient:
    token: str
    repository: str = PRIVATE_REPOSITORY
    issue_number: int = CONTROL_ISSUE

    def _open(self, url: str, *, method: str = "GET", payload: bytes | None = None):
        if not self.token:
            raise ApkTransactionIntegrationFailure("GitHub token is unavailable")
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "mobile-proxy-production-apk-transaction",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            return urllib.request.urlopen(request, timeout=20)
        except (urllib.error.URLError, TimeoutError) as error:
            raise ApkTransactionIntegrationFailure("GitHub CONTROL evidence transport failed") from error

    def list_comments(self) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        for page in range(1, 101):
            url = (
                f"https://api.github.com/repos/{self.repository}/issues/{self.issue_number}"
                f"/comments?per_page=100&page={page}"
            )
            with self._open(url) as response:
                payload = json.load(response)
            if not isinstance(payload, list):
                raise ApkTransactionIntegrationFailure("CONTROL comment inventory is invalid")
            result.extend(item for item in payload if isinstance(item, Mapping))
            if len(payload) < 100:
                return result
        raise ApkTransactionIntegrationFailure("CONTROL comment inventory is unexpectedly large")

    def create_comment(self, body: str) -> int:
        url = f"https://api.github.com/repos/{self.repository}/issues/{self.issue_number}/comments"
        payload = json.dumps({"body": body}).encode("utf-8")
        with self._open(url, method="POST", payload=payload) as response:
            result = json.load(response)
        comment_id = result.get("id") if isinstance(result, Mapping) else None
        if not isinstance(comment_id, int) or comment_id <= 0:
            raise ApkTransactionIntegrationFailure("durable CONTROL comment was not created")
        return comment_id


@dataclass(frozen=True)
class ApkTransactionContext:
    canonical_sha: str
    canonical_quality_run_id: int
    private_sha: str
    request_ref: str
    transaction_id: str
    admitted_artifact_ref: str
    serial: str
    target_binding_key: str
    workflow_run_id: int
    workflow_run_attempt: int
    mutation_scope_ref: str = "github-actions:production-phone-global-mutation"


def _text(value: str, label: str, pattern: re.Pattern[str] | None = None) -> str:
    value = value.strip()
    if not value or (pattern is not None and pattern.fullmatch(value) is None):
        raise ApkTransactionIntegrationFailure(f"{label} is invalid")
    return value


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ApkTransactionIntegrationFailure(f"{label} must be positive")
    return value


def derive_target_binding_id(serial: str, key: str) -> str:
    serial = serial.strip()
    if not serial or len(serial) > 128 or any(ch.isspace() for ch in serial):
        raise ApkTransactionIntegrationFailure("registered production target binding is invalid")
    if len(key) < 32 or key == serial:
        raise ApkTransactionIntegrationFailure("independent target binding HMAC key is unavailable")
    message = b"android-production\0" + serial.encode("utf-8")
    return "tb-hmac-sha256:" + hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _body(heading: str, payload: Mapping[str, Any]) -> str:
    return "\n".join((heading, "", "```json", json.dumps(payload, sort_keys=True, separators=(",", ":")), "```"))


def _trusted(comment: Mapping[str, Any]) -> bool:
    user = comment.get("user")
    return isinstance(user, Mapping) and user.get("login") == TRUSTED_BOT


def _parse(comment: Mapping[str, Any], heading: str) -> dict[str, Any] | None:
    body = comment.get("body")
    if not isinstance(body, str) or not body.startswith(heading) or not _trusted(comment):
        return None
    prefix = heading + "\n\n```json\n"
    suffix = "\n```"
    normalized = body[:-1] if body.endswith("\n") else body
    if not normalized.startswith(prefix) or not normalized.endswith(suffix):
        raise ApkTransactionIntegrationFailure(f"trusted {heading} comment is malformed")
    raw = normalized[len(prefix) : -len(suffix)]
    if "\n" in raw:
        raise ApkTransactionIntegrationFailure(f"trusted {heading} JSON is not bounded")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ApkTransactionIntegrationFailure(f"trusted {heading} JSON is invalid") from error
    if not isinstance(payload, dict):
        raise ApkTransactionIntegrationFailure(f"trusted {heading} payload is invalid")
    return payload


def _phase_dict(item: Any) -> dict[str, Any]:
    return {
        "step_id": item.step_id,
        "status": item.status,
        "transaction_id": item.transaction_id,
        "source_ref": item.source_ref,
        "authority": item.authority,
        "lifecycle": item.lifecycle,
    }


class PrivateApkTransactionPorts:
    def __init__(self, *, transaction_module: Any, operation_module: Any, control_module: Any, preflight_module: Any, comments: IssueCommentClient, context: ApkTransactionContext) -> None:
        self.transaction = transaction_module
        self.operation = operation_module
        self.control = control_module
        self.preflight = preflight_module
        self.comments = comments
        self.context = context
        self.target_binding_id = derive_target_binding_id(context.serial, context.target_binding_key)
        self._validate_context()

    def _validate_context(self) -> None:
        c = self.context
        _text(c.canonical_sha, "canonical SHA", _SHA)
        _positive(c.canonical_quality_run_id, "Quality run ID")
        _text(c.private_sha, "private SHA", _SHA)
        _text(c.request_ref, "request reference", _REF)
        _text(c.transaction_id, "transaction ID", _TX)
        _text(c.admitted_artifact_ref, "artifact identity", _ARTIFACT)
        _positive(c.workflow_run_id, "workflow run ID")
        _positive(c.workflow_run_attempt, "workflow run attempt")
        _text(self.target_binding_id, "target binding", _TARGET_BINDING)
        if c.mutation_scope_ref != "github-actions:production-phone-global-mutation":
            raise ApkTransactionIntegrationFailure("global production-phone mutation scope is not represented")

    def _persist(self, heading: str, payload: Mapping[str, Any]) -> str:
        return f"issue-comment:{self.comments.create_comment(_body(heading, payload))}"

    def resolve_authority(self, request: object, contract: Any):
        if contract.operation_id != APK_OPERATION or contract.target != TARGET:
            raise ApkTransactionIntegrationFailure("APK operation contract differs")
        tx = _text(getattr(request, "transaction_id", ""), "request transaction", _TX)
        artifact = _text(getattr(request, "artifact_ref", ""), "request artifact", _ARTIFACT)
        authorized = tx == self.context.transaction_id and artifact == self.context.admitted_artifact_ref
        source_ref = f"authority:{self.context.canonical_sha}:quality:{self.context.canonical_quality_run_id}:request:{self.context.request_ref}"
        return self.transaction.AuthorityProof(authorized, source_ref)

    @contextmanager
    def acquire_mutation_scope(self, target: str, transaction_id: str) -> Iterator[str]:
        if target != TARGET or transaction_id != self.context.transaction_id:
            raise ApkTransactionIntegrationFailure("APK mutation scope identity differs")
        yield self.context.mutation_scope_ref

    def prove_same_transaction_boundary(self, contract: Any, transaction_id: str):
        if contract.operation_id != APK_OPERATION or transaction_id != self.context.transaction_id:
            raise ApkTransactionIntegrationFailure("APK boundary transaction differs")
        session_id = f"apk-tx-session:{self.context.workflow_run_id}:{self.context.workflow_run_attempt}:{transaction_id}"
        observation_ref = f"apk-boundary:{self.context.workflow_run_id}:{self.context.workflow_run_attempt}:{transaction_id}"
        report = self.preflight.build_report(
            self.context.canonical_sha,
            target_binding_id=self.target_binding_id,
            session_id=session_id,
            observation_ref=observation_ref,
            transaction_id=transaction_id,
        )
        if not isinstance(report, Mapping) or report.get("accepted") is not True or report.get("mode") != "read_only" or report.get("mutation_performed") is not False or report.get("raw_device_identifier_recorded") is not False:
            raise ApkTransactionIntegrationFailure("transaction-bound phone probe is not admissible")
        facts = report.get("observed_facts")
        if not isinstance(facts, list) or len(facts) != 1 or not isinstance(facts[0], Mapping):
            raise ApkTransactionIntegrationFailure("transaction-bound phone fact set differs")
        raw_fact = dict(facts[0])
        expected_dependencies = [
            {"scope": TARGET_SCOPE, "identity": self.target_binding_id},
            {"scope": OBSERVER_SCOPE, "identity": OBSERVER_ID},
            {"scope": SESSION_SCOPE, "identity": session_id},
            {"scope": f"transaction/{transaction_id}", "identity": transaction_id},
        ]
        expected_fact = {
            "subject": "phone",
            "predicate": "registered_phone_access_proven",
            "value": True,
            "target": TARGET,
            "observation_ref": observation_ref,
            "source_ref": self.context.canonical_sha,
            "dependencies": expected_dependencies,
            "authority": "CONTROL",
            "persisted": False,
        }
        if raw_fact != expected_fact:
            raise ApkTransactionIntegrationFailure("canonical SAME_TRANSACTION fact differs")
        raw_record = self._base_record("APK_SAME_TRANSACTION_PHONE_ACCESS_RAW") | {
            "fact": raw_fact,
            "phone_mutation_performed": False,
        }
        raw_ref = self._persist(BOUNDARY_RAW_HEADING, raw_record)
        promoted = copy.deepcopy(raw_fact)
        promoted["persisted"] = True
        reverted = copy.deepcopy(promoted)
        reverted["persisted"] = False
        if reverted != raw_fact:
            raise ApkTransactionIntegrationFailure("fact promotion changed physical truth")
        promoted_record = self._base_record("PROMOTED_OBSERVED_FACT") | {
            "raw_evidence_ref": raw_ref,
            "fact": promoted,
            "promotion_changes_only_persistence": True,
            "phone_mutation_performed": False,
        }
        promoted_ref = self._persist(BOUNDARY_FACT_HEADING, promoted_record)
        dependencies = tuple(self.control.FactDependency(item["scope"], item["identity"]) for item in promoted["dependencies"])
        fact = self.control.ObservedFact(
            subject=promoted["subject"], predicate=promoted["predicate"], value=True,
            target=TARGET, observation_ref=promoted_ref, source_ref=promoted["source_ref"],
            dependencies=dependencies, authority="CONTROL", persisted=True,
        )
        current_context = {
            TARGET_SCOPE: self.target_binding_id,
            OBSERVER_SCOPE: OBSERVER_ID,
            SESSION_SCOPE: session_id,
            f"transaction/{transaction_id}": transaction_id,
        }
        return self.transaction.BoundaryProof(fact=fact, current_context=current_context)

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
            "operation_id": APK_OPERATION,
            "operation_transaction_id": self.context.transaction_id,
            "target": TARGET,
            "target_binding_id": self.target_binding_id,
            "raw_device_identifier_recorded": False,
        }

    def persist_mutation_intent(self, intent: Any) -> str:
        expected_generation = {PACKAGE_DOMAIN: self.context.transaction_id}
        if intent.operation_id != APK_OPERATION or intent.target != TARGET or intent.transaction_id != self.context.transaction_id or intent.dispatch_step_id != "install_apk" or intent.mutation_subject_ref != self.context.admitted_artifact_ref or dict(intent.affected_domain_generations) != expected_generation:
            raise ApkTransactionIntegrationFailure("APK mutation intent differs from canonical contract")
        payload = self._base_record("MUTATION_DISPATCH_INTENT") | {
            "workflow_run_id": self.context.workflow_run_id,
            "workflow_run_attempt": self.context.workflow_run_attempt,
            "destructive_step_id": "install_apk",
            "mutation_subject_ref": self.context.admitted_artifact_ref,
            "affected_domain_generations": expected_generation,
            "dispatch_may_reach_target": True,
            "adapter_invocation_allowed_only_after_persistence": True,
            "blind_retry_allowed": False,
            "phone_mutation_performed": False,
        }
        return self._persist(INTENT_HEADING, payload)

    def persist_terminal(self, record: Any) -> str:
        if record.operation_id != APK_OPERATION or record.target != TARGET or record.transaction_id != self.context.transaction_id:
            raise ApkTransactionIntegrationFailure("APK terminal identity differs")
        generations = dict(record.affected_domain_generations)
        if generations not in ({}, {PACKAGE_DOMAIN: self.context.transaction_id}):
            raise ApkTransactionIntegrationFailure("APK terminal package generation differs")
        payload = self._base_record("APK_TRANSACTION_TERMINAL") | {
            "mutation_subject_ref": self.context.admitted_artifact_ref,
            "affected_domain_generations": generations,
            "evidence": [_phase_dict(item) for item in record.evidence],
            "derived": dict(record.derived),
            "blind_retry_allowed": False,
        }
        return self._persist(TERMINAL_HEADING, payload)

    def _validate_intent(self, payload: Mapping[str, Any]) -> None:
        expected_generation = {PACKAGE_DOMAIN: self.context.transaction_id}
        if payload.get("format_version") != 1 or payload.get("evidence_type") != "MUTATION_DISPATCH_INTENT" or payload.get("authority") != "CONTROL" or payload.get("lifecycle") != "CURRENT" or payload.get("private_repository") != PRIVATE_REPOSITORY or payload.get("canonical_repository") != CANONICAL_REPOSITORY or payload.get("request_ref") != self.context.request_ref or payload.get("operation_id") != APK_OPERATION or payload.get("operation_transaction_id") != self.context.transaction_id or payload.get("target") != TARGET or payload.get("target_binding_id") != self.target_binding_id or payload.get("mutation_subject_ref") != self.context.admitted_artifact_ref or payload.get("affected_domain_generations") != expected_generation or payload.get("dispatch_may_reach_target") is not True or payload.get("adapter_invocation_allowed_only_after_persistence") is not True or payload.get("blind_retry_allowed") is not False or payload.get("raw_device_identifier_recorded") is not False:
            raise ApkTransactionIntegrationFailure("trusted APK mutation intent differs")

    def _terminal_evidence(self, payload: Mapping[str, Any]) -> tuple[Any, ...]:
        if payload.get("format_version") != 1 or payload.get("evidence_type") != "APK_TRANSACTION_TERMINAL" or payload.get("authority") != "CONTROL" or payload.get("lifecycle") != "CURRENT" or payload.get("private_repository") != PRIVATE_REPOSITORY or payload.get("canonical_repository") != CANONICAL_REPOSITORY or payload.get("request_ref") != self.context.request_ref or payload.get("operation_id") != APK_OPERATION or payload.get("operation_transaction_id") != self.context.transaction_id or payload.get("target") != TARGET or payload.get("target_binding_id") != self.target_binding_id or payload.get("mutation_subject_ref") != self.context.admitted_artifact_ref or payload.get("blind_retry_allowed") is not False or payload.get("raw_device_identifier_recorded") is not False:
            raise ApkTransactionIntegrationFailure("trusted APK terminal differs")
        rows = payload.get("evidence")
        if not isinstance(rows, list) or not rows:
            raise ApkTransactionIntegrationFailure("trusted APK terminal evidence is missing")
        result = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("transaction_id") != self.context.transaction_id:
                raise ApkTransactionIntegrationFailure("trusted APK terminal phase differs")
            result.append(self.operation.PhaseEvidence(row["step_id"], row["status"], row["transaction_id"], row["source_ref"], row.get("authority", "CONTROL"), row.get("lifecycle", "CURRENT")))
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
            raise ApkTransactionIntegrationFailure("duplicate durable APK transaction evidence exists")
        if terminals:
            return self._terminal_evidence(terminals[0])
        if not intents:
            return ()
        comment_id, intent = intents[0]
        self._validate_intent(intent)
        ref = f"issue-comment:{comment_id}"
        tx = self.context.transaction_id
        # Durable may-have-reached intent is conservatively reconstructed as DISPATCHED.
        # This blocks rerun even if controller loss occurred just before adapter call.
        return (
            self.operation.PhaseEvidence("resolve_authority", self.operation.PASSED, tx, ref),
            self.operation.PhaseEvidence("mutation_scope", self.operation.PASSED, tx, ref),
            self.operation.PhaseEvidence("phone_access_boundary", self.operation.PASSED, tx, ref),
            self.operation.PhaseEvidence("mutation_intent", self.operation.PASSED, tx, ref),
            self.operation.PhaseEvidence("install_apk", self.operation.DISPATCHED, tx, ref),
        )
