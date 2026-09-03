#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from deployment_request import validate_deployment_request  # noqa: E402
from evidence_store import IssueEvidenceStore  # noqa: E402


class RecoveryModeError(RuntimeError):
    pass


def resolve_mode(
    *,
    admission_recovery_only: bool,
    durable_intent_exists: bool,
    durable_terminal_exists: bool,
) -> bool:
    """Return whether target execution must be observation-only.

    Durable private evidence is authoritative over the earlier hosted admission
    hint. This closes the race where another queued execution persists a mutation
    intent after this workflow was admitted but before it acquires the target lock.
    """
    if durable_terminal_exists:
        return False
    if durable_intent_exists:
        return True
    if admission_recovery_only:
        raise RecoveryModeError(
            "hosted admission required recovery but durable mutation intent is now unavailable"
        )
    return False


def _output(name: str, value: object) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    if "\n" in text or "\r" in text:
        raise RecoveryModeError("workflow output must be one line")
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={text}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--admission-recovery-only", choices=("true", "false"), required=True)
    args = parser.parse_args()

    request = json.loads(args.request_json)
    validate_deployment_request(request)
    evidence = IssueEvidenceStore(os.environ.get("GITHUB_TOKEN", ""))
    intent, terminal = evidence.request_history(str(request["request_id"]))

    recovery_only = resolve_mode(
        admission_recovery_only=args.admission_recovery_only == "true",
        durable_intent_exists=intent is not None,
        durable_terminal_exists=terminal is not None,
    )
    _output("recovery_only", recovery_only)
    _output("durable_intent_exists", intent is not None)
    _output("durable_terminal_exists", terminal is not None)
    _output("intent_ref", "" if intent is None else intent.ref)
    _output("terminal_ref", "" if terminal is None else terminal.ref)
    print(
        "RECOVERY_MODE_RECONCILED "
        f"recovery_only={str(recovery_only).lower()} "
        f"intent={str(intent is not None).lower()} "
        f"terminal={str(terminal is not None).lower()}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RecoveryModeError, json.JSONDecodeError) as exc:
        print(f"RECOVERY_MODE_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
