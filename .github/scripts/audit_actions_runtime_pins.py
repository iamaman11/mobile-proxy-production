#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

WORKFLOWS = Path('.github/workflows')

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
SHA_RE = re.compile(r'^[0-9a-f]{40}$')


def main() -> int:
    if not WORKFLOWS.is_dir():
        print('workflow directory missing', file=sys.stderr)
        return 2

    inventory: list[tuple[str, int, str, str]] = []
    violations: list[str] = []

    paths = sorted([*WORKFLOWS.glob('*.yml'), *WORKFLOWS.glob('*.yaml')])
    for path in paths:
        text = path.read_text(encoding='utf-8')
        for match in USES_RE.finditer(text):
            action = match.group('action')
            ref = match.group('ref')
            line = text.count('\n', 0, match.start()) + 1
            inventory.append((path.as_posix(), line, action, ref))

            if SHA_RE.fullmatch(ref) is None:
                violations.append(
                    f'{path}:{line}: floating action ref forbidden: {action}@{ref}'
                )
                continue
            approved = APPROVED_NODE24_PINS.get(action)
            if approved is None:
                violations.append(
                    f'{path}:{line}: immutable action pin has no verified Node24 approval: {action}@{ref}'
                )
                continue
            if ref not in approved:
                violations.append(
                    f'{path}:{line}: immutable action pin is not a verified Node24 commit: {action}@{ref}'
                )

    counts = Counter(action for _, _, action, _ in inventory)
    print(f'ACTION_RUNTIME_PIN_INVENTORY workflows={len(paths)} refs={len(inventory)}')
    for action in sorted(counts):
        print(f'ACTION_RUNTIME_PIN_COUNT action={action} count={counts[action]}')
    for path, line, action, ref in inventory:
        print(f'ACTION_RUNTIME_PIN_REF path={path} line={line} ref={action}@{ref}')

    if violations:
        print(f'ACTION_RUNTIME_PIN_AUDIT_FAIL violations={len(violations)}', file=sys.stderr)
        for violation in violations:
            print(f'VIOLATION {violation}', file=sys.stderr)
        return 1

    print(f'ACTION_RUNTIME_PIN_AUDIT_OK refs={len(inventory)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
