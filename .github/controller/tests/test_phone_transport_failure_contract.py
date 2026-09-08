from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import phone_target  # noqa: E402


def _assert_phase(call, expected: phone_target.PhoneFailurePhase) -> None:
    try:
        call()
    except phone_target.PhoneTargetDiagnosticFailure as exc:
        assert exc.phase is expected
        return
    raise AssertionError(f"expected typed diagnostic failure: {expected.value}")


def _root_result(
    *,
    status: str = "completed",
    returncode: int | None = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
) -> phone_target.RootScriptResult:
    return phone_target.RootScriptResult(
        status=status,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def test_adb_tooling_unavailable_is_typed_without_text_parsing() -> None:
    with mock.patch.object(phone_target.shutil, "which", return_value=None):
        _assert_phase(phone_target._adb, phone_target.PhoneFailurePhase.ADB_TOOLING_UNAVAILABLE)


def test_adb_timeout_is_typed_at_subprocess_boundary() -> None:
    with mock.patch.object(
        phone_target.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd=["adb"], timeout=15),
    ):
        _assert_phase(
            lambda: phone_target._run(["adb"], timeout=15),
            phone_target.PhoneFailurePhase.ADB_TRANSPORT_TIMEOUT,
        )


def test_adb_os_error_is_bounded_unknown() -> None:
    with mock.patch.object(phone_target.subprocess, "run", side_effect=OSError("raw host path secret")):
        try:
            phone_target._run(["adb"], timeout=15)
        except phone_target.PhoneTargetDiagnosticFailure as exc:
            assert exc.phase is phone_target.PhoneFailurePhase.UNKNOWN
            assert "raw host path secret" not in str(exc)
        else:
            raise AssertionError("expected typed UNKNOWN failure")


def test_registered_device_non_device_state_is_typed() -> None:
    with (
        mock.patch.object(phone_target, "_adb", return_value="/usr/bin/adb"),
        mock.patch.object(
            phone_target,
            "_run",
            return_value=SimpleNamespace(returncode=0, stdout="offline\n", stderr="raw-secret"),
        ),
    ):
        _assert_phase(
            lambda: phone_target._require_device("registered-target"),
            phone_target.PhoneFailurePhase.REGISTERED_DEVICE_NOT_DEVICE,
        )


def test_root_spawn_timeout_truncation_and_unknown_are_typed() -> None:
    cases = (
        (_root_result(status="spawn_error", returncode=None), phone_target.PhoneFailurePhase.ROOT_SHELL_SPAWN_FAILED),
        (_root_result(status="timeout", returncode=None), phone_target.PhoneFailurePhase.ROOT_SCRIPT_TIMEOUT),
        (
            _root_result(
                status="transport_error",
                returncode=None,
                stdout=b"token=secret https://example.invalid/",
                stdout_truncated=True,
            ),
            phone_target.PhoneFailurePhase.ROOT_SCRIPT_OUTPUT_TRUNCATED,
        ),
        (_root_result(status="transport_error", returncode=None), phone_target.PhoneFailurePhase.UNKNOWN),
    )
    for result, expected in cases:
        with mock.patch.object(phone_target, "_run_root_script", return_value=result):
            try:
                phone_target._probe_root_stdout_contract("registered-target")
            except phone_target.PhoneTargetDiagnosticFailure as exc:
                assert exc.phase is expected
                assert "secret" not in str(exc)
                assert "example.invalid" not in str(exc)
            else:
                raise AssertionError(f"expected {expected.value}")


def test_positive_root_probe_distinguishes_nonzero_from_protocol_mismatch() -> None:
    with mock.patch.object(
        phone_target,
        "_run_root_script",
        return_value=_root_result(returncode=9, stdout=b"root=0\ngrammar=ok\ntools=ok\n"),
    ):
        _assert_phase(
            lambda: phone_target._probe_root_stdout_contract("registered-target"),
            phone_target.PhoneFailurePhase.ROOT_SCRIPT_NONZERO,
        )

    with mock.patch.object(
        phone_target,
        "_run_root_script",
        return_value=_root_result(returncode=0, stdout=b"unexpected token=secret\n"),
    ):
        try:
            phone_target._probe_root_stdout_contract("registered-target")
        except phone_target.PhoneTargetDiagnosticFailure as exc:
            assert exc.phase is phone_target.PhoneFailurePhase.ROOT_SCRIPT_PROTOCOL_MISMATCH
            assert "secret" not in str(exc)
        else:
            raise AssertionError("expected protocol mismatch")


def test_stderr_nonzero_probe_treats_wrong_expected_exit_as_protocol_mismatch() -> None:
    for result in (
        _root_result(returncode=0, stderr=b"stderr=ok\n"),
        _root_result(returncode=23, stdout=b"unexpected", stderr=b"stderr=ok\n"),
        _root_result(returncode=23, stderr=b"token=secret\n"),
    ):
        with mock.patch.object(phone_target, "_run_root_script", return_value=result):
            try:
                phone_target._probe_root_stderr_exit_contract("registered-target")
            except phone_target.PhoneTargetDiagnosticFailure as exc:
                assert exc.phase is phone_target.PhoneFailurePhase.ROOT_SCRIPT_PROTOCOL_MISMATCH
                assert "secret" not in str(exc)
            else:
                raise AssertionError("expected protocol mismatch")


def test_root_capability_keeps_exact_two_accepted_scripts() -> None:
    calls: list[bytes] = []

    def fake_run(serial: str, script: bytes, timeout: int = 30):
        calls.append(script)
        if len(calls) == 1:
            return _root_result(stdout=b"root=0\ngrammar=ok\ntools=ok\n")
        return _root_result(returncode=23, stderr=b"stderr=ok\n")

    with mock.patch.object(phone_target, "_run_root_script", side_effect=fake_run):
        phone_target._probe_root_capability("registered-target")

    assert len(calls) == 2
    assert calls[1] == b"printf 'stderr=ok\\n' >&2\nexit 23\n"
    assert b"id -u" in calls[0]
    assert b"command -v readlink" in calls[0]
    assert b"command -v test" in calls[0]
    assert b"command -v sha256sum" in calls[0]


def main() -> int:
    tests = sorted(
        (value for name, value in globals().items() if name.startswith("test_") and callable(value)),
        key=lambda item: item.__name__,
    )
    for test in tests:
        test()
    print(f"PHONE_TRANSPORT_FAILURE_CONTRACT_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
