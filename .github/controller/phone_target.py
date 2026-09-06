from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from android_target import DispatchResult, dispatch_install_once

_ROOT = "/data/adb/mobile-proxy-node"
_BOOT_HOOK = "/data/adb/service.d/99-mobile-proxy-runtime.sh"
_RELEASE_ID = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
_SAFE_REMOTE = re.compile(r"[A-Za-z0-9_./-]+")
_ROOT_CAPABILITY_UNAVAILABLE = "rooted runtime capability unavailable"
_ROOT_LAYOUT_OBSERVATION_FAILED = "rooted runtime layout observation failed"
_ROOT_LAYOUT_OBSERVATION_MALFORMED = "rooted runtime state observation is malformed"
_MAX_ROOT_SCRIPT_BYTES = 16 * 1024
_MAX_ROOT_OUTPUT_BYTES = 4 * 1024
_ROOT_DRAIN_CHUNK_BYTES = 8 * 1024


class PhoneTargetUnavailable(RuntimeError):
    pass


class PhoneTargetMutationOutcomeUnknown(PhoneTargetUnavailable):
    pass


@dataclass(frozen=True)
class RootScriptResult:
    status: str
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass(frozen=True)
class RuntimeObservation:
    target_release: str
    target_release_exists: bool
    current_target: str | None
    exact_files_verified: bool
    required_file_count: int
    desired: bool
    admissible_for_new_dispatch: bool
    mode: str = "read_only"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _adb() -> str:
    value = shutil.which("adb")
    if value is None:
        raise PhoneTargetUnavailable("ADB tooling is unavailable")
    return value


def _run(
    command: list[str], *, timeout: int, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PhoneTargetUnavailable("phone target transport unavailable") from exc


def _require_device(serial: str) -> str:
    adb = _adb()
    state = _run([adb, "-s", serial, "get-state"], timeout=15)
    if state.returncode != 0 or state.stdout.strip() != "device":
        raise PhoneTargetUnavailable("registered phone target is not in device state")
    return adb


def _read(
    serial: str,
    arguments: list[str],
    *,
    timeout: int = 30,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    adb = _require_device(serial)
    return _run([adb, "-s", serial, *arguments], timeout=timeout, input_text=input_text)


def _drain_root_stream(stream, retained: bytearray, truncated: list[bool]) -> None:
    try:
        while True:
            chunk = stream.read(_ROOT_DRAIN_CHUNK_BYTES)
            if not chunk:
                return
            remaining = max(0, _MAX_ROOT_OUTPUT_BYTES - len(retained))
            if remaining:
                retained.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated[0] = True
    finally:
        stream.close()


def _write_root_script_input(stream, script: bytes) -> None:
    try:
        stream.write(script)
        stream.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        stream.close()


def _run_root_script(serial: str, script: bytes, *, timeout: int = 30) -> RootScriptResult:
    if not isinstance(script, bytes) or not script or len(script) > _MAX_ROOT_SCRIPT_BYTES:
        raise PhoneTargetUnavailable("rooted runtime script is invalid")
    adb = _require_device(serial)
    command = [adb, "-s", serial, "shell", "-T", "su", "0"]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError:
        return RootScriptResult(status="spawn_error", returncode=None, stdout=b"", stderr=b"")

    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        return RootScriptResult(status="spawn_error", returncode=None, stdout=b"", stderr=b"")

    stdout = bytearray()
    stderr = bytearray()
    stdout_truncated = [False]
    stderr_truncated = [False]
    stdout_thread = threading.Thread(
        target=_drain_root_stream,
        args=(process.stdout, stdout, stdout_truncated),
        name="root-stdout-drain",
    )
    stderr_thread = threading.Thread(
        target=_drain_root_stream,
        args=(process.stderr, stderr, stderr_truncated),
        name="root-stderr-drain",
    )
    stdin_thread = threading.Thread(
        target=_write_root_script_input,
        args=(process.stdin, script),
        name="root-stdin-write",
    )
    stdout_thread.start()
    stderr_thread.start()
    stdin_thread.start()

    timed_out = False
    returncode: int | None = None
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    finally:
        stdin_thread.join()
        stdout_thread.join()
        stderr_thread.join()

    result_stdout = bytes(stdout)
    result_stderr = bytes(stderr)
    if timed_out:
        return RootScriptResult(
            status="timeout",
            returncode=None,
            stdout=result_stdout,
            stderr=result_stderr,
            stdout_truncated=stdout_truncated[0],
            stderr_truncated=stderr_truncated[0],
        )
    if stdout_truncated[0] or stderr_truncated[0]:
        return RootScriptResult(
            status="transport_error",
            returncode=None,
            stdout=result_stdout,
            stderr=result_stderr,
            stdout_truncated=stdout_truncated[0],
            stderr_truncated=stderr_truncated[0],
        )
    return RootScriptResult(
        status="completed",
        returncode=returncode,
        stdout=result_stdout,
        stderr=result_stderr,
    )


def _probe_root_capability(serial: str) -> None:
    success = _run_root_script(
        serial,
        (
            b"set -eu\n"
            b"[ \"$(id -u)\" = \"0\" ]\n"
            b"printf 'root=0\\n'\n"
            b"if [ 1 -eq 1 ]; then printf 'grammar=ok\\n'; fi\n"
            b"command -v readlink >/dev/null\n"
            b"command -v test >/dev/null\n"
            b"printf 'tools=ok\\n'\n"
        ),
    )
    if (
        success.status != "completed"
        or success.returncode != 0
        or success.stdout != b"root=0\ngrammar=ok\ntools=ok\n"
        or success.stderr != b""
    ):
        raise PhoneTargetUnavailable(_ROOT_CAPABILITY_UNAVAILABLE)

    nonzero = _run_root_script(
        serial,
        b"printf 'stderr=ok\\n' >&2\nexit 23\n",
    )
    if (
        nonzero.status != "completed"
        or nonzero.returncode != 23
        or nonzero.stdout != b""
        or nonzero.stderr != b"stderr=ok\n"
    ):
        raise PhoneTargetUnavailable(_ROOT_CAPABILITY_UNAVAILABLE)


def _safe_release_id(value: str) -> str:
    if _RELEASE_ID.fullmatch(value) is None:
        raise PhoneTargetUnavailable("phone runtime release id is invalid")
    return value


def _files(release_root: Path, required_paths: tuple[str, ...]) -> tuple[tuple[str, Path, str], ...]:
    if not release_root.is_dir() or not required_paths:
        raise PhoneTargetUnavailable("materialized phone runtime release is unavailable")
    result: list[tuple[str, Path, str]] = []
    seen: set[str] = set()
    for raw in required_paths:
        if not raw or raw.startswith("/") or ".." in raw.split("/") or raw in seen or _SAFE_REMOTE.fullmatch(raw) is None:
            raise PhoneTargetUnavailable("required phone runtime path is unsafe or duplicate")
        local = release_root / raw
        if not local.is_file() or local.stat().st_size <= 0:
            raise PhoneTargetUnavailable(f"required phone runtime file is unavailable: {raw}")
        result.append((raw, local, hashlib.sha256(local.read_bytes()).hexdigest()))
        seen.add(raw)
    return tuple(result)


def observe_runtime(*, serial: str, release_root: Path, release_id: str, required_paths: tuple[str, ...]) -> RuntimeObservation:
    release_id = _safe_release_id(release_id)
    files = _files(release_root, required_paths)
    target = f"{_ROOT}/releases/{release_id}"
    _probe_root_capability(serial)
    probe = _run_root_script(
        serial,
        (
            f"if [ -e '{target}' ] || [ -L '{target}' ]; then echo target=present; else echo target=absent; fi; "
            f"if [ -L '{_ROOT}/current' ]; then printf 'current='; readlink '{_ROOT}/current'; "
            "elif [ -e '" + _ROOT + "/current' ]; then echo current=invalid; else echo current=absent; fi"
        ).encode(),
    )
    if probe.status != "completed" or probe.returncode != 0:
        raise PhoneTargetUnavailable(_ROOT_LAYOUT_OBSERVATION_FAILED)
    try:
        output = probe.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PhoneTargetUnavailable(_ROOT_LAYOUT_OBSERVATION_MALFORMED) from exc
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in {"target", "current"} and key not in values:
            values[key] = value
    if values.get("target") not in {"present", "absent"} or "current" not in values:
        raise PhoneTargetUnavailable(_ROOT_LAYOUT_OBSERVATION_MALFORMED)
    exists = values["target"] == "present"
    current = None if values["current"] == "absent" else values["current"]
    exact = False
    if exists and current == target:
        exact = True
        for relative, _local, expected in files:
            result = _read(serial, ["shell", "sha256sum", f"{target}/{relative}"])
            actual = result.stdout.split()[0] if result.returncode == 0 and result.stdout.split() else ""
            if actual != expected:
                exact = False
                break
    desired = exists and current == target and exact
    current_is_managed = current is None or current.startswith(f"{_ROOT}/releases/")
    return RuntimeObservation(
        target_release=target, target_release_exists=exists, current_target=current,
        exact_files_verified=exact, required_file_count=len(files), desired=desired,
        admissible_for_new_dispatch=(desired or (not exists and current_is_managed)),
    )


def _stage_runtime(*, serial: str, release_root: Path, release_id: str, required_paths: tuple[str, ...]) -> tuple[str, tuple[tuple[str, Path, str], ...]]:
    files = _files(release_root, required_paths)
    stage = f"/data/local/tmp/mobile-proxy-release-{release_id}"
    adb = _adb()
    if _run([adb, "-s", serial, "shell", "rm", "-rf", stage], timeout=30).returncode != 0:
        raise PhoneTargetUnavailable("phone runtime stage cleanup failed")
    if _run([adb, "-s", serial, "shell", "mkdir", "-p", stage], timeout=30).returncode != 0:
        raise PhoneTargetUnavailable("phone runtime stage creation failed")
    if _run([adb, "-s", serial, "push", str(release_root) + "/.", stage + "/"], timeout=180).returncode != 0:
        raise PhoneTargetUnavailable("phone runtime stage upload failed")
    for relative, _local, expected in files:
        result = _read(serial, ["shell", "sha256sum", f"{stage}/{relative}"])
        actual = result.stdout.split()[0] if result.returncode == 0 and result.stdout.split() else ""
        if actual != expected:
            raise PhoneTargetUnavailable("phone runtime staged bytes differ")
    return stage, files


def _materialize_inactive(*, serial: str, release_id: str, stage: str, files: tuple[tuple[str, Path, str], ...]) -> str:
    target = f"{_ROOT}/releases/{release_id}"
    script = f"""
set -eu
umask 077
ROOT='{_ROOT}'
TARGET='{target}'
STAGE='{stage}'
mkdir -p "$ROOT/releases" "$ROOT/logs" "$ROOT/state"
[ ! -e "$TARGET" ] && [ ! -L "$TARGET" ]
mkdir "$TARGET"
cp -pR "$STAGE/." "$TARGET/"
find "$TARGET" -type f -exec chmod 0600 {{}} +
chmod 0700 "$TARGET/service.sh" "$TARGET/bin/runtime-supervisor" "$TARGET/bin/host-daemon" "$TARGET/bin/sing-box"
""".encode()
    result = _run_root_script(serial, script, timeout=180)
    if result.status != "completed" or result.returncode != 0:
        raise PhoneTargetUnavailable("rooted runtime inactive release materialization failed")
    for relative, _local, expected in files:
        result = _read(serial, ["shell", "sha256sum", f"{target}/{relative}"])
        actual = result.stdout.split()[0] if result.returncode == 0 and result.stdout.split() else ""
        if actual != expected:
            raise PhoneTargetUnavailable("rooted runtime materialized bytes differ before activation")
    return target


def _activate(*, serial: str, release_id: str, target: str) -> None:
    script = f"""
set -eu
umask 077
ROOT='{_ROOT}'
TARGET='{target}'
BOOT='{_BOOT_HOOK}'
[ -d "$TARGET" ]
command -v ln >/dev/null
command -v readlink >/dev/null
if [ -f "$ROOT/logs/runtime-watchdog.pid" ]; then
  pid="$(cat "$ROOT/logs/runtime-watchdog.pid" 2>/dev/null || true)"
  if [ -n "$pid" ] && [ -r "/proc/$pid/cmdline" ]; then
    cmd="$(tr '\\000' ' ' < "/proc/$pid/cmdline")"
    case "$cmd" in *"$ROOT/logs/runtime-watchdog.sh"*"$ROOT/current"*) kill -TERM "$pid" 2>/dev/null || true ;; esac
  fi
fi
for proc in /proc/[0-9]*; do
  [ -r "$proc/exe" ] || continue
  exe="$(readlink -f "$proc/exe" 2>/dev/null || true)"
  case "$exe" in "$ROOT"/releases/*/bin/runtime-supervisor|"$ROOT"/releases/*/bin/host-daemon|"$ROOT"/releases/*/bin/sing-box)
    kill -TERM "${{proc#/proc/}}" 2>/dev/null || true
    ;;
  esac
done
ln -sfn "$TARGET" "$ROOT/current"
[ "$(readlink "$ROOT/current")" = "$TARGET" ]
cat > "$BOOT" <<'MOBILE_PROXY_BOOT'
#!/system/bin/sh
set -eu
umask 077
ROOT='/data/adb/mobile-proxy-node'
i=0
while [ "$i" -lt 30 ]; do
  if [ -x "$ROOT/current/service.sh" ]; then
    sh "$ROOT/current/service.sh" >> "$ROOT/logs/boot-service.log" 2>&1
    exit "$?"
  fi
  i=$((i + 1))
  sleep 1
done
exit 1
MOBILE_PROXY_BOOT
chmod 0700 "$BOOT"
sh "$ROOT/current/service.sh"
""".encode()
    result = _run_root_script(serial, script, timeout=150)
    if result.status == "completed":
        if result.returncode != 0:
            raise PhoneTargetUnavailable("rooted runtime activation/start command failed")
        return
    raise PhoneTargetMutationOutcomeUnknown("rooted runtime activation/start outcome is unknown")


def dispatch_release_once(
    *, serial: str, apk: Path, release_root: Path, release_id: str,
    required_paths: tuple[str, ...], install_apk: bool, install_runtime: bool,
) -> DispatchResult:
    """Cross one composite durable dispatch boundary at most once."""
    release_id = _safe_release_id(release_id)
    if not install_apk and not install_runtime:
        return DispatchResult(True, False, None)
    if install_apk:
        apk_result = dispatch_install_once(serial=serial, apk=apk)
        if apk_result.outcome_unknown or not apk_result.confirmed:
            return apk_result
    if install_runtime:
        try:
            stage, files = _stage_runtime(
                serial=serial, release_root=release_root, release_id=release_id, required_paths=required_paths,
            )
            target = _materialize_inactive(serial=serial, release_id=release_id, stage=stage, files=files)
            _activate(serial=serial, release_id=release_id, target=target)
        except PhoneTargetUnavailable:
            return DispatchResult(False, True, "ROOTED_RUNTIME_MUTATION_OUTCOME_UNKNOWN")
    return DispatchResult(True, False, None)
