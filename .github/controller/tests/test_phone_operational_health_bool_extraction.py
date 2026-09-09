from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
if str(CONTROLLER) not in sys.path:
    sys.path.insert(0, str(CONTROLLER))

import runtime_operational_observer as runtime_observer  # noqa: E402


_PORTABLE_BOOL_PATTERN = (
    r's/.*"serving"[[:space:]]*:[[:space:]]*\([a-z][a-z]*\).*/\1/p'
)


def _extract(raw: str) -> str:
    completed = subprocess.run(
        ["sed", "-n", _PORTABLE_BOOL_PATTERN],
        input=raw,
        capture_output=True,
        text=True,
        check=True,
    )
    value = completed.stdout.strip()
    return value if value in {"true", "false"} else "unknown"


def test_generated_observer_uses_posix_bre_boolean_token_capture() -> None:
    script = runtime_observer._operational_script("bounded-test-token").decode("utf-8")
    assert r"\(true\|false\|null\)" not in script
    assert r"\([a-z][a-z]*\)" in script


def test_portable_boolean_extraction_preserves_fail_closed_semantics() -> None:
    assert _extract('{"serving":true}') == "true"
    assert _extract('{"serving": false}') == "false"
    assert _extract('{"serving":null}') == "unknown"
    assert _extract('{"serving":"true"}') == "unknown"
    assert _extract('{"other":true}') == "unknown"
