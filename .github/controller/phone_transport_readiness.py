from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from android_target import (
    AndroidObservationUnavailable,
    _adb,
    _ensure_adb_server,
    _registered_target_state,
)
from phone_target import PhoneTargetUnavailable, _probe_root_capability, _require_device

_BOUNDED_TARGET_STATES = frozenset(
    {"offline", "unauthorized", "absent_from_adb_inventory", "no_permissions", "other"}
)
_FAILURE_CODES = frozenset(
    {
        "ADB_TOOLING_UNAVAILABLE",
        "ADB_SERVER_UNAVAILABLE",
        "ADB_DEVICE_INVENTORY_UNAVAILABLE",
        "ADB_TRANSPORT_TIMEOUT",
        "ADB_TRANSPORT_UNAVAILABLE",
        "REGISTERED_DEVICE_NOT_DEVICE",
        "ADB_GET_STATE_FAILED",
        "ROOT_CONTRACT_FAILED",
    }
)
_FAILURE_PHASES = frozenset(
    {"adb_tooling", "adb_server", "registered_device_state", "strict_get_state", "root_contract"}
)


@dataclass(frozen=True)
class PhoneTransportReadiness:
    ready: bool
    timing_ms: dict[str, int]
    failure_phase: str | None = None
    failure_code: str | None = None
    target_state: str | None = None

    def __post_init__(self) -> None:
        if self.ready:
            if self.failure_phase is not None or self.failure_code is not None or self.target_state is not None:
                raise ValueError("ready transport must not carry failure evidence")
            return
        if self.failure_phase not in _FAILURE_PHASES or self.failure_code not in _FAILURE_CODES:
            raise ValueError("bounded phone transport failure evidence differs")
        if self.failure_code == "REGISTERED_DEVICE_NOT_DEVICE":
            if self.target_state not in _BOUNDED_TARGET_STATES:
                raise ValueError("bounded registered target state differs")
        elif self.target_state is not None:
            raise ValueError("non-target-state failure must not carry target state")


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _transport_failure_code(exc: BaseException, *, default: str) -> str:
    cause = exc.__cause__
    if isinstance(cause, subprocess.TimeoutExpired):
        return "ADB_TRANSPORT_TIMEOUT"
    if isinstance(cause, OSError):
        return "ADB_TRANSPORT_UNAVAILABLE"
    return default


def probe_registered_phone_transport(serial: str) -> PhoneTransportReadiness:
    """Return bounded readiness for the registered phone without raw transport output.

    This is diagnostic-only. It may establish the local ADB server, inspect only
    the registered target's bounded state, require strict ``adb get-state``
    success, and verify the existing rooted-shell contract. It never returns the
    registered serial, raw ADB inventory, stdout/stderr, PIDs, paths, or secrets.
    """
    timings: dict[str, int] = {}

    phase_started = time.monotonic()
    try:
        adb = _adb()
    except AndroidObservationUnavailable:
        timings["adb_tooling"] = _elapsed_ms(phase_started)
        return PhoneTransportReadiness(
            ready=False,
            timing_ms=timings,
            failure_phase="adb_tooling",
            failure_code="ADB_TOOLING_UNAVAILABLE",
        )
    timings["adb_tooling"] = _elapsed_ms(phase_started)

    phase_started = time.monotonic()
    try:
        _ensure_adb_server(adb)
    except AndroidObservationUnavailable as exc:
        timings["adb_server"] = _elapsed_ms(phase_started)
        return PhoneTransportReadiness(
            ready=False,
            timing_ms=timings,
            failure_phase="adb_server",
            failure_code=_transport_failure_code(exc, default="ADB_SERVER_UNAVAILABLE"),
        )
    timings["adb_server"] = _elapsed_ms(phase_started)

    phase_started = time.monotonic()
    try:
        state = _registered_target_state(adb, serial)
    except AndroidObservationUnavailable as exc:
        timings["registered_device_state"] = _elapsed_ms(phase_started)
        return PhoneTransportReadiness(
            ready=False,
            timing_ms=timings,
            failure_phase="registered_device_state",
            failure_code=_transport_failure_code(exc, default="ADB_DEVICE_INVENTORY_UNAVAILABLE"),
        )
    timings["registered_device_state"] = _elapsed_ms(phase_started)
    if state != "device":
        if state not in _BOUNDED_TARGET_STATES:
            state = "other"
        return PhoneTransportReadiness(
            ready=False,
            timing_ms=timings,
            failure_phase="registered_device_state",
            failure_code="REGISTERED_DEVICE_NOT_DEVICE",
            target_state=state,
        )

    phase_started = time.monotonic()
    try:
        _require_device(serial)
    except PhoneTargetUnavailable as exc:
        timings["strict_get_state"] = _elapsed_ms(phase_started)
        return PhoneTransportReadiness(
            ready=False,
            timing_ms=timings,
            failure_phase="strict_get_state",
            failure_code=_transport_failure_code(exc, default="ADB_GET_STATE_FAILED"),
        )
    timings["strict_get_state"] = _elapsed_ms(phase_started)

    phase_started = time.monotonic()
    try:
        _probe_root_capability(serial)
    except PhoneTargetUnavailable:
        timings["root_contract"] = _elapsed_ms(phase_started)
        return PhoneTransportReadiness(
            ready=False,
            timing_ms=timings,
            failure_phase="root_contract",
            failure_code="ROOT_CONTRACT_FAILED",
        )
    timings["root_contract"] = _elapsed_ms(phase_started)

    return PhoneTransportReadiness(ready=True, timing_ms=timings)
