from __future__ import annotations

from dataclasses import dataclass

from github_projection import PublicDeploymentMatch

_SAFE_REUSABLE_STATES = frozenset({"queued", "in_progress"})


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
) -> ProjectionAdmissionDecision:
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
        if match.latest_state not in _SAFE_REUSABLE_STATES:
            raise ProjectionAdmissionError(
                "public Deployment state conflicts with missing private canonical terminal: "
                + repr(match.latest_state)
            )
        if durable_deployment_id is not None and match.deployment_id != durable_deployment_id:
            raise ProjectionAdmissionError("durable private admission points to a different public Deployment")
        return ProjectionAdmissionDecision(
            deployment_id=match.deployment_id,
            reused=True,
            observed_state=str(match.latest_state),
        )

    if durable_deployment_id is not None:
        raise ProjectionAdmissionError("durable private admission public Deployment is not observable")

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
