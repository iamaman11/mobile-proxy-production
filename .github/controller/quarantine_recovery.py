from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

RECOVERY_INTENT_HEADING = "## QUARANTINE RECOVERY INTENT V1"
RECOVERY_TERMINAL_HEADING = "## QUARANTINE RECOVERY TERMINAL V1"
RECOVERY_SCHEMA = "production-quarantine-recovery.v1"
RECOVERY_INTENT_SCHEMA = "production-quarantine-recovery-intent.v1"
RECOVERY_TERMINAL_SCHEMA = "production-quarantine-recovery-terminal.v1"
RECOVERY_OPERATION = "recover-quarantined-product-release"
RECOVERY_TARGET = "phone-production"

_ROOT = "/data/adb/mobile-proxy-node"
_SHA = re.compile(r"[0-9a-f]{40}")
_SEMVER = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
_REQUEST = re.compile(r"req-sha256:[0-9a-f]{64}")
_RECOVERY_ID = re.compile(r"recovery-sha256:[0-9a-f]{64}")
_EXECUTION = re.compile(r"gh-run:[1-9][0-9]*:[1-9][0-9]*")
_EVIDENCE_REF = re.compile(r"issue-comment:[1-9][0-9]*")
_BINDING = re.compile(r"tb-hmac-sha256:[0-9a-f]{64}")
_ALLOWED_STATES = frozenset({"ACCEPTED", "REFUSED", "UNKNOWN", "QUARANTINED"})


class QuarantineRecoveryError(RuntimeError):
    pass


def _require_release(release: str) -> str:
    if _SEMVER.fullmatch(release) is None:
        raise QuarantineRecoveryError("recovery Product Release tag is invalid")
    return release


def _require_request(request_id: str) -> str:
    if _REQUEST.fullmatch(request_id) is None:
        raise QuarantineRecoveryError("recovery quarantined request id is invalid")
    return request_id


def _require_ref(value: str, *, label: str) -> str:
    if _EVIDENCE_REF.fullmatch(value) is None:
        raise QuarantineRecoveryError(f"recovery {label} evidence ref is invalid")
    return value


def managed_release_tag(current_target: object) -> str | None:
    if not isinstance(current_target, str):
        return None
    prefix = f"{_ROOT}/releases/"
    if not current_target.startswith(prefix):
        return None
    release = current_target[len(prefix):]
    return release if _SEMVER.fullmatch(release) is not None else None


def recovery_semantic_id(
    *,
    target: str,
    release: str,
    quarantined_request_id: str,
    quarantined_terminal_ref: str,
    parent_recovery_terminal_ref: str,
) -> str:
    if target != RECOVERY_TARGET:
        raise QuarantineRecoveryError("recovery target differs from phone-production")
    _require_release(release)
    _require_request(quarantined_request_id)
    _require_ref(quarantined_terminal_ref, label="quarantined terminal")
    _require_ref(parent_recovery_terminal_ref, label="parent terminal")
    identity = {
        "schema": RECOVERY_SCHEMA,
        "operation": RECOVERY_OPERATION,
        "target": target,
        "product_release": release,
        "quarantined_request_id": quarantined_request_id,
        "quarantined_terminal_ref": quarantined_terminal_ref,
        "parent_recovery_terminal_ref": parent_recovery_terminal_ref,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "recovery-sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_semantic_identity(payload: Mapping[str, object], *, kind: str) -> None:
    semantic_id = str(payload.get("semantic_recovery_id", ""))
    if _RECOVERY_ID.fullmatch(semantic_id) is None:
        raise QuarantineRecoveryError(f"recovery {kind} semantic id is invalid")
    expected = recovery_semantic_id(
        target=str(payload.get("target", "")),
        release=str(payload.get("product_release", "")),
        quarantined_request_id=str(payload.get("quarantined_request_id", "")),
        quarantined_terminal_ref=str(payload.get("quarantined_terminal_ref", "")),
        parent_recovery_terminal_ref=str(payload.get("parent_recovery_terminal_ref", "")),
    )
    if semantic_id != expected:
        raise QuarantineRecoveryError(f"recovery {kind} semantic identity differs from its durable lineage")


def validate_quarantined_deployment_intent(
    payload: Mapping[str, object],
    *,
    target: str,
    release: str,
    request_id: str,
    release_id: int,
) -> None:
    if payload.get("schema") != "production-deployment-intent.v2":
        raise QuarantineRecoveryError("quarantined deployment intent schema differs")
    if target != RECOVERY_TARGET or payload.get("target") != target:
        raise QuarantineRecoveryError("quarantined deployment intent target differs")
    _require_release(release)
    _require_request(request_id)
    if payload.get("semantic_request_id") != request_id or payload.get("product_release") != release:
        raise QuarantineRecoveryError("quarantined deployment intent identity differs")
    if not isinstance(release_id, int) or release_id <= 0 or payload.get("release_id") != release_id:
        raise QuarantineRecoveryError("quarantined deployment intent Release id differs")
    if _BINDING.fullmatch(str(payload.get("target_binding_id", ""))) is None:
        raise QuarantineRecoveryError("quarantined deployment intent target binding is invalid")
    if payload.get("blind_retry_allowed") is not False or payload.get("dispatch_may_reach_target") is not True:
        raise QuarantineRecoveryError("quarantined deployment intent dispatch boundary differs")
    if payload.get("mutation_performed") is not False:
        raise QuarantineRecoveryError("quarantined deployment intent cannot claim mutation")
    physical = payload.get("physical_domains")
    if not isinstance(physical, Mapping) or physical.get("runtime") is not True:
        raise QuarantineRecoveryError("quarantined deployment intent does not own runtime mutation")


def quarantined_current_release(payload: Mapping[str, object], *, release: str) -> str:
    _require_release(release)
    facts = payload.get("facts")
    recovery = facts.get("recovery_observation") if isinstance(facts, Mapping) else None
    runtime = recovery.get("runtime") if isinstance(recovery, Mapping) else None
    apk = recovery.get("apk") if isinstance(recovery, Mapping) else None
    if not isinstance(runtime, Mapping) or not isinstance(apk, Mapping):
        raise QuarantineRecoveryError("quarantined deployment recovery observation is incomplete")
    if apk.get("desired") is not True or apk.get("exact_artifact_verified") is not True:
        raise QuarantineRecoveryError("quarantined deployment does not prove exact installed APK")
    expected_target = f"{_ROOT}/releases/{release}"
    if (
        runtime.get("target_release") != expected_target
        or runtime.get("target_release_exists") is not True
        or runtime.get("desired") is not False
        or runtime.get("admissible_for_new_dispatch") is not False
    ):
        raise QuarantineRecoveryError("quarantined deployment runtime shape is not activation-recoverable")
    current_release = managed_release_tag(runtime.get("current_target"))
    if current_release is None or current_release == release:
        raise QuarantineRecoveryError("quarantined deployment current runtime is not a distinct managed release")
    return current_release


def validate_quarantined_deployment_terminal(
    payload: Mapping[str, object],
    *,
    target: str,
    release: str,
    request_id: str,
    release_id: int,
) -> None:
    if payload.get("schema") != "production-deployment-terminal.v2":
        raise QuarantineRecoveryError("quarantined deployment terminal schema differs")
    if target != RECOVERY_TARGET or payload.get("target") != target:
        raise QuarantineRecoveryError("quarantined deployment terminal target differs")
    _require_release(release)
    _require_request(request_id)
    if payload.get("semantic_request_id") != request_id or payload.get("product_release") != release:
        raise QuarantineRecoveryError("quarantined deployment terminal identity differs")
    if not isinstance(release_id, int) or release_id <= 0 or payload.get("release_id") != release_id:
        raise QuarantineRecoveryError("quarantined deployment terminal Release id differs")
    if (
        payload.get("state") != "QUARANTINED"
        or payload.get("mutation_performed") is not True
        or payload.get("postcondition_verified") is not True
    ):
        raise QuarantineRecoveryError("deployment terminal is not a verified quarantined mutation")
    if payload.get("next_allowed_operation") != "read-only-observation-or-approved-recovery":
        raise QuarantineRecoveryError("quarantined deployment does not authorize bounded recovery")
    quarantined_current_release(payload, release=release)


def validate_recovery_intent(payload: Mapping[str, object]) -> None:
    if payload.get("schema") != RECOVERY_INTENT_SCHEMA:
        raise QuarantineRecoveryError("recovery intent schema differs")
    if payload.get("operation") != RECOVERY_OPERATION or payload.get("target") != RECOVERY_TARGET:
        raise QuarantineRecoveryError("recovery intent operation/target differs")
    _require_release(str(payload.get("product_release", "")))
    _require_request(str(payload.get("quarantined_request_id", "")))
    if not isinstance(payload.get("release_id"), int) or int(payload["release_id"]) <= 0:
        raise QuarantineRecoveryError("recovery intent Release id is invalid")
    _require_ref(str(payload.get("quarantined_intent_ref", "")), label="quarantined intent")
    _require_ref(str(payload.get("quarantined_terminal_ref", "")), label="quarantined terminal")
    _require_ref(str(payload.get("parent_recovery_terminal_ref", "")), label="parent terminal")
    _validate_semantic_identity(payload, kind="intent")
    if _EXECUTION.fullmatch(str(payload.get("execution_id", ""))) is None or _SHA.fullmatch(str(payload.get("controller_revision", ""))) is None:
        raise QuarantineRecoveryError("recovery intent execution provenance is invalid")
    if _BINDING.fullmatch(str(payload.get("target_binding_id", ""))) is None:
        raise QuarantineRecoveryError("recovery intent target binding is invalid")
    if payload.get("apk_exact") is not True or payload.get("inactive_runtime_exact") is not True:
        raise QuarantineRecoveryError("recovery intent lacks exact precondition proof")
    current_before = str(payload.get("current_before_release", ""))
    _require_release(current_before)
    if current_before == payload.get("product_release"):
        raise QuarantineRecoveryError("recovery intent current runtime already equals target release")
    if payload.get("activation_may_reach_target") is not True or payload.get("blind_retry_allowed") is not False:
        raise QuarantineRecoveryError("recovery intent lacks exactly-once/no-blind-retry boundary")
    if payload.get("mutation_performed") is not False:
        raise QuarantineRecoveryError("recovery intent cannot claim mutation")


def validate_recovery_terminal(payload: Mapping[str, object]) -> None:
    if payload.get("schema") != RECOVERY_TERMINAL_SCHEMA:
        raise QuarantineRecoveryError("recovery terminal schema differs")
    if payload.get("operation") != RECOVERY_OPERATION or payload.get("target") != RECOVERY_TARGET:
        raise QuarantineRecoveryError("recovery terminal operation/target differs")
    _require_release(str(payload.get("product_release", "")))
    _require_request(str(payload.get("quarantined_request_id", "")))
    if not isinstance(payload.get("release_id"), int) or int(payload["release_id"]) <= 0:
        raise QuarantineRecoveryError("recovery terminal Release id is invalid")
    _require_ref(str(payload.get("quarantined_terminal_ref", "")), label="quarantined terminal")
    _require_ref(str(payload.get("parent_recovery_terminal_ref", "")), label="parent terminal")
    _validate_semantic_identity(payload, kind="terminal")
    if _EXECUTION.fullmatch(str(payload.get("execution_id", ""))) is None or _SHA.fullmatch(str(payload.get("controller_revision", ""))) is None:
        raise QuarantineRecoveryError("recovery terminal execution provenance is invalid")
    state = payload.get("state")
    if state not in _ALLOWED_STATES:
        raise QuarantineRecoveryError("recovery terminal state is invalid")
    mutation_performed = payload.get("mutation_performed")
    postcondition_verified = payload.get("postcondition_verified")
    if not isinstance(mutation_performed, bool) or not isinstance(postcondition_verified, bool):
        raise QuarantineRecoveryError("recovery terminal boolean contract differs")
    if payload.get("blind_retry_allowed") is not False:
        raise QuarantineRecoveryError("recovery terminal blind-retry boundary differs")
    if state == "ACCEPTED" and (mutation_performed is not True or postcondition_verified is not True):
        raise QuarantineRecoveryError("ACCEPTED recovery requires verified activation postcondition")
    if state == "REFUSED" and mutation_performed is not False:
        raise QuarantineRecoveryError("REFUSED recovery cannot follow activation attempt")
    if state == "UNKNOWN" and (mutation_performed is not True or postcondition_verified is not False):
        raise QuarantineRecoveryError("UNKNOWN recovery must follow ambiguous activation without verified postcondition")
    if state == "QUARANTINED" and (mutation_performed is not True or postcondition_verified is not True):
        raise QuarantineRecoveryError("QUARANTINED recovery requires observed post-activation state")
    if not isinstance(payload.get("facts"), Mapping):
        raise QuarantineRecoveryError("recovery terminal facts are invalid")
