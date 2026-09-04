#!/usr/bin/env python3
"""Fail-closed registry-driven classifier for private Issue #1 commands."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "production" / "command-control-registry.json"
TARGETS_PATH = ROOT / "production" / "targets.json"

_SHA = re.compile(r"[0-9a-f]{40}")
_ROUTE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,80}")
_WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9._-]+\.yml")
_ALLOWED_HANDLERS = frozenset({"dispatch_workflow", "deployment", "workflow_call"})
_ALLOWED_CLASSES = frozenset({
    "OBSERVE", "DIAGNOSTIC", "VERIFY", "BUILD", "RELEASE_VERIFY",
    "DEPLOY", "ROLLBACK", "RECOVER", "RECONCILE", "STATUS", "RUNNER_TOOLING",
})
_ALLOWED_REF_POLICIES = frozenset({
    "controller-main-exact", "controller-event-sha-exact", "immutable-release-ref",
})
_ALLOWED_CAPTURE_NAMES = frozenset({"target", "release", "canonical_sha"})


class RouteRefused(ValueError):
    """The event or registry cannot safely resolve to one admitted route."""


@dataclass(frozen=True)
class Route:
    route_id: str
    handler: str
    workflow: str
    ref: str
    operation: str
    operation_class: str
    read_only: bool
    destructive: bool
    target: str = ""
    release_tag: str = ""
    canonical_sha: str = ""
    semantic_identity_policy: str = ""
    idempotency_policy: str = ""
    evidence_policy: str = ""
    recovery_policy: str = ""


def _load_object(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RouteRefused(f"registry unavailable: {path.name}") from exc
    if not isinstance(value, Mapping):
        raise RouteRefused(f"registry is not an object: {path.name}")
    return value


def _nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise RouteRefused(f"registry field invalid: {field}")
    return value


def validate_target_registry(value: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    if value.get("schema") != "production-target-registry.v1":
        raise RouteRefused("target registry schema differs")
    raw_targets = value.get("targets")
    if not isinstance(raw_targets, Mapping) or not raw_targets:
        raise RouteRefused("target registry is empty")
    result: dict[str, Mapping[str, Any]] = {}
    for name, raw in raw_targets.items():
        if not isinstance(name, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", name) is None:
            raise RouteRefused("target registry contains invalid target name")
        if not isinstance(raw, Mapping):
            raise RouteRefused(f"target contract is invalid: {name}")
        for field in ("target_type", "adapter", "serialization_domain", "required_postcondition"):
            _nonempty_text(raw.get(field), f"target.{name}.{field}")
        if not isinstance(raw.get("active"), bool):
            raise RouteRefused(f"target active flag invalid: {name}")
        allowed = raw.get("allowed_operations")
        domains = raw.get("physical_domains")
        if not isinstance(allowed, list) or not all(isinstance(item, str) and item for item in allowed):
            raise RouteRefused(f"target allowed_operations invalid: {name}")
        if not isinstance(domains, list) or not all(isinstance(item, str) and item for item in domains):
            raise RouteRefused(f"target physical_domains invalid: {name}")
        result[name] = raw
    return result


def validate_registry(
    value: Mapping[str, Any],
    targets: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    if value.get("schema") != "production-command-control-registry.v1":
        raise RouteRefused("command registry schema differs")
    if value.get("repository") != "iamaman11/mobile-proxy-production":
        raise RouteRefused("command registry repository differs")
    if value.get("issue_number") != 1:
        raise RouteRefused("command registry Issue differs")
    authors = value.get("allowed_authors")
    if authors != ["iamaman11"]:
        raise RouteRefused("command registry author allowlist differs")
    rules = value.get("extension_rules")
    if not isinstance(rules, Mapping):
        raise RouteRefused("command registry extension rules missing")
    required_rules = {
        "unknown_commands": "refuse",
        "unknown_arguments": "refuse",
        "user_supplied_workflow": False,
        "user_supplied_ref": False,
        "new_destructive_routes_require_recovery_policy": True,
        "new_destructive_routes_require_idempotency_policy": True,
        "new_destructive_routes_require_concurrency_domain": True,
        "new_routes_require_tests": True,
    }
    if any(rules.get(key) != expected for key, expected in required_rules.items()):
        raise RouteRefused("command registry fail-closed extension rules differ")

    routes = value.get("routes")
    if not isinstance(routes, list) or not routes:
        raise RouteRefused("command registry routes missing")

    ids: set[str] = set()
    active_patterns: set[str] = set()
    validated: list[Mapping[str, Any]] = []
    for raw in routes:
        if not isinstance(raw, Mapping):
            raise RouteRefused("route contract is not an object")
        route_id = _nonempty_text(raw.get("id"), "route.id")
        if _ROUTE_ID.fullmatch(route_id) is None or route_id in ids:
            raise RouteRefused("route id invalid or duplicated")
        ids.add(route_id)

        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            raise RouteRefused(f"route enabled flag invalid: {route_id}")
        pattern_text = _nonempty_text(raw.get("pattern"), f"{route_id}.pattern")
        if len(pattern_text) > 256 or not pattern_text.startswith("^/") or not pattern_text.endswith("$"):
            raise RouteRefused(f"route pattern is not exact/anchored: {route_id}")
        try:
            pattern = re.compile(pattern_text)
        except re.error as exc:
            raise RouteRefused(f"route pattern invalid: {route_id}") from exc
        unknown_groups = set(pattern.groupindex) - _ALLOWED_CAPTURE_NAMES
        if unknown_groups:
            raise RouteRefused(f"route pattern has unsupported captures: {route_id}")
        if enabled and pattern_text in active_patterns:
            raise RouteRefused("active route patterns must be unique")
        if enabled:
            active_patterns.add(pattern_text)

        handler = _nonempty_text(raw.get("handler"), f"{route_id}.handler")
        if handler not in _ALLOWED_HANDLERS:
            raise RouteRefused(f"route handler invalid: {route_id}")
        workflow = _nonempty_text(raw.get("workflow"), f"{route_id}.workflow")
        if _WORKFLOW.fullmatch(workflow) is None or "${" in workflow or ".." in workflow:
            raise RouteRefused(f"route workflow must be literal: {route_id}")
        if raw.get("ref") != "main":
            raise RouteRefused(f"route ref must be literal main: {route_id}")
        ref_policy = _nonempty_text(raw.get("ref_policy"), f"{route_id}.ref_policy")
        if ref_policy not in _ALLOWED_REF_POLICIES:
            raise RouteRefused(f"route ref policy invalid: {route_id}")

        operation = _nonempty_text(raw.get("operation"), f"{route_id}.operation")
        operation_class = _nonempty_text(raw.get("operation_class"), f"{route_id}.operation_class")
        if operation_class not in _ALLOWED_CLASSES:
            raise RouteRefused(f"route operation class invalid: {route_id}")
        read_only = raw.get("read_only")
        destructive = raw.get("destructive")
        if not isinstance(read_only, bool) or not isinstance(destructive, bool):
            raise RouteRefused(f"route safety flags invalid: {route_id}")
        if read_only and destructive:
            raise RouteRefused(f"read-only route cannot be destructive: {route_id}")

        for flag in ("requires_phone", "requires_vm", "requires_release"):
            if not isinstance(raw.get(flag), bool):
                raise RouteRefused(f"route capability flag invalid: {route_id}.{flag}")

        allowed_targets = raw.get("allowed_targets")
        domains = raw.get("physical_domains")
        if not isinstance(allowed_targets, list) or not all(isinstance(item, str) for item in allowed_targets):
            raise RouteRefused(f"route allowed_targets invalid: {route_id}")
        if not isinstance(domains, list) or not all(isinstance(item, str) and item for item in domains):
            raise RouteRefused(f"route physical_domains invalid: {route_id}")
        if read_only and domains:
            raise RouteRefused(f"read-only route cannot own physical domains: {route_id}")

        concurrency = _nonempty_text(raw.get("concurrency_domain"), f"{route_id}.concurrency_domain")
        semantic = _nonempty_text(raw.get("semantic_identity_policy"), f"{route_id}.semantic_identity_policy")
        idempotency = _nonempty_text(raw.get("idempotency_policy"), f"{route_id}.idempotency_policy")
        evidence = _nonempty_text(raw.get("evidence_policy"), f"{route_id}.evidence_policy")
        recovery = _nonempty_text(raw.get("recovery_policy"), f"{route_id}.recovery_policy")
        if destructive:
            if concurrency == "none":
                raise RouteRefused(f"destructive route lacks concurrency domain: {route_id}")
            if "semantic" not in idempotency.lower():
                raise RouteRefused(f"destructive route lacks semantic idempotency: {route_id}")
            if "UNKNOWN" not in recovery or "no-blind-retry" not in recovery:
                raise RouteRefused(f"destructive route lacks UNKNOWN/no-blind-retry recovery: {route_id}")
            if "TERMINAL" not in evidence or "POSTCONDITION" not in evidence:
                raise RouteRefused(f"destructive route lacks terminal/postcondition evidence: {route_id}")

        for target_name in allowed_targets:
            target = targets.get(target_name)
            if target is None:
                raise RouteRefused(f"route references unknown target: {route_id}")
            allowed_ops = target.get("allowed_operations")
            if operation not in allowed_ops:
                raise RouteRefused(f"route operation is not admitted by target: {route_id}/{target_name}")

        if operation_class == "DEPLOY":
            if handler != "deployment" or not destructive or not raw.get("requires_release"):
                raise RouteRefused("DEPLOY route contract is incomplete")
            if semantic != "existing-deployment-request-v2":
                raise RouteRefused("DEPLOY semantic identity must remain deployment-request-v2")

        if handler == "dispatch_workflow" and idempotency == "single-run-attempt" and not read_only:
            raise RouteRefused("single-run-attempt dispatch must remain read-only")

        validated.append(raw)
    return validated


def load_contracts() -> tuple[Mapping[str, Any], list[Mapping[str, Any]], Mapping[str, Mapping[str, Any]]]:
    target_registry = _load_object(TARGETS_PATH)
    targets = validate_target_registry(target_registry)
    registry = _load_object(REGISTRY_PATH)
    routes = validate_registry(registry, targets)
    return registry, routes, targets


def classify(
    *,
    repository: str,
    issue_number: int,
    author: str,
    command: str,
    event_sha: str,
    current_main_sha: str,
    run_attempt: int,
) -> Route:
    registry, routes, targets = load_contracts()
    if repository != registry["repository"]:
        raise RouteRefused("unexpected repository")
    if issue_number != registry["issue_number"]:
        raise RouteRefused("commands are accepted only from private Issue #1")
    if author not in registry["allowed_authors"]:
        raise RouteRefused("comment author is not allowlisted")
    if command != command.strip() or any(ch in command for ch in ("\n", "\r", "\x00")):
        raise RouteRefused("command must be exactly one clean line")
    if _SHA.fullmatch(event_sha) is None or _SHA.fullmatch(current_main_sha) is None:
        raise RouteRefused("controller revision is not a full commit SHA")
    if event_sha != current_main_sha:
        raise RouteRefused("private main drifted after the comment event")
    if run_attempt <= 0:
        raise RouteRefused("run attempt is invalid")

    matches: list[tuple[Mapping[str, Any], re.Match[str]]] = []
    for raw in routes:
        if raw.get("enabled") is not True:
            continue
        match = re.fullmatch(str(raw["pattern"]), command)
        if match is not None:
            matches.append((raw, match))
    if len(matches) != 1:
        raise RouteRefused("command does not resolve to exactly one active contract")

    raw, match = matches[0]
    if raw["idempotency_policy"] == "single-run-attempt" and run_attempt != 1:
        raise RouteRefused("this read-only dispatch is admitted only on the original router attempt")

    groups = match.groupdict()
    target = groups.get("target") or ""
    release = groups.get("release") or ""
    canonical_sha = groups.get("canonical_sha") or ""

    allowed_targets = raw.get("allowed_targets", [])
    if target:
        if target not in allowed_targets:
            raise RouteRefused("captured target is outside route contract")
        target_contract = targets.get(target)
        if target_contract is None or raw["operation"] not in target_contract.get("allowed_operations", []):
            raise RouteRefused("captured target does not admit operation")
    elif allowed_targets:
        raise RouteRefused("target-bound route did not capture a target")

    if release and re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", release) is None:
        raise RouteRefused("release identity is invalid")
    if canonical_sha and _SHA.fullmatch(canonical_sha) is None:
        raise RouteRefused("canonical SHA argument is invalid")

    return Route(
        route_id=str(raw["id"]),
        handler=str(raw["handler"]),
        workflow=str(raw["workflow"]),
        ref=str(raw["ref"]),
        operation=str(raw["operation"]),
        operation_class=str(raw["operation_class"]),
        read_only=bool(raw["read_only"]),
        destructive=bool(raw["destructive"]),
        target=target,
        release_tag=release,
        canonical_sha=canonical_sha,
        semantic_identity_policy=str(raw["semantic_identity_policy"]),
        idempotency_policy=str(raw["idempotency_policy"]),
        evidence_policy=str(raw["evidence_policy"]),
        recovery_policy=str(raw["recovery_policy"]),
    )


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
    parser.add_argument("--run-attempt", required=True, type=int)
    args = parser.parse_args(argv)

    try:
        route = classify(
            repository=args.repository,
            issue_number=args.issue_number,
            author=args.author,
            command=args.command,
            event_sha=args.event_sha,
            current_main_sha=args.current_main_sha,
            run_attempt=args.run_attempt,
        )
    except RouteRefused as exc:
        print(f"ISSUE_COMMAND_ROUTE_REFUSED: {exc}", file=sys.stderr)
        return 2

    payload = {
        "route_id": route.route_id,
        "handler": route.handler,
        "workflow": route.workflow,
        "ref": route.ref,
        "operation": route.operation,
        "operation_class": route.operation_class,
        "read_only": route.read_only,
        "destructive": route.destructive,
        "target": route.target,
        "release_tag": route.release_tag,
        "canonical_sha": route.canonical_sha,
        "semantic_identity_policy": route.semantic_identity_policy,
        "idempotency_policy": route.idempotency_policy,
        "evidence_policy": route.evidence_policy,
        "recovery_policy": route.recovery_policy,
    }
    for key, value in payload.items():
        rendered = "true" if value is True else "false" if value is False else str(value)
        _write_output(key, rendered)
    _write_output("route_json", json.dumps(payload, sort_keys=True, separators=(",", ":")))
    print("ISSUE_COMMAND_ROUTE_ACCEPTED " + json.dumps(
        {
            "route_id": route.route_id,
            "operation": route.operation,
            "operation_class": route.operation_class,
            "read_only": route.read_only,
            "destructive": route.destructive,
        },
        sort_keys=True,
        separators=(",", ":"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
