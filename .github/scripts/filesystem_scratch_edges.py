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


@dataclass(frozen=True)
class AdbScratchRoundtripEdge:
    serial: str
    transaction_module: Any

    def scratch_roundtrip_once(self, request: object):
        scratch_ref, payload_ref = _require_request(request)
        path = shlex.quote(scratch_ref)
        payload = shlex.quote(payload_ref)
        script = " ".join(
            (
                "set -eu;",
                f"p={path};",
                f"v={payload};",
                "test ! -e \"$p\" && test ! -L \"$p\";",
                "mkdir \"$p\";",
                "cleanup(){ rm -f \"$p/payload\" \"$p/roundtrip\" 2>/dev/null || true; rmdir \"$p\" 2>/dev/null || true; };",
                "trap cleanup EXIT HUP INT TERM;",
                "printf '%s' \"$v\" > \"$p/payload\";",
                "cp \"$p/payload\" \"$p/roundtrip\";",
                "actual=$(cat \"$p/roundtrip\");",
                "test \"$actual\" = \"$v\";",
                "cleanup;",
                "trap - EXIT HUP INT TERM;",
                "test ! -e \"$p\" && test ! -L \"$p\";",
                "printf SCRATCH_ROUNDTRIP_OK;",
            )
        )
        try:
            result = _adb_shell(self.serial, script, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise self.transaction_module.DispatchOutcomeUnknown(
                "scratch dispatch transport outcome is unknown"
            ) from error
        if result.returncode != 0 or result.stdout.strip() != "SCRATCH_ROUNDTRIP_OK":
            raise self.transaction_module.DispatchOutcomeUnknown(
                "scratch dispatch may have reached the target but success is unproven"
            )
        return self.transaction_module.DispatchReceipt(
            "adb-dispatch:filesystem-scratch-roundtrip"
        )


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
