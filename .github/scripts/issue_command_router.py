#!/usr/bin/env python3
"""Fail-closed classifier for the bounded read-only Issue #1 command."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass


AUTHORITATIVE_REPOSITORY = "iamaman11/mobile-proxy-production"
AUTHORITATIVE_ISSUE_NUMBER = 1
ALLOWED_AUTHORS = frozenset({"iamaman11"})
ALLOWED_COMMAND = "/observe-public-deployment-projection"
ALLOWED_WORKFLOW = ".github/workflows/public-deployment-projection-observer.yml"
ALLOWED_REF = "main"
_SHA = re.compile(r"[0-9a-f]{40}")


class RouteRefused(ValueError):
    """The event is outside the one explicitly admitted route."""


@dataclass(frozen=True)
class Route:
    workflow: str
    ref: str


def classify(
    *,
    repository: str,
    issue_number: int,
    author: str,
    command: str,
    event_sha: str,
    current_main_sha: str,
) -> Route:
    """Return the sole safe route or reject the complete event."""
    if repository != AUTHORITATIVE_REPOSITORY:
        raise RouteRefused("unexpected repository")
    if issue_number != AUTHORITATIVE_ISSUE_NUMBER:
        raise RouteRefused("commands are accepted only from private Issue #1")
    if author not in ALLOWED_AUTHORS:
        raise RouteRefused("comment author is not allowlisted")
    if command != ALLOWED_COMMAND:
        raise RouteRefused("command is not allowlisted")
    if _SHA.fullmatch(event_sha) is None or _SHA.fullmatch(current_main_sha) is None:
        raise RouteRefused("authoritative revision is not a full commit SHA")
    if event_sha != current_main_sha:
        raise RouteRefused("private main drifted after the comment event")
    return Route(workflow=ALLOWED_WORKFLOW, ref=ALLOWED_REF)


def _write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--author", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--event-sha", required=True)
    parser.add_argument("--current-main-sha", required=True)
    args = parser.parse_args(argv)

    try:
        route = classify(
            repository=args.repository,
            issue_number=args.issue_number,
            author=args.author,
            command=args.command,
            event_sha=args.event_sha,
            current_main_sha=args.current_main_sha,
        )
    except RouteRefused as exc:
        print(f"ISSUE_COMMAND_ROUTE_REFUSED: {exc}", file=sys.stderr)
        return 2

    _write_output("workflow", route.workflow)
    _write_output("ref", route.ref)
    print("ISSUE_COMMAND_ROUTE_ACCEPTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
