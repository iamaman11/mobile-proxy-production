#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Mapping

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from durable_release_identity import durable_release_identity, payload_matches_release_identity  # noqa: E402
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
_TERMINAL_TOP_LEVEL_RELEASE_FIELDS = (
    "target",
    "product_release",
    "release_id",
    "release_source_sha",
    "artifact_digest",
)
_RECONCILE_MODE = "reconcile_unknown_read_only"


def _output(name: str, value: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    text = json.dumps(value, sort_keys=True, separators=(",", ":")) if isinstance(value, (dict, list)) else str(value)
    if "\n" in text or "\r" in text:
        raise QuarantineRecoveryError("workflow output must be one line")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={text}\n")


def _terminal_matches_release_identity(payload: Mapping[str, object], identity: object) -> bool:
    """Bind a v2 terminal to the immutable Release without inventing terminal fields."""
    expected = durable_release_identity(identity, target=RECOVERY_TARGET)
    if any(payload.get(field) != expected[field] for field in _TERMINAL_TOP_LEVEL_RELEASE_FIELDS):
        return False
    facts = payload.get("facts")
    if not isinstance(facts, Mapping):
        return False
    release_admission = facts.get("release_admission")
    if not isinstance(release_admission, Mapping):
        return False
    return payload_matches_release_identity(release_admission, identity, target=RECOVERY_TARGET)


def _records(evidence: IssueEvidenceStore):
    intents = [
        item for item in evidence.list_records(RECOVERY_INTENT_HEADING)
        if item.payload.get("quarantined_request_id") == QUARANTINED_REQUEST_ID
    ]
    terminals = [
        item for item in evidence.list_records(RECOVERY_TERMINAL_HEADING)
        if item.payload.get("quarantined_request_id") == QUARANTINED_REQUEST_ID
    ]
    return intents, terminals


def _one(records, semantic_id: str, *, kind: str):
    selected = [item for item in records if item.payload.get("semantic_recovery_id") == semantic_id]
    if len(selected) != 1:
        raise QuarantineRecoveryError(f"exact {kind} recovery evidence is unavailable or duplicated")
    return selected[0]


def _validate_recovery_lineage(evidence: IssueEvidenceStore):
    initial_semantic_id = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
    )
    activation_semantic_id = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_PARENT_TERMINAL_REF,
    )
    reconciliation_semantic_id = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_UNKNOWN_TERMINAL_REF,
    )
    intents, terminals = _records(evidence)
    allowed = {initial_semantic_id, activation_semantic_id, reconciliation_semantic_id}
    if any(item.payload.get("semantic_recovery_id") not in allowed for item in intents + terminals):
        raise QuarantineRecoveryError("unexpected recovery lineage exists for the quarantined deployment")

    first_intent = _one(intents, initial_semantic_id, kind="first intent")
    first_terminal = _one(terminals, initial_semantic_id, kind="first terminal")
    if first_intent.ref != RECOVERY_PARENT_INTENT_REF or first_terminal.ref != RECOVERY_PARENT_TERMINAL_REF:
        raise QuarantineRecoveryError("first recovery generation differs from the authorized Stage 3 lineage")
    validate_recovery_intent(first_intent.payload)
    validate_recovery_terminal(first_terminal.payload)
    if (
        first_terminal.payload.get("state") != "QUARANTINED"
        or first_terminal.payload.get("mutation_performed") is not True
        or first_terminal.payload.get("postcondition_verified") is not True
        or first_terminal.payload.get("blind_retry_allowed") is not False
        or first_terminal.payload.get("recovery_intent_ref") != RECOVERY_PARENT_INTENT_REF
    ):
        raise QuarantineRecoveryError("first recovery terminal is not the exact observed Stage 3 quarantine")

    unknown_intent = _one(intents, activation_semantic_id, kind="UNKNOWN child intent")
    unknown_terminal = _one(terminals, activation_semantic_id, kind="UNKNOWN child terminal")
    if unknown_intent.ref != RECOVERY_UNKNOWN_INTENT_REF or unknown_terminal.ref != RECOVERY_UNKNOWN_TERMINAL_REF:
        raise QuarantineRecoveryError("UNKNOWN recovery generation differs from the exact Stage 3 evidence")
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
        or unknown_terminal.payload.get("blocking_predicate") != "activation outcome is unknown"
    ):
        raise QuarantineRecoveryError("recovery parent is not the exact post-intent UNKNOWN Stage 3 terminal")

    reconcile_intents = [item for item in intents if item.payload.get("semantic_recovery_id") == reconciliation_semantic_id]
    if reconcile_intents:
        raise QuarantineRecoveryError("read-only UNKNOWN reconciliation must never create a recovery intent")
    reconcile_terminals = [item for item in terminals if item.payload.get("semantic_recovery_id") == reconciliation_semantic_id]
    if len(reconcile_terminals) > 1:
        raise QuarantineRecoveryError("conflicting UNKNOWN reconciliation terminals exist")
    if reconcile_terminals:
        terminal = reconcile_terminals[0]
        validate_recovery_terminal(terminal.payload)
        if (
            terminal.payload.get("parent_recovery_terminal_ref") != RECOVERY_UNKNOWN_TERMINAL_REF
            or terminal.payload.get("mutation_performed") is not False
            or terminal.payload.get("recovery_intent_ref") is not None
        ):
            raise QuarantineRecoveryError("existing UNKNOWN reconciliation terminal violates read-only lineage")

    return unknown_terminal, reconciliation_semantic_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--quarantined-request-id", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args()

    if args.target != RECOVERY_TARGET or args.release != RECOVERY_RELEASE or args.quarantined_request_id != QUARANTINED_REQUEST_ID:
        raise QuarantineRecoveryError("recovery command differs from the exact authorized quarantined state")
    if _SHA.fullmatch(args.controller_revision) is None or _EXECUTION.fullmatch(args.execution_id) is None:
        raise QuarantineRecoveryError("recovery execution provenance is invalid")

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
    terminal = original_terminal.payload
    if (
        terminal.get("state") != "QUARANTINED"
        or terminal.get("mutation_performed") is not True
        or terminal.get("target") != RECOVERY_TARGET
        or terminal.get("product_release") != RECOVERY_RELEASE
        or terminal.get("release_id") != 383454833
    ):
        raise QuarantineRecoveryError("deployment terminal is not the authorized v0.1.7 quarantine state")

    unknown_terminal, semantic_id = _validate_recovery_lineage(evidence)

    admitted = resolve_release(tag=RECOVERY_RELEASE, target=RECOVERY_TARGET)
    if not payload_matches_release_identity(original_intent.payload, admitted.identity, target=RECOVERY_TARGET):
        raise QuarantineRecoveryError("quarantined deployment intent conflicts with current immutable Release identity")
    if not _terminal_matches_release_identity(original_terminal.payload, admitted.identity):
        raise QuarantineRecoveryError("quarantined deployment terminal conflicts with current immutable Release identity")

    _output("admitted_release_json", admitted.to_dict())
    _output("release_source_sha", admitted.identity.source_sha)
    _output("semantic_recovery_id", semantic_id)
    _output("original_intent_ref", original_intent.ref)
    _output("original_terminal_ref", original_terminal.ref)
    _output("recovery_parent_terminal_ref", unknown_terminal.ref)
    _output("recovery_mode", _RECONCILE_MODE)
    print(
        "QUARANTINE_UNKNOWN_RECONCILIATION_ADMITTED "
        f"semantic_recovery_id={semantic_id} parent={unknown_terminal.ref} target={RECOVERY_TARGET} release={RECOVERY_RELEASE}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QuarantineRecoveryError, EvidenceError, ReleaseAdmissionError, json.JSONDecodeError) as exc:
        print(f"QUARANTINE_RECOVERY_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
