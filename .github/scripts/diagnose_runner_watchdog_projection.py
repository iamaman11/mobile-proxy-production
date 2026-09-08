from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "controller"
sys.path.insert(0, str(CONTROLLER))

import runner_transport_probe as probe


def diagnose_projection(
    path: Path = probe.WATCHDOG_PROJECTION,
    *,
    now_epoch: int | None = None,
    expected_owner_uid: int = 0,
) -> tuple[str, dict[str, object] | None]:
    now = int(time.time()) if now_epoch is None else now_epoch
    if type(expected_owner_uid) is not int or expected_owner_uid < 0:
        return "EXPECTED_OWNER_INVALID", None

    try:
        parent = path.parent.lstat()
    except OSError:
        return "PARENT_UNAVAILABLE", None
    if not stat.S_ISDIR(parent.st_mode):
        return "PARENT_NOT_DIRECTORY", None
    if parent.st_uid != expected_owner_uid:
        return "PARENT_OWNER_INVALID", None
    if parent.st_mode & 0o022:
        return "PARENT_MODE_INVALID", None

    try:
        info = path.lstat()
    except OSError:
        return "FILE_UNAVAILABLE", None
    if not stat.S_ISREG(info.st_mode):
        return "FILE_NOT_REGULAR", None
    if info.st_uid != expected_owner_uid:
        return "FILE_OWNER_INVALID", None
    if info.st_size <= 0 or info.st_size > probe._MAX_PROJECTION_BYTES:
        return "FILE_SIZE_INVALID", None
    if info.st_mode & 0o022:
        return "FILE_MODE_INVALID", None

    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "JSON_INVALID", None

    expected = {
        "schema",
        "observed_at_epoch",
        "expires_at_epoch",
        "decision",
        "cooldown_active",
        "restart_budget_remaining",
        "post_restart_grace_active",
        "restart_count_window",
    }
    if not isinstance(value, dict) or set(value) != expected:
        return "FIELDS_INVALID", None
    if value.get("schema") != probe.WATCHDOG_PROJECTION_SCHEMA:
        return "SCHEMA_INVALID", None

    observed = value.get("observed_at_epoch")
    expires = value.get("expires_at_epoch")
    if type(observed) is not int or type(expires) is not int or observed < 0 or expires <= observed:
        return "TIMESTAMP_SHAPE_INVALID", None
    if now < observed:
        return "PROJECTION_FROM_FUTURE", None
    if now > expires:
        return "PROJECTION_STALE", None
    if value.get("decision") not in probe._WATCHDOG_DECISIONS:
        return "DECISION_INVALID", None

    for key in ("cooldown_active", "post_restart_grace_active"):
        if type(value.get(key)) is not bool:
            return "BOOLEAN_FIELD_INVALID", None
    for key in ("restart_budget_remaining", "restart_count_window"):
        item = value.get(key)
        if type(item) is not int or not 0 <= item <= probe._MAX_PROJECTION_COUNTER:
            return "COUNTER_INVALID", None

    parsed = probe._parse_watchdog_projection(
        path,
        now_epoch=now,
        expected_owner_uid=expected_owner_uid,
    )
    if parsed is None:
        return "PARSER_INTERNAL_MISMATCH", None
    return "NONE", parsed


def main() -> int:
    reason, value = diagnose_projection()
    print(f"projection_rejection={reason}")
    print(f"projection_trusted={'true' if value is not None else 'false'}")
    if value is not None:
        print(f"watchdog_decision={value['decision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
