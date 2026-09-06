from __future__ import annotations

from dataclasses import dataclass

from github_projection import PublicDeploymentMatch

_SAFE_REUSABLE_STATES = frozenset({"queued", "in_progress"})
_RETRY_REOPENABLE_STATES = frozenset({"failure", "error"})


class ProjectionAdmissionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectionAdmissionDecision:
    deployment_id: int
    reused: bool
    observed_state: str


def resolve_projection_admission(
    *,
    projection: object,
    source_sha: str,
    environment: str,
    release_tag: str,
    release_id: int,
    durable_deployment_id: int | None,
    retry_authorized: bool = False,
    retry_expected_deployment_id: int | None = None,
) -> ProjectionAdmissionDecision:
    if retry_expected_deployment_id is not None and not retry_authorized:
        raise ProjectionAdmissionError("retry Deployment identity supplied without admitted retry lineage")

    matches = projection.find_exact(
        source_sha=source_sha,
        environment=environment,
        release_tag=release_tag,
        release_id=release_id,
    )
    if len(matches) > 1:
        raise ProjectionAdmissionError("multiple exact public Deployments exist for one semantic release target")
    if len(matches) == 1:
        match: PublicDeploymentMatch = matches[0]
        if durable_deployment_id is not None and match.deployment_id != durable_deployment_id:
            raise ProjectionAdmissionError("durable private admission points to a different public Deployment")
        if retry_expected_deployment_id is not None and match.deployment_id != retry_expected_deployment_id:
            raise ProjectionAdmissionError("retry lineage points to a different public Deployment")
        if match.latest_state in _SAFE_REUSABLE_STATES:
            return ProjectionAdmissionDecision(
                deployment_id=match.deployment_id,
                reused=True,
                observed_state=str(match.latest_state),
            )
        if (
            retry_authorized
            and retry_expected_deployment_id == match.deployment_id
            and match.latest_state in _RETRY_REOPENABLE_STATES
        ):
            projection.status(
                deployment_id=match.deployment_id,
                state="queued",
                description=f"{release_tag} explicit REFUSED retry admitted by production controller",
            )
            return ProjectionAdmissionDecision(
                deployment_id=match.deployment_id,
                reused=True,
                observed_state="queued",
            )
        raise ProjectionAdmissionError(
            "public Deployment state conflicts with missing private canonical terminal: "
            + repr(match.latest_state)
        )

    if durable_deployment_id is not None:
        raise ProjectionAdmissionError("durable private admission public Deployment is not observable")
    if retry_expected_deployment_id is not None:
        raise ProjectionAdmissionError("retry lineage public Deployment is not observable")

    deployment_id = projection.create(
        source_sha=source_sha,
        environment=environment,
        release_tag=release_tag,
        release_id=release_id,
    )
    projection.status(
        deployment_id=deployment_id,
        state="queued",
        description=f"{release_tag} admitted by production controller",
    )
    return ProjectionAdmissionDecision(
        deployment_id=deployment_id,
        reused=False,
        observed_state="queued",
    )
