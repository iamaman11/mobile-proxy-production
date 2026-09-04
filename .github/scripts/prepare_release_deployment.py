#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from deployment_request import validate_deployment_request  # noqa: E402
from deployment_state_machine import DeploymentState, reduce_state  # noqa: E402
from evidence_store import EvidenceError, IssueEvidenceStore  # noqa: E402
from github_projection import ProjectionError, PublicDeploymentProjection  # noqa: E402
from projection_admission import ProjectionAdmissionError, resolve_projection_admission  # noqa: E402
from release_resolver import ReleaseAdmissionError, resolve_release  # noqa: E402
from terminal_result import DeploymentTerminal, validate_terminal  # noqa: E402

_SHA = re.compile(r"[0-9a-f]{40}")


def _output(name: str, value: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    else:
        text = str(value)
    if "\n" in text or "\r" in text:
        raise RuntimeError("workflow output must be one line")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={text}\n")


def _next(state: str) -> str:
    return {
        "REFUSED": "fix_blocking_predicates_then_reissue",
        "ACCEPTED": "deploy-product-release",
        "UNKNOWN": "read-only-recovery-observation",
        "RECOVERED": "operator-review-before-new-mutation",
        "QUARANTINED": "read-only-observation-or-approved-recovery",
    }.get(state, "none")


def _pre_release_refusal(*, request: dict[str, object], execution_id: str, controller_revision: str, reason: str) -> dict[str, object]:
    state = reduce_state(DeploymentState(), "request_received")
    state = reduce_state(state, "authorize_refused", reason=reason)
    terminal = DeploymentTerminal(
        operation="deploy-product-release",
        semantic_request_id=str(request["request_id"]),
        execution_id=execution_id,
        controller_revision=controller_revision,
        target=str(request["target"]),
        product_release=str(request["product_release_tag"]),
        release_id=None,
        release_source_sha=None,
        artifact_digest=None,
        deployment_id=None,
        state=state.state,
        current_step=state.current_step,
        facts={},
        blocking_predicates=state.blocking_predicates,
        mutation_performed=False,
        postcondition_verified=False,
        recovery_required=False,
        next_allowed_operation=_next(state.state),
        evidence_refs=(f"issue-comment:{request['provenance']['comment_id']}",),
    ).to_dict()
    validate_terminal(terminal)
    return terminal


def _admission_payload(
    *,
    request: dict[str, object],
    execution_id: str,
    controller_revision: str,
    admitted: object,
    deployment_id: int,
) -> dict[str, object]:
    identity = admitted.identity
    return {
        "schema": "production-deployment-admission.v2",
        "semantic_request_id": request["request_id"],
        "execution_id": execution_id,
        "controller_revision": controller_revision,
        "target": request["target"],
        "product_release": identity.tag,
        "release_id": identity.release_id,
        "release_source_sha": identity.source_sha,
        "artifact_digest": identity.artifact_digest,
        "deployment_id": deployment_id,
        "initial_projection_state": "queued",
        "mutation_authority": False,
        "dispatch_authority": False,
        "mutation_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args()

    request = json.loads(args.request_json)
    validate_deployment_request(request)
    if _SHA.fullmatch(args.controller_revision) is None:
        raise SystemExit("controller revision is invalid")
    if not re.fullmatch(r"gh-run:[1-9][0-9]*:[1-9][0-9]*", args.execution_id):
        raise SystemExit("execution id is invalid")

    evidence = IssueEvidenceStore(os.environ.get("GITHUB_TOKEN", ""))
    existing_intent, existing_terminal = evidence.request_history(str(request["request_id"]))
    if existing_terminal is not None:
        evidence.persist_duplicate_projection({
            "schema": "production-deployment-duplicate.v2",
            "semantic_request_id": request["request_id"],
            "execution_id": args.execution_id,
            "controller_revision": args.controller_revision,
            "target": request["target"],
            "product_release": request["product_release_tag"],
            "canonical_terminal_ref": existing_terminal.ref,
            "canonical_terminal": existing_terminal.payload,
            "mutation_performed": False,
            "reason": "semantic_request_already_terminal",
        })
        _output("admitted", False)
        _output("duplicate", True)
        _output("recovery_only", False)
        _output("canonical_terminal_ref", existing_terminal.ref)
        _output("admission_ref", "")
        return 0

    # VM support is deliberately not prebuilt. Until the Android controller path is
    # accepted end-to-end, a VM request is classified before Release lookup or any
    # public Deployment creation, so it cannot leave an orphan external projection.
    if request["target"] == "vm-production":
        terminal = _pre_release_refusal(
            request=request,
            execution_id=args.execution_id,
            controller_revision=args.controller_revision,
            reason="vm_target_adapter_not_accepted_until_phone_controller_proof",
        )
        record = evidence.persist_terminal(terminal)
        _output("admitted", False)
        _output("duplicate", False)
        _output("recovery_only", False)
        _output("canonical_terminal_ref", record.ref)
        _output("admission_ref", "")
        return 0

    state = reduce_state(DeploymentState(), "request_received")
    try:
        admitted = resolve_release(
            tag=str(request["product_release_tag"]),
            target=str(request["target"]),
        )
    except ReleaseAdmissionError as exc:
        terminal = _pre_release_refusal(
            request=request,
            execution_id=args.execution_id,
            controller_revision=args.controller_revision,
            reason=str(exc),
        )
        record = evidence.persist_terminal(terminal)
        _output("admitted", False)
        _output("duplicate", False)
        _output("recovery_only", False)
        _output("canonical_terminal_ref", record.ref)
        _output("admission_ref", "")
        return 0

    admitted_payload = admitted.to_dict()
    identity = admitted.identity

    if existing_intent is not None:
        payload = existing_intent.payload
        exact = (
            payload.get("product_release") == identity.tag
            and payload.get("release_id") == identity.release_id
            and payload.get("release_source_sha") == identity.source_sha
            and payload.get("artifact_digest") == identity.artifact_digest
            and payload.get("target") == request["target"]
        )
        if not exact:
            raise SystemExit("existing mutation intent conflicts with current immutable Release identity")
        deployment_id = payload.get("deployment_id")
        if not isinstance(deployment_id, int) or deployment_id <= 0:
            raise SystemExit("existing mutation intent lacks public Deployment id")
        admission = evidence.reusable_admission(str(request["request_id"]))
        _output("admitted", True)
        _output("duplicate", False)
        _output("recovery_only", True)
        _output("admitted_release_json", admitted_payload)
        _output("deployment_id", deployment_id)
        _output("canonical_terminal_ref", "")
        _output("admission_ref", admission.ref if admission is not None else "")
        return 0

    reusable = evidence.reusable_admission(str(request["request_id"]))
    durable_deployment_id: int | None = None
    if reusable is not None:
        reusable_payload = reusable.payload
        exact = (
            reusable_payload.get("product_release") == identity.tag
            and reusable_payload.get("release_id") == identity.release_id
            and reusable_payload.get("release_source_sha") == identity.source_sha
            and reusable_payload.get("artifact_digest") == identity.artifact_digest
            and reusable_payload.get("target") == request["target"]
        )
        if not exact:
            raise SystemExit("durable deployment admission conflicts with current immutable Release identity")
        candidate_id = reusable_payload.get("deployment_id")
        if not isinstance(candidate_id, int) or candidate_id <= 0:
            raise SystemExit("durable deployment admission lacks public Deployment id")
        durable_deployment_id = candidate_id

    deployment_id: int | None = durable_deployment_id
    try:
        projection = PublicDeploymentProjection(os.environ.get("PUBLIC_DEPLOYMENTS_TOKEN", ""))
        decision = resolve_projection_admission(
            projection=projection,
            source_sha=identity.source_sha,
            environment=str(request["target"]),
            release_tag=identity.tag,
            release_id=identity.release_id,
            durable_deployment_id=durable_deployment_id,
        )
        deployment_id = decision.deployment_id
    except (ProjectionError, ProjectionAdmissionError) as exc:
        state = reduce_state(state, "authorize_refused", reason=str(exc))
        terminal = DeploymentTerminal(
            operation="deploy-product-release",
            semantic_request_id=str(request["request_id"]),
            execution_id=args.execution_id,
            controller_revision=args.controller_revision,
            target=str(request["target"]),
            product_release=identity.tag,
            release_id=identity.release_id,
            release_source_sha=identity.source_sha,
            artifact_digest=identity.artifact_digest,
            deployment_id=deployment_id,
            state=state.state,
            current_step=state.current_step,
            facts={"immutability_control": admitted.immutability_control},
            blocking_predicates=state.blocking_predicates,
            mutation_performed=False,
            postcondition_verified=False,
            recovery_required=False,
            next_allowed_operation=_next(state.state),
            evidence_refs=(f"issue-comment:{request['provenance']['comment_id']}",),
        ).to_dict()
        validate_terminal(terminal)
        record = evidence.persist_terminal(terminal)
        _output("admitted", False)
        _output("duplicate", False)
        _output("recovery_only", False)
        _output("canonical_terminal_ref", record.ref)
        _output("admission_ref", reusable.ref if reusable is not None else "")
        return 0

    assert deployment_id is not None
    admission_payload = _admission_payload(
        request=request,
        execution_id=args.execution_id,
        controller_revision=args.controller_revision,
        admitted=admitted,
        deployment_id=deployment_id,
    )
    try:
        admission = evidence.persist_admission(admission_payload)
    except EvidenceError as exc:
        try:
            projection.status(
                deployment_id=deployment_id,
                state="error",
                description="durable private deployment admission unavailable",
            )
        except ProjectionError:
            pass
        raise SystemExit("durable public Deployment admission could not be persisted") from exc

    _output("admitted", True)
    _output("duplicate", False)
    _output("recovery_only", False)
    _output("admitted_release_json", admitted_payload)
    _output("deployment_id", deployment_id)
    _output("canonical_terminal_ref", "")
    _output("admission_ref", admission.ref)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
