#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

WORKFLOWS = Path('.github/workflows')
REGISTRY = Path('.github/production/command-control-registry.json')

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

USES_RE = re.compile(
    r'^\s*(?:-\s*)?uses:\s*["\']?(?P<action>actions/[A-Za-z0-9_.-]+)@(?P<ref>[^\s#"\']+)',
    re.MULTILINE,
)
LOCAL_WORKFLOW_RE = re.compile(
    r'^\s*(?:-\s*)?uses:\s*["\']?(?P<path>\./\.github/workflows/[^\s#"\']+)',
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


def main() -> int:
    if not WORKFLOWS.is_dir():
        print('workflow directory missing', file=sys.stderr)
        return 2
    if not REGISTRY.is_file():
        print('command-control registry missing', file=sys.stderr)
        return 2

    paths = sorted([*WORKFLOWS.glob('*.yml'), *WORKFLOWS.glob('*.yaml')])
    texts = {path: path.read_text(encoding='utf-8') for path in paths}
    authority = current_authority_workflows(paths, texts)

    inventory: list[tuple[Path, int, str, str]] = []
    authority_violations: list[str] = []
    nonauthority_debt: list[str] = []

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

    if authority_violations:
        print(
            f'ACTION_RUNTIME_PIN_AUDIT_FAIL authority_violations={len(authority_violations)}',
            file=sys.stderr,
        )
        for violation in authority_violations:
            print(f'AUTHORITY_VIOLATION {violation}', file=sys.stderr)
        return 1

    print(
        'ACTION_RUNTIME_PIN_AUDIT_OK '
        f'authority_refs={authority_refs} nonauthority_debt={len(nonauthority_debt)}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
