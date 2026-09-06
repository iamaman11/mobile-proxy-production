#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Mapping

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from android_target import AndroidArtifactRefused, AndroidObservationUnavailable, observe  # noqa: E402
from durable_release_identity import payload_matches_release_identity  # noqa: E402
from evidence_store import EvidenceError, EvidenceWriteAmbiguous, IssueEvidenceStore, evidence_identity  # noqa: E402
from phone_runtime import PhoneRuntimeRefused  # noqa: E402
from phone_target import PhoneTargetUnavailable  # noqa: E402
from quarantine_phone_observer import observe_exact_inactive_runtime  # noqa: E402
from quarantine_recovery import (  # noqa: E402
    QUARANTINED_INTENT_REF,
    QUARANTINED_REQUEST_ID,
    QUARANTINED_TERMINAL_REF,
    RECOVERY_INTENT_HEADING,
    RECOVERY_OPERATION,
    RECOVERY_PARENT_TERMINAL_REF,
    RECOVERY_RELEASE,
    RECOVERY_RELEASE_ID,
    RECOVERY_TARGET,
    RECOVERY_TERMINAL_HEADING,
    RECOVERY_TERMINAL_SCHEMA,
    RECOVERY_UNKNOWN_INTENT_REF,
    RECOVERY_UNKNOWN_TERMINAL_REF,
    QuarantineRecoveryError,
    recovery_semantic_id,
    validate_recovery_intent,
    validate_recovery_terminal,
)
from release_handoff import parse_admitted_release  # noqa: E402
from release_resolver import ReleaseAdmissionError  # noqa: E402
from run_phone_release_deployment import (  # noqa: E402
    _materialize_verified_release_apk,
    _materialize_verified_release_runtime,
)
from terminal_result import TerminalContractError, validate_terminal  # noqa: E402


def _records(evidence: IssueEvidenceStore, heading: str, semantic_id: str):
    return [item for item in evidence.list_records(heading) if item.payload.get("semantic_recovery_id") == semantic_id]


def _existing_unique(evidence: IssueEvidenceStore, heading: str, semantic_id: str):
    records = _records(evidence, heading, semantic_id)
    if len(records) > 1:
        first = records[0].identity
        if any(record.identity != first for record in records[1:]):
            raise QuarantineRecoveryError("conflicting UNKNOWN reconciliation evidence exists")
    return records[0] if records else None


def _persist_terminal(evidence: IssueEvidenceStore, payload: Mapping[str, object]):
    semantic_id = str(payload["semantic_recovery_id"])
    expected = evidence_identity(RECOVERY_TERMINAL_HEADING, payload)
    existing = _existing_unique(evidence, RECOVERY_TERMINAL_HEADING, semantic_id)
    if existing is not None:
        if existing.identity != expected:
            raise QuarantineRecoveryError("UNKNOWN reconciliation semantic id has different durable evidence")
        return existing
    try:
        return evidence.create(RECOVERY_TERMINAL_HEADING, payload)
    except EvidenceWriteAmbiguous as first_error:
        reconciled = _existing_unique(evidence, RECOVERY_TERMINAL_HEADING, semantic_id)
        if reconciled is not None and reconciled.identity == expected:
            return reconciled
        try:
            return evidence.create(RECOVERY_TERMINAL_HEADING, payload)
        except EvidenceWriteAmbiguous as second_error:
            reconciled = _existing_unique(evidence, RECOVERY_TERMINAL_HEADING, semantic_id)
            if reconciled is not None and reconciled.identity == expected:
                return reconciled
            raise QuarantineRecoveryError("UNKNOWN reconciliation terminal write remains ambiguous") from second_error


def _validate_unknown_parent_under_lock(evidence: IssueEvidenceStore) -> None:
    continuation_id = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_PARENT_TERMINAL_REF,
    )
    intents = _records(evidence, RECOVERY_INTENT_HEADING, continuation_id)
    terminals = _records(evidence, RECOVERY_TERMINAL_HEADING, continuation_id)
    if len(intents) != 1 or intents[0].ref != RECOVERY_UNKNOWN_INTENT_REF:
        raise QuarantineRecoveryError("exact generation-2 recovery intent is unavailable under target lock")
    if len(terminals) != 1 or terminals[0].ref != RECOVERY_UNKNOWN_TERMINAL_REF:
        raise QuarantineRecoveryError("exact generation-2 UNKNOWN terminal is unavailable under target lock")
    validate_recovery_intent(intents[0].payload)
    validate_recovery_terminal(terminals[0].payload)
    terminal = terminals[0].payload
    if (
        terminal.get("state") != "UNKNOWN"
        or terminal.get("mutation_performed") is not True
        or terminal.get("postcondition_verified") is not False
        or terminal.get("blind_retry_allowed") is not False
        or terminal.get("recovery_intent_ref") != RECOVERY_UNKNOWN_INTENT_REF
        or terminal.get("parent_recovery_terminal_ref") != RECOVERY_PARENT_TERMINAL_REF
    ):
        raise QuarantineRecoveryError("generation-2 parent is not the exact unresolved UNKNOWN")


def _terminal(
    *, semantic_id: str, execution_id: str, controller_revision: str,
    state: str, facts: Mapping[str, object], reason: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": RECOVERY_TERMINAL_SCHEMA,
        "semantic_recovery_id": semantic_id,
        "operation": RECOVERY_OPERATION,
        "execution_id": execution_id,
        "controller_revision": controller_revision,
        "target": RECOVERY_TARGET,
        "product_release": RECOVERY_RELEASE,
        "release_id": RECOVERY_RELEASE_ID,
        "quarantined_request_id": QUARANTINED_REQUEST_ID,
        "quarantined_terminal_ref": QUARANTINED_TERMINAL_REF,
        "parent_recovery_terminal_ref": RECOVERY_UNKNOWN_TERMINAL_REF,
        "recovery_intent_ref": None,
        "state": state,
        "mutation_performed": False,
        "postcondition_verified": True,
        "blocking_predicate": reason,
        "facts": dict(facts),
        "blind_retry_allowed": False,
    }
    validate_recovery_terminal(payload)
    return payload


def _write_output(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--quarantined-request-id", required=True)
    parser.add_argument("--recovery-parent-terminal-ref", required=True)
    parser.add_argument("--admitted-release-json", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--product-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.target != RECOVERY_TARGET or args.release != RECOVERY_RELEASE or args.quarantined_request_id != QUARANTINED_REQUEST_ID:
        raise QuarantineRecoveryError("runtime UNKNOWN reconciliation inputs differ from exact Stage 3 lineage")
    if args.recovery_parent_terminal_ref != RECOVERY_UNKNOWN_TERMINAL_REF:
        raise QuarantineRecoveryError("runtime UNKNOWN reconciliation parent differs from hosted admission")
    if os.environ.get("GITHUB_SHA") != args.controller_revision:
        raise QuarantineRecoveryError("runtime UNKNOWN reconciliation controller revision differs")

    try:
        admitted = parse_admitted_release(json.loads(args.admitted_release_json), tag=RECOVERY_RELEASE, target=RECOVERY_TARGET)
    except (json.JSONDecodeError, ReleaseAdmissionError) as exc:
        raise QuarantineRecoveryError("hosted immutable Release handoff is invalid") from exc
    if admitted.identity.release_id != RECOVERY_RELEASE_ID:
        raise QuarantineRecoveryError("immutable Release id differs from Stage 3 authority")

    evidence = IssueEvidenceStore(os.environ.get("GITHUB_TOKEN", ""))
    original_intent, original_terminal = evidence.request_history(QUARANTINED_REQUEST_ID)
    if original_intent is None or original_intent.ref != QUARANTINED_INTENT_REF:
        raise QuarantineRecoveryError("exact original deployment intent is unavailable under target lock")
    if original_terminal is None or original_terminal.ref != QUARANTINED_TERMINAL_REF:
        raise QuarantineRecoveryError("exact original deployment terminal is unavailable under target lock")
    try:
        validate_terminal(original_terminal.payload)
    except TerminalContractError as exc:
        raise QuarantineRecoveryError("original quarantined terminal contract is invalid") from exc
    if original_terminal.payload.get("state") != "QUARANTINED":
        raise QuarantineRecoveryError("original deployment terminal is not QUARANTINED")
    if not payload_matches_release_identity(original_intent.payload, admitted.identity, target=RECOVERY_TARGET):
        raise QuarantineRecoveryError("original deployment intent differs from immutable Release identity")

    _validate_unknown_parent_under_lock(evidence)
    semantic_id = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_UNKNOWN_TERMINAL_REF,
    )
    if _records(evidence, RECOVERY_INTENT_HEADING, semantic_id):
        raise QuarantineRecoveryError("read-only UNKNOWN reconciliation unexpectedly has a mutation intent")
    existing_terminal = _existing_unique(evidence, RECOVERY_TERMINAL_HEADING, semantic_id)
    if existing_terminal is not None:
        validate_recovery_terminal(existing_terminal.payload)
        _write_output(args.output, existing_terminal.payload)
        return 0 if existing_terminal.payload.get("state") == "ACCEPTED" else 2

    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
    if not serial or not binding_key:
        raise QuarantineRecoveryError("registered phone target binding is unavailable")

    facts: dict[str, object] = {
        "mode": "read_only_reconciliation",
        "parent_unknown_terminal_ref": RECOVERY_UNKNOWN_TERMINAL_REF,
        "original_quarantined_terminal_ref": QUARANTINED_TERMINAL_REF,
        "vm_provider_access_performed": False,
        "apk_mutation_performed": False,
        "runtime_mutation_performed": False,
        "durable_mutation_intent_created": False,
    }
    with tempfile.TemporaryDirectory(prefix="mobile-proxy-unknown-reconciliation-") as td:
        root = Path(td)
        apk = root / admitted.identity.artifact_name
        runtime_archive = root / str(admitted.identity.phone_runtime_artifact_name)
        try:
            _materialize_verified_release_apk(admitted, apk, facts)
            materialized = _materialize_verified_release_runtime(
                admitted,
                archive=runtime_archive,
                work_root=root / "runtime",
                product_root=args.product_root,
                runtime_manifest_path=args.runtime_manifest,
                binding_key=binding_key,
                facts=facts,
            )
            apk_observation = observe(
                serial=serial,
                binding_key=binding_key,
                expected_version_name=str(admitted.android_version_name),
                expected_version_code=int(admitted.android_version_code or 0),
                expected_artifact_sha256=admitted.artifact_transport_sha256,
            )
            runtime_observation = observe_exact_inactive_runtime(
                serial=serial,
                release_root=materialized.release_root,
                release_id=RECOVERY_RELEASE,
                required_paths=materialized.required_live_release_paths,
            )
        except (AndroidArtifactRefused, PhoneRuntimeRefused, AndroidObservationUnavailable, PhoneTargetUnavailable) as exc:
            print(f"STAGE3_UNKNOWN_RECONCILIATION_OBSERVATION_UNAVAILABLE: {exc}", file=sys.stderr)
            return 2

    original_binding = str(original_intent.payload.get("target_binding_id", ""))
    target_binding_matches = apk_observation.target_binding_id == original_binding
    postcondition = {
        "apk": apk_observation.to_dict(),
        "runtime": runtime_observation,
        "target_binding_matches_original_intent": target_binding_matches,
        "exact_release_identity": True,
        "mode": "read_only_reconciliation",
    }
    facts["postcondition"] = postcondition
    desired = bool(
        apk_observation.desired
        and apk_observation.exact_artifact_verified
        and target_binding_matches
        and runtime_observation["desired"] is True
        and runtime_observation["inactive_exact_files_verified"] is True
    )
    terminal = _terminal(
        semantic_id=semantic_id,
        execution_id=args.execution_id,
        controller_revision=args.controller_revision,
        state="ACCEPTED" if desired else "REFUSED",
        facts=facts,
        reason=None if desired else "read-only reconciliation observed deterministic non-desired Stage 3 state",
    )
    _persist_terminal(evidence, terminal)
    _write_output(args.output, terminal)
    return 0 if desired else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QuarantineRecoveryError, EvidenceError, json.JSONDecodeError) as exc:
        print(f"STAGE3_UNKNOWN_RECONCILIATION_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
