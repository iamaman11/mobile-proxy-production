#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Mapping

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from android_target import AndroidArtifactRefused, AndroidObservationUnavailable, observe  # noqa: E402
from durable_release_identity import payload_matches_release_identity  # noqa: E402
from evidence_store import EvidenceError, EvidenceWriteAmbiguous, IssueEvidenceStore, evidence_identity  # noqa: E402
from phone_runtime import PhoneRuntimeRefused  # noqa: E402
from phone_target import PhoneTargetUnavailable, _files, _run_root_script  # noqa: E402
from quarantine_recovery import (  # noqa: E402
    QUARANTINED_INTENT_REF,
    QUARANTINED_REQUEST_ID,
    QUARANTINED_TERMINAL_REF,
    RECOVERY_INTENT_HEADING,
    RECOVERY_OPERATION,
    RECOVERY_PARENT_INTENT_REF,
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

_ROOT = "/data/adb/mobile-proxy-node"
_READ_ATTEMPTS = 3


def _records(evidence: IssueEvidenceStore, heading: str, semantic_id: str):
    return [item for item in evidence.list_records(heading) if item.payload.get("semantic_recovery_id") == semantic_id]


def _existing_unique(evidence: IssueEvidenceStore, heading: str, semantic_id: str):
    records = _records(evidence, heading, semantic_id)
    if len(records) > 1:
        first = records[0].identity
        if any(record.identity != first for record in records[1:]):
            raise QuarantineRecoveryError("conflicting durable UNKNOWN reconciliation evidence exists")
    return records[0] if records else None


def _persist_terminal(evidence: IssueEvidenceStore, payload: Mapping[str, object]):
    semantic_id = str(payload["semantic_recovery_id"])
    expected = evidence_identity(RECOVERY_TERMINAL_HEADING, payload)
    existing = _existing_unique(evidence, RECOVERY_TERMINAL_HEADING, semantic_id)
    if existing is not None:
        if existing.identity != expected:
            raise QuarantineRecoveryError("UNKNOWN reconciliation semantic id already has different durable evidence")
        return existing
    try:
        return evidence.create(RECOVERY_TERMINAL_HEADING, payload)
    except EvidenceWriteAmbiguous as first_error:
        reconciled = _existing_unique(evidence, RECOVERY_TERMINAL_HEADING, semantic_id)
        if reconciled is not None:
            if reconciled.identity != expected:
                raise QuarantineRecoveryError("ambiguous reconciliation write resolved to different evidence")
            return reconciled
        try:
            return evidence.create(RECOVERY_TERMINAL_HEADING, payload)
        except EvidenceWriteAmbiguous as second_error:
            reconciled = _existing_unique(evidence, RECOVERY_TERMINAL_HEADING, semantic_id)
            if reconciled is not None and reconciled.identity == expected:
                return reconciled
            raise QuarantineRecoveryError("read-only reconciliation terminal write remains ambiguous") from second_error


def _root_runtime_observation(
    *, serial: str, release_root: Path, release_id: str, required_paths: tuple[str, ...]
) -> dict[str, object]:
    files = _files(release_root, required_paths)
    target = f"{_ROOT}/releases/{release_id}"
    script_lines = [
        "set -eu",
        f"ROOT='{_ROOT}'",
        f"TARGET='{target}'",
        "if [ -d \"$TARGET\" ]; then echo target=present; else echo target=absent; fi",
        "if [ -L \"$ROOT/current\" ]; then printf 'current='; readlink \"$ROOT/current\"; elif [ -e \"$ROOT/current\" ]; then echo current=invalid; else echo current=absent; fi",
        "command -v sha256sum >/dev/null",
    ]
    for index, (relative, _local, _expected) in enumerate(files):
        remote = f"{target}/{relative}"
        script_lines.append(
            f"if [ -f '{remote}' ]; then printf 'h{index}='; sha256sum '{remote}' | awk '{{print $1}}'; else echo 'h{index}=missing'; fi"
        )
    result = _run_root_script(serial, ("\n".join(script_lines) + "\n").encode("utf-8"), timeout=60)
    if result.status != "completed" or result.returncode != 0 or result.stderr:
        raise PhoneTargetUnavailable("rooted UNKNOWN reconciliation observation failed")
    try:
        lines = result.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PhoneTargetUnavailable("rooted UNKNOWN reconciliation observation is malformed") from exc
    values: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key not in values:
            values[key] = value
    if values.get("target") not in {"present", "absent"} or "current" not in values:
        raise PhoneTargetUnavailable("rooted UNKNOWN reconciliation observation is malformed")
    exists = values["target"] == "present"
    exact = exists
    for index, (_relative, _local, expected) in enumerate(files):
        if values.get(f"h{index}") != expected:
            exact = False
            break
    current_raw = values["current"]
    current = None if current_raw == "absent" else current_raw
    current_managed = isinstance(current, str) and current.startswith(f"{_ROOT}/releases/")
    return {
        "mode": "read_only",
        "target_release": target,
        "target_release_exists": exists,
        "inactive_exact_files_verified": exact,
        "required_file_count": len(files),
        "current_target": current,
        "current_managed": current_managed,
        "desired": bool(exact and current == target),
    }


def _observe_exact(
    *, serial: str, binding_key: str, admitted, release_root: Path, required_paths: tuple[str, ...]
):
    last_error: Exception | None = None
    for attempt in range(1, _READ_ATTEMPTS + 1):
        try:
            apk = observe(
                serial=serial,
                binding_key=binding_key,
                expected_version_name=str(admitted.android_version_name),
                expected_version_code=int(admitted.android_version_code or 0),
                expected_artifact_sha256=admitted.artifact_transport_sha256,
            )
            runtime = _root_runtime_observation(
                serial=serial,
                release_root=release_root,
                release_id=RECOVERY_RELEASE,
                required_paths=required_paths,
            )
            return apk, runtime
        except (AndroidObservationUnavailable, PhoneTargetUnavailable) as exc:
            last_error = exc
            if attempt < _READ_ATTEMPTS:
                time.sleep(attempt)
    raise QuarantineRecoveryError("read-only UNKNOWN reconciliation observation unavailable after bounded attempts") from last_error


def _validate_parent_under_lock(evidence: IssueEvidenceStore, admitted) -> object:
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
        raise QuarantineRecoveryError("original intent differs from immutable Release identity")

    first_semantic = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
    )
    unknown_semantic = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_PARENT_TERMINAL_REF,
    )
    first_intent = _existing_unique(evidence, RECOVERY_INTENT_HEADING, first_semantic)
    first_terminal = _existing_unique(evidence, RECOVERY_TERMINAL_HEADING, first_semantic)
    unknown_intent = _existing_unique(evidence, RECOVERY_INTENT_HEADING, unknown_semantic)
    unknown_terminal = _existing_unique(evidence, RECOVERY_TERMINAL_HEADING, unknown_semantic)
    if first_intent is None or first_terminal is None or unknown_intent is None or unknown_terminal is None:
        raise QuarantineRecoveryError("exact recovery lineage is unavailable under target lock")
    if first_intent.ref != RECOVERY_PARENT_INTENT_REF or first_terminal.ref != RECOVERY_PARENT_TERMINAL_REF:
        raise QuarantineRecoveryError("first recovery generation differs under target lock")
    if unknown_intent.ref != RECOVERY_UNKNOWN_INTENT_REF or unknown_terminal.ref != RECOVERY_UNKNOWN_TERMINAL_REF:
        raise QuarantineRecoveryError("UNKNOWN recovery generation differs under target lock")
    validate_recovery_intent(first_intent.payload)
    validate_recovery_terminal(first_terminal.payload)
    validate_recovery_intent(unknown_intent.payload)
    validate_recovery_terminal(unknown_terminal.payload)
    if (
        unknown_intent.payload.get("parent_recovery_terminal_ref") != RECOVERY_PARENT_TERMINAL_REF
        or unknown_terminal.payload.get("parent_recovery_terminal_ref") != RECOVERY_PARENT_TERMINAL_REF
        or unknown_terminal.payload.get("state") != "UNKNOWN"
        or unknown_terminal.payload.get("mutation_performed") is not True
        or unknown_terminal.payload.get("postcondition_verified") is not False
        or unknown_terminal.payload.get("recovery_intent_ref") != RECOVERY_UNKNOWN_INTENT_REF
        or unknown_terminal.payload.get("blocking_predicate") != "activation outcome is unknown"
    ):
        raise QuarantineRecoveryError("parent recovery is not the exact post-intent UNKNOWN state")
    return original_intent


def _terminal(
    *, semantic_id: str, execution_id: str, controller_revision: str,
    state: str, postcondition_verified: bool, facts: Mapping[str, object], reason: str | None,
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
        "postcondition_verified": postcondition_verified,
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
    parser.add_argument("--admitted-release-json", required=True)
    parser.add_argument("--semantic-recovery-id", required=True)
    parser.add_argument("--recovery-parent-terminal-ref", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--product-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.target != RECOVERY_TARGET or args.release != RECOVERY_RELEASE or args.quarantined_request_id != QUARANTINED_REQUEST_ID:
        raise QuarantineRecoveryError("UNKNOWN reconciliation inputs differ from exact Stage 3 quarantine")
    if args.recovery_parent_terminal_ref != RECOVERY_UNKNOWN_TERMINAL_REF:
        raise QuarantineRecoveryError("UNKNOWN reconciliation parent terminal differs")
    expected_semantic = recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=RECOVERY_RELEASE,
        quarantined_request_id=QUARANTINED_REQUEST_ID,
        parent_recovery_terminal_ref=RECOVERY_UNKNOWN_TERMINAL_REF,
    )
    if args.semantic_recovery_id != expected_semantic:
        raise QuarantineRecoveryError("UNKNOWN reconciliation semantic identity differs")
    if os.environ.get("GITHUB_SHA") != args.controller_revision:
        raise QuarantineRecoveryError("UNKNOWN reconciliation controller revision differs")

    try:
        admitted = parse_admitted_release(json.loads(args.admitted_release_json), tag=RECOVERY_RELEASE, target=RECOVERY_TARGET)
    except (json.JSONDecodeError, ReleaseAdmissionError) as exc:
        raise QuarantineRecoveryError("hosted immutable Release handoff is invalid") from exc
    if admitted.identity.release_id != RECOVERY_RELEASE_ID:
        raise QuarantineRecoveryError("immutable Release id differs from authorized reconciliation")

    evidence = IssueEvidenceStore(os.environ.get("GITHUB_TOKEN", ""))
    original_intent = _validate_parent_under_lock(evidence, admitted)
    if _records(evidence, RECOVERY_INTENT_HEADING, expected_semantic):
        raise QuarantineRecoveryError("read-only UNKNOWN reconciliation cannot have a durable mutation intent")
    existing_terminal = _existing_unique(evidence, RECOVERY_TERMINAL_HEADING, expected_semantic)
    if existing_terminal is not None:
        validate_recovery_terminal(existing_terminal.payload)
        if (
            existing_terminal.payload.get("parent_recovery_terminal_ref") != RECOVERY_UNKNOWN_TERMINAL_REF
            or existing_terminal.payload.get("mutation_performed") is not False
            or existing_terminal.payload.get("recovery_intent_ref") is not None
        ):
            raise QuarantineRecoveryError("existing UNKNOWN reconciliation terminal violates read-only lineage")
        _write_output(args.output, existing_terminal.payload)
        return 0 if existing_terminal.payload.get("state") == "ACCEPTED" else 2

    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
    if not serial or not binding_key:
        raise QuarantineRecoveryError("registered phone target binding is unavailable")

    facts: dict[str, object] = {
        "original_quarantined_terminal_ref": QUARANTINED_TERMINAL_REF,
        "original_quarantined_intent_ref": QUARANTINED_INTENT_REF,
        "parent_recovery_terminal_ref": RECOVERY_UNKNOWN_TERMINAL_REF,
        "reconciliation_mode": "read_only",
        "vm_provider_access_performed": False,
        "apk_mutation_performed": False,
        "runtime_bytes_rematerialized": False,
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
        except (AndroidArtifactRefused, PhoneRuntimeRefused) as exc:
            terminal = _terminal(
                semantic_id=expected_semantic,
                execution_id=args.execution_id,
                controller_revision=args.controller_revision,
                state="REFUSED",
                postcondition_verified=False,
                facts=facts | {"reconciliation_observation_available": False},
                reason=str(exc),
            )
            _persist_terminal(evidence, terminal)
            _write_output(args.output, terminal)
            return 2

        try:
            apk_observed, runtime_observed = _observe_exact(
                serial=serial,
                binding_key=binding_key,
                admitted=admitted,
                release_root=materialized.release_root,
                required_paths=materialized.required_live_release_paths,
            )
        except QuarantineRecoveryError as exc:
            terminal = _terminal(
                semantic_id=expected_semantic,
                execution_id=args.execution_id,
                controller_revision=args.controller_revision,
                state="REFUSED",
                postcondition_verified=False,
                facts=facts | {"reconciliation_observation_available": False},
                reason=str(exc),
            )
            _persist_terminal(evidence, terminal)
            _write_output(args.output, terminal)
            return 2

        original_binding = str(original_intent.payload.get("target_binding_id", ""))
        binding_matches = apk_observed.target_binding_id == original_binding
        postcondition = {
            "apk": apk_observed.to_dict(),
            "runtime": runtime_observed,
            "target_binding_matches_original_intent": binding_matches,
            "mode": "read_only",
        }
        facts["postcondition"] = postcondition

        if not binding_matches:
            reason = "read-only UNKNOWN reconciliation proves target binding differs from original deployment intent"
        elif not apk_observed.desired or not apk_observed.exact_artifact_verified:
            reason = "read-only UNKNOWN reconciliation proves installed APK is not exact v0.1.7"
        elif runtime_observed["target_release_exists"] is not True or runtime_observed["inactive_exact_files_verified"] is not True:
            reason = "read-only UNKNOWN reconciliation proves inactive v0.1.7 runtime is missing or differs"
        elif runtime_observed["desired"] is not True:
            reason = "read-only UNKNOWN reconciliation proves runtime current does not resolve to v0.1.7"
        else:
            reason = None

        desired = reason is None
        terminal = _terminal(
            semantic_id=expected_semantic,
            execution_id=args.execution_id,
            controller_revision=args.controller_revision,
            state="ACCEPTED" if desired else "REFUSED",
            postcondition_verified=True,
            facts=facts,
            reason=reason,
        )
        _persist_terminal(evidence, terminal)
        _write_output(args.output, terminal)
        return 0 if desired else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QuarantineRecoveryError, EvidenceError, json.JSONDecodeError) as exc:
        print(f"QUARANTINE_UNKNOWN_RECONCILIATION_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
