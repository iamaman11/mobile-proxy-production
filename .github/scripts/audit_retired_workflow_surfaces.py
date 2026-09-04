#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from audit_actions_runtime_pins import LOCAL_WORKFLOW_RE, current_authority_workflows

WORKFLOWS = Path('.github/workflows')
REGISTRY = Path('.github/production/command-control-registry.json')
CLASSIFICATION = Path('.github/production/non-authority-action-runtime-classification.json')

RETIRED_CLASS = 'retired/historical'
EXPECTED_RETIRED_WORKFLOWS = 12
EXPECTED_RETIRED_BASELINE_REFS = 55
ALLOWED_RETIRED_TRIGGERS = {'workflow_call'}


def parse_on_triggers(text: str, workflow: str) -> set[str]:
    lines = text.splitlines()
    on_index = None
    inline = None
    for index, line in enumerate(lines):
        if not line or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'on:\s*(.*)', line)
        if match:
            on_index = index
            inline = match.group(1).strip()
            break
    if on_index is None:
        raise ValueError(f'workflow has no top-level on trigger block: {workflow}')

    if inline:
        if inline == 'workflow_call':
            return {'workflow_call'}
        raise ValueError(f'unsupported inline retired trigger syntax for {workflow}: {inline!r}')

    triggers: set[str] = set()
    for line in lines[on_index + 1:]:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if not line.startswith((' ', '\t')):
            break
        match = re.match(r'^  ([A-Za-z0-9_-]+):(?:\s.*)?$', line)
        if match:
            triggers.add(match.group(1))
    if not triggers:
        raise ValueError(f'retired workflow has empty/unparseable trigger block: {workflow}')
    return triggers


def main() -> int:
    if not WORKFLOWS.is_dir():
        print('retired workflow audit: workflow directory missing', file=sys.stderr)
        return 2
    if not REGISTRY.is_file():
        print('retired workflow audit: command registry missing', file=sys.stderr)
        return 2
    if not CLASSIFICATION.is_file():
        print('retired workflow audit: Stage 2AG classification missing', file=sys.stderr)
        return 2

    paths = sorted([*WORKFLOWS.glob('*.yml'), *WORKFLOWS.glob('*.yaml')])
    texts = {path: path.read_text(encoding='utf-8') for path in paths}
    path_by_posix = {path.as_posix(): path for path in paths}

    document = json.loads(CLASSIFICATION.read_text(encoding='utf-8'))
    if document.get('schema') != 'non-authority-action-runtime-classification.v1':
        print('retired workflow audit: classification schema mismatch', file=sys.stderr)
        return 2
    entries = document.get('workflows')
    if not isinstance(entries, dict):
        print('retired workflow audit: classification entries unavailable', file=sys.stderr)
        return 2

    retired: dict[str, dict[str, object]] = {}
    baseline_refs = 0
    for workflow, entry in entries.items():
        if not isinstance(entry, dict) or entry.get('classification') != RETIRED_CLASS:
            continue
        if workflow not in path_by_posix:
            print(f'retired workflow audit: classified workflow missing: {workflow}', file=sys.stderr)
            return 2
        legacy = entry.get('stage_2af_legacy_refs')
        if not isinstance(legacy, dict) or not legacy:
            print(f'retired workflow audit: baseline refs unavailable: {workflow}', file=sys.stderr)
            return 2
        count = 0
        for ref, occurrences in legacy.items():
            if (
                not isinstance(ref, str)
                or not isinstance(occurrences, int)
                or isinstance(occurrences, bool)
                or occurrences < 1
            ):
                print(f'retired workflow audit: malformed baseline ref: {workflow}', file=sys.stderr)
                return 2
            count += occurrences
        baseline_refs += count
        retired[workflow] = entry

    if len(retired) != EXPECTED_RETIRED_WORKFLOWS:
        print(
            'retired workflow audit: Stage 2AG retired workflow count drifted '
            f'expected={EXPECTED_RETIRED_WORKFLOWS} actual={len(retired)}',
            file=sys.stderr,
        )
        return 2
    if baseline_refs != EXPECTED_RETIRED_BASELINE_REFS:
        print(
            'retired workflow audit: Stage 2AG retired legacy-ref baseline drifted '
            f'expected={EXPECTED_RETIRED_BASELINE_REFS} actual={baseline_refs}',
            file=sys.stderr,
        )
        return 2

    registry = json.loads(REGISTRY.read_text(encoding='utf-8'))
    routes = registry.get('routes')
    if not isinstance(routes, list):
        print('retired workflow audit: registry routes unavailable', file=sys.stderr)
        return 2
    enabled_routes: set[str] = set()
    for route in routes:
        if not isinstance(route, dict) or route.get('enabled') is not True:
            continue
        workflow = route.get('workflow')
        if not isinstance(workflow, str):
            print('retired workflow audit: enabled route workflow malformed', file=sys.stderr)
            return 2
        enabled_routes.add(workflow)

    authority = current_authority_workflows(paths, texts)
    authority_paths = {path.as_posix() for path in authority}

    callers: dict[str, set[str]] = defaultdict(set)
    for caller, text in texts.items():
        for match in LOCAL_WORKFLOW_RE.finditer(text):
            target = match.group('path')[2:]
            if target in path_by_posix:
                callers[target].add(caller.as_posix())

    violations: list[str] = []
    records: list[tuple[str, set[str], list[str], list[str], bool, bool]] = []
    for workflow in sorted(retired):
        try:
            triggers = parse_on_triggers(texts[path_by_posix[workflow]], workflow)
        except ValueError as error:
            violations.append(str(error))
            triggers = {'UNPARSEABLE'}

        workflow_callers = sorted(callers.get(workflow, set()))
        authority_callers = sorted(set(workflow_callers) & authority_paths)
        enabled_route = workflow in enabled_routes
        current_authority = workflow in authority_paths

        if triggers != ALLOWED_RETIRED_TRIGGERS:
            violations.append(
                f'retired workflow exposes non-reusable trigger surface: '
                f'{workflow} triggers={",".join(sorted(triggers))}'
            )
        if enabled_route:
            violations.append(f'retired workflow entered enabled Issue #1 route: {workflow}')
        if current_authority:
            violations.append(f'retired workflow entered current-authority closure: {workflow}')
        if authority_callers:
            violations.append(
                f'retired workflow has current-authority local caller: '
                f'{workflow} callers={",".join(authority_callers)}'
            )

        records.append(
            (
                workflow,
                triggers,
                workflow_callers,
                authority_callers,
                enabled_route,
                current_authority,
            )
        )

    print(
        'RETIRED_WORKFLOW_SURFACE_INVENTORY '
        f'workflows={len(retired)} baseline_legacy_refs={baseline_refs}'
    )
    for (
        workflow,
        triggers,
        workflow_callers,
        authority_callers,
        enabled_route,
        current_authority,
    ) in records:
        print(
            'RETIRED_WORKFLOW_SURFACE '
            f'path={workflow} '
            f'triggers={",".join(sorted(triggers))} '
            f'callers={",".join(workflow_callers) if workflow_callers else "none"} '
            f'authority_callers={",".join(authority_callers) if authority_callers else "none"} '
            f'enabled_issue1_route={str(enabled_route).lower()} '
            f'current_authority={str(current_authority).lower()}'
        )

    if violations:
        print(
            f'RETIRED_WORKFLOW_SURFACE_AUDIT_FAIL violations={len(violations)}',
            file=sys.stderr,
        )
        for violation in violations:
            print(f'RETIRED_WORKFLOW_SURFACE_VIOLATION {violation}', file=sys.stderr)
        return 1

    print(
        'RETIRED_WORKFLOW_SURFACE_AUDIT_OK '
        f'workflows={len(retired)} allowed_trigger=workflow_call'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
