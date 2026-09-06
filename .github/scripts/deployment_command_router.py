#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from deployment_request import (  # noqa: E402
    DeploymentRequestError,
    RequestProvenance,
    build_deployment_request,
    build_retry_deployment_request,
)

DEPLOY_COMMAND = "/deploy"
RETRY_COMMAND = "/retry-deploy"


class RouteRefused(ValueError):
    pass


def classify(
    *, command_body: str, repository: str, issue_number: int, comment_id: int, actor: str, owner: str
):
    if issue_number != 1:
        raise RouteRefused("production deployment commands are accepted only from private Issue #1")
    if actor != owner:
        raise RouteRefused("production deployment actor is not repository owner")
    if command_body != command_body.strip() or "\n" in command_body or "\r" in command_body or "\x00" in command_body:
        raise RouteRefused("deployment command must be exactly one clean line")
    tokens = command_body.split()
    provenance = RequestProvenance(repository, issue_number, comment_id, actor)
    try:
        if len(tokens) == 3 and tokens[0] == DEPLOY_COMMAND:
            return build_deployment_request(
                target=tokens[1],
                product_release_tag=tokens[2],
                provenance=provenance,
            )
        if len(tokens) == 4 and tokens[0] == RETRY_COMMAND:
            return build_retry_deployment_request(
                target=tokens[1],
                product_release_tag=tokens[2],
                retry_of_request_id=tokens[3],
                provenance=provenance,
            )
        raise RouteRefused(
            "expected exact command: /deploy <target> <vX.Y.Z> or "
            "/retry-deploy phone-production <vX.Y.Z> <prior-request-id>"
        )
    except DeploymentRequestError as exc:
        raise RouteRefused(str(exc)) from exc


def _output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--comment-id", type=int, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    try:
        request = classify(
            command_body=args.command,
            repository=args.repository,
            issue_number=args.issue_number,
            comment_id=args.comment_id,
            actor=args.actor,
            owner=args.owner,
        )
    except RouteRefused as exc:
        print(f"CONTROL_ROUTE_REFUSED: {exc}", file=sys.stderr)
        return 2
    compact = request.to_json()
    _output("operation", request.operation)
    _output("target", request.target)
    _output("release_tag", request.product_release_tag)
    _output("request_id", request.request_id)
    _output("request_json", compact)
    print(compact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
