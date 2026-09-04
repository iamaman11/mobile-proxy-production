#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

WORKFLOWS = Path('.github/workflows')
REGISTRY = Path('.github/production/command-control-registry.json')
NONAUTHORITY_CLASSIFICATION = Path(
    '.github/production/non-authority-action-runtime-classification.json'
)

# Exact immutable action commits independently verified to declare
# runs.using=node24 in upstream action metadata. Multiple immutable Node24
# commits are permitted where the repository already uses one safely; the audit
# is about runtime class + immutability, not forcing unrelated version churn.
APPROVED_NODE24_PINS = {
    'actions/checkout': {
        '3d3c42e5aac5ba805825da76410c181273ba90b1',  # v7.0.1
    },
    'actions/upload-artifact': {
        '043fb46d1a93c77aae656e7c1c64a875d1fc6a0a',  # v7.0.1
    },
    'actions/download-artifact': {
        '3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c',  # v8.0.1
    },
    'actions/setup-java': {
        'dd06d9cba3e5552c54d9f8ea23572deb30010f7c',  # v6.0.0
        'b6effb05e454b25005698d916606bdc6ffcbf961',  # verified node24 immutable commit
    },
    'actions/github-script': {
        'ed597411d8f924073f98dfc5c65a23a2325f34cd',  # v8, node24
    },
}

ALLOWED_NONAUTHORITY_CLASSES = {
    'retired/historical',
    'rehearsal-only',
    'independently manual/diagnostic',
}
EXPECTED_STARTING_INVENTORY = {
    'source_stage': '2AF',
    'source_private_main': '92413eab76503fbc440964031a829c9914ab975a',
    'source_scanner_run_id': 33925743186,
    'source_scanner_job_id': 101193752503,
    'workflow_count': 23,
    'legacy_action_ref_count': 86,
}

USES_RE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*[\"']?(?P<action>actions/[A-Za-z0-9_.-]+)@(?P<ref>[^\s#\"']+)",
    re.MULTILINE,
)
LOCAL_WORKFLOW_RE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*[\"']?(?P<path>\./\.github/workflows/[^\s#\"']+)",
    re.MULTILINE,
)
POLICY_TRIGGER_RE = re.compile(r'(?m)^  (?:push|pull_request):(?:\s|$)')
SHA_RE = re.compile(r'^[0-9a-f]{40}$')


def classify(action: str, ref: str) -> str | None:
    if SHA_RE.fullmatch(ref) is None:
        return 'floating-action-ref'
    approved = APPROVED_NODE24_PINS.get(action)
    if approved is None:
        return 'immutable-pin-without-verified-node24-approval'
    if ref not in approved:
        return 'immutable-pin-not-verified-node24'
    return None


def current_authority_workflows(paths: list[Path], texts: dict[Path, str]) -> set[Path]:
    path_by_posix = {path.as_posix(): path for path in paths}
    authority: set[Path] = set()

    # Hosted push/PR policies are current CI authority.
    for path in paths:
        if POLICY_TRIGGER_RE.search(texts[path]):
            authority.add(path)

    # Sole production ingress and the current read-only signing observer are
    # explicit current authority even when they are reusable-only workflows.
    for explicit in (
        '.github/workflows/production-control-router.yml',
        '.github/workflows/phone-signing-identity.yml',
    ):
        if explicit not in path_by_posix:
            raise SystemExit(f'current-authority workflow missing: {explicit}')
        authority.add(path_by_posix[explicit])

    # Active declarative command routes define the production execution closure.
    registry = json.loads(REGISTRY.read_text(encoding='utf-8'))
    routes = registry.get('routes')
    if not isinstance(routes, list):
        raise SystemExit('command-control registry routes are unavailable')
    for route in routes:
        if not isinstance(route, dict) or route.get('enabled') is not True:
            continue
        workflow = route.get('workflow')
        if not isinstance(workflow, str) or workflow not in path_by_posix:
            raise SystemExit(f'active route workflow missing from repository: {workflow!r}')
        authority.add(path_by_posix[workflow])

    # Recursively include local reusable workflows called by any authority file.
    changed = True
    while changed:
        changed = False
        for path in tuple(authority):
            for match in LOCAL_WORKFLOW_RE.finditer(texts[path]):
                local = match.group('path')[2:]
                target = path_by_posix.get(local)
                if target is None:
                    raise SystemExit(f'authority workflow references missing local workflow: {local}')
                if target not in authority:
                    authority.add(target)
                    changed = True
    return authority


def load_nonauthority_classification(
    path_by_posix: dict[str, Path],
) -> tuple[dict[str, dict[str, object]], dict[str, Counter[str]]]:
    if not NONAUTHORITY_CLASSIFICATION.is_file():
        raise SystemExit('non-authority Action runtime classification missing')

    try:
        document = json.loads(NONAUTHORITY_CLASSIFICATION.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise SystemExit(f'non-authority Action runtime classification is invalid JSON: {error}') from error

    if document.get('schema') != 'non-authority-action-runtime-classification.v1':
        raise SystemExit('non-authority Action runtime classification schema mismatch')
    if document.get('starting_inventory') != EXPECTED_STARTING_INVENTORY:
        raise SystemExit('Stage 2AF non-authority starting inventory metadata mismatch')

    entries = document.get('workflows')
    if not isinstance(entries, dict):
        raise SystemExit('non-authority Action runtime classifications are unavailable')

    by_path: dict[str, dict[str, object]] = {}
    baseline_refs_by_path: dict[str, Counter[str]] = {}
    for workflow, entry in entries.items():
        if not isinstance(workflow, str) or workflow not in path_by_posix:
            raise SystemExit(f'classified non-authority workflow missing from repository: {workflow!r}')
        if not isinstance(entry, dict):
            raise SystemExit(f'classification for {workflow} must be an object')
        classification = entry.get('classification')
        baseline_refs = entry.get('stage_2af_legacy_refs')
        rationale = entry.get('rationale')
        if classification not in ALLOWED_NONAUTHORITY_CLASSES:
            raise SystemExit(
                f'invalid non-authority workflow classification for {workflow}: {classification!r}'
            )
        if not isinstance(baseline_refs, dict) or not baseline_refs:
            raise SystemExit(f'Stage 2AF legacy Action refs missing for {workflow}')
        if not isinstance(rationale, str) or not rationale.strip():
            raise SystemExit(f'non-authority workflow rationale missing: {workflow}')

        counter: Counter[str] = Counter()
        for ref, count in baseline_refs.items():
            if not isinstance(ref, str) or not ref.startswith('actions/') or '@' not in ref:
                raise SystemExit(f'invalid Stage 2AF legacy Action ref for {workflow}: {ref!r}')
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise SystemExit(
                    f'invalid Stage 2AF legacy Action ref count for {workflow}: {count!r}'
                )
            counter[ref] = count

        by_path[workflow] = entry
        baseline_refs_by_path[workflow] = counter

    if len(by_path) != EXPECTED_STARTING_INVENTORY['workflow_count']:
        raise SystemExit(
            'non-authority classified workflow count differs from Stage 2AF starting inventory'
        )
    baseline_total = sum(sum(counter.values()) for counter in baseline_refs_by_path.values())
    if baseline_total != EXPECTED_STARTING_INVENTORY['legacy_action_ref_count']:
        raise SystemExit(
            'classified Stage 2AF legacy Action refs differ from the exact starting inventory'
        )
    return by_path, baseline_refs_by_path


def main() -> int:
    if not WORKFLOWS.is_dir():
        print('workflow directory missing', file=sys.stderr)
        return 2
    if not REGISTRY.is_file():
        print('command-control registry missing', file=sys.stderr)
        return 2

    paths = sorted([*WORKFLOWS.glob('*.yml'), *WORKFLOWS.glob('*.yaml')])
    texts = {path: path.read_text(encoding='utf-8') for path in paths}
    path_by_posix = {path.as_posix(): path for path in paths}
    authority = current_authority_workflows(paths, texts)
    classification_by_path, baseline_refs_by_path = load_nonauthority_classification(
        path_by_posix
    )

    inventory: list[tuple[Path, int, str, str]] = []
    authority_violations: list[str] = []
    nonauthority_debt: list[str] = []
    nonauthority_debt_refs_by_path: dict[str, Counter[str]] = defaultdict(Counter)

    for path in paths:
        text = texts[path]
        for match in USES_RE.finditer(text):
            action = match.group('action')
            ref = match.group('ref')
            line = text.count('\n', 0, match.start()) + 1
            inventory.append((path, line, action, ref))
            problem = classify(action, ref)
            if problem is None:
                continue
            record = f'{path}:{line}: {problem}: {action}@{ref}'
            if path in authority:
                authority_violations.append(record)
            else:
                nonauthority_debt.append(record)
                nonauthority_debt_refs_by_path[path.as_posix()][f'{action}@{ref}'] += 1

    classification_violations: list[str] = []
    authority_paths = {path.as_posix() for path in authority}
    classified_paths = set(classification_by_path)
    current_debt_paths = {
        workflow for workflow, refs in nonauthority_debt_refs_by_path.items() if refs
    }

    # Classified legacy workflows are a fail-closed admission boundary: none may
    # silently enter the current Issue #1/current-authority closure.
    for workflow in sorted(classified_paths & authority_paths):
        classification_violations.append(
            f'classified non-authority workflow entered current authority: {workflow}'
        )

    # New non-authority legacy debt must be classified explicitly.
    for workflow in sorted(current_debt_paths - classified_paths):
        classification_violations.append(
            f'non-authority legacy Action debt lacks classification: {workflow}'
        )

    # Stage 2AF is the immutable upper bound. Targeted future remediation may
    # remove old refs, but no new legacy ref or occurrence may be added silently.
    for workflow in sorted(classified_paths):
        baseline = baseline_refs_by_path[workflow]
        current = nonauthority_debt_refs_by_path.get(workflow, Counter())
        for ref, count in sorted(current.items()):
            baseline_count = baseline.get(ref, 0)
            if count > baseline_count:
                classification_violations.append(
                    f'classified legacy Action debt expanded for {workflow}: '
                    f'{ref} baseline={baseline_count} current={count}'
                )

    class_workflows = Counter(
        str(entry['classification']) for entry in classification_by_path.values()
    )
    class_baseline_debt: Counter[str] = Counter()
    class_current_debt: Counter[str] = Counter()
    for workflow, entry in classification_by_path.items():
        classification = str(entry['classification'])
        class_baseline_debt[classification] += sum(baseline_refs_by_path[workflow].values())
        class_current_debt[classification] += sum(
            nonauthority_debt_refs_by_path.get(workflow, Counter()).values()
        )

    counts = Counter(action for _, _, action, _ in inventory)
    authority_refs = sum(1 for path, _, _, _ in inventory if path in authority)
    print(
        'ACTION_RUNTIME_PIN_INVENTORY '
        f'workflows={len(paths)} refs={len(inventory)} '
        f'authority_workflows={len(authority)} authority_refs={authority_refs}'
    )
    for action in sorted(counts):
        print(f'ACTION_RUNTIME_PIN_COUNT action={action} count={counts[action]}')
    for path in sorted(authority):
        print(f'ACTION_RUNTIME_AUTHORITY_WORKFLOW path={path}')
    for path, line, action, ref in inventory:
        scope = 'authority' if path in authority else 'non-authority'
        print(
            f'ACTION_RUNTIME_PIN_REF scope={scope} path={path} line={line} '
            f'ref={action}@{ref}'
        )
    print(f'ACTION_RUNTIME_NONAUTHORITY_DEBT count={len(nonauthority_debt)}')
    for item in nonauthority_debt:
        print(f'NONAUTHORITY_DEBT {item}')

    print(
        'ACTION_RUNTIME_NONAUTHORITY_CLASSIFICATION '
        f'workflows={len(classification_by_path)} '
        f"baseline_debt={EXPECTED_STARTING_INVENTORY['legacy_action_ref_count']} "
        f'current_debt={len(nonauthority_debt)} '
        f"retired_historical_workflows={class_workflows['retired/historical']} "
        f"retired_historical_baseline_refs={class_baseline_debt['retired/historical']} "
        f"retired_historical_current_refs={class_current_debt['retired/historical']} "
        f"rehearsal_only_workflows={class_workflows['rehearsal-only']} "
        f"rehearsal_only_baseline_refs={class_baseline_debt['rehearsal-only']} "
        f"rehearsal_only_current_refs={class_current_debt['rehearsal-only']} "
        'independently_manual_diagnostic_workflows='
        f"{class_workflows['independently manual/diagnostic']} "
        'independently_manual_diagnostic_baseline_refs='
        f"{class_baseline_debt['independently manual/diagnostic']} "
        'independently_manual_diagnostic_current_refs='
        f"{class_current_debt['independently manual/diagnostic']}"
    )
    for workflow in sorted(classification_by_path):
        entry = classification_by_path[workflow]
        print(
            'ACTION_RUNTIME_NONAUTHORITY_WORKFLOW '
            f"classification={entry['classification']} path={workflow} "
            f'baseline_legacy_refs={sum(baseline_refs_by_path[workflow].values())} '
            f'current_legacy_refs={sum(nonauthority_debt_refs_by_path.get(workflow, Counter()).values())} '
            f'current_authority={str(workflow in authority_paths).lower()}'
        )

    if authority_violations or classification_violations:
        print(
            'ACTION_RUNTIME_PIN_AUDIT_FAIL '
            f'authority_violations={len(authority_violations)} '
            f'classification_violations={len(classification_violations)}',
            file=sys.stderr,
        )
        for violation in authority_violations:
            print(f'AUTHORITY_VIOLATION {violation}', file=sys.stderr)
        for violation in classification_violations:
            print(f'CLASSIFICATION_VIOLATION {violation}', file=sys.stderr)
        return 1

    print(
        'ACTION_RUNTIME_PIN_AUDIT_OK '
        f'authority_refs={authority_refs} nonauthority_debt={len(nonauthority_debt)} '
        f'classified_nonauthority_workflows={len(classification_by_path)}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
