from __future__ import annotations

from dataclasses import dataclass

from evidence_store import EvidenceError, EvidenceRecord
from terminal_result import validate_terminal


class ProjectionReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectionDecision:
    deployment_id: int
    state: str
    description: str
    canonical_terminal: bool


def _admission_deployment_id(admission: EvidenceRecord, semantic_request_id: str) -> int:
    payload = admission.payload
    if payload.get("semantic_request_id") != semantic_request_id:
        raise ProjectionReconciliationError("durable admission semantic request differs")
    deployment_id = payload.get("deployment_id")
    if not isinstance(deployment_id, int) or deployment_id <= 0:
        raise ProjectionReconciliationError("durable admission public Deployment id is invalid")
    if payload.get("mutation_authority") is not False or payload.get("dispatch_authority") is not False:
        raise ProjectionReconciliationError("durable admission unexpectedly grants mutation authority")
    return deployment_id


def decide_projection(
    *,
    admission: EvidenceRecord,
    semantic_request_id: str,
    intent: EvidenceRecord | None,
    terminal: EvidenceRecord | None,
    target_job_result: str,
) -> ProjectionDecision:
    deployment_id = _admission_deployment_id(admission, semantic_request_id)

    if terminal is not None:
        payload = terminal.payload
        if payload.get("semantic_request_id") != semantic_request_id:
            raise ProjectionReconciliationError("canonical terminal semantic request differs")
        try:
            validate_terminal(payload)
        except Exception as exc:
            raise ProjectionReconciliationError("canonical terminal is invalid") from exc
        if payload.get("deployment_id") != deployment_id:
            raise ProjectionReconciliationError("canonical terminal public Deployment id differs")
        state = str(payload.get("deployment_projection", ""))
        if state not in {"success", "failure", "error"}:
            raise ProjectionReconciliationError("canonical terminal projection is invalid")
        if state == "success" and payload.get("state") != "ACCEPTED":
            raise ProjectionReconciliationError("public success lacks canonical ACCEPTED terminal")
        return ProjectionDecision(
            deployment_id=deployment_id,
            state=state,
            description=f"canonical controller terminal: {payload['state']}",
            canonical_terminal=True,
        )

    if intent is not None:
        payload = intent.payload
        if payload.get("semantic_request_id") != semantic_request_id:
            raise ProjectionReconciliationError("durable mutation intent semantic request differs")
        if payload.get("deployment_id") != deployment_id:
            raise ProjectionReconciliationError("durable mutation intent public Deployment id differs")
        return ProjectionDecision(
            deployment_id=deployment_id,
            state="error",
            description="canonical terminal unavailable after durable mutation intent; recovery required",
            canonical_terminal=False,
        )

    result = target_job_result if target_job_result in {"success", "failure", "cancelled", "skipped"} else "unknown"
    return ProjectionDecision(
        deployment_id=deployment_id,
        state="error",
        description=f"target execution {result} before durable mutation intent/terminal",
        canonical_terminal=False,
    )


def reconcile_projection(
    *,
    evidence: object,
    projection: object,
    semantic_request_id: str,
    execution_id: str,
    target_job_result: str,
) -> ProjectionDecision:
    try:
        admission = evidence.admission_for_execution(semantic_request_id, execution_id)
        if admission is None:
            admission = evidence.reusable_admission(semantic_request_id)
        if admission is None:
            raise ProjectionReconciliationError("durable public Deployment admission is unavailable")
        intent, terminal = evidence.request_history(semantic_request_id)
    except EvidenceError as exc:
        raise ProjectionReconciliationError("private canonical evidence could not be read") from exc

    decision = decide_projection(
        admission=admission,
        semantic_request_id=semantic_request_id,
        intent=intent,
        terminal=terminal,
        target_job_result=target_job_result,
    )
    projection.status(
        deployment_id=decision.deployment_id,
        state=decision.state,
        description=decision.description,
    )
    return decision
