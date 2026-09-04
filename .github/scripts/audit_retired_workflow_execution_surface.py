#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from audit_actions_runtime_pins import current_authority_workflows

WORKFLOWS = Path('.github/workflows')
REGISTRY = Path('.github/production/command-control-registry.json')
CLASSIFICATION = Path('.github/production/non-authority-action-runtime-classification.json')
LOCAL_WORKFLOW_RE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*[\"']?(?P<path>\./\.github/workflows/[^\s#\"']+)",
    re.MULTILINE,
)
EXPECTED_RETIRED_WORKFLOWS = 12
EXPECTED_RETIRED_LEGACY_REFS = 55


def trigger_surface(text: str) -> set[str]:
    lines = text.splitlines()
    on_index = None
    inline = None
    for index, line in enumerate(lines):
        if line.startswith('on:'):
            on_index = index
            inline = line[3:].strip()
            break
    if on_index is None:
        raise ValueError('missing top-level on: trigger block')
    if inline:
        raise ValueError(f'inline trigger syntax is not permitted for retired workflows: {inline!r}')

    triggers: set[str] = set()
    for line in lines[on_index + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(line) - len(line.lstrip(' '))
        if indent == 0:
            break
        if indent != 2:
            continue
        match = re.fullmatch(r'([A-Za-z0-9_-]+):(?:\s.*)?', stripped)
        if match is None:
            raise ValueError(f'unrecognized top-level trigger line: {line!r}')
        triggers.add(match.group(1))
    if not triggers:
        raise ValueError('empty top-level on: trigger block')
    return triggers


def main() -> int:
    if not WORKFLOWS.is_dir() or not REGISTRY.is_file() or not CLASSIFICATION.is_file():
        print('retired execution-surface inputs missing', file=sys.stderr)
        return 2

    document = json.loads(CLASSIFICATION.read_text(encoding='utf-8'))
    if document.get('schema') != 'non-authority-action-runtime-classification.v1':
        raise SystemExit('Stage 2AG classification schema mismatch')
    entries = document.get('workflows')
    if not isinstance(entries, dict):
        raise SystemExit('Stage 2AG workflow classification inventory unavailable')

    retired: dict[str, dict[str, object]] = {
        path: entry
        for path, entry in entries.items()
        if isinstance(entry, dict) and entry.get('classification') == 'retired/historical'
    }
    retired_legacy_refs = sum(
        sum(int(count) for count in entry.get('stage_2af_legacy_refs', {}).values())
        for entry in retired.values()
    )
    if len(retired) != EXPECTED_RETIRED_WORKFLOWS:
        raise SystemExit(
            f'retired/historical workflow inventory differs: expected={EXPECTED_RETIRED_WORKFLOWS} actual={len(retired)}'
        )
    if retired_legacy_refs != EXPECTED_RETIRED_LEGACY_REFS:
        raise SystemExit(
            f'retired/historical legacy-ref inventory differs: expected={EXPECTED_RETIRED_LEGACY_REFS} actual={retired_legacy_refs}'
        )

    paths = sorted([*WORKFLOWS.glob('*.yml'), *WORKFLOWS.glob('*.yaml')])
    texts = {path: path.read_text(encoding='utf-8') for path in paths}
    path_by_posix = {path.as_posix(): path for path in paths}
    missing = sorted(set(retired) - set(path_by_posix))
    if missing:
        raise SystemExit(f'retired/historical workflow missing from repository: {missing}')

    authority = current_authority_workflows(paths, texts)
    authority_paths = {path.as_posix() for path in authority}

    registry = json.loads(REGISTRY.read_text(encoding='utf-8'))
    routes = registry.get('routes')
    if not isinstance(routes, list):
        raise SystemExit('Issue #1 route registry unavailable')
    enabled_routes = {
        route.get('workflow')
        for route in routes
        if isinstance(route, dict) and route.get('enabled') is True
    }

    callers: dict[str, set[str]] = defaultdict(set)
    for caller, text in texts.items():
        for match in LOCAL_WORKFLOW_RE.finditer(text):
            target = match.group('path')[2:]
            callers[target].add(caller.as_posix())

    violations: list[str] = []
    records: list[str] = []
    for workflow in sorted(retired):
        text = texts[path_by_posix[workflow]]
        try:
            triggers = trigger_surface(text)
        except ValueError as error:
            violations.append(f'{workflow}: {error}')
            triggers = set()

        # Stage 2AH retirement invariant: historical source is reusable-only.
        # Any independent trigger, including workflow_run, is production-capable
        # drift and must fail closed rather than silently reactivating history.
        if triggers != {'workflow_call'}:
            violations.append(
                f'{workflow}: retired workflow trigger surface must be workflow_call-only; actual={sorted(triggers)}'
            )
        if workflow in enabled_routes:
            violations.append(f'{workflow}: retired workflow is reachable from enabled Issue #1 registry')
        if workflow in authority_paths:
            violations.append(f'{workflow}: retired workflow entered current-authority recursive closure')

        direct_callers = sorted(callers.get(workflow, set()))
        authority_callers = sorted(set(direct_callers) & authority_paths)
        if authority_callers:
            violations.append(
                f'{workflow}: retired workflow has current-authority local caller(s): {authority_callers}'
            )

        records.append(
            'RETIRED_EXECUTION_SURFACE_WORKFLOW '
            f'path={workflow} triggers={",".join(sorted(triggers)) or "NONE"} '
            f'direct_callers={len(direct_callers)} authority_callers={len(authority_callers)} '
            f'issue1_enabled={str(workflow in enabled_routes).lower()} '
            f'current_authority={str(workflow in authority_paths).lower()}'
        )
        for caller in direct_callers:
            print(
                'RETIRED_EXECUTION_SURFACE_CALLER '
                f'target={workflow} caller={caller} '
                f'caller_current_authority={str(caller in authority_paths).lower()}'
            )

    print(
        'RETIRED_EXECUTION_SURFACE_INVENTORY '
        f'workflows={len(retired)} legacy_refs={retired_legacy_refs} '
        f'authority_workflows={len(authority_paths)} enabled_issue1_routes={len(enabled_routes)}'
    )
    for record in records:
        print(record)

    if violations:
        print(
            f'RETIRED_EXECUTION_SURFACE_AUDIT_FAIL violations={len(violations)}',
            file=sys.stderr,
        )
        for violation in violations:
            print(f'RETIRED_EXECUTION_SURFACE_VIOLATION {violation}', file=sys.stderr)
        return 1

    print(
        'RETIRED_EXECUTION_SURFACE_AUDIT_OK '
        f'workflows={len(retired)} legacy_refs={retired_legacy_refs} '
        'independent_triggers=0 issue1_reachable=0 current_authority_reachable=0'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
