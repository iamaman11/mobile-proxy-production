#!/usr/bin/env python3
"""Concrete production-phone edges for one bounded scratch-roundtrip transaction."""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex
import subprocess
from typing import Any


_SCRATCH = re.compile(r"^/data/local/tmp/mobile-proxy-kernel-[0-9a-f]{32}$")
_PAYLOAD = re.compile(r"^payload/gen-sha256:[0-9a-f]{64}$")
_DISPATCH_PREFIX = "SCRATCH_ROUNDTRIP_V1:"
_DISPATCH_STAGES = frozenset(
    {
        "PRECONDITION_PRESENT",
        "MKDIR_FAILED",
        "WRITE_FAILED",
        "COPY_FAILED",
        "READ_FAILED",
        "VERIFY_FAILED",
        "CLEANUP_FAILED",
        "FINAL_ABSENCE_FAILED",
    }
)


class ScratchEdgeFailure(RuntimeError):
    pass


def _require_request(request: object) -> tuple[str, str]:
    scratch_ref = str(getattr(request, "scratch_ref", ""))
    payload_ref = str(getattr(request, "payload_ref", ""))
    if _SCRATCH.fullmatch(scratch_ref) is None:
        raise ScratchEdgeFailure("scratch path is outside the bounded kernel namespace")
    if _PAYLOAD.fullmatch(payload_ref) is None:
        raise ScratchEdgeFailure("scratch payload identity is invalid")
    return scratch_ref, payload_ref


def _adb_shell(serial: str, script: str, *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["adb", "-s", serial, "shell", "sh", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _bounded_dispatch_stage(result: subprocess.CompletedProcess[str]) -> str | None:
    stdout = result.stdout.strip()
    if stdout == f"{_DISPATCH_PREFIX}OK" and result.returncode == 0:
        return "OK"
    if stdout.startswith(_DISPATCH_PREFIX):
        stage = stdout.removeprefix(_DISPATCH_PREFIX)
        if stage in _DISPATCH_STAGES:
            return stage
    return None


@dataclass(frozen=True)
class AdbScratchRoundtripEdge:
    serial: str
    transaction_module: Any

    def scratch_roundtrip_once(self, request: object):
        scratch_ref, payload_ref = _require_request(request)
        path = shlex.quote(scratch_ref)
        payload = shlex.quote(payload_ref)
        # Exactly one controller->phone shell invocation. Every emitted value is a
        # fixed whitelisted token; raw stdout/stderr is never propagated to durable
        # transaction evidence.
        script = " ".join(
            (
                f"p={path};",
                f"v={payload};",
                f"prefix={shlex.quote(_DISPATCH_PREFIX)};",
                "if test -e \"$p\" || test -L \"$p\"; then printf '%sPRECONDITION_PRESENT' \"$prefix\"; exit 20; fi;",
                "if ! mkdir \"$p\"; then printf '%sMKDIR_FAILED' \"$prefix\"; exit 21; fi;",
                "cleanup(){ rm -f \"$p/payload\" \"$p/roundtrip\" >/dev/null 2>&1 || true; rmdir \"$p\" >/dev/null 2>&1 || true; };",
                "trap cleanup EXIT HUP INT TERM;",
                "if ! printf '%s' \"$v\" > \"$p/payload\"; then printf '%sWRITE_FAILED' \"$prefix\"; exit 22; fi;",
                "if ! cp \"$p/payload\" \"$p/roundtrip\"; then printf '%sCOPY_FAILED' \"$prefix\"; exit 23; fi;",
                "if ! actual=$(cat \"$p/roundtrip\"); then printf '%sREAD_FAILED' \"$prefix\"; exit 24; fi;",
                "if test \"$actual\" != \"$v\"; then printf '%sVERIFY_FAILED' \"$prefix\"; exit 25; fi;",
                "cleanup;",
                "if test -e \"$p\" || test -L \"$p\"; then printf '%sCLEANUP_FAILED' \"$prefix\"; exit 26; fi;",
                "trap - EXIT HUP INT TERM;",
                "if test -e \"$p\" || test -L \"$p\"; then printf '%sFINAL_ABSENCE_FAILED' \"$prefix\"; exit 27; fi;",
                "printf '%sOK' \"$prefix\";",
            )
        )
        try:
            result = _adb_shell(self.serial, script, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise self.transaction_module.DispatchOutcomeUnknown(
                "scratch dispatch transport outcome is unknown"
            ) from error
        stage = _bounded_dispatch_stage(result)
        if stage == "OK":
            return self.transaction_module.DispatchReceipt(
                "adb-dispatch:filesystem-scratch-roundtrip"
            )
        if stage in _DISPATCH_STAGES:
            raise self.transaction_module.DispatchOutcomeUnknown(
                f"scratch dispatch bounded_stage={stage}"
            )
        raise self.transaction_module.DispatchOutcomeUnknown(
            "scratch dispatch may have reached the target but bounded stage is unproven"
        )


@dataclass(frozen=True)
class RecoveryDispatchForbiddenEdge:
    """Defense-in-depth binding for a recovery execution.

    Even if a future Kernel regression attempted the primary dispatch method, this
    edge contains no ADB call and fails before any device mutation can occur.
    """

    transaction_module: Any

    def scratch_roundtrip_once(self, request: object):
        _require_request(request)
        raise ScratchEdgeFailure("primary scratch dispatch is forbidden in recovery binding")


@dataclass(frozen=True)
class AdbScratchAbsenceObserver:
    serial: str
    transaction_module: Any

    def observe_scratch_roundtrip(self, request: object):
        scratch_ref, _ = _require_request(request)
        path = shlex.quote(scratch_ref)
        script = (
            f"p={path}; "
            "if test ! -e \"$p\" && test ! -L \"$p\"; "
            "then printf ABSENT; else printf PRESENT; fi"
        )
        try:
            result = _adb_shell(self.serial, script, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ScratchEdgeFailure("scratch postcondition transport failed") from error
        if result.returncode != 0:
            raise ScratchEdgeFailure("scratch postcondition transport returned nonzero")
        state = result.stdout.strip()
        if state == "ABSENT":
            return self.transaction_module.PostconditionProof(
                True,
                "adb-observation:filesystem-scratch-absent",
            )
        if state == "PRESENT":
            return self.transaction_module.PostconditionProof(
                False,
                "adb-observation:filesystem-scratch-present",
            )
        raise ScratchEdgeFailure("scratch postcondition observation is ambiguous")
