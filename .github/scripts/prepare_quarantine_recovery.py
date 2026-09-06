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
    RECOVERY_RELEASE,
    RECOVERY_TARGET,
    RECOVERY_TERMINAL_HEADING,
    QuarantineRecoveryError,
    recovery_semantic_id,
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

    existing_intents = [
        item for item in evidence.list_records(RECOVERY_INTENT_HEADING)
        if item.payload.get("quarantined_request_id") == QUARANTINED_REQUEST_ID
    ]
    existing_terminals = [
        item for item in evidence.list_records(RECOVERY_TERMINAL_HEADING)
        if item.payload.get("quarantined_request_id") == QUARANTINED_REQUEST_ID
    ]
    if existing_intents or existing_terminals:
        raise QuarantineRecoveryError("quarantined deployment already has durable recovery evidence; no new activation is admitted")

    admitted = resolve_release(tag=RECOVERY_RELEASE, target=RECOVERY_TARGET)
    if not payload_matches_release_identity(original_intent.payload, admitted.identity, target=RECOVERY_TARGET):
        raise QuarantineRecoveryError("quarantined deployment intent conflicts with current immutable Release identity")
    if not payload_matches_release_identity(original_terminal.payload, admitted.identity, target=RECOVERY_TARGET):
        raise QuarantineRecoveryError("quarantined deployment terminal conflicts with current immutable Release identity")

    semantic_id = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
    )
    _output("admitted_release_json", admitted.to_dict())
    _output("release_source_sha", admitted.identity.source_sha)
    _output("semantic_recovery_id", semantic_id)
    _output("original_intent_ref", original_intent.ref)
    _output("original_terminal_ref", original_terminal.ref)
    print(
        "QUARANTINE_RECOVERY_ADMITTED "
        f"semantic_recovery_id={semantic_id} target={RECOVERY_TARGET} release={RECOVERY_RELEASE}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QuarantineRecoveryError, EvidenceError, ReleaseAdmissionError, json.JSONDecodeError) as exc:
        print(f"QUARANTINE_RECOVERY_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
