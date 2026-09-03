#!/usr/bin/env python3
"""Pure fail-closed classifier for the private Issue #1 production command bus."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from control_request import RequestContractError, RequestProvenance, build_request_envelope

REGISTRY_SCHEMA = "production-command-routes.v1"
_COMMAND_RE = re.compile(r"/[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_CURSOR_RE = re.compile(r"issue179-comment-[1-9][0-9]*")


class RouteRefused(ValueError):
    """A command is not uniquely admissible to any operation path."""


@dataclass(frozen=True)
class Route:
    command: str
    operation: str
    workflow: str
    mutating: bool
    authority_cursor: str


def _authority_cursor(value: object, *, field: str) -> str:
    cursor = str(value).strip()
    if _CURSOR_RE.fullmatch(cursor) is None:
        raise RouteRefused(f"invalid {field}")
    return cursor


def load_registry(path: Path) -> tuple[str, dict[str, Route]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema") != REGISTRY_SCHEMA:
        raise RouteRefused("unsupported command route registry schema")
    cursor = _authority_cursor(raw.get("authority_cursor", ""), field="global authority cursor")
    routes: dict[str, Route] = {}
    operations: set[str] = set()
    for item in raw.get("routes", []):
        if not isinstance(item, dict):
            raise RouteRefused("invalid command route entry")
        route_cursor = _authority_cursor(
            item.get("authority_cursor", cursor),
            field="route authority cursor",
        )
        route = Route(
            command=str(item.get("command", "")),
            operation=str(item.get("operation", "")),
            workflow=str(item.get("workflow", "")),
            mutating=bool(item.get("mutating", False)),
            authority_cursor=route_cursor,
        )
        if _COMMAND_RE.fullmatch(route.command) is None:
            raise RouteRefused(f"invalid registered command: {route.command!r}")
        if not route.workflow.endswith(".yml") or "/" in route.workflow or "\\" in route.workflow:
            raise RouteRefused("invalid registered workflow path")
        if route.command in routes or route.operation in operations:
            raise RouteRefused("duplicate command/operation in route registry")
        routes[route.command] = route
        operations.add(route.operation)
    if not routes:
        raise RouteRefused("empty command route registry")
    return cursor, routes


def classify(
    *,
    command_body: str,
    registry_path: Path,
    repository: str,
    issue_number: int,
    comment_id: int,
    actor: str,
    owner: str,
) -> tuple[Route, object]:
    if issue_number != 1:
        raise RouteRefused("production commands are accepted only from private Issue #1")
    if actor != owner:
        raise RouteRefused("production command actor is not repository owner")
    if command_body != command_body.strip():
        raise RouteRefused("leading/trailing whitespace is not accepted")
    if not command_body or "\n" in command_body or "\r" in command_body or "\x00" in command_body:
        raise RouteRefused("command must be exactly one non-empty line")

    tokens = command_body.split()
    if not tokens or _COMMAND_RE.fullmatch(tokens[0]) is None:
        raise RouteRefused("invalid command token")

    _, routes = load_registry(registry_path)
    route = routes.get(tokens[0])
    if route is None:
        raise RouteRefused("unknown production command")

    recognized_embedded = [value for value in tokens[1:] if value in routes]
    if recognized_embedded:
        raise RouteRefused("ambiguous command contains more than one recognized operation token")
    if any(value.startswith("/") for value in tokens[1:]):
        raise RouteRefused("slash-prefixed argument is refused")

    provenance = RequestProvenance(
        repository=repository,
        issue_number=issue_number,
        comment_id=comment_id,
        actor=actor,
    )
    try:
        envelope = build_request_envelope(
            operation=route.operation,
            arguments=tokens[1:],
            authority_cursor=route.authority_cursor,
            mutating=route.mutating,
            provenance=provenance,
        )
    except RequestContractError as exc:
        raise RouteRefused(str(exc)) from exc
    return route, envelope


def _write_output(name: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--comment-id", type=int, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args(argv)

    try:
        route, envelope = classify(
            command_body=args.command,
            registry_path=args.registry,
            repository=args.repository,
            issue_number=args.issue_number,
            comment_id=args.comment_id,
            actor=args.actor,
            owner=args.owner,
        )
    except (RouteRefused, OSError, json.JSONDecodeError) as exc:
        print(f"CONTROL_ROUTE_REFUSED: {exc}", file=sys.stderr)
        return 2

    compact = envelope.to_json()
    _write_output("operation", route.operation)
    _write_output("workflow", route.workflow)
    _write_output("mutating", "true" if route.mutating else "false")
    _write_output("request_id", envelope.request_id)
    _write_output("request_json", compact)
    print(compact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())