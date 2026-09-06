from __future__ import annotations

from pathlib import Path

from phone_target import PhoneTargetUnavailable, _files, _run_root_script

_ROOT = "/data/adb/mobile-proxy-node"
_MAX_MISMATCH_IDENTIFIERS = 8


def observe_exact_inactive_runtime(
    *, serial: str, release_root: Path, release_id: str, required_paths: tuple[str, ...]
) -> dict[str, object]:
    """Observe exact rooted runtime state without any phone mutation capability."""
    files = _files(release_root, required_paths)
    target = f"{_ROOT}/releases/{release_id}"
    script_lines = [
        "set -eu",
        f"ROOT='{_ROOT}'",
        f"TARGET='{target}'",
        "if [ -d \"$TARGET\" ]; then echo target=present; else echo target=absent; fi",
        "if [ -L \"$ROOT/current\" ]; then printf 'current='; readlink \"$ROOT/current\"; elif [ -e \"$ROOT/current\" ]; then echo current=invalid; else echo current=absent; fi",
        "command -v sha256sum >/dev/null",
    ]
    for index, (relative, _local, _expected) in enumerate(files):
        remote = f"{target}/{relative}"
        script_lines.append(
            f"if [ -f '{remote}' ]; then printf 'h{index}='; sha256sum '{remote}' | awk '{{print $1}}'; else echo 'h{index}=missing'; fi"
        )
    result = _run_root_script(serial, ("\n".join(script_lines) + "\n").encode("utf-8"), timeout=60)
    if result.status != "completed" or result.returncode != 0 or result.stderr:
        raise PhoneTargetUnavailable("rooted UNKNOWN reconciliation observation failed")
    try:
        lines = result.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PhoneTargetUnavailable("rooted UNKNOWN reconciliation observation is malformed") from exc

    values: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key not in values:
            values[key] = value
    if values.get("target") not in {"present", "absent"} or "current" not in values:
        raise PhoneTargetUnavailable("rooted UNKNOWN reconciliation observation is malformed")

    exists = values["target"] == "present"
    mismatches: list[str] = []
    for index, (relative, _local, expected) in enumerate(files):
        if values.get(f"h{index}") != expected:
            mismatches.append(relative)
    exact = exists and not mismatches

    current_raw = values["current"]
    if current_raw == target:
        current_relation = "target"
    elif current_raw == "absent":
        current_relation = "absent"
    elif current_raw == "invalid":
        current_relation = "invalid"
    elif current_raw.startswith(f"{_ROOT}/releases/"):
        current_relation = "other-managed"
    else:
        current_relation = "unmanaged"

    return {
        "mode": "read_only_reconciliation",
        "target_release_exists": exists,
        "inactive_exact_files_verified": exact,
        "required_file_count": len(files),
        "mismatch_count": len(mismatches),
        "mismatch_files": mismatches[:_MAX_MISMATCH_IDENTIFIERS],
        "current_relation": current_relation,
        "desired": exact and current_relation == "target",
    }
