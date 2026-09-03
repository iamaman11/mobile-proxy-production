#!/usr/bin/env python3
"""Hosted safety tests for the first real filesystem scratch Kernel seam."""

from __future__ import annotations

import pathlib
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import filesystem_scratch_edges as edges


class FakeTransaction:
    class DispatchOutcomeUnknown(RuntimeError):
        pass

    class DispatchReceipt:
        def __init__(self, source_ref: str) -> None:
            self.source_ref = source_ref

    class PostconditionProof:
        def __init__(self, passed: bool, source_ref: str) -> None:
            self.passed = passed
            self.source_ref = source_ref


def request():
    return SimpleNamespace(
        scratch_ref="/data/local/tmp/mobile-proxy-kernel-" + "a" * 32,
        payload_ref="payload/gen-sha256:" + "b" * 64,
    )


def completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def test_dispatch_success_and_single_transport_edge() -> None:
    calls = []

    def fake(serial: str, script: str, *, timeout: int):
        calls.append((serial, script, timeout))
        return completed(0, "SCRATCH_ROUNDTRIP_OK")

    with patch.object(edges, "_adb_shell", side_effect=fake):
        receipt = edges.AdbScratchRoundtripEdge("device", FakeTransaction).scratch_roundtrip_once(request())
    require(receipt.source_ref == "adb-dispatch:filesystem-scratch-roundtrip", "dispatch receipt differs")
    require(len(calls) == 1, "atomic dispatch must cross the controller->phone transport exactly once")
    script = calls[0][1]
    for required in ("mkdir", "printf '%s'", "cp", "actual=$(cat", "rmdir", "SCRATCH_ROUNDTRIP_OK"):
        require(required in script, f"dispatch script missing bounded roundtrip primitive: {required}")
    require("/data/local/tmp/mobile-proxy-kernel-" in script, "dispatch escaped bounded scratch namespace")


def test_any_dispatch_nonzero_is_unknown() -> None:
    with patch.object(edges, "_adb_shell", return_value=completed(1, "")):
        try:
            edges.AdbScratchRoundtripEdge("device", FakeTransaction).scratch_roundtrip_once(request())
        except FakeTransaction.DispatchOutcomeUnknown:
            pass
        else:
            raise AssertionError("nonzero physical dispatch was not classified UNKNOWN")


def test_dispatch_timeout_is_unknown() -> None:
    with patch.object(edges, "_adb_shell", side_effect=subprocess.TimeoutExpired("adb", 30)):
        try:
            edges.AdbScratchRoundtripEdge("device", FakeTransaction).scratch_roundtrip_once(request())
        except FakeTransaction.DispatchOutcomeUnknown:
            pass
        else:
            raise AssertionError("dispatch timeout was not classified UNKNOWN")


def test_observer_is_independent_and_read_only() -> None:
    calls = []

    def fake(serial: str, script: str, *, timeout: int):
        calls.append(script)
        return completed(0, "ABSENT")

    with patch.object(edges, "_adb_shell", side_effect=fake):
        proof = edges.AdbScratchAbsenceObserver("device", FakeTransaction).observe_scratch_roundtrip(request())
    require(proof.passed is True, "absent namespace must satisfy postcondition")
    require(len(calls) == 1, "postcondition observer must be one independent transport edge")
    for forbidden in ("mkdir", "rm -", "rmdir", "cp ", ">"):
        require(forbidden not in calls[0], f"postcondition observer contains mutation primitive: {forbidden}")


def test_present_namespace_fails_postcondition_without_mutation() -> None:
    with patch.object(edges, "_adb_shell", return_value=completed(0, "PRESENT")):
        proof = edges.AdbScratchAbsenceObserver("device", FakeTransaction).observe_scratch_roundtrip(request())
    require(proof.passed is False, "present scratch namespace must fail postcondition")


def test_static_kernel_only_path() -> None:
    entry = (HERE / "run_filesystem_scratch_transaction.py").read_text(encoding="utf-8")
    ports = (HERE / "filesystem_scratch_transaction_ports.py").read_text(encoding="utf-8")
    edge = (HERE / "filesystem_scratch_edges.py").read_text(encoding="utf-8")
    require("TransactionRunner().run" in entry, "entrypoint does not invoke Universal TransactionRunner")
    require("FilesystemScratchRoundtripBinding" in entry, "entrypoint does not use canonical scratch binding")
    require("FilesystemScratchRoundtripExecutor" in entry, "entrypoint does not use canonical scratch executor")
    require("run_android_filesystem_certification.py" not in entry, "legacy composite certification entered Kernel path")
    require("production-phone-global-mutation" in ports, "private ports do not represent global mutation scope")
    require("blind_retry_allowed\": False" in ports or '"blind_retry_allowed": False' in ports, "private evidence does not forbid blind retry")
    require(edge.count("subprocess.run(") == 1, "ADB transport helper must remain centralized in one subprocess call site")


def main() -> int:
    test_dispatch_success_and_single_transport_edge()
    test_any_dispatch_nonzero_is_unknown()
    test_dispatch_timeout_is_unknown()
    test_observer_is_independent_and_read_only()
    test_present_namespace_fails_postcondition_without_mutation()
    test_static_kernel_only_path()
    print("FILESYSTEM_SCRATCH_TRANSACTION_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
