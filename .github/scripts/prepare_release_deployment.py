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
from evidence_store import IssueEvidenceStore  # noqa: E402
from github_projection import ProjectionError, PublicDeploymentProjection  # noqa: E402
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
            "mutation_performed": False,
            "reason": "semantic_request_already_terminal",
        })
        _output("admitted", False)
        _output("duplicate", True)
        _output("recovery_only", False)
        _output("canonical_terminal_ref", existing_terminal.ref)
        return 0

    state = reduce_state(DeploymentState(), "request_received")
    try:
        admitted = resolve_release(
            tag=str(request["product_release_tag"]),
            target=str(request["target"]),
        )
    except ReleaseAdmissionError as exc:
        state = reduce_state(state, "authorize_refused", reason=str(exc))
        terminal = DeploymentTerminal(
            operation="deploy-product-release",
            semantic_request_id=str(request["request_id"]),
            execution_id=args.execution_id,
            controller_revision=args.controller_revision,
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
        record = evidence.persist_terminal(terminal)
        _output("admitted", False)
        _output("duplicate", False)
        _output("recovery_only", False)
        _output("canonical_terminal_ref", record.ref)
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
        _output("admitted", True)
        _output("duplicate", False)
        _output("recovery_only", True)
        _output("admitted_release_json", admitted_payload)
        _output("deployment_id", deployment_id)
        _output("canonical_terminal_ref", "")
        return 0

    try:
        projection = PublicDeploymentProjection(os.environ.get("PUBLIC_DEPLOYMENTS_TOKEN", ""))
        deployment_id = projection.create(
            source_sha=identity.source_sha,
            environment=str(request["target"]),
            release_tag=identity.tag,
            release_id=identity.release_id,
        )
        projection.status(
            deployment_id=deployment_id,
            state="queued",
            description=f"{identity.tag} admitted by production controller",
        )
    except ProjectionError as exc:
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
            deployment_id=None,
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
        return 0

    _output("admitted", True)
    _output("duplicate", False)
    _output("recovery_only", False)
    _output("admitted_release_json", admitted_payload)
    _output("deployment_id", deployment_id)
    _output("canonical_terminal_ref", "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
