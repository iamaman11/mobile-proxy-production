from __future__ import annotations

from dataclasses import dataclass

from durable_release_identity import payload_matches_release_identity
from evidence_store import ADMISSION_HEADING


class PhoneReleaseReconcileError(RuntimeError):
    pass


@dataclass(frozen=True)
class PhoneReleaseProjectionResult:
    deployment_id: int
    previous_state: str | None
    final_state: str
    projection_updated: bool
    durable_admission_count: int


def _require_durable_admission(
    *,
    evidence: object,
    admitted: object,
    deployment_id: int,
) -> int:
    try:
        records = evidence.list_records(ADMISSION_HEADING)
    except Exception as exc:
        raise PhoneReleaseReconcileError("durable deployment admission evidence is unavailable") from exc

    compatible = []
    conflicting_ids: set[int] = set()
    for record in records:
        payload = record.payload
        if not payload_matches_release_identity(
            payload,
            admitted.identity,
            target="phone-production",
        ):
            continue
        if (
            payload.get("schema") != "production-deployment-admission.v2"
            or payload.get("mutation_authority") is not False
            or payload.get("dispatch_authority") is not False
            or payload.get("mutation_performed") is not False
        ):
            raise PhoneReleaseReconcileError("compatible durable admission safety contract differs")
        admitted_deployment_id = payload.get("deployment_id")
        if not isinstance(admitted_deployment_id, int) or admitted_deployment_id <= 0:
            raise PhoneReleaseReconcileError("compatible durable admission Deployment id is invalid")
        conflicting_ids.add(admitted_deployment_id)
        if admitted_deployment_id == deployment_id:
            compatible.append(record)

    if not compatible:
        raise PhoneReleaseReconcileError("exact public Deployment lacks compatible durable admission")
    if conflicting_ids != {deployment_id}:
        raise PhoneReleaseReconcileError("compatible durable admissions point to different public Deployments")
    return len(compatible)


def reconcile_healthy_exact_projection(
    *,
    evidence: object,
    projection: object,
    admitted: object,
    environment: str,
) -> PhoneReleaseProjectionResult:
    """Repair only the projection for a target already proven HEALTHY_EXACT.

    This function never creates a GitHub Deployment and never writes private
    mutation intent/terminal evidence. It only appends a public Deployment status
    when the already-admitted exact projection is stale, then reads it back.
    """

    identity = admitted.identity
    matches = projection.find_exact(
        source_sha=identity.source_sha,
        environment=environment,
        release_tag=identity.tag,
        release_id=identity.release_id,
    )
    if len(matches) != 1:
        raise PhoneReleaseReconcileError(
            "exact public Deployment match count is not exactly one"
        )
    match = matches[0]
    admission_count = _require_durable_admission(
        evidence=evidence,
        admitted=admitted,
        deployment_id=match.deployment_id,
    )

    previous_state = match.latest_state
    updated = False
    if previous_state != "success":
        projection.status(
            deployment_id=match.deployment_id,
            state="success",
            description=(
                f"{identity.tag} target reconcile: HEALTHY_EXACT; canonical terminal unchanged"
            ),
        )
        updated = True

    readback = projection.find_exact(
        source_sha=identity.source_sha,
        environment=environment,
        release_tag=identity.tag,
        release_id=identity.release_id,
    )
    if len(readback) != 1 or readback[0].deployment_id != match.deployment_id:
        raise PhoneReleaseReconcileError("public Deployment reconcile read-back identity differs")
    if readback[0].latest_state != "success":
        raise PhoneReleaseReconcileError("public Deployment reconcile read-back state differs")

    return PhoneReleaseProjectionResult(
        deployment_id=match.deployment_id,
        previous_state=previous_state,
        final_state="success",
        projection_updated=updated,
        durable_admission_count=admission_count,
    )
