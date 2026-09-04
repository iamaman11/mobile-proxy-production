#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from audit_actions_runtime_pins import (
    LOCAL_WORKFLOW_RE,
    USES_RE,
    classify,
    current_authority_workflows,
)
from audit_retired_workflow_surfaces import parse_on_triggers

WORKFLOWS = Path('.github/workflows')
REGISTRY = Path('.github/production/command-control-registry.json')
CLASSIFICATION = Path('.github/production/non-authority-action-runtime-classification.json')
DISPOSITION = Path('.github/production/non-authority-execution-surface-disposition.json')

IN_SCOPE_CLASSES = {'rehearsal-only', 'independently manual/diagnostic'}
EXPECTED_WORKFLOWS = 11
EXPECTED_BASELINE_REFS = 31
EXPECTED_REHEARSAL_WORKFLOWS = 1
EXPECTED_DIAGNOSTIC_WORKFLOWS = 10
ALLOWED_TRIGGERS = {'workflow_call'}
EXPECTED_DISPOSITION = 'reusable-only-unreachable'


def main() -> int:
    required = (WORKFLOWS, REGISTRY, CLASSIFICATION, DISPOSITION)
    if any(not path.exists() for path in required):
        print('non-authority surface audit: required input missing', file=sys.stderr)
        return 2

    paths = sorted([*WORKFLOWS.glob('*.yml'), *WORKFLOWS.glob('*.yaml')])
    texts = {path: path.read_text(encoding='utf-8') for path in paths}
    path_by_posix = {path.as_posix(): path for path in paths}

    classification_doc = json.loads(CLASSIFICATION.read_text(encoding='utf-8'))
    if classification_doc.get('schema') != 'non-authority-action-runtime-classification.v1':
        print('non-authority surface audit: Stage 2AG classification schema mismatch', file=sys.stderr)
        return 2
    classification_entries = classification_doc.get('workflows')
    if not isinstance(classification_entries, dict):
        print('non-authority surface audit: Stage 2AG workflow inventory unavailable', file=sys.stderr)
        return 2

    scoped: dict[str, dict[str, object]] = {}
    baseline_by_path: dict[str, Counter[str]] = {}
    class_counts: Counter[str] = Counter()
    baseline_total = 0
    for workflow, entry in classification_entries.items():
        if not isinstance(entry, dict) or entry.get('classification') not in IN_SCOPE_CLASSES:
            continue
        if workflow not in path_by_posix:
            print(f'non-authority surface audit: classified workflow missing: {workflow}', file=sys.stderr)
            return 2
        legacy = entry.get('stage_2af_legacy_refs')
        if not isinstance(legacy, dict) or not legacy:
            print(f'non-authority surface audit: baseline refs unavailable: {workflow}', file=sys.stderr)
            return 2
        counter: Counter[str] = Counter()
        for ref, occurrences in legacy.items():
            if (
                not isinstance(ref, str)
                or not isinstance(occurrences, int)
                or isinstance(occurrences, bool)
                or occurrences < 1
            ):
                print(f'non-authority surface audit: malformed baseline ref: {workflow}', file=sys.stderr)
                return 2
            counter[ref] = occurrences
        baseline_by_path[workflow] = counter
        baseline_total += sum(counter.values())
        classification = str(entry['classification'])
        class_counts[classification] += 1
        scoped[workflow] = entry

    if len(scoped) != EXPECTED_WORKFLOWS or baseline_total != EXPECTED_BASELINE_REFS:
        print(
            'non-authority surface audit: Stage 2AG scoped inventory drifted '
            f'workflows={len(scoped)} baseline_refs={baseline_total}',
            file=sys.stderr,
        )
        return 2
    if (
        class_counts['rehearsal-only'] != EXPECTED_REHEARSAL_WORKFLOWS
        or class_counts['independently manual/diagnostic'] != EXPECTED_DIAGNOSTIC_WORKFLOWS
    ):
        print('non-authority surface audit: Stage 2AG class counts drifted', file=sys.stderr)
        return 2

    disposition_doc = json.loads(DISPOSITION.read_text(encoding='utf-8'))
    expected_header = {
        'schema': 'non-authority-execution-surface-disposition.v1',
        'source_classification_stage': '2AG',
        'source_classification_private_main': 'a15c6f70fae257784c8d3d5588cdea2a56cfc3ab',
        'stage_2ai_base_private_main': '026bb4917c8177917e8600c2843ec946d7c71b16',
        'workflow_count': EXPECTED_WORKFLOWS,
        'legacy_action_ref_count': EXPECTED_BASELINE_REFS,
    }
    for key, value in expected_header.items():
        if disposition_doc.get(key) != value:
            print(f'non-authority surface audit: disposition header mismatch: {key}', file=sys.stderr)
            return 2
    dispositions = disposition_doc.get('workflows')
    if not isinstance(dispositions, dict) or set(dispositions) != set(scoped):
        print('non-authority surface audit: disposition workflow inventory differs from Stage 2AG scope', file=sys.stderr)
        return 2

    registry = json.loads(REGISTRY.read_text(encoding='utf-8'))
    routes = registry.get('routes')
    if not isinstance(routes, list):
        print('non-authority surface audit: registry routes unavailable', file=sys.stderr)
        return 2
    enabled_routes: set[str] = set()
    for route in routes:
        if not isinstance(route, dict) or route.get('enabled') is not True:
            continue
        workflow = route.get('workflow')
        if not isinstance(workflow, str):
            print('non-authority surface audit: enabled route workflow malformed', file=sys.stderr)
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

    current_legacy_by_path: dict[str, Counter[str]] = defaultdict(Counter)
    for path in paths:
        for match in USES_RE.finditer(texts[path]):
            action = match.group('action')
            ref = match.group('ref')
            if classify(action, ref) is not None:
                current_legacy_by_path[path.as_posix()][f'{action}@{ref}'] += 1

    violations: list[str] = []
    records: list[tuple[str, str, set[str], list[str], list[str], bool, bool, int]] = []
    independently_runnable_count = 0

    for workflow in sorted(scoped):
        source_entry = scoped[workflow]
        disposition = dispositions.get(workflow)
        if not isinstance(disposition, dict):
            violations.append(f'disposition entry malformed: {workflow}')
            continue
        classification = str(source_entry['classification'])
        if disposition.get('classification') != classification:
            violations.append(f'disposition classification differs from Stage 2AG: {workflow}')
        if disposition.get('disposition') != EXPECTED_DISPOSITION:
            violations.append(f'unsupported Stage 2AI disposition: {workflow}')
        independently_runnable = disposition.get('independently_runnable')
        if independently_runnable is not False:
            violations.append(
                f'non-authority surface became independently runnable without separate authority: {workflow}'
            )
        if independently_runnable is True:
            independently_runnable_count += 1
        rationale = disposition.get('rationale')
        if not isinstance(rationale, str) or not rationale.strip():
            violations.append(f'disposition rationale missing: {workflow}')

        try:
            triggers = parse_on_triggers(texts[path_by_posix[workflow]], workflow)
        except ValueError as error:
            violations.append(str(error))
            triggers = {'UNPARSEABLE'}

        workflow_callers = sorted(callers.get(workflow, set()))
        authority_callers = sorted(set(workflow_callers) & authority_paths)
        enabled_route = workflow in enabled_routes
        current_authority = workflow in authority_paths

        if triggers != ALLOWED_TRIGGERS:
            violations.append(
                f'non-retired non-authority workflow exposes independent trigger: '
                f'{workflow} triggers={",".join(sorted(triggers))}'
            )
        if enabled_route:
            violations.append(f'non-authority workflow entered enabled Issue #1 route: {workflow}')
        if current_authority:
            violations.append(f'non-authority workflow entered current-authority closure: {workflow}')
        if authority_callers:
            violations.append(
                f'non-authority workflow has current-authority local caller: '
                f'{workflow} callers={",".join(authority_callers)}'
            )

        baseline = baseline_by_path[workflow]
        current = current_legacy_by_path.get(workflow, Counter())
        for ref, count in sorted(current.items()):
            if count > baseline.get(ref, 0):
                violations.append(
                    f'non-authority legacy Action debt expanded: {workflow} '
                    f'{ref} baseline={baseline.get(ref, 0)} current={count}'
                )

        records.append(
            (
                workflow,
                classification,
                triggers,
                workflow_callers,
                authority_callers,
                enabled_route,
                current_authority,
                sum(current.values()),
            )
        )

    current_legacy_total = sum(record[-1] for record in records)
    print(
        'NONRETIRED_NONAUTHORITY_SURFACE_INVENTORY '
        f'workflows={len(scoped)} baseline_legacy_refs={baseline_total} '
        f'current_legacy_refs={current_legacy_total} '
        f'rehearsal_only={class_counts["rehearsal-only"]} '
        f'independently_manual_diagnostic={class_counts["independently manual/diagnostic"]}'
    )
    for (
        workflow,
        classification,
        triggers,
        workflow_callers,
        authority_callers,
        enabled_route,
        current_authority,
        current_legacy_refs,
    ) in records:
        print(
            'NONRETIRED_NONAUTHORITY_SURFACE '
            f'path={workflow} classification={classification} '
            f'disposition={EXPECTED_DISPOSITION} independently_runnable=false '
            f'triggers={",".join(sorted(triggers))} '
            f'callers={",".join(workflow_callers) if workflow_callers else "none"} '
            f'authority_callers={",".join(authority_callers) if authority_callers else "none"} '
            f'enabled_issue1_route={str(enabled_route).lower()} '
            f'current_authority={str(current_authority).lower()} '
            f'current_legacy_refs={current_legacy_refs}'
        )

    if violations:
        print(
            f'NONRETIRED_NONAUTHORITY_SURFACE_AUDIT_FAIL violations={len(violations)}',
            file=sys.stderr,
        )
        for violation in violations:
            print(f'NONRETIRED_NONAUTHORITY_SURFACE_VIOLATION {violation}', file=sys.stderr)
        return 1

    print(
        'NONRETIRED_NONAUTHORITY_SURFACE_AUDIT_OK '
        f'workflows={len(scoped)} allowed_trigger=workflow_call '
        f'independently_runnable={independently_runnable_count} '
        'enabled_issue1_reachable=0 current_authority_reachable=0'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
