#!/usr/bin/env python3
"""One-time deterministic migration from issue_comment fan-out to one router.

This script is intentionally repository-local and phone-free.  It rewrites only
workflow trigger/control plumbing; operation job bodies remain byte-for-byte
apart from github.event source references becoming typed workflow_call inputs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
REGISTRY = ROOT / ".github" / "production" / "command-routes.json"
ROUTER = WORKFLOWS / "production-control-router.yml"

AUTHORITY_CURSOR = "issue179-comment-5529299637"
REGISTRY_SCHEMA = "production-command-routes.v1"

# Current canonical mutation classification from production-phone-mutation-policy.yml.
# Permanent policy validates that these two inventories cannot drift.
MUTATORS = {
    "android-signing-migration.yml",
    "phone-clean-install.yml",
    "phone-filesystem-certification.yml",
    "phone-filesystem-quarantine-cleanup.yml",
    "phone-runtime-recovery.yml",
    "phone-runtime-binary-repair.yml",
    "runtime-reconstruction-execution.yml",
}

EXCLUDED = {
    "production-control-router.yml",
    "production-command-router-policy.yml",
    "c0o-branch-migration.yml",
}

COMMAND_PATTERNS = (
    re.compile(r"startsWith\(\s*github\.event\.comment\.body\s*,\s*['\"](?P<cmd>/[A-Za-z0-9._-]+)"),
    re.compile(r"github\.event\.comment\.body\s*==\s*['\"](?P<cmd>/[A-Za-z0-9._-]+)(?:\s|['\"])"),
    re.compile(r"re\.fullmatch\(\s*r?[\"'](?P<cmd>/[A-Za-z0-9._-]+)"),
    re.compile(r"tokens\[0\]\s*(?:!=|==)\s*['\"](?P<cmd>/[A-Za-z0-9._-]+)"),
)

WORKFLOW_CALL_BLOCK = """on:
  workflow_call:
    inputs:
      command:
        required: true
        type: string
      source_event_name:
        required: true
        type: string
      source_issue_number:
        required: true
        type: number
      source_comment_id:
        required: true
        type: number
      source_actor:
        required: true
        type: string
      source_comment_url:
        required: true
        type: string
      control_request_id:
        required: true
        type: string
      control_request_json:
        required: true
        type: string
"""

SOURCE_REPLACEMENTS = (
    ("github.event.comment.html_url", "inputs.source_comment_url"),
    ("github.event.comment.user.login", "inputs.source_actor"),
    ("github.event.comment.body", "inputs.command"),
    ("github.event.comment.id", "inputs.source_comment_id"),
    ("github.event.issue.number", "inputs.source_issue_number"),
    ("github.event_name", "inputs.source_event_name"),
)


def _event_block_span(text: str) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip("\r\n") == "on:"), None)
    if start is None:
        raise RuntimeError("workflow has no canonical top-level on: block")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped and not lines[i].startswith((" ", "\t")) and not stripped.startswith("#"):
            end = i
            break
    return sum(len(v) for v in lines[:start]), sum(len(v) for v in lines[:end])


def _extract_command(text: str, path: Path) -> str:
    found: set[str] = set()
    for pattern in COMMAND_PATTERNS:
        found.update(match.group("cmd") for match in pattern.finditer(text))
    if len(found) != 1:
        raise RuntimeError(f"{path.name}: expected exactly one production command, found {sorted(found)}")
    return next(iter(found))


def _top_level_permissions(text: str) -> dict[str, str]:
    match = re.search(r"(?ms)^permissions:\n(?P<body>(?:^  [^\n]+\n)+)", text)
    if match is None:
        return {"contents": "read"}
    result: dict[str, str] = {}
    for raw in match.group("body").splitlines():
        key, sep, value = raw.strip().partition(":")
        if not sep:
            raise RuntimeError("unsupported permissions syntax")
        value = value.strip()
        if value not in {"read", "write", "none"}:
            raise RuntimeError(f"unsupported permission level: {raw}")
        result[key] = value
    return result


def _merge_permissions(all_permissions: list[dict[str, str]]) -> dict[str, str]:
    rank = {"none": 0, "read": 1, "write": 2}
    result: dict[str, str] = {}
    for permissions in all_permissions:
        for key, value in permissions.items():
            if rank[value] > rank.get(result.get(key, "none"), 0):
                result[key] = value
    return result


def _migrate_workflow(path: Path, text: str) -> str:
    start, end = _event_block_span(text)
    migrated = text[:start] + WORKFLOW_CALL_BLOCK + "\n" + text[end:]
    for old, new in SOURCE_REPLACEMENTS:
        migrated = migrated.replace(old, new)
    leftovers = sorted(set(re.findall(r"github\.event\.(?:comment|issue)\.[A-Za-z0-9_.]+", migrated)))
    if leftovers:
        raise RuntimeError(f"{path.name}: unmigrated source event fields: {leftovers}")
    if re.search(r"(?m)^  issue_comment:\s*$", migrated):
        raise RuntimeError(f"{path.name}: direct issue_comment trigger survived migration")
    return migrated


def _router_yaml(routes: list[dict[str, object]], permissions: dict[str, str]) -> str:
    permission_lines = "\n".join(f"  {key}: {value}" for key, value in sorted(permissions.items()))
    calls: list[str] = []
    for route in routes:
        operation = str(route["operation"])
        job_id = "op_" + re.sub(r"[^A-Za-z0-9_]", "_", operation)
        workflow = str(route["workflow"])
        calls.append(
            f"""  {job_id}:
    name: Route {operation}
    needs: route
    if: needs.route.outputs.operation == '{operation}'
    uses: ./.github/workflows/{workflow}
    with:
      command: ${{{{ github.event.comment.body }}}}
      source_event_name: issue_comment
      source_issue_number: ${{{{ github.event.issue.number }}}}
      source_comment_id: ${{{{ github.event.comment.id }}}}
      source_actor: ${{{{ github.event.comment.user.login }}}}
      source_comment_url: ${{{{ github.event.comment.html_url }}}}
      control_request_id: ${{{{ needs.route.outputs.request_id }}}}
      control_request_json: ${{{{ needs.route.outputs.request_json }}}}
    secrets: inherit
"""
        )

    return f"""name: Production control — strict Issue #1 router

on:
  issue_comment:
    types: [created]

permissions:
{permission_lines}

jobs:
  route:
    name: Classify one owner command exactly once
    if: >-
      github.event.issue.number == 1 &&
      github.event.comment.user.login == github.repository_owner &&
      startsWith(github.event.comment.body, '/')
    runs-on: ubuntu-latest
    timeout-minutes: 5
    outputs:
      operation: ${{{{ steps.route.outputs.operation }}}}
      request_id: ${{{{ steps.route.outputs.request_id }}}}
      request_json: ${{{{ steps.route.outputs.request_json }}}}
    steps:
      - name: Check out exact private execution revision
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        with:
          ref: ${{{{ github.sha }}}}

      - name: Strictly classify command and build semantic request envelope
        id: route
        shell: bash
        env:
          COMMAND: ${{{{ github.event.comment.body }}}}
        run: |
          set -euo pipefail
          python3 .github/scripts/production_command_router.py \\
            --registry .github/production/command-routes.json \\
            --command "$COMMAND" \\
            --repository "${{{{ github.repository }}}}" \\
            --issue-number "${{{{ github.event.issue.number }}}}" \\
            --comment-id "${{{{ github.event.comment.id }}}}" \\
            --actor "${{{{ github.event.comment.user.login }}}}" \\
            --owner "${{{{ github.repository_owner }}}}"

{chr(10).join(calls)}"""


def main() -> int:
    candidates: list[tuple[Path, str, str, dict[str, str]]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if path.name in EXCLUDED:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"(?m)^  issue_comment:\s*$", text) is None:
            continue
        command = _extract_command(text, path)
        candidates.append((path, text, command, _top_level_permissions(text)))

    if not candidates:
        # Idempotent second run after the migration commit.
        if ROUTER.is_file() and REGISTRY.is_file():
            print("C0O_MIGRATION_ALREADY_APPLIED")
            return 0
        raise RuntimeError("no issue_comment command workflows discovered")

    commands = [command for _, _, command, _ in candidates]
    if len(commands) != len(set(commands)):
        raise RuntimeError("duplicate production command prefixes across workflows")

    discovered_names = {path.name for path, _, _, _ in candidates}
    missing_mutators = sorted(MUTATORS - discovered_names)
    if missing_mutators:
        raise RuntimeError("classified mutator missing from command surface: " + ", ".join(missing_mutators))

    routes: list[dict[str, object]] = []
    for path, text, command, _ in candidates:
        operation = path.stem
        routes.append(
            {
                "command": command,
                "operation": operation,
                "workflow": path.name,
                "mutating": path.name in MUTATORS,
            }
        )
        migrated = _migrate_workflow(path, text)
        path.write_text(migrated, encoding="utf-8")

    routes.sort(key=lambda item: str(item["command"]))
    registry = {
        "schema": REGISTRY_SCHEMA,
        "authority_cursor": AUTHORITY_CURSOR,
        "routes": routes,
    }
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    permissions = _merge_permissions([permissions for _, _, _, permissions in candidates])
    ROUTER.write_text(_router_yaml(routes, permissions), encoding="utf-8")

    print(f"C0O_MIGRATED_WORKFLOWS={len(candidates)}")
    print(f"C0O_ROUTES={len(routes)}")
    print("C0O_MUTATORS=" + ",".join(sorted(MUTATORS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
