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

from android_target import AndroidObservationUnavailable, observe  # noqa: E402
from durable_release_identity import durable_release_identity, payload_matches_release_identity  # noqa: E402
from evidence_store import EvidenceError, EvidenceWriteAmbiguous, IssueEvidenceStore, evidence_identity  # noqa: E402
from phone_release_state import prepare_verified_release_runtime  # noqa: E402
from phone_runtime import PhoneRuntimeRefused  # noqa: E402
from phone_target import PhoneTargetMutationOutcomeUnknown, PhoneTargetUnavailable, _activate  # noqa: E402
from quarantine_phone_observer import observe_exact_inactive_runtime  # noqa: E402
from quarantine_recovery import (  # noqa: E402
    RECOVERY_INTENT_HEADING,
    RECOVERY_INTENT_SCHEMA,
    RECOVERY_OPERATION,
    RECOVERY_TARGET,
    RECOVERY_TERMINAL_HEADING,
    RECOVERY_TERMINAL_SCHEMA,
    QuarantineRecoveryError,
    quarantined_current_release,
    recovery_semantic_id,
    validate_quarantined_deployment_intent,
    validate_quarantined_deployment_terminal,
    validate_recovery_intent,
    validate_recovery_terminal,
)
from release_handoff import parse_admitted_release  # noqa: E402
from release_resolver import ReleaseAdmissionError  # noqa: E402
from runtime_operational_observer import (  # noqa: E402
    RuntimeOperationalObservationUnavailable,
    RuntimeOperationalOutputValidationFailure,
    observe_runtime_operational_health,
)
from terminal_result import TerminalContractError, validate_terminal  # noqa: E402

_ROOT = "/data/adb/mobile-proxy-node"
_TERMINAL_TOP_LEVEL_RELEASE_FIELDS = (
    "target",
    "product_release",
    "release_id",
    "release_source_sha",
    "artifact_digest",
)


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


def _bounded_runtime(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "target_release_exists": value.get("target_release_exists") is True,
        "inactive_exact_files_verified": value.get("inactive_exact_files_verified") is True,
        "required_file_count": value.get("required_file_count"),
        "mismatch_count": value.get("mismatch_count"),
        "current_relation": value.get("current_relation"),
        "current_release_tag": value.get("current_release_tag"),
        "desired": value.get("desired") is True,
        "mode": value.get("mode"),
        "raw_current_target_path_recorded": False,
        "raw_runtime_release_paths_recorded": False,
        "expected_file_digests_recorded": False,
        "observed_file_digests_recorded": False,
    }


def _bounded_apk(value: object) -> dict[str, object]:
    raw = value.to_dict()
    return {
        "package_name": raw.get("package_name"),
        "installed": raw.get("installed"),
        "version_name": raw.get("version_name"),
        "version_code": raw.get("version_code"),
        "exact_artifact_verified": raw.get("exact_artifact_verified"),
        "desired": raw.get("desired"),
        "mode": raw.get("mode"),
        "raw_device_identifier_recorded": False,
        "artifact_digest_recorded": False,
        "target_binding_id_recorded": False,
    }


def _terminal(
    *,
    semantic_id: str,
    execution_id: str,
    controller_revision: str,
    target: str,
    release: str,
    release_id: int,
    quarantined_request_id: str,
    quarantined_terminal_ref: str,
    parent_terminal_ref: str,
    state: str,
    mutation_performed: bool,
    postcondition_verified: bool,
    facts: Mapping[str, object],
    intent_ref: str | None,
    reason: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": RECOVERY_TERMINAL_SCHEMA,
        "semantic_recovery_id": semantic_id,
        "operation": RECOVERY_OPERATION,
        "execution_id": execution_id,
        "controller_revision": controller_revision,
        "target": target,
        "product_release": release,
        "release_id": release_id,
        "quarantined_request_id": quarantined_request_id,
        "quarantined_terminal_ref": quarantined_terminal_ref,
        "parent_recovery_terminal_ref": parent_terminal_ref,
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


def _persist_terminal(
    *,
    evidence: IssueEvidenceStore,
    output: Path,
    payload: Mapping[str, object],
) -> int:
    _persist_exact(evidence, RECOVERY_TERMINAL_HEADING, payload, retry_safe=True)
    _write_output(output, payload)
    return 0 if payload.get("state") == "ACCEPTED" else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--quarantined-request-id", required=True)
    parser.add_argument("--quarantined-intent-ref", required=True)
    parser.add_argument("--quarantined-terminal-ref", required=True)
    parser.add_argument("--recovery-parent-terminal-ref", required=True)
    parser.add_argument("--admitted-release-json", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--product-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.target != RECOVERY_TARGET:
        raise QuarantineRecoveryError("runtime recovery target differs from phone-production")
    if os.environ.get("GITHUB_SHA") != args.controller_revision:
        raise QuarantineRecoveryError("runtime recovery controller revision differs")
    if args.recovery_parent_terminal_ref != args.quarantined_terminal_ref:
        raise QuarantineRecoveryError("first bounded recovery parent must be the quarantined deployment terminal")

    try:
        admitted = parse_admitted_release(json.loads(args.admitted_release_json), tag=args.release, target=args.target)
    except (json.JSONDecodeError, ReleaseAdmissionError) as exc:
        raise QuarantineRecoveryError("hosted immutable Product Release handoff is invalid") from exc
    release_id = admitted.identity.release_id
    if not isinstance(release_id, int) or release_id <= 0:
        raise QuarantineRecoveryError("immutable Product Release id is unavailable")

    evidence = IssueEvidenceStore(os.environ.get("GITHUB_TOKEN", ""))
    original_intent, original_terminal = evidence.request_history(args.quarantined_request_id)
    if original_intent is None or original_intent.ref != args.quarantined_intent_ref:
        raise QuarantineRecoveryError("exact quarantined deployment intent changed under target lock")
    if original_terminal is None or original_terminal.ref != args.quarantined_terminal_ref:
        raise QuarantineRecoveryError("exact quarantined deployment terminal changed under target lock")

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
        raise QuarantineRecoveryError("quarantined deployment intent differs from immutable Product Release identity")
    if not _terminal_matches_release_identity(original_terminal.payload, admitted.identity, target=args.target):
        raise QuarantineRecoveryError("quarantined deployment terminal differs from immutable Product Release identity")

    expected_current_release = quarantined_current_release(original_terminal.payload, release=args.release)
    semantic_id = recovery_semantic_id(
        target=args.target,
        release=args.release,
        quarantined_request_id=args.quarantined_request_id,
        quarantined_terminal_ref=args.quarantined_terminal_ref,
        parent_recovery_terminal_ref=args.recovery_parent_terminal_ref,
    )
    if _existing_unique(evidence, RECOVERY_INTENT_HEADING, semantic_id) is not None:
        raise QuarantineRecoveryError("recovery intent already exists; activation will not be repeated")
    if _existing_unique(evidence, RECOVERY_TERMINAL_HEADING, semantic_id) is not None:
        raise QuarantineRecoveryError("recovery terminal already exists; duplicate execution is forbidden")

    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
    admin_token = os.environ.get("MOBILE_PROXY_ADMIN_TOKEN", "")
    if not serial or len(binding_key) < 32 or not admin_token:
        raise QuarantineRecoveryError("registered phone recovery binding is unavailable")

    facts: dict[str, object] = {
        "quarantined_intent_ref": args.quarantined_intent_ref,
        "quarantined_terminal_ref": args.quarantined_terminal_ref,
        "parent_recovery_terminal_ref": args.recovery_parent_terminal_ref,
        "expected_runtime_prepared_locally": False,
        "phone_runtime_bytes_rematerialized": False,
        "apk_mutation_performed": False,
        "provider_access_performed": False,
        "provider_mutation_performed": False,
        "vm_access_performed": False,
        "vm_mutation_performed": False,
        "blind_retry_performed": False,
    }

    with tempfile.TemporaryDirectory(prefix="mobile-proxy-quarantine-recovery-") as td:
        root = Path(td)
        materialization_facts: dict[str, object] = {}
        try:
            materialized = prepare_verified_release_runtime(
                admitted,
                archive=root / "phone-runtime.tar.gz",
                work_root=root / "runtime",
                product_root=args.product_root,
                runtime_manifest_path=args.runtime_manifest,
                binding_key=binding_key,
                facts=materialization_facts,
            )
            facts["expected_runtime_prepared_locally"] = True
            apk_pre = observe(
                serial=serial,
                binding_key=binding_key,
                expected_version_name=str(admitted.android_version_name),
                expected_version_code=int(admitted.android_version_code or 0),
                expected_artifact_sha256=admitted.artifact_transport_sha256,
            )
            runtime_pre = observe_exact_inactive_runtime(
                serial=serial,
                release_root=materialized.release_root,
                release_id=args.release,
                required_paths=materialized.required_live_release_paths,
            )
        except (PhoneRuntimeRefused, AndroidObservationUnavailable, PhoneTargetUnavailable):
            terminal = _terminal(
                semantic_id=semantic_id,
                execution_id=args.execution_id,
                controller_revision=args.controller_revision,
                target=args.target,
                release=args.release,
                release_id=release_id,
                quarantined_request_id=args.quarantined_request_id,
                quarantined_terminal_ref=args.quarantined_terminal_ref,
                parent_terminal_ref=args.recovery_parent_terminal_ref,
                state="REFUSED",
                mutation_performed=False,
                postcondition_verified=False,
                facts=facts | {"precondition_available": False},
                intent_ref=None,
                reason="PRECONDITION_OBSERVATION_UNAVAILABLE",
            )
            return _persist_terminal(evidence=evidence, output=args.output, payload=terminal)

        original_binding = str(original_intent.payload.get("target_binding_id", ""))
        precondition = {
            "apk": _bounded_apk(apk_pre),
            "runtime": _bounded_runtime(runtime_pre),
            "target_binding_matches_original_intent": apk_pre.target_binding_id == original_binding,
            "current_release_matches_quarantined_terminal": runtime_pre.get("current_release_tag") == expected_current_release,
            "mode": "read_only",
        }
        facts["precondition"] = precondition

        if apk_pre.target_binding_id != original_binding:
            reason = "TARGET_BINDING_CHANGED"
        elif not apk_pre.desired or not apk_pre.exact_artifact_verified:
            reason = "APK_NOT_EXACT"
        elif runtime_pre.get("target_release_exists") is not True or runtime_pre.get("inactive_exact_files_verified") is not True:
            reason = "INACTIVE_RUNTIME_NOT_EXACT"
        elif runtime_pre.get("current_release_tag") != expected_current_release:
            reason = "CURRENT_RUNTIME_CHANGED"
        elif runtime_pre.get("current_relation") != "other-managed":
            reason = "CURRENT_RUNTIME_RELATION_INVALID"
        else:
            reason = None

        if reason is not None:
            terminal = _terminal(
                semantic_id=semantic_id,
                execution_id=args.execution_id,
                controller_revision=args.controller_revision,
                target=args.target,
                release=args.release,
                release_id=release_id,
                quarantined_request_id=args.quarantined_request_id,
                quarantined_terminal_ref=args.quarantined_terminal_ref,
                parent_terminal_ref=args.recovery_parent_terminal_ref,
                state="REFUSED",
                mutation_performed=False,
                postcondition_verified=False,
                facts=facts,
                intent_ref=None,
                reason=reason,
            )
            return _persist_terminal(evidence=evidence, output=args.output, payload=terminal)

        intent_payload: dict[str, object] = {
            "schema": RECOVERY_INTENT_SCHEMA,
            "semantic_recovery_id": semantic_id,
            "operation": RECOVERY_OPERATION,
            "execution_id": args.execution_id,
            "controller_revision": args.controller_revision,
            "target": args.target,
            "target_binding_id": apk_pre.target_binding_id,
            "product_release": args.release,
            "release_id": release_id,
            "quarantined_request_id": args.quarantined_request_id,
            "quarantined_intent_ref": args.quarantined_intent_ref,
            "quarantined_terminal_ref": args.quarantined_terminal_ref,
            "parent_recovery_terminal_ref": args.recovery_parent_terminal_ref,
            "apk_exact": True,
            "inactive_runtime_exact": True,
            "current_before_release": expected_current_release,
            "activation_may_reach_target": True,
            "blind_retry_allowed": False,
            "mutation_performed": False,
        }
        validate_recovery_intent(intent_payload)
        intent_record = _persist_exact(evidence, RECOVERY_INTENT_HEADING, intent_payload, retry_safe=False)
        facts["recovery_intent_ref"] = intent_record.ref

        target_path = f"{_ROOT}/releases/{args.release}"
        activation_state = "confirmed"
        try:
            _activate(serial=serial, release_id=args.release, target=target_path)
        except PhoneTargetMutationOutcomeUnknown:
            terminal = _terminal(
                semantic_id=semantic_id,
                execution_id=args.execution_id,
                controller_revision=args.controller_revision,
                target=args.target,
                release=args.release,
                release_id=release_id,
                quarantined_request_id=args.quarantined_request_id,
                quarantined_terminal_ref=args.quarantined_terminal_ref,
                parent_terminal_ref=args.recovery_parent_terminal_ref,
                state="UNKNOWN",
                mutation_performed=True,
                postcondition_verified=False,
                facts=facts | {"activation_outcome": "unknown"},
                intent_ref=intent_record.ref,
                reason="ACTIVATION_OUTCOME_UNKNOWN",
            )
            return _persist_terminal(evidence=evidence, output=args.output, payload=terminal)
        except PhoneTargetUnavailable:
            activation_state = "completed_failure"
        facts["activation_outcome"] = activation_state

        try:
            apk_post = observe(
                serial=serial,
                binding_key=binding_key,
                expected_version_name=str(admitted.android_version_name),
                expected_version_code=int(admitted.android_version_code or 0),
                expected_artifact_sha256=admitted.artifact_transport_sha256,
            )
            runtime_post = observe_exact_inactive_runtime(
                serial=serial,
                release_root=materialized.release_root,
                release_id=args.release,
                required_paths=materialized.required_live_release_paths,
            )
            operational_post = observe_runtime_operational_health(serial, admin_token=admin_token)
        except (
            AndroidObservationUnavailable,
            PhoneTargetUnavailable,
            RuntimeOperationalObservationUnavailable,
            RuntimeOperationalOutputValidationFailure,
        ):
            terminal = _terminal(
                semantic_id=semantic_id,
                execution_id=args.execution_id,
                controller_revision=args.controller_revision,
                target=args.target,
                release=args.release,
                release_id=release_id,
                quarantined_request_id=args.quarantined_request_id,
                quarantined_terminal_ref=args.quarantined_terminal_ref,
                parent_terminal_ref=args.recovery_parent_terminal_ref,
                state="UNKNOWN",
                mutation_performed=True,
                postcondition_verified=False,
                facts=facts | {"postcondition_available": False},
                intent_ref=intent_record.ref,
                reason="POSTCONDITION_OBSERVATION_UNAVAILABLE",
            )
            return _persist_terminal(evidence=evidence, output=args.output, payload=terminal)

        exact_release_desired = bool(
            apk_post.desired
            and apk_post.exact_artifact_verified
            and apk_post.target_binding_id == original_binding
            and runtime_post.get("desired") is True
            and runtime_post.get("inactive_exact_files_verified") is True
            and runtime_post.get("current_release_tag") == args.release
        )
        operational_desired = bool(operational_post.desired)
        facts["postcondition"] = {
            "apk": _bounded_apk(apk_post),
            "runtime": _bounded_runtime(runtime_post),
            "target_binding_matches_original_intent": apk_post.target_binding_id == original_binding,
            "exact_release_desired": exact_release_desired,
            "operational_desired": operational_desired,
            "operational_mode": "read_only",
        }

        accepted = activation_state == "confirmed" and exact_release_desired and operational_desired
        if accepted:
            reason = None
        elif activation_state == "completed_failure":
            reason = "ACTIVATION_COMPLETED_FAILURE"
        elif not exact_release_desired:
            reason = "EXACT_RELEASE_POSTCONDITION_MISMATCH"
        else:
            reason = "OPERATIONAL_POSTCONDITION_NOT_READY"
        terminal = _terminal(
            semantic_id=semantic_id,
            execution_id=args.execution_id,
            controller_revision=args.controller_revision,
            target=args.target,
            release=args.release,
            release_id=release_id,
            quarantined_request_id=args.quarantined_request_id,
            quarantined_terminal_ref=args.quarantined_terminal_ref,
            parent_terminal_ref=args.recovery_parent_terminal_ref,
            state="ACCEPTED" if accepted else "QUARANTINED",
            mutation_performed=True,
            postcondition_verified=True,
            facts=facts,
            intent_ref=intent_record.ref,
            reason=reason,
        )
        return _persist_terminal(evidence=evidence, output=args.output, payload=terminal)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QuarantineRecoveryError, EvidenceError, json.JSONDecodeError) as exc:
        print(f"QUARANTINE_RECOVERY_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
