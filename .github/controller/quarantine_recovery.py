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
RECOVERY_RELEASE = "v0.1.7"
RECOVERY_RELEASE_ID = 383454833
QUARANTINED_REQUEST_ID = "req-sha256:74489a27b4c845b9060056af498090beded81db009e05f0290091af846c4e5d7"
QUARANTINED_INTENT_REF = "issue-comment:5562280938"
QUARANTINED_TERMINAL_REF = "issue-comment:5562289272"
RECOVERY_PARENT_INTENT_REF = "issue-comment:5562604412"
RECOVERY_PARENT_TERMINAL_REF = "issue-comment:5562605748"
RECOVERY_UNKNOWN_INTENT_REF = "issue-comment:5562924241"
RECOVERY_UNKNOWN_TERMINAL_REF = "issue-comment:5562925687"
_ALLOWED_PARENT_RECOVERY_TERMINAL_REFS = frozenset({
    RECOVERY_PARENT_TERMINAL_REF,
    RECOVERY_UNKNOWN_TERMINAL_REF,
})

_SHA = re.compile(r"[0-9a-f]{40}")
_RECOVERY_ID = re.compile(r"recovery-sha256:[0-9a-f]{64}")
_EXECUTION = re.compile(r"gh-run:[1-9][0-9]*:[1-9][0-9]*")
_ALLOWED_STATES = frozenset({"ACCEPTED", "REFUSED", "UNKNOWN", "QUARANTINED"})


class QuarantineRecoveryError(RuntimeError):
    pass


def recovery_semantic_id(
    *,
    target: str,
    release: str,
    quarantined_request_id: str,
    parent_recovery_terminal_ref: str | None = None,
) -> str:
    if target != RECOVERY_TARGET or release != RECOVERY_RELEASE or quarantined_request_id != QUARANTINED_REQUEST_ID:
        raise QuarantineRecoveryError("recovery identity differs from authorized quarantined v0.1.7 state")
    if parent_recovery_terminal_ref is not None and parent_recovery_terminal_ref not in _ALLOWED_PARENT_RECOVERY_TERMINAL_REFS:
        raise QuarantineRecoveryError("recovery parent terminal differs from the bounded Stage 3 continuation")
    identity: dict[str, object] = {
        "schema": RECOVERY_SCHEMA,
        "operation": RECOVERY_OPERATION,
        "target": target,
        "product_release": release,
        "quarantined_request_id": quarantined_request_id,
        "quarantined_terminal_ref": QUARANTINED_TERMINAL_REF,
    }
    if parent_recovery_terminal_ref is not None:
        identity["parent_recovery_terminal_ref"] = parent_recovery_terminal_ref
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "recovery-sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_semantic_identity(payload: Mapping[str, object], *, kind: str) -> None:
    semantic_id = str(payload.get("semantic_recovery_id", ""))
    if _RECOVERY_ID.fullmatch(semantic_id) is None:
        raise QuarantineRecoveryError(f"recovery {kind} semantic id is invalid")
    raw_parent = payload.get("parent_recovery_terminal_ref")
    if raw_parent is not None and not isinstance(raw_parent, str):
        raise QuarantineRecoveryError(f"recovery {kind} parent terminal is invalid")
    expected = recovery_semantic_id(
        target=str(payload.get("target", "")),
        release=str(payload.get("product_release", "")),
        quarantined_request_id=str(payload.get("quarantined_request_id", "")),
        parent_recovery_terminal_ref=raw_parent,
    )
    if semantic_id != expected:
        raise QuarantineRecoveryError(f"recovery {kind} semantic identity differs from its lineage")


def validate_recovery_intent(payload: Mapping[str, object]) -> None:
    if payload.get("schema") != RECOVERY_INTENT_SCHEMA:
        raise QuarantineRecoveryError("recovery intent schema differs")
    if payload.get("operation") != RECOVERY_OPERATION or payload.get("target") != RECOVERY_TARGET:
        raise QuarantineRecoveryError("recovery intent operation/target differs")
    if payload.get("product_release") != RECOVERY_RELEASE or payload.get("release_id") != RECOVERY_RELEASE_ID:
        raise QuarantineRecoveryError("recovery intent Release identity differs")
    if payload.get("quarantined_request_id") != QUARANTINED_REQUEST_ID:
        raise QuarantineRecoveryError("recovery intent parent request differs")
    if payload.get("quarantined_intent_ref") != QUARANTINED_INTENT_REF or payload.get("quarantined_terminal_ref") != QUARANTINED_TERMINAL_REF:
        raise QuarantineRecoveryError("recovery intent parent evidence differs")
    _validate_semantic_identity(payload, kind="intent")
    if _EXECUTION.fullmatch(str(payload.get("execution_id", ""))) is None or _SHA.fullmatch(str(payload.get("controller_revision", ""))) is None:
        raise QuarantineRecoveryError("recovery intent execution provenance is invalid")
    if not str(payload.get("target_binding_id", "")).startswith("tb-hmac-sha256:"):
        raise QuarantineRecoveryError("recovery intent target binding is unavailable")
    if payload.get("inactive_runtime_exact") is not True or payload.get("apk_exact") is not True:
        raise QuarantineRecoveryError("recovery intent lacks exact precondition proof")
    if payload.get("activation_may_reach_target") is not True or payload.get("blind_retry_allowed") is not False:
        raise QuarantineRecoveryError("recovery intent lacks exactly-once/no-blind-retry boundary")
    if payload.get("mutation_performed") is not False:
        raise QuarantineRecoveryError("recovery intent cannot claim mutation")


def validate_recovery_terminal(payload: Mapping[str, object]) -> None:
    if payload.get("schema") != RECOVERY_TERMINAL_SCHEMA:
        raise QuarantineRecoveryError("recovery terminal schema differs")
    if payload.get("operation") != RECOVERY_OPERATION or payload.get("target") != RECOVERY_TARGET:
        raise QuarantineRecoveryError("recovery terminal operation/target differs")
    if payload.get("product_release") != RECOVERY_RELEASE or payload.get("release_id") != RECOVERY_RELEASE_ID:
        raise QuarantineRecoveryError("recovery terminal Release identity differs")
    if payload.get("quarantined_request_id") != QUARANTINED_REQUEST_ID or payload.get("quarantined_terminal_ref") != QUARANTINED_TERMINAL_REF:
        raise QuarantineRecoveryError("recovery terminal parent evidence differs")
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
    if state == "ACCEPTED" and postcondition_verified is not True:
        raise QuarantineRecoveryError("ACCEPTED recovery lacks verified postcondition")
    if state == "REFUSED" and mutation_performed is not False:
        raise QuarantineRecoveryError("REFUSED recovery cannot follow mutation")
    if state == "UNKNOWN" and mutation_performed is not True:
        raise QuarantineRecoveryError("UNKNOWN recovery must follow activation attempt")
    if state == "QUARANTINED" and postcondition_verified is not True:
        raise QuarantineRecoveryError("QUARANTINED recovery lacks observed postcondition")
    facts = payload.get("facts")
    if not isinstance(facts, Mapping):
        raise QuarantineRecoveryError("recovery terminal facts are invalid")
