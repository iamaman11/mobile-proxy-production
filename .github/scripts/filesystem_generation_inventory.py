#!/usr/bin/env python3
"""Build canonical filesystem-generation inventory from durable private CONTROL comments.

This module is private execution/evidence orchestration only. It does not derive a
physical generation itself; the public canonical ``physical_domain_generation.py``
remains the sole resolver.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping


PRIVATE_REPOSITORY = "iamaman11/mobile-proxy-production"
CANONICAL_REPOSITORY = "iamaman11/mobile-proxy"
TRUSTED_BOT = "github-actions[bot]"

JOURNAL_HEADING = "## CONTROL FILESYSTEM MUTATION INTENT"
CERT_RESULT_HEADING = "## CONTROL ANDROID FILESYSTEM MUTATION RESULT"
CLEANUP_RESULT_HEADING = "## CONTROL ANDROID FILESYSTEM QUARANTINE CLEANUP RESULT"

CERT_OPERATION = "android.filesystem-certification.v1"
CLEANUP_OPERATION = "android.filesystem-quarantine-cleanup.v1"
DOMAIN_SCOPE = "domain/filesystem"
BOOTSTRAP_GENERATION = "filesystem-bootstrap-v1"

_SHA = re.compile(r"[0-9a-f]{40}")
_TRANSACTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_TARGET_BINDING = re.compile(r"tb-hmac-sha256:[0-9a-f]{64}")
_JOURNAL_BODY = re.compile(
    r"\A## CONTROL FILESYSTEM MUTATION INTENT\n\n```json\n(?P<payload>\{[^\n]*\})\n```\n?\Z"
)
_CLASSIFICATION = re.compile(r"^- classification: \*\*(?P<value>[A-Z0-9_]+)\*\*$")
_FIELD = re.compile(r"^- (?P<key>[A-Za-z0-9_./-]+): `(?P<value>[^`\n]*)`$")

_COMMON_JOURNAL_KEYS = {
    "format_version",
    "journal_type",
    "evidence_type",
    "authority",
    "lifecycle",
    "private_repository",
    "private_sha",
    "canonical_repository",
    "canonical_sha",
    "canonical_quality_run_id",
    "workflow_run_id",
    "workflow_run_attempt",
    "operation_id",
    "operation_transaction_id",
    "target",
    "target_binding_id",
    "affected_domain_generations",
    "dispatch_intent_artifact_persisted",
    "journaled_before_adapter_invocation",
    "dispatch_may_reach_target",
    "blind_retry_allowed",
    "raw_device_identifier_recorded",
}

_CERT_TERMINAL = {
    "FILESYSTEM_MUTATION_PROVEN": "ACCEPTED",
    "FILESYSTEM_MUTATION_RECOVERED": "RECOVERED",
    "FILESYSTEM_MUTATION_REFUSED": "REFUSED",
    "FILESYSTEM_MUTATION_QUARANTINED": "QUARANTINED",
}
_CERT_UNRESOLVED = {"UNKNOWN_EXECUTION_OUTCOME"}

_CLEANUP_TERMINAL = {
    "FILESYSTEM_QUARANTINE_CLEANUP_PROVEN": "CLEANED",
    "FILESYSTEM_QUARANTINE_ALREADY_CLEAN": "ALREADY_CLEAN",
    "FILESYSTEM_QUARANTINE_CLEANUP_REFUSED": "REFUSED",
    "FILESYSTEM_QUARANTINE_CLEANUP_QUARANTINED": "QUARANTINED",
}
_CLEANUP_UNRESOLVED = {
    "UNKNOWN_EXECUTION_OUTCOME",
    "FILESYSTEM_QUARANTINE_CLEANUP_OBSERVED_UNPERSISTED",
}


class EvidenceAdapterError(RuntimeError):
    pass


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise EvidenceAdapterError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise EvidenceAdapterError(f"{label} must be a positive integer") from error
    if parsed <= 0:
        raise EvidenceAdapterError(f"{label} must be a positive integer")
    return parsed


def _require_text(value: Any, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceAdapterError(f"{label} is required")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise EvidenceAdapterError(f"{label} is invalid")
    return value


def _expected_transaction(operation_id: str, run_id: int, run_attempt: int) -> str:
    if operation_id == CERT_OPERATION:
        return f"fs-{run_id}-{run_attempt}"
    if operation_id == CLEANUP_OPERATION:
        return f"fs-quarantine-clean-{run_id}-{run_attempt}"
    raise EvidenceAdapterError(f"unsupported filesystem mutation operation: {operation_id}")


def _trusted(comment: Mapping[str, Any]) -> bool:
    user = comment.get("user")
    return isinstance(user, Mapping) and user.get("login") == TRUSTED_BOT


def _comment_body(comment: Mapping[str, Any]) -> str:
    body = comment.get("body")
    return body if isinstance(body, str) else ""


def _parse_journal(comment: Mapping[str, Any]) -> dict[str, Any] | None:
    body = _comment_body(comment)
    if not body.startswith(JOURNAL_HEADING):
        return None
    if not _trusted(comment):
        return None

    match = _JOURNAL_BODY.fullmatch(body)
    if match is None:
        raise EvidenceAdapterError("trusted filesystem mutation journal body is malformed")
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError as error:
        raise EvidenceAdapterError("trusted filesystem mutation journal JSON is invalid") from error
    if not isinstance(payload, dict):
        raise EvidenceAdapterError("trusted filesystem mutation journal payload must be an object")

    operation_id = _require_text(payload.get("operation_id"), "journal operation_id")
    allowed = set(_COMMON_JOURNAL_KEYS)
    if operation_id == CLEANUP_OPERATION:
        allowed.add("target_transaction_ids")
    if set(payload) != allowed:
        raise EvidenceAdapterError(
            "trusted filesystem mutation journal schema differs: "
            + ",".join(sorted(set(payload) ^ allowed))
        )

    if payload.get("format_version") != 1:
        raise EvidenceAdapterError("journal format_version differs")
    if payload.get("journal_type") != "FILESYSTEM_MUTATION_INTENT":
        raise EvidenceAdapterError("journal_type differs")
    if payload.get("evidence_type") != "MUTATION_DISPATCH_INTENT":
        raise EvidenceAdapterError("journal evidence_type differs")
    if payload.get("authority") != "CONTROL" or payload.get("lifecycle") != "CURRENT":
        raise EvidenceAdapterError("journal authority/lifecycle differs")
    if payload.get("private_repository") != PRIVATE_REPOSITORY:
        raise EvidenceAdapterError("journal private repository differs")
    _require_text(payload.get("private_sha"), "journal private_sha", _SHA)
    if payload.get("canonical_repository") != CANONICAL_REPOSITORY:
        raise EvidenceAdapterError("journal canonical repository differs")
    _require_text(payload.get("canonical_sha"), "journal canonical_sha", _SHA)
    _positive_int(payload.get("canonical_quality_run_id"), "journal canonical quality run ID")
    run_id = _positive_int(payload.get("workflow_run_id"), "journal workflow run ID")
    run_attempt = _positive_int(payload.get("workflow_run_attempt"), "journal workflow run attempt")
    transaction_id = _require_text(
        payload.get("operation_transaction_id"), "journal operation transaction", _TRANSACTION
    )
    if transaction_id != _expected_transaction(operation_id, run_id, run_attempt):
        raise EvidenceAdapterError("journal transaction does not match operation run/attempt identity")
    if payload.get("target") != "android-production":
        raise EvidenceAdapterError("journal target differs")
    _require_text(payload.get("target_binding_id"), "journal target binding", _TARGET_BINDING)
    if payload.get("affected_domain_generations") != {DOMAIN_SCOPE: transaction_id}:
        raise EvidenceAdapterError("journal filesystem generation differs from transaction identity")
    for key in (
        "dispatch_intent_artifact_persisted",
        "journaled_before_adapter_invocation",
        "dispatch_may_reach_target",
    ):
        if payload.get(key) is not True:
            raise EvidenceAdapterError(f"journal {key} must be true")
    if payload.get("blind_retry_allowed") is not False:
        raise EvidenceAdapterError("journal blind_retry_allowed must be false")
    if payload.get("raw_device_identifier_recorded") is not False:
        raise EvidenceAdapterError("journal raw device identifier flag differs")

    if operation_id == CLEANUP_OPERATION:
        target_ids = payload.get("target_transaction_ids")
        if not isinstance(target_ids, list) or not (1 <= len(target_ids) <= 8):
            raise EvidenceAdapterError("cleanup journal target transaction set is invalid")
        if len(set(target_ids)) != len(target_ids):
            raise EvidenceAdapterError("cleanup journal target transaction set contains duplicates")
        for value in target_ids:
            _require_text(value, "cleanup target transaction", _TRANSACTION)
    elif operation_id != CERT_OPERATION:
        raise EvidenceAdapterError(f"unsupported journal operation: {operation_id}")

    return payload


def _parse_result_fields(body: str, heading: str) -> tuple[str, dict[str, str]]:
    if not body.startswith(heading + "\n"):
        raise EvidenceAdapterError("trusted result heading differs")
    classification: str | None = None
    fields: dict[str, str] = {}
    for line in body.splitlines()[1:]:
        if not line:
            continue
        match = _CLASSIFICATION.fullmatch(line)
        if match:
            if classification is not None:
                raise EvidenceAdapterError("trusted result classification is duplicated")
            classification = match.group("value")
            continue
        match = _FIELD.fullmatch(line)
        if match:
            key = match.group("key")
            if key in fields:
                raise EvidenceAdapterError(f"trusted result field is duplicated: {key}")
            fields[key] = match.group("value")
    if classification is None:
        raise EvidenceAdapterError("trusted result classification is missing")
    return classification, fields


def _bool_field(fields: Mapping[str, str], key: str) -> bool:
    value = fields.get(key)
    if value not in {"true", "false"}:
        raise EvidenceAdapterError(f"trusted result boolean field is invalid: {key}")
    return value == "true"


def _parse_result(comment: Mapping[str, Any]) -> dict[str, Any] | None:
    body = _comment_body(comment)
    if body.startswith(CERT_RESULT_HEADING):
        heading = CERT_RESULT_HEADING
        operation_id = CERT_OPERATION
        terminal = _CERT_TERMINAL
        unresolved = _CERT_UNRESOLVED
        state_key = "transaction_state"
    elif body.startswith(CLEANUP_RESULT_HEADING):
        heading = CLEANUP_RESULT_HEADING
        operation_id = CLEANUP_OPERATION
        terminal = _CLEANUP_TERMINAL
        unresolved = _CLEANUP_UNRESOLVED
        state_key = "cleanup_state"
    else:
        return None
    if not _trusted(comment):
        return None

    classification, fields = _parse_result_fields(body, heading)
    if fields.get("operation_id") != operation_id:
        raise EvidenceAdapterError("trusted result operation_id differs")
    transaction_id = _require_text(
        fields.get("operation_transaction_id"), "result operation transaction", _TRANSACTION
    )
    run_id = _positive_int(fields.get("workflow_run_id"), "result workflow run ID")
    run_attempt = _positive_int(fields.get("workflow_run_attempt"), "result workflow run attempt")
    if transaction_id != _expected_transaction(operation_id, run_id, run_attempt):
        raise EvidenceAdapterError("result transaction does not match operation run/attempt identity")
    if not _bool_field(fields, "dispatch_intent_persisted"):
        raise EvidenceAdapterError("trusted Stage-B result does not prove dispatch intent persistence")
    if fields.get("domain/filesystem_generation") != transaction_id:
        raise EvidenceAdapterError("trusted result filesystem generation differs")
    if _bool_field(fields, "blind_retry_allowed"):
        raise EvidenceAdapterError("trusted result permits blind retry")

    state = fields.get(state_key, "")
    if classification in terminal:
        expected_state = terminal[classification]
        if state != expected_state:
            raise EvidenceAdapterError("trusted terminal classification/state pair differs")
        result_persisted = True
        result_state = expected_state
    elif classification in unresolved:
        result_persisted = False
        result_state = ""
    else:
        raise EvidenceAdapterError(f"unsupported trusted Stage-B result classification: {classification}")

    if operation_id == CLEANUP_OPERATION and classification in _CLEANUP_TERMINAL:
        if not _bool_field(fields, "result_validated"):
            raise EvidenceAdapterError("terminal cleanup result is not validated")
        if not _bool_field(fields, "artifact_persisted"):
            raise EvidenceAdapterError("terminal cleanup result artifact is not persisted")

    return {
        "operation_id": operation_id,
        "operation_transaction_id": transaction_id,
        "workflow_run_id": run_id,
        "workflow_run_attempt": run_attempt,
        "result_persisted": result_persisted,
        "result_state": result_state,
    }


def build_inventory(comments: list[Mapping[str, Any]]) -> dict[str, Any]:
    journals: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    results: dict[tuple[str, str, int, int], dict[str, Any]] = {}

    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        journal = _parse_journal(comment)
        if journal is not None:
            key = (
                journal["operation_id"],
                journal["operation_transaction_id"],
                int(journal["workflow_run_id"]),
                int(journal["workflow_run_attempt"]),
            )
            if key in journals:
                raise EvidenceAdapterError("duplicate trusted filesystem mutation journal")
            journals[key] = journal
            continue
        result = _parse_result(comment)
        if result is not None:
            key = (
                result["operation_id"],
                result["operation_transaction_id"],
                result["workflow_run_id"],
                result["workflow_run_attempt"],
            )
            if key in results:
                raise EvidenceAdapterError("duplicate trusted filesystem mutation result")
            results[key] = result

    unknown_results = sorted(set(results) - set(journals))
    if unknown_results:
        raise EvidenceAdapterError("trusted mutation result has no exact Stage-B journal pair")

    intents: list[dict[str, Any]] = []
    for key, journal in sorted(
        journals.items(),
        key=lambda item: (item[0][2], item[0][3], item[0][1]),
    ):
        result = results.get(key)
        intents.append(
            {
                "operation_transaction_id": journal["operation_transaction_id"],
                "workflow_run_id": int(journal["workflow_run_id"]),
                "workflow_run_attempt": int(journal["workflow_run_attempt"]),
                "affected_domain_generations": {
                    DOMAIN_SCOPE: journal["operation_transaction_id"]
                },
                "result_persisted": bool(result and result["result_persisted"]),
                "result_state": result["result_state"] if result and result["result_persisted"] else "",
                "authority": "CONTROL",
                "lifecycle": "CURRENT",
                "persisted": True,
            }
        )

    return {
        "format_version": 1,
        "domain_scope": DOMAIN_SCOPE,
        "bootstrap_generation": BOOTSTRAP_GENERATION,
        "intents": intents,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comments", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        raw = json.loads(args.comments.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise EvidenceAdapterError("issue comment inventory root must be a list")
        inventory = build_inventory(raw)
        args.output.write_text(
            json.dumps(inventory, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, json.JSONDecodeError, EvidenceAdapterError, TypeError, ValueError) as error:
        print(f"filesystem generation evidence adapter failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
