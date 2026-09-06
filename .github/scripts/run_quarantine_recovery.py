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
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from android_target import AndroidArtifactRefused, AndroidObservationUnavailable, observe  # noqa: E402
from durable_release_identity import payload_matches_release_identity  # noqa: E402
from evidence_store import EvidenceError, EvidenceWriteAmbiguous, IssueEvidenceStore, evidence_identity  # noqa: E402
from phone_runtime import PhoneRuntimeRefused  # noqa: E402
from phone_target import PhoneTargetUnavailable, _activate, _files, _run_root_script  # noqa: E402
from quarantine_recovery import (  # noqa: E402
    QUARANTINED_INTENT_REF,
    QUARANTINED_REQUEST_ID,
    QUARANTINED_TERMINAL_REF,
    RECOVERY_INTENT_HEADING,
    RECOVERY_INTENT_SCHEMA,
    RECOVERY_OPERATION,
    RECOVERY_RELEASE,
    RECOVERY_RELEASE_ID,
    RECOVERY_TARGET,
    RECOVERY_TERMINAL_HEADING,
    RECOVERY_TERMINAL_SCHEMA,
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


def _records(evidence: IssueEvidenceStore, heading: str, semantic_id: str):
    return [item for item in evidence.list_records(heading) if item.payload.get("semantic_recovery_id") == semantic_id]


def _existing_unique(evidence: IssueEvidenceStore, heading: str, semantic_id: str):
    records = _records(evidence, heading, semantic_id)
    if len(records) > 1:
        first = records[0].identity
        if any(record.identity != first for record in records[1:]):
            raise QuarantineRecoveryError("conflicting durable quarantine recovery evidence exists")
    return records[0] if records else None


def _persist_exact(
    evidence: IssueEvidenceStore,
    heading: str,
    payload: Mapping[str, object],
    *,
    retry_safe: bool,
):
    semantic_id = str(payload["semantic_recovery_id"])
    expected = evidence_identity(heading, payload)
    existing = _existing_unique(evidence, heading, semantic_id)
    if existing is not None:
        if existing.identity != expected:
            raise QuarantineRecoveryError("semantic recovery id already has different durable evidence")
        return existing
    try:
        return evidence.create(heading, payload)
    except EvidenceWriteAmbiguous as first_error:
        reconciled = _existing_unique(evidence, heading, semantic_id)
        if reconciled is not None:
            if reconciled.identity != expected:
                raise QuarantineRecoveryError("ambiguous recovery evidence write reconciled to different payload")
            return reconciled
        if not retry_safe:
            raise QuarantineRecoveryError("recovery intent write is ambiguous; activation is forbidden") from first_error
        try:
            return evidence.create(heading, payload)
        except EvidenceWriteAmbiguous as second_error:
            reconciled = _existing_unique(evidence, heading, semantic_id)
            if reconciled is not None and reconciled.identity == expected:
                return reconciled
            raise QuarantineRecoveryError("recovery terminal write remains ambiguous after bounded reconciliation") from second_error


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
        script_lines.extend(
            (
                f"if [ -f '{remote}' ]; then printf 'h{index}='; sha256sum '{remote}' | awk '{{print $1}}'; else echo 'h{index}=missing'; fi",
            )
        )
    result = _run_root_script(serial, ("\n".join(script_lines) + "\n").encode("utf-8"), timeout=60)
    if result.status != "completed" or result.returncode != 0 or result.stderr:
        raise PhoneTargetUnavailable("rooted inactive runtime observation failed")
    try:
        lines = result.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PhoneTargetUnavailable("rooted inactive runtime observation is malformed") from exc
    values: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key not in values:
            values[key] = value
    if values.get("target") not in {"present", "absent"} or "current" not in values:
        raise PhoneTargetUnavailable("rooted inactive runtime observation is malformed")
    exists = values["target"] == "present"
    exact = exists
    for index, (_relative, _local, expected) in enumerate(files):
        if values.get(f"h{index}") != expected:
            exact = False
            break
    current_raw = values["current"]
    current = None if current_raw == "absent" else current_raw
    current_managed = isinstance(current, str) and current.startswith(f"{_ROOT}/releases/")
    desired = exact and current == target
    return {
        "mode": "read_only",
        "target_release": target,
        "target_release_exists": exists,
        "inactive_exact_files_verified": exact,
        "required_file_count": len(files),
        "current_target": current,
        "current_managed": current_managed,
        "desired": desired,
    }


def _terminal(
    *, semantic_id: str, execution_id: str, controller_revision: str,
    state: str, mutation_performed: bool, postcondition_verified: bool,
    facts: Mapping[str, object], intent_ref: str | None, reason: str | None,
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
        "recovery_intent_ref": intent_ref,
        "state": state,
        "mutation_performed": mutation_performed,
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
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--product-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.target != RECOVERY_TARGET or args.release != RECOVERY_RELEASE or args.quarantined_request_id != QUARANTINED_REQUEST_ID:
        raise QuarantineRecoveryError("runtime recovery inputs differ from exact authorized quarantine")
    if os.environ.get("GITHUB_SHA") != args.controller_revision:
        raise QuarantineRecoveryError("runtime recovery controller revision differs")

    try:
        admitted = parse_admitted_release(json.loads(args.admitted_release_json), tag=RECOVERY_RELEASE, target=RECOVERY_TARGET)
    except (json.JSONDecodeError, ReleaseAdmissionError) as exc:
        raise QuarantineRecoveryError("hosted immutable Release handoff is invalid") from exc
    if admitted.identity.release_id != RECOVERY_RELEASE_ID:
        raise QuarantineRecoveryError("immutable Release id differs from authorized recovery")

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
        raise QuarantineRecoveryError("original intent differs from immutable Release identity")

    semantic_id = recovery_semantic_id(target=RECOVERY_TARGET, release=RECOVERY_RELEASE, quarantined_request_id=QUARANTINED_REQUEST_ID)
    existing_terminal = _existing_unique(evidence, RECOVERY_TERMINAL_HEADING, semantic_id)
    if existing_terminal is not None:
        validate_recovery_terminal(existing_terminal.payload)
        _write_output(args.output, existing_terminal.payload)
        return 0 if existing_terminal.payload.get("state") == "ACCEPTED" else 2
    existing_intent = _existing_unique(evidence, RECOVERY_INTENT_HEADING, semantic_id)
    if existing_intent is not None:
        validate_recovery_intent(existing_intent.payload)
        raise QuarantineRecoveryError("recovery intent already exists without terminal; activation will not be repeated")

    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
    if not serial or not binding_key:
        raise QuarantineRecoveryError("registered phone target binding is unavailable")

    facts: dict[str, object] = {
        "original_quarantined_terminal_ref": QUARANTINED_TERMINAL_REF,
        "original_quarantined_intent_ref": QUARANTINED_INTENT_REF,
        "vm_provider_access_performed": False,
        "apk_mutation_performed": False,
        "runtime_bytes_rematerialized": False,
    }
    with tempfile.TemporaryDirectory(prefix="mobile-proxy-quarantine-recovery-") as td:
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
            apk_pre = observe(
                serial=serial,
                binding_key=binding_key,
                expected_version_name=str(admitted.android_version_name),
                expected_version_code=int(admitted.android_version_code or 0),
                expected_artifact_sha256=admitted.artifact_transport_sha256,
            )
            runtime_pre = _root_runtime_observation(
                serial=serial,
                release_root=materialized.release_root,
                release_id=RECOVERY_RELEASE,
                required_paths=materialized.required_live_release_paths,
            )
        except (AndroidArtifactRefused, PhoneRuntimeRefused, AndroidObservationUnavailable, PhoneTargetUnavailable) as exc:
            terminal = _terminal(
                semantic_id=semantic_id, execution_id=args.execution_id, controller_revision=args.controller_revision,
                state="REFUSED", mutation_performed=False, postcondition_verified=False,
                facts=facts | {"precondition_available": False}, intent_ref=None, reason=str(exc),
            )
            _persist_exact(evidence, RECOVERY_TERMINAL_HEADING, terminal, retry_safe=True)
            _write_output(args.output, terminal)
            return 2

        facts["precondition"] = {
            "apk": apk_pre.to_dict(),
            "runtime": runtime_pre,
            "exact_release_identity": True,
            "mode": "read_only",
        }
        original_binding = str(original_intent.payload.get("target_binding_id", ""))
        target = str(runtime_pre["target_release"])
        if apk_pre.target_binding_id != original_binding:
            reason = "registered phone target binding differs from original deployment intent"
        elif not apk_pre.desired:
            reason = "installed APK is not exact admitted v0.1.7"
        elif runtime_pre["target_release_exists"] is not True or runtime_pre["inactive_exact_files_verified"] is not True:
            reason = "inactive v0.1.7 rooted runtime is missing or differs from immutable Release"
        elif runtime_pre["current_target"] != target and runtime_pre["current_managed"] is not True:
            reason = "runtime current relation is not an existing managed release"
        else:
            reason = None
        if reason is not None:
            terminal = _terminal(
                semantic_id=semantic_id, execution_id=args.execution_id, controller_revision=args.controller_revision,
                state="REFUSED", mutation_performed=False, postcondition_verified=False,
                facts=facts, intent_ref=None, reason=reason,
            )
            _persist_exact(evidence, RECOVERY_TERMINAL_HEADING, terminal, retry_safe=True)
            _write_output(args.output, terminal)
            return 2

        if runtime_pre["desired"] is True:
            terminal = _terminal(
                semantic_id=semantic_id, execution_id=args.execution_id, controller_revision=args.controller_revision,
                state="ACCEPTED", mutation_performed=False, postcondition_verified=True,
                facts=facts | {"postcondition": facts["precondition"]}, intent_ref=None, reason=None,
            )
            _persist_exact(evidence, RECOVERY_TERMINAL_HEADING, terminal, retry_safe=True)
            _write_output(args.output, terminal)
            return 0

        intent_payload: dict[str, object] = {
            "schema": RECOVERY_INTENT_SCHEMA,
            "semantic_recovery_id": semantic_id,
            "operation": RECOVERY_OPERATION,
            "execution_id": args.execution_id,
            "controller_revision": args.controller_revision,
            "target": RECOVERY_TARGET,
            "target_binding_id": apk_pre.target_binding_id,
            "product_release": RECOVERY_RELEASE,
            "release_id": RECOVERY_RELEASE_ID,
            "quarantined_request_id": QUARANTINED_REQUEST_ID,
            "quarantined_intent_ref": QUARANTINED_INTENT_REF,
            "quarantined_terminal_ref": QUARANTINED_TERMINAL_REF,
            "apk_exact": True,
            "inactive_runtime_exact": True,
            "current_before": runtime_pre["current_target"],
            "activation_may_reach_target": True,
            "blind_retry_allowed": False,
            "mutation_performed": False,
        }
        validate_recovery_intent(intent_payload)
        intent_record = _persist_exact(evidence, RECOVERY_INTENT_HEADING, intent_payload, retry_safe=False)
        facts["recovery_intent_ref"] = intent_record.ref

        try:
            _activate(serial=serial, release_id=RECOVERY_RELEASE, target=target)
        except PhoneTargetUnavailable as exc:
            terminal = _terminal(
                semantic_id=semantic_id, execution_id=args.execution_id, controller_revision=args.controller_revision,
                state="UNKNOWN", mutation_performed=True, postcondition_verified=False,
                facts=facts | {"activation_outcome": "unknown"}, intent_ref=intent_record.ref,
                reason="activation outcome is unknown",
            )
            _persist_exact(evidence, RECOVERY_TERMINAL_HEADING, terminal, retry_safe=True)
            _write_output(args.output, terminal)
            return 2

        facts["activation_outcome"] = "confirmed"
        try:
            apk_post = observe(
                serial=serial,
                binding_key=binding_key,
                expected_version_name=str(admitted.android_version_name),
                expected_version_code=int(admitted.android_version_code or 0),
                expected_artifact_sha256=admitted.artifact_transport_sha256,
            )
            runtime_post = _root_runtime_observation(
                serial=serial,
                release_root=materialized.release_root,
                release_id=RECOVERY_RELEASE,
                required_paths=materialized.required_live_release_paths,
            )
        except (AndroidObservationUnavailable, PhoneTargetUnavailable) as exc:
            terminal = _terminal(
                semantic_id=semantic_id, execution_id=args.execution_id, controller_revision=args.controller_revision,
                state="UNKNOWN", mutation_performed=True, postcondition_verified=False,
                facts=facts | {"postcondition_available": False}, intent_ref=intent_record.ref,
                reason="post-activation observation unavailable",
            )
            _persist_exact(evidence, RECOVERY_TERMINAL_HEADING, terminal, retry_safe=True)
            _write_output(args.output, terminal)
            return 2

        facts["postcondition"] = {
            "apk": apk_post.to_dict(),
            "runtime": runtime_post,
            "target_binding_matches_original_intent": apk_post.target_binding_id == original_binding,
            "mode": "read_only",
        }
        desired = bool(
            apk_post.desired
            and apk_post.target_binding_id == original_binding
            and runtime_post["desired"] is True
            and runtime_post["inactive_exact_files_verified"] is True
        )
        terminal = _terminal(
            semantic_id=semantic_id, execution_id=args.execution_id, controller_revision=args.controller_revision,
            state="ACCEPTED" if desired else "QUARANTINED", mutation_performed=True,
            postcondition_verified=True, facts=facts, intent_ref=intent_record.ref,
            reason=None if desired else "recovery postcondition mismatch",
        )
        _persist_exact(evidence, RECOVERY_TERMINAL_HEADING, terminal, retry_safe=True)
        _write_output(args.output, terminal)
        return 0 if desired else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QuarantineRecoveryError, EvidenceError, json.JSONDecodeError) as exc:
        print(f"QUARANTINE_RECOVERY_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
