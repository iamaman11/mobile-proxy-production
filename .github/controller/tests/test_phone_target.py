from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

fake_android = ModuleType("android_target")


class DispatchResult:
    def __init__(self, confirmed: bool, outcome_unknown: bool, error_class: str | None):
        self.confirmed = confirmed
        self.outcome_unknown = outcome_unknown
        self.error_class = error_class


fake_android.DispatchResult = DispatchResult
fake_android.dispatch_install_once = lambda **kwargs: DispatchResult(True, False, None)
sys.modules["android_target"] = fake_android

spec = importlib.util.spec_from_file_location("phone_target", Path(__file__).resolve().parents[1] / "phone_target.py")
phone_target = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["phone_target"] = phone_target
spec.loader.exec_module(phone_target)


def runtime_release(root: Path) -> tuple[Path, tuple[str, ...]]:
    release = root / "release"
    files = {
        "service.sh": b"service\n",
        "bin/runtime-supervisor": b"runtime\n",
        "config/host-daemon.json": b"{}\n",
        "config/sing-box.json": b"{}\n",
    }
    for relative, body in files.items():
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return release, tuple(files)


def root_result(
    *,
    status: str = "completed",
    returncode: int | None = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
):
    return phone_target.RootScriptResult(
        status=status,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_root_script_uses_canonical_no_pty_stdin_bytes_not_adb_argv() -> None:
    script = b"if [ -e '/data/a' ]; then printf 'yes\\n'; else printf 'no\\n'; fi\n"
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append((list(command), dict(kwargs)))
        return SimpleNamespace(returncode=0, stdout=b"ok\n", stderr=b"")

    originals = (phone_target._require_device, phone_target.subprocess.run)
    phone_target._require_device = lambda serial: "/usr/bin/adb"
    phone_target.subprocess.run = fake_subprocess_run
    try:
        result = phone_target._run_root_script("serial", script, timeout=41)
    finally:
        phone_target._require_device, phone_target.subprocess.run = originals

    assert result.status == "completed" and result.returncode == 0
    assert calls == [(
        ["/usr/bin/adb", "-s", "serial", "shell", "-T", "su", "0"],
        {
            "input": script,
            "capture_output": True,
            "text": False,
            "timeout": 41,
            "check": False,
        },
    )]
    argv = calls[0][0]
    assert script not in argv
    assert "sh" not in argv[3:] and "-s" not in argv[3:] and "-c" not in argv[3:]
    assert argv[-4:] == ["shell", "-T", "su", "0"]


def test_root_script_rejects_oversized_controller_script() -> None:
    called = False

    def fake_require_device(serial):
        nonlocal called
        called = True
        return "/usr/bin/adb"

    original = phone_target._require_device
    phone_target._require_device = fake_require_device
    try:
        try:
            phone_target._run_root_script("serial", b"x" * (phone_target._MAX_ROOT_SCRIPT_BYTES + 1))
        except phone_target.PhoneTargetUnavailable as error:
            assert str(error) == "rooted runtime script is invalid"
        else:
            raise AssertionError("oversized root script was accepted")
    finally:
        phone_target._require_device = original
    assert called is False


def test_root_script_bounds_output_and_fails_closed() -> None:
    def fake_subprocess_run(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=b"A" * (phone_target._MAX_ROOT_OUTPUT_BYTES + 10),
            stderr=b"",
        )

    originals = (phone_target._require_device, phone_target.subprocess.run)
    phone_target._require_device = lambda serial: "/usr/bin/adb"
    phone_target.subprocess.run = fake_subprocess_run
    try:
        result = phone_target._run_root_script("serial", b"printf ok\n")
    finally:
        phone_target._require_device, phone_target.subprocess.run = originals

    assert result.status == "transport_error"
    assert result.returncode is None
    assert result.stdout_truncated is True
    assert len(result.stdout) == phone_target._MAX_ROOT_OUTPUT_BYTES


def test_root_script_timeout_is_ambiguous_not_completed() -> None:
    def fake_subprocess_run(command, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=command,
            timeout=kwargs["timeout"],
            output=b"partial",
            stderr=b"bounded-error",
        )

    originals = (phone_target._require_device, phone_target.subprocess.run)
    phone_target._require_device = lambda serial: "/usr/bin/adb"
    phone_target.subprocess.run = fake_subprocess_run
    try:
        result = phone_target._run_root_script("serial", b"sleep 1\n", timeout=1)
    finally:
        phone_target._require_device, phone_target.subprocess.run = originals

    assert result.status == "timeout"
    assert result.returncode is None
    assert result.stdout == b"partial"
    assert result.stderr == b"bounded-error"


def test_root_capability_preflight_uses_same_adapter_and_proves_rc_stderr() -> None:
    calls: list[tuple[bytes, int]] = []

    def fake_root_script(serial, script, timeout=30):
        calls.append((script, timeout))
        if len(calls) == 1:
            return root_result(stdout=b"root=0\ngrammar=ok\ntools=ok\n")
        return root_result(returncode=23, stderr=b"stderr=ok\n")

    original = phone_target._run_root_script
    phone_target._run_root_script = fake_root_script
    try:
        phone_target._probe_root_capability("serial")
    finally:
        phone_target._run_root_script = original

    assert len(calls) == 2
    assert all(isinstance(script, bytes) for script, _ in calls)
    assert b"id -u" in calls[0][0]
    assert b"if [ 1 -eq 1 ]; then" in calls[0][0]
    assert b"readlink" in calls[0][0] and b"command -v test" in calls[0][0]
    assert calls[1][0] == b"printf 'stderr=ok\\n' >&2\nexit 23\n"


def test_root_capability_nonzero_fails_before_layout() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        layout_called = False

        def fail_preflight(serial):
            raise phone_target.PhoneTargetUnavailable("rooted runtime capability unavailable")

        def fake_root_script(serial, script, timeout=30):
            nonlocal layout_called
            layout_called = True
            return root_result()

        originals = (phone_target._probe_root_capability, phone_target._run_root_script)
        phone_target._probe_root_capability = fail_preflight
        phone_target._run_root_script = fake_root_script
        try:
            try:
                phone_target.observe_runtime(
                    serial="serial", release_root=release, release_id="v0.1.6", required_paths=required
                )
            except phone_target.PhoneTargetUnavailable as error:
                assert str(error) == "rooted runtime capability unavailable"
            else:
                raise AssertionError("root capability failure was not classified")
        finally:
            phone_target._probe_root_capability, phone_target._run_root_script = originals
        assert layout_called is False


def test_observe_runtime_empty_layout_is_exact_and_read_only() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        target = "/data/adb/mobile-proxy-node/releases/v0.1.6"
        expected_layout = (
            f"if [ -e '{target}' ] || [ -L '{target}' ]; then echo target=present; else echo target=absent; fi; "
            "if [ -L '/data/adb/mobile-proxy-node/current' ]; then printf 'current='; readlink '/data/adb/mobile-proxy-node/current'; "
            "elif [ -e '/data/adb/mobile-proxy-node/current' ]; then echo current=invalid; else echo current=absent; fi"
        ).encode()
        root_scripts: list[tuple[bytes, int]] = []

        def fake_root_script(serial, script, timeout=30):
            root_scripts.append((script, timeout))
            return root_result(stdout=b"target=absent\ncurrent=absent\n")

        originals = (phone_target._probe_root_capability, phone_target._run_root_script)
        phone_target._probe_root_capability = lambda serial: None
        phone_target._run_root_script = fake_root_script
        try:
            observed = phone_target.observe_runtime(
                serial="serial", release_root=release, release_id="v0.1.6", required_paths=required
            )
        finally:
            phone_target._probe_root_capability, phone_target._run_root_script = originals

        assert root_scripts == [(expected_layout, 30)]
        assert b"if " in root_scripts[0][0] and b" then " in root_scripts[0][0] and b"; fi" in root_scripts[0][0]
        assert observed.target_release_exists is False
        assert observed.current_target is None
        assert observed.desired is False
        assert observed.admissible_for_new_dispatch is True
        assert observed.mode == "read_only"


def test_observe_runtime_classifies_layout_probe_nonzero_after_root_success() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        originals = (phone_target._probe_root_capability, phone_target._run_root_script)
        phone_target._probe_root_capability = lambda serial: None
        phone_target._run_root_script = lambda *args, **kwargs: root_result(returncode=9)
        try:
            try:
                phone_target.observe_runtime(
                    serial="serial", release_root=release, release_id="v0.1.6", required_paths=required
                )
            except phone_target.PhoneTargetUnavailable as error:
                assert str(error) == "rooted runtime layout observation failed"
            else:
                raise AssertionError("layout probe failure was not classified")
        finally:
            phone_target._probe_root_capability, phone_target._run_root_script = originals


def test_observe_runtime_classifies_malformed_layout_without_exposing_output() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        originals = (phone_target._probe_root_capability, phone_target._run_root_script)
        phone_target._probe_root_capability = lambda serial: None
        phone_target._run_root_script = lambda *args, **kwargs: root_result(stdout=b"arbitrary-root-output\n")
        try:
            try:
                phone_target.observe_runtime(
                    serial="serial", release_root=release, release_id="v0.1.6", required_paths=required
                )
            except phone_target.PhoneTargetUnavailable as error:
                assert str(error) == "rooted runtime state observation is malformed"
                assert "arbitrary-root-output" not in str(error)
            else:
                raise AssertionError("malformed layout was not classified")
        finally:
            phone_target._probe_root_capability, phone_target._run_root_script = originals


def test_observe_runtime_requires_exact_current_and_all_bytes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        target = "/data/adb/mobile-proxy-node/releases/v0.1.6"

        def fake_read(serial, args, timeout=30, input_text=None):
            relative = args[-1].split(target + "/", 1)[1]
            digest = hashlib.sha256((release / relative).read_bytes()).hexdigest()
            return SimpleNamespace(returncode=0, stdout=f"{digest}  file\n")

        originals = (phone_target._probe_root_capability, phone_target._run_root_script, phone_target._read)
        phone_target._probe_root_capability = lambda serial: None
        phone_target._run_root_script = lambda *args, **kwargs: root_result(
            stdout=f"target=present\ncurrent={target}\n".encode()
        )
        phone_target._read = fake_read
        try:
            observed = phone_target.observe_runtime(
                serial="serial", release_root=release, release_id="v0.1.6", required_paths=required
            )
        finally:
            phone_target._probe_root_capability, phone_target._run_root_script, phone_target._read = originals
        assert observed.desired is True
        assert observed.exact_files_verified is True
        assert observed.admissible_for_new_dispatch is True


def test_observe_runtime_rejects_existing_noncurrent_target_for_new_dispatch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        originals = (phone_target._probe_root_capability, phone_target._run_root_script)
        phone_target._probe_root_capability = lambda serial: None
        phone_target._run_root_script = lambda *args, **kwargs: root_result(
            stdout=b"target=present\ncurrent=/data/adb/mobile-proxy-node/releases/v0.1.5\n"
        )
        try:
            observed = phone_target.observe_runtime(
                serial="serial", release_root=release, release_id="v0.1.6", required_paths=required
            )
        finally:
            phone_target._probe_root_capability, phone_target._run_root_script = originals
        assert observed.desired is False
        assert observed.admissible_for_new_dispatch is False


def test_observe_runtime_rejects_unmanaged_current_even_when_target_absent() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        originals = (phone_target._probe_root_capability, phone_target._run_root_script)
        phone_target._probe_root_capability = lambda serial: None
        phone_target._run_root_script = lambda *args, **kwargs: root_result(
            stdout=b"target=absent\ncurrent=/data/local/tmp/foreign-runtime\n"
        )
        try:
            observed = phone_target.observe_runtime(
                serial="serial", release_root=release, release_id="v0.1.6", required_paths=required
            )
        finally:
            phone_target._probe_root_capability, phone_target._run_root_script = originals
        assert observed.desired is False
        assert observed.admissible_for_new_dispatch is False


def test_materialize_and_activate_share_root_script_primitive() -> None:
    calls: list[tuple[str, bytes, int]] = []
    expected = "a" * 64
    files = (("service.sh", Path("/unused/service.sh"), expected),)

    def fake_root_script(serial, script, timeout=30):
        calls.append((serial, script, timeout))
        return root_result()

    def fake_read(serial, args, timeout=30, input_text=None):
        return SimpleNamespace(returncode=0, stdout=f"{expected}  file\n")

    originals = (phone_target._run_root_script, phone_target._read)
    phone_target._run_root_script = fake_root_script
    phone_target._read = fake_read
    try:
        target = phone_target._materialize_inactive(
            serial="serial", release_id="v0.1.6", stage="/data/local/tmp/stage", files=files
        )
        phone_target._activate(serial="serial", release_id="v0.1.6", target=target)
    finally:
        phone_target._run_root_script, phone_target._read = originals

    assert len(calls) == 2
    assert calls[0][0] == "serial" and calls[0][2] == 180
    assert b"set -eu" in calls[0][1] and b"mkdir -p" in calls[0][1] and b"cp -pR" in calls[0][1]
    assert calls[1][0] == "serial" and calls[1][2] == 150
    assert b"set -eu" in calls[1][1] and b"MOBILE_PROXY_BOOT" in calls[1][1]
    assert b'sh "$ROOT/current/service.sh"' in calls[1][1]


def test_composite_dispatch_calls_apk_then_runtime_once() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        apk = Path(raw) / "app.apk"
        apk.write_bytes(b"apk")
        calls: list[str] = []
        originals = (
            phone_target._stage_runtime,
            phone_target.dispatch_install_once,
            phone_target._materialize_inactive,
            phone_target._activate,
        )

        def install(**kwargs):
            calls.append("apk")
            return DispatchResult(True, False, None)

        def stage(**kwargs):
            calls.append("stage")
            return "/stage", tuple()

        def materialize(**kwargs):
            calls.append("runtime")
            return "/data/adb/mobile-proxy-node/releases/v0.1.6"

        phone_target.dispatch_install_once = install
        phone_target._stage_runtime = stage
        phone_target._materialize_inactive = materialize
        phone_target._activate = lambda **kwargs: calls.append("activate")
        try:
            result = phone_target.dispatch_release_once(
                serial="serial", apk=apk, release_root=release, release_id="v0.1.6",
                required_paths=required, install_apk=True, install_runtime=True,
            )
        finally:
            (
                phone_target._stage_runtime,
                phone_target.dispatch_install_once,
                phone_target._materialize_inactive,
                phone_target._activate,
            ) = originals
        assert result.confirmed is True and result.outcome_unknown is False
        assert calls == ["apk", "stage", "runtime", "activate"]


def test_rooted_runtime_timeout_remains_unknown_after_mutation_boundary() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        apk = Path(raw) / "app.apk"
        apk.write_bytes(b"apk")
        originals = (phone_target._stage_runtime, phone_target._run_root_script)
        phone_target._stage_runtime = lambda **kwargs: ("/stage", tuple())
        phone_target._run_root_script = lambda *args, **kwargs: root_result(status="timeout", returncode=None)
        try:
            result = phone_target.dispatch_release_once(
                serial="serial", apk=apk, release_root=release, release_id="v0.1.6",
                required_paths=required, install_apk=False, install_runtime=True,
            )
        finally:
            phone_target._stage_runtime, phone_target._run_root_script = originals
        assert result.confirmed is False
        assert result.outcome_unknown is True
        assert result.error_class == "ROOTED_RUNTIME_MUTATION_OUTCOME_UNKNOWN"


def test_apk_unknown_stops_before_any_rooted_runtime_mutation() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        apk = Path(raw) / "app.apk"
        apk.write_bytes(b"apk")
        mutated: list[str] = []
        originals = (phone_target._stage_runtime, phone_target.dispatch_install_once, phone_target._materialize_inactive)
        phone_target.dispatch_install_once = lambda **kwargs: DispatchResult(False, True, "ADB_UNKNOWN")
        phone_target._stage_runtime = lambda **kwargs: mutated.append("stage")
        phone_target._materialize_inactive = lambda **kwargs: mutated.append("runtime")
        try:
            result = phone_target.dispatch_release_once(
                serial="serial", apk=apk, release_root=release, release_id="v0.1.6",
                required_paths=required, install_apk=True, install_runtime=True,
            )
        finally:
            phone_target._stage_runtime, phone_target.dispatch_install_once, phone_target._materialize_inactive = originals
        assert result.outcome_unknown is True
        assert mutated == []


def test_composite_dispatch_can_skip_already_desired_apk_domain() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        apk = Path(raw) / "app.apk"
        apk.write_bytes(b"apk")
        calls: list[str] = []
        originals = (
            phone_target._stage_runtime,
            phone_target.dispatch_install_once,
            phone_target._materialize_inactive,
            phone_target._activate,
        )
        phone_target.dispatch_install_once = lambda **kwargs: calls.append("apk")
        phone_target._stage_runtime = lambda **kwargs: (calls.append("stage") or ("/stage", tuple()))
        phone_target._materialize_inactive = lambda **kwargs: (
            calls.append("runtime") or "/data/adb/mobile-proxy-node/releases/v0.1.6"
        )
        phone_target._activate = lambda **kwargs: calls.append("activate")
        try:
            result = phone_target.dispatch_release_once(
                serial="serial", apk=apk, release_root=release, release_id="v0.1.6",
                required_paths=required, install_apk=False, install_runtime=True,
            )
        finally:
            (
                phone_target._stage_runtime,
                phone_target.dispatch_install_once,
                phone_target._materialize_inactive,
                phone_target._activate,
            ) = originals
        assert result.confirmed is True and result.outcome_unknown is False
        assert calls == ["stage", "runtime", "activate"]


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
    print(f"PHONE_TARGET_TESTS_OK count={len(tests)}")


if __name__ == "__main__":
    main()
