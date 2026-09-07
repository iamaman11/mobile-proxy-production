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
    RECOVERY_RECONCILED_REFUSED_TERMINAL_REF,
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


def _semantic(parent: str | None = None) -> str:
    return recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=parent,
    )


def _exact_reconciled_state(payload: Mapping[str, object]) -> bool:
    facts = payload.get("facts")
    post = facts.get("postcondition") if isinstance(facts, Mapping) else None
    if not isinstance(post, Mapping):
        return False
    apk = post.get("apk")
    runtime = post.get("runtime")
    return bool(
        isinstance(apk, Mapping)
        and apk.get("desired") is True
        and apk.get("exact_artifact_verified") is True
        and isinstance(runtime, Mapping)
        and runtime.get("target_release_exists") is True
        and runtime.get("inactive_exact_files_verified") is True
        and runtime.get("mismatch_count") == 0
        and runtime.get("current_relation") == "other-managed"
        and post.get("target_binding_matches_original_intent") is True
    )


def _validated_parent_recovery(evidence: IssueEvidenceStore):
    initial_id = _semantic()
    continuation_id = _semantic(RECOVERY_PARENT_TERMINAL_REF)
    reconciliation_id = _semantic(RECOVERY_UNKNOWN_TERMINAL_REF)
    final_id = _semantic(RECOVERY_RECONCILED_REFUSED_TERMINAL_REF)
    intents = [
        item for item in evidence.list_records(RECOVERY_INTENT_HEADING)
        if item.payload.get("quarantined_request_id") == QUARANTINED_REQUEST_ID
    ]
    terminals = [
        item for item in evidence.list_records(RECOVERY_TERMINAL_HEADING)
        if item.payload.get("quarantined_request_id") == QUARANTINED_REQUEST_ID
    ]
    allowed = {initial_id, continuation_id, reconciliation_id, final_id}
    if any(item.payload.get("semantic_recovery_id") not in allowed for item in intents + terminals):
        raise QuarantineRecoveryError("unexpected recovery lineage exists for the quarantined deployment")

    def by_id(records, semantic_id):
        return [item for item in records if item.payload.get("semantic_recovery_id") == semantic_id]

    initial_intents = by_id(intents, initial_id)
    initial_terminals = by_id(terminals, initial_id)
    if len(initial_intents) != 1 or len(initial_terminals) != 1:
        raise QuarantineRecoveryError("initial Stage 3 recovery evidence differs")
    if initial_intents[0].ref != RECOVERY_PARENT_INTENT_REF or initial_terminals[0].ref != RECOVERY_PARENT_TERMINAL_REF:
        raise QuarantineRecoveryError("initial Stage 3 recovery refs differ")
    validate_recovery_intent(initial_intents[0].payload)
    validate_recovery_terminal(initial_terminals[0].payload)
    if (
        initial_terminals[0].payload.get("state") != "QUARANTINED"
        or initial_terminals[0].payload.get("mutation_performed") is not True
        or initial_terminals[0].payload.get("postcondition_verified") is not True
        or initial_terminals[0].payload.get("recovery_intent_ref") != RECOVERY_PARENT_INTENT_REF
    ):
        raise QuarantineRecoveryError("initial Stage 3 recovery terminal differs")

    continuation_intents = by_id(intents, continuation_id)
    continuation_terminals = by_id(terminals, continuation_id)
    if len(continuation_intents) != 1 or len(continuation_terminals) != 1:
        raise QuarantineRecoveryError("generation-2 Stage 3 recovery evidence differs")
    if continuation_intents[0].ref != RECOVERY_UNKNOWN_INTENT_REF or continuation_terminals[0].ref != RECOVERY_UNKNOWN_TERMINAL_REF:
        raise QuarantineRecoveryError("generation-2 Stage 3 recovery refs differ")
    validate_recovery_intent(continuation_intents[0].payload)
    validate_recovery_terminal(continuation_terminals[0].payload)
    if (
        continuation_intents[0].payload.get("parent_recovery_terminal_ref") != RECOVERY_PARENT_TERMINAL_REF
        or continuation_terminals[0].payload.get("parent_recovery_terminal_ref") != RECOVERY_PARENT_TERMINAL_REF
        or continuation_terminals[0].payload.get("state") != "UNKNOWN"
        or continuation_terminals[0].payload.get("mutation_performed") is not True
        or continuation_terminals[0].payload.get("postcondition_verified") is not False
        or continuation_terminals[0].payload.get("recovery_intent_ref") != RECOVERY_UNKNOWN_INTENT_REF
    ):
        raise QuarantineRecoveryError("generation-2 UNKNOWN lineage differs")

    reconciliation_intents = by_id(intents, reconciliation_id)
    reconciliation_terminals = by_id(terminals, reconciliation_id)
    if reconciliation_intents or len(reconciliation_terminals) != 1:
        raise QuarantineRecoveryError("read-only UNKNOWN reconciliation evidence differs")
    reconciled = reconciliation_terminals[0]
    if reconciled.ref != RECOVERY_RECONCILED_REFUSED_TERMINAL_REF:
        raise QuarantineRecoveryError("read-only reconciliation terminal ref differs")
    validate_recovery_terminal(reconciled.payload)
    if (
        reconciled.payload.get("parent_recovery_terminal_ref") != RECOVERY_UNKNOWN_TERMINAL_REF
        or reconciled.payload.get("state") != "REFUSED"
        or reconciled.payload.get("mutation_performed") is not False
        or reconciled.payload.get("postcondition_verified") is not True
        or reconciled.payload.get("recovery_intent_ref") is not None
        or not _exact_reconciled_state(reconciled.payload)
    ):
        raise QuarantineRecoveryError("read-only reconciliation is not the exact known safe Stage 3 state")

    final_intents = by_id(intents, final_id)
    final_terminals = by_id(terminals, final_id)
    if len(final_intents) > 1 or len(final_terminals) > 1:
        raise QuarantineRecoveryError("conflicting final Stage 3 recovery evidence exists")
    for item in final_intents:
        validate_recovery_intent(item.payload)
        if item.payload.get("parent_recovery_terminal_ref") != RECOVERY_RECONCILED_REFUSED_TERMINAL_REF:
            raise QuarantineRecoveryError("final recovery intent parent differs")
    for item in final_terminals:
        validate_recovery_terminal(item.payload)
        if item.payload.get("parent_recovery_terminal_ref") != RECOVERY_RECONCILED_REFUSED_TERMINAL_REF:
            raise QuarantineRecoveryError("final recovery terminal parent differs")
        if item.payload.get("recovery_intent_ref") is not None:
            if len(final_intents) != 1 or final_intents[0].ref != item.payload.get("recovery_intent_ref"):
                raise QuarantineRecoveryError("final recovery terminal intent lineage differs")
    return reconciled


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

    parent_terminal = _validated_parent_recovery(evidence)
    admitted = resolve_release(tag=RECOVERY_RELEASE, target=RECOVERY_TARGET)
    if not payload_matches_release_identity(original_intent.payload, admitted.identity, target=RECOVERY_TARGET):
        raise QuarantineRecoveryError("quarantined deployment intent conflicts with current immutable Release identity")
    if not _terminal_matches_release_identity(original_terminal.payload, admitted.identity):
        raise QuarantineRecoveryError("quarantined deployment terminal conflicts with current immutable Release identity")

    semantic_id = _semantic(parent_terminal.ref)
    _output("admitted_release_json", admitted.to_dict())
    _output("release_source_sha", admitted.identity.source_sha)
    _output("semantic_recovery_id", semantic_id)
    _output("original_intent_ref", original_intent.ref)
    _output("original_terminal_ref", original_terminal.ref)
    _output("recovery_parent_terminal_ref", parent_terminal.ref)
    print(
        "STAGE3_FINAL_RECOVERY_ADMITTED "
        f"semantic_recovery_id={semantic_id} parent={parent_terminal.ref} target={RECOVERY_TARGET} release={RECOVERY_RELEASE}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QuarantineRecoveryError, EvidenceError, ReleaseAdmissionError, json.JSONDecodeError) as exc:
        print(f"QUARANTINE_RECOVERY_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
