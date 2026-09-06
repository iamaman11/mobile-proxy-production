#!/usr/bin/env python3
"""Classify the registered phone's rooted observation boundary without mutation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
if str(_CONTROLLER) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER))

import phone_target  # noqa: E402

_RELEASE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
_PASS = "PASS"
_OTHER_NONZERO = "OTHER_NONZERO"


def _failure_class(result: Any) -> str:
    if getattr(result, "returncode", 1) == 0:
        return _PASS
    stderr = str(getattr(result, "stderr", "") or "").lower()
    if "syntax error" in stderr:
        return "SHELL_SYNTAX"
    if "unknown option" in stderr or "illegal option" in stderr or "bad option" in stderr:
        return "SHELL_OPTION_UNSUPPORTED"
    if "permission denied" in stderr or "operation not permitted" in stderr:
        return "PERMISSION_DENIED"
    if "not found" in stderr or "inaccessible or not found" in stderr:
        return "COMMAND_NOT_FOUND"
    return _OTHER_NONZERO


def _run_root(serial: str, script: str) -> Any:
    try:
        return phone_target._run_root_script(serial, script)
    except phone_target.PhoneTargetUnavailable:
        return None


def _root_result(result: Any, *, expected: set[str] | None = None) -> str:
    if result is None:
        return "TRANSPORT_UNAVAILABLE"
    failure = _failure_class(result)
    if failure != _PASS:
        return failure
    if expected is None:
        return _PASS
    value = str(getattr(result, "stdout", "") or "").strip()
    return value.upper() if value in expected else "MALFORMED"


def diagnose(*, serial: str, release_id: str) -> dict[str, object]:
    if not serial:
        raise ValueError("registered phone serial is unavailable")
    if _RELEASE.fullmatch(release_id) is None:
        raise ValueError("release id is invalid")

    report: dict[str, object] = {
        "schema": "phone-target-observation-diagnostic.v1",
        "target": "phone-production",
        "release": release_id,
        "device_state": "UNAVAILABLE",
        "root_capability": "UNOBSERVED",
        "stdin_shell": "UNOBSERVED",
        "target_predicate": "UNOBSERVED",
        "current_predicate": "UNOBSERVED",
        "current_symlink_resolution": "UNOBSERVED",
        "combined_layout_probe": "UNOBSERVED",
        "phone_mutation_performed": False,
        "runner_mutation_performed": False,
        "raw_device_identifier_recorded": False,
        "raw_root_output_recorded": False,
        "raw_stderr_recorded": False,
        "filesystem_contents_recorded": False,
        "secret_values_recorded": False,
    }

    try:
        adb = phone_target._adb()
        state = phone_target._run([adb, "-s", serial, "get-state"], timeout=15)
    except phone_target.PhoneTargetUnavailable:
        return report
    if state.returncode != 0 or state.stdout.strip() != "device":
        return report
    report["device_state"] = "DEVICE"

    try:
        root = phone_target._read(serial, ["shell", "su", "0", "sh", "-c", "true"])
    except phone_target.PhoneTargetUnavailable:
        report["root_capability"] = "TRANSPORT_UNAVAILABLE"
        return report
    report["root_capability"] = _failure_class(root)
    if report["root_capability"] != _PASS:
        return report

    smoke = _run_root(serial, "printf 'stdin-ok\\n'\n")
    report["stdin_shell"] = _root_result(smoke, expected={"stdin-ok"})

    target = f"{phone_target._ROOT}/releases/{release_id}"
    current = f"{phone_target._ROOT}/current"

    target_probe = _run_root(
        serial,
        f"if [ -e '{target}' ] || [ -L '{target}' ]; then printf 'present\\n'; else printf 'absent\\n'; fi\n",
    )
    report["target_predicate"] = _root_result(target_probe, expected={"present", "absent"})

    current_probe = _run_root(
        serial,
        f"if [ -L '{current}' ]; then printf 'symlink\\n'; elif [ -e '{current}' ]; then printf 'invalid\\n'; else printf 'absent\\n'; fi\n",
    )
    report["current_predicate"] = _root_result(current_probe, expected={"symlink", "invalid", "absent"})

    if report["current_predicate"] == "SYMLINK":
        resolution = _run_root(serial, f"readlink '{current}' >/dev/null\n")
        report["current_symlink_resolution"] = _root_result(resolution)
    elif report["current_predicate"] in {"ABSENT", "INVALID"}:
        report["current_symlink_resolution"] = "NOT_APPLICABLE"

    combined = _run_root(
        serial,
        (
            f"if [ -e '{target}' ] || [ -L '{target}' ]; then echo target=present; else echo target=absent; fi; "
            f"if [ -L '{current}' ]; then printf 'current='; readlink '{current}'; "
            f"elif [ -e '{current}' ]; then echo current=invalid; else echo current=absent; fi"
        ),
    )
    combined_class = _root_result(combined)
    if combined_class == _PASS:
        values: dict[str, str] = {}
        for line in str(getattr(combined, "stdout", "") or "").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key in {"target", "current"} and key not in values:
                values[key] = value
        if values.get("target") not in {"present", "absent"} or "current" not in values:
            combined_class = "MALFORMED"
    report["combined_layout_probe"] = combined_class
    return report


def _validate_bounded_report(report: dict[str, object]) -> None:
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 4096:
        raise ValueError("diagnostic report exceeds bound")
    if report.get("schema") != "phone-target-observation-diagnostic.v1":
        raise ValueError("diagnostic schema differs")
    for key in (
        "phone_mutation_performed",
        "runner_mutation_performed",
        "raw_device_identifier_recorded",
        "raw_root_output_recorded",
        "raw_stderr_recorded",
        "filesystem_contents_recorded",
        "secret_values_recorded",
    ):
        if report.get(key) is not False:
            raise ValueError(f"unsafe diagnostic evidence flag: {key}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = diagnose(
            serial=os.environ.get("ANDROID_PRODUCTION_SERIAL", ""),
            release_id=args.release_id,
        )
        _validate_bounded_report(report)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("PHONE_TARGET_OBSERVATION_DIAGNOSTIC " + json.dumps(report, sort_keys=True, separators=(",", ":")))
    except (OSError, ValueError) as exc:
        print(f"PHONE_TARGET_OBSERVATION_DIAGNOSTIC_REFUSED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
