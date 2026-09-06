#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from durable_release_identity import payload_matches_release_identity  # noqa: E402
from evidence_store import EvidenceError, IssueEvidenceStore  # noqa: E402
from quarantine_recovery import (  # noqa: E402
    QUARANTINED_INTENT_REF,
    QUARANTINED_REQUEST_ID,
    QUARANTINED_TERMINAL_REF,
    RECOVERY_INTENT_HEADING,
    RECOVERY_PARENT_INTENT_REF,
    RECOVERY_PARENT_TERMINAL_REF,
    RECOVERY_RELEASE,
    RECOVERY_TARGET,
    RECOVERY_TERMINAL_HEADING,
    RECOVERY_UNKNOWN_INTENT_REF,
    RECOVERY_UNKNOWN_TERMINAL_REF,
    QuarantineRecoveryError,
    recovery_semantic_id,
    validate_recovery_intent,
    validate_recovery_terminal,
)
from release_resolver import ReleaseAdmissionError, resolve_release  # noqa: E402
from terminal_result import TerminalContractError, validate_terminal  # noqa: E402

_SHA = re.compile(r"[0-9a-f]{40}")
_EXECUTION = re.compile(r"gh-run:[1-9][0-9]*:[1-9][0-9]*")


def _output(name: str, value: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    text = json.dumps(value, sort_keys=True, separators=(",", ":")) if isinstance(value, (dict, list)) else str(value)
    if "\n" in text or "\r" in text:
        raise QuarantineRecoveryError("workflow output must be one line")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={text}\n")


def _records(evidence: IssueEvidenceStore, heading: str, semantic_id: str):
    return [item for item in evidence.list_records(heading) if item.payload.get("semantic_recovery_id") == semantic_id]


def _require_one(records, *, ref: str, label: str):
    if len(records) != 1 or records[0].ref != ref:
        raise QuarantineRecoveryError(f"exact {label} is unavailable")
    return records[0]


def _validate_exact_unknown_parent(evidence: IssueEvidenceStore):
    initial_id = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
    )
    continuation_id = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_PARENT_TERMINAL_REF,
    )
    reconciliation_id = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_UNKNOWN_TERMINAL_REF,
    )

    all_intents = [
        item for item in evidence.list_records(RECOVERY_INTENT_HEADING)
        if item.payload.get("quarantined_request_id") == QUARANTINED_REQUEST_ID
    ]
    all_terminals = [
        item for item in evidence.list_records(RECOVERY_TERMINAL_HEADING)
        if item.payload.get("quarantined_request_id") == QUARANTINED_REQUEST_ID
    ]
    allowed = {initial_id, continuation_id, reconciliation_id}
    if any(item.payload.get("semantic_recovery_id") not in allowed for item in all_intents + all_terminals):
        raise QuarantineRecoveryError("unexpected Stage 3 recovery lineage exists")

    initial_intent = _require_one(_records(evidence, RECOVERY_INTENT_HEADING, initial_id), ref=RECOVERY_PARENT_INTENT_REF, label="generation-1 recovery intent")
    initial_terminal = _require_one(_records(evidence, RECOVERY_TERMINAL_HEADING, initial_id), ref=RECOVERY_PARENT_TERMINAL_REF, label="generation-1 recovery terminal")
    validate_recovery_intent(initial_intent.payload)
    validate_recovery_terminal(initial_terminal.payload)
    if (
        initial_terminal.payload.get("state") != "QUARANTINED"
        or initial_terminal.payload.get("mutation_performed") is not True
        or initial_terminal.payload.get("postcondition_verified") is not True
        or initial_terminal.payload.get("recovery_intent_ref") != RECOVERY_PARENT_INTENT_REF
    ):
        raise QuarantineRecoveryError("generation-1 recovery terminal differs from the exact Stage 3 quarantine")

    unknown_intent = _require_one(_records(evidence, RECOVERY_INTENT_HEADING, continuation_id), ref=RECOVERY_UNKNOWN_INTENT_REF, label="generation-2 recovery intent")
    unknown_terminal = _require_one(_records(evidence, RECOVERY_TERMINAL_HEADING, continuation_id), ref=RECOVERY_UNKNOWN_TERMINAL_REF, label="generation-2 UNKNOWN terminal")
    validate_recovery_intent(unknown_intent.payload)
    validate_recovery_terminal(unknown_terminal.payload)
    if (
        unknown_intent.payload.get("parent_recovery_terminal_ref") != RECOVERY_PARENT_TERMINAL_REF
        or unknown_terminal.payload.get("parent_recovery_terminal_ref") != RECOVERY_PARENT_TERMINAL_REF
        or unknown_terminal.payload.get("state") != "UNKNOWN"
        or unknown_terminal.payload.get("mutation_performed") is not True
        or unknown_terminal.payload.get("postcondition_verified") is not False
        or unknown_terminal.payload.get("blind_retry_allowed") is not False
        or unknown_terminal.payload.get("recovery_intent_ref") != RECOVERY_UNKNOWN_INTENT_REF
    ):
        raise QuarantineRecoveryError("generation-2 terminal is not the exact unresolved Stage 3 UNKNOWN")

    reconciliation_intents = _records(evidence, RECOVERY_INTENT_HEADING, reconciliation_id)
    if reconciliation_intents:
        raise QuarantineRecoveryError("read-only UNKNOWN reconciliation must not have a mutation intent")
    reconciliation_terminals = _records(evidence, RECOVERY_TERMINAL_HEADING, reconciliation_id)
    if len(reconciliation_terminals) > 1:
        raise QuarantineRecoveryError("conflicting UNKNOWN reconciliation terminal evidence exists")
    if reconciliation_terminals:
        validate_recovery_terminal(reconciliation_terminals[0].payload)

    return unknown_terminal, reconciliation_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--quarantined-request-id", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args()

    if args.target != RECOVERY_TARGET or args.release != RECOVERY_RELEASE or args.quarantined_request_id != QUARANTINED_REQUEST_ID:
        raise QuarantineRecoveryError("UNKNOWN reconciliation command differs from the exact Stage 3 lineage")
    if _SHA.fullmatch(args.controller_revision) is None or _EXECUTION.fullmatch(args.execution_id) is None:
        raise QuarantineRecoveryError("UNKNOWN reconciliation execution provenance is invalid")

    evidence = IssueEvidenceStore(os.environ.get("GITHUB_TOKEN", ""))
    original_intent, original_terminal = evidence.request_history(QUARANTINED_REQUEST_ID)
    if original_intent is None or original_intent.ref != QUARANTINED_INTENT_REF:
        raise QuarantineRecoveryError("exact quarantined deployment intent is unavailable")
    if original_terminal is None or original_terminal.ref != QUARANTINED_TERMINAL_REF:
        raise QuarantineRecoveryError("exact quarantined deployment terminal is unavailable")
    try:
        validate_terminal(original_terminal.payload)
    except TerminalContractError as exc:
        raise QuarantineRecoveryError("quarantined deployment terminal contract is invalid") from exc
    if original_terminal.payload.get("state") != "QUARANTINED":
        raise QuarantineRecoveryError("original deployment terminal is not QUARANTINED")

    unknown_terminal, semantic_id = _validate_exact_unknown_parent(evidence)

    admitted = resolve_release(tag=RECOVERY_RELEASE, target=RECOVERY_TARGET)
    if not payload_matches_release_identity(original_intent.payload, admitted.identity, target=RECOVERY_TARGET):
        raise QuarantineRecoveryError("original deployment intent conflicts with immutable Release identity")
    if admitted.identity.release_id != 383454833:
        raise QuarantineRecoveryError("immutable Release id differs from Stage 3 authority")

    _output("admitted_release_json", admitted.to_dict())
    _output("release_source_sha", admitted.identity.source_sha)
    _output("semantic_recovery_id", semantic_id)
    _output("recovery_parent_terminal_ref", unknown_terminal.ref)
    print(
        "STAGE3_UNKNOWN_RECONCILIATION_ADMITTED "
        f"semantic_recovery_id={semantic_id} parent={unknown_terminal.ref} target={RECOVERY_TARGET} release={RECOVERY_RELEASE}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QuarantineRecoveryError, EvidenceError, ReleaseAdmissionError, json.JSONDecodeError) as exc:
        print(f"STAGE3_UNKNOWN_RECONCILIATION_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
