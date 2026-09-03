#!/usr/bin/env python3
from __future__ import annotations

from reconcile_recovery_mode import RecoveryModeError, resolve_mode


def expect_refused(**kwargs) -> None:
    try:
        resolve_mode(**kwargs)
    except RecoveryModeError:
        return
    raise AssertionError("unsafe recovery-mode downgrade unexpectedly admitted")


def main() -> int:
    assert resolve_mode(
        admission_recovery_only=False,
        durable_intent_exists=False,
        durable_terminal_exists=False,
    ) is False

    # A queued duplicate may have been admitted before another execution persisted
    # its mutation intent. Durable evidence must upgrade it to observation-only.
    assert resolve_mode(
        admission_recovery_only=False,
        durable_intent_exists=True,
        durable_terminal_exists=False,
    ) is True

    assert resolve_mode(
        admission_recovery_only=True,
        durable_intent_exists=True,
        durable_terminal_exists=False,
    ) is True

    # Once a terminal exists, run_phone_release_deployment returns that terminal
    # before any dispatch path; no recovery dispatch hint is needed.
    assert resolve_mode(
        admission_recovery_only=True,
        durable_intent_exists=True,
        durable_terminal_exists=True,
    ) is False

    # Hosted evidence must never be silently downgraded if the durable intent that
    # justified recovery has disappeared or cannot be found.
    expect_refused(
        admission_recovery_only=True,
        durable_intent_exists=False,
        durable_terminal_exists=False,
    )

    print("RECOVERY_MODE_RECONCILIATION_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
