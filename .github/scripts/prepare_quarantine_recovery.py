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
    RECOVERY_INTENT_HEADING,
    RECOVERY_TARGET,
    RECOVERY_TERMINAL_HEADING,
    QuarantineRecoveryError,
    recovery_semantic_id,
    validate_quarantined_deployment_intent,
    validate_quarantined_deployment_terminal,
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


def _terminal_matches_release_identity(payload: Mapping[str, object], identity: object, *, target: str) -> bool:
    expected = durable_release_identity(identity, target=target)
    if any(payload.get(field) != expected[field] for field in _TERMINAL_TOP_LEVEL_RELEASE_FIELDS):
        return False
    facts = payload.get("facts")
    release_admission = facts.get("release_admission") if isinstance(facts, Mapping) else None
    return bool(
        isinstance(release_admission, Mapping)
        and payload_matches_release_identity(release_admission, identity, target=target)
    )


def _matching_recovery_records(
    evidence: IssueEvidenceStore,
    *,
    semantic_id: str,
) -> tuple[list[object], list[object]]:
    intents = [
        item for item in evidence.list_records(RECOVERY_INTENT_HEADING)
        if item.payload.get("semantic_recovery_id") == semantic_id
    ]
    terminals = [
        item for item in evidence.list_records(RECOVERY_TERMINAL_HEADING)
        if item.payload.get("semantic_recovery_id") == semantic_id
    ]
    for item in intents:
        validate_recovery_intent(item.payload)
    for item in terminals:
        validate_recovery_terminal(item.payload)
    return intents, terminals


def _request_recovery_records(
    evidence: IssueEvidenceStore,
    *,
    request_id: str,
) -> tuple[list[object], list[object]]:
    intents = [
        item for item in evidence.list_records(RECOVERY_INTENT_HEADING)
        if item.payload.get("quarantined_request_id") == request_id
    ]
    terminals = [
        item for item in evidence.list_records(RECOVERY_TERMINAL_HEADING)
        if item.payload.get("quarantined_request_id") == request_id
    ]
    return intents, terminals


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--quarantined-request-id", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args()

    if args.target != RECOVERY_TARGET:
        raise QuarantineRecoveryError("recovery target differs from phone-production")
    if _SHA.fullmatch(args.controller_revision) is None or _EXECUTION.fullmatch(args.execution_id) is None:
        raise QuarantineRecoveryError("recovery execution provenance is invalid")

    admitted = resolve_release(tag=args.release, target=args.target)
    release_id = admitted.identity.release_id
    if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id <= 0:
        raise QuarantineRecoveryError("immutable Product Release id is unavailable")

    evidence = IssueEvidenceStore(os.environ.get("GITHUB_TOKEN", ""))
    original_intent, original_terminal = evidence.request_history(args.quarantined_request_id)
    if original_intent is None:
        raise QuarantineRecoveryError("quarantined deployment intent is unavailable")
    if original_terminal is None:
        raise QuarantineRecoveryError("quarantined deployment terminal is unavailable")

    validate_quarantined_deployment_intent(
        original_intent.payload,
        target=args.target,
        release=args.release,
        request_id=args.quarantined_request_id,
        release_id=release_id,
    )
    try:
        validate_terminal(original_terminal.payload)
    except TerminalContractError as exc:
        raise QuarantineRecoveryError("quarantined deployment terminal contract is invalid") from exc
    validate_quarantined_deployment_terminal(
        original_terminal.payload,
        target=args.target,
        release=args.release,
        request_id=args.quarantined_request_id,
        release_id=release_id,
    )

    if not payload_matches_release_identity(original_intent.payload, admitted.identity, target=args.target):
        raise QuarantineRecoveryError("quarantined deployment intent conflicts with immutable Product Release identity")
    if not _terminal_matches_release_identity(original_terminal.payload, admitted.identity, target=args.target):
        raise QuarantineRecoveryError("quarantined deployment terminal conflicts with immutable Product Release identity")

    existing_request_intents, existing_request_terminals = _request_recovery_records(
        evidence,
        request_id=args.quarantined_request_id,
    )
    if existing_request_intents or existing_request_terminals:
        raise QuarantineRecoveryError("quarantined request already has recovery lineage; new activation requires a separate reconciled authority")

    parent_terminal_ref = original_terminal.ref
    semantic_id = recovery_semantic_id(
        target=args.target,
        release=args.release,
        quarantined_request_id=args.quarantined_request_id,
        quarantined_terminal_ref=original_terminal.ref,
        parent_recovery_terminal_ref=parent_terminal_ref,
    )
    intents, terminals = _matching_recovery_records(evidence, semantic_id=semantic_id)
    if intents or terminals:
        raise QuarantineRecoveryError("authorized recovery semantic identity already has durable evidence")

    _output("admitted_release_json", admitted.to_dict())
    _output("release_source_sha", admitted.identity.source_sha)
    _output("semantic_recovery_id", semantic_id)
    _output("original_intent_ref", original_intent.ref)
    _output("original_terminal_ref", original_terminal.ref)
    _output("recovery_parent_terminal_ref", parent_terminal_ref)
    print(
        "QUARANTINE_RECOVERY_ADMITTED "
        f"semantic_recovery_id={semantic_id} target={args.target} release={args.release}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QuarantineRecoveryError, EvidenceError, ReleaseAdmissionError, json.JSONDecodeError) as exc:
        print(f"QUARANTINE_RECOVERY_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
