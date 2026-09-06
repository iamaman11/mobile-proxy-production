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

_SHA = re.compile(r"[0-9a-f]{40}")
_RECOVERY_ID = re.compile(r"recovery-sha256:[0-9a-f]{64}")
_EXECUTION = re.compile(r"gh-run:[1-9][0-9]*:[1-9][0-9]*")
_ALLOWED_STATES = frozenset({"ACCEPTED", "REFUSED", "UNKNOWN", "QUARANTINED"})


class QuarantineRecoveryError(RuntimeError):
    pass


def recovery_semantic_id(*, target: str, release: str, quarantined_request_id: str) -> str:
    if target != RECOVERY_TARGET or release != RECOVERY_RELEASE or quarantined_request_id != QUARANTINED_REQUEST_ID:
        raise QuarantineRecoveryError("recovery identity differs from authorized quarantined v0.1.7 state")
    encoded = json.dumps(
        {
            "schema": RECOVERY_SCHEMA,
            "operation": RECOVERY_OPERATION,
            "target": target,
            "product_release": release,
            "quarantined_request_id": quarantined_request_id,
            "quarantined_terminal_ref": QUARANTINED_TERMINAL_REF,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "recovery-sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_recovery_intent(payload: Mapping[str, object]) -> None:
    if payload.get("schema") != RECOVERY_INTENT_SCHEMA:
        raise QuarantineRecoveryError("recovery intent schema differs")
    if _RECOVERY_ID.fullmatch(str(payload.get("semantic_recovery_id", ""))) is None:
        raise QuarantineRecoveryError("recovery intent semantic id is invalid")
    if payload.get("operation") != RECOVERY_OPERATION or payload.get("target") != RECOVERY_TARGET:
        raise QuarantineRecoveryError("recovery intent operation/target differs")
    if payload.get("product_release") != RECOVERY_RELEASE or payload.get("release_id") != RECOVERY_RELEASE_ID:
        raise QuarantineRecoveryError("recovery intent Release identity differs")
    if payload.get("quarantined_request_id") != QUARANTINED_REQUEST_ID:
        raise QuarantineRecoveryError("recovery intent parent request differs")
    if payload.get("quarantined_intent_ref") != QUARANTINED_INTENT_REF or payload.get("quarantined_terminal_ref") != QUARANTINED_TERMINAL_REF:
        raise QuarantineRecoveryError("recovery intent parent evidence differs")
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
    if _RECOVERY_ID.fullmatch(str(payload.get("semantic_recovery_id", ""))) is None:
        raise QuarantineRecoveryError("recovery terminal semantic id is invalid")
    if payload.get("operation") != RECOVERY_OPERATION or payload.get("target") != RECOVERY_TARGET:
        raise QuarantineRecoveryError("recovery terminal operation/target differs")
    if payload.get("product_release") != RECOVERY_RELEASE or payload.get("release_id") != RECOVERY_RELEASE_ID:
        raise QuarantineRecoveryError("recovery terminal Release identity differs")
    if payload.get("quarantined_request_id") != QUARANTINED_REQUEST_ID or payload.get("quarantined_terminal_ref") != QUARANTINED_TERMINAL_REF:
        raise QuarantineRecoveryError("recovery terminal parent evidence differs")
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
