"""Bounded read-only evidence from the existing Windows USB bridge owner."""
from __future__ import annotations

import stat
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BRIDGE_LOG_PATH = Path(
    "/mnt/c/ProgramData/MobileProxy/usb-bridge/mobile-proxy-usb-bridge.log"
)
_MAX_LOG_BYTES = 65536
_MAX_EVENT_AGE_SECONDS = 60
_MAX_FUTURE_SKEW_SECONDS = 5
_PREFIX = "mobile-proxy-usb-bridge "

_FAILURE_CATEGORIES = frozenset(
    {
        "attach_distro_missing",
        "attach_distro_not_running",
        "attach_distro_not_wsl2",
        "attach_failed",
        "attach_firewall_blocked",
        "attach_host_address_unavailable",
        "attach_kernel_not_usbip_capable",
        "attach_networking_mode_unsupported",
        "attach_unknown",
        "attach_usbip_client_attach_failed",
        "attach_usbip_client_unavailable",
        "attach_usbipd_not_local_drive",
        "attach_usbipd_service_unavailable",
        "attach_vhci_unavailable",
        "attach_windows_device_busy",
        "attach_wsl2_unavailable",
        "attach_wsl_support_missing",
        "attach_wsl_support_mount_failed",
        "bind_failed",
        "inventory_unavailable",
        "state_unavailable",
        "unexpected_local_error",
        "wsl_unavailable",
    }
)


def not_evaluated_host_usb_bridge_evidence() -> dict[str, object]:
    return {
        "classification": "NOT_EVALUATED",
        "failure_category": None,
        "freshness": "NOT_EVALUATED",
        "age_seconds": None,
    }


def _unavailable() -> dict[str, object]:
    return {
        "classification": "EVIDENCE_UNAVAILABLE",
        "failure_category": None,
        "freshness": "UNKNOWN",
        "age_seconds": None,
    }


def _invalid() -> dict[str, object]:
    return {
        "classification": "EVIDENCE_INVALID",
        "failure_category": None,
        "freshness": "UNKNOWN",
        "age_seconds": None,
    }


def _parse_message(message: str) -> tuple[str, str | None] | None:
    if message == "bridge_started":
        return "BRIDGE_STARTED", None
    if message == "approved_usb_device_not_present; retrying":
        return "USB_DEVICE_NOT_PRESENT", None
    if message == "allowlisted_usb_attached_to_wsl":
        return "ATTACH_SUCCEEDED", None

    for category in sorted(_FAILURE_CATEGORIES, key=len, reverse=True):
        prefix = f"bridge_{category}_"
        if not message.startswith(prefix):
            continue
        suffix = message[len(prefix) :]
        if not suffix.endswith("; retrying"):
            return None
        diagnostic = suffix[: -len("; retrying")]
        if "_line" not in diagnostic:
            return None
        exception_type, line = diagnostic.rsplit("_line", 1)
        if not exception_type or not exception_type.replace("_", "").isalnum():
            return None
        if not line.isdigit():
            return None
        return "BRIDGE_FAILURE", category
    return None


def collect_host_usb_bridge_evidence(
    *,
    path: Path = DEFAULT_BRIDGE_LOG_PATH,
    now: datetime | None = None,
) -> dict[str, object]:
    """Reduce the existing bounded bridge log to a secret-safe current event class."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("bridge evidence clock must be timezone-aware")
    current = current.astimezone(timezone.utc)

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _unavailable()
    except OSError:
        return _unavailable()

    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return _invalid()
    if metadata.st_size < 1 or metadata.st_size > _MAX_LOG_BYTES:
        return _invalid()

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _unavailable()
    except (OSError, UnicodeError):
        return _invalid()

    lines = [line for line in raw.splitlines()[-200:] if line]
    if not lines:
        return _invalid()
    line = lines[-1]

    try:
        timestamp_text, owner, message = line.split(" ", 2)
    except ValueError:
        return _invalid()
    if not owner or not message:
        return _invalid()

    # PowerShell's UTC universal sortable format is two whitespace-separated
    # date/time tokens, so recover the timestamp without retaining the raw line.
    parts = line.split(" ", 3)
    if len(parts) != 4 or parts[2] != _PREFIX.strip():
        return _invalid()
    timestamp_text = f"{parts[0]} {parts[1]}"
    message = parts[3]

    try:
        event_time = datetime.strptime(timestamp_text, "%Y-%m-%d %H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return _invalid()

    parsed = _parse_message(message)
    if parsed is None:
        return _invalid()
    event_classification, failure_category = parsed

    age = int((current - event_time).total_seconds())
    if age < -_MAX_FUTURE_SKEW_SECONDS:
        return _invalid()
    age = max(0, age)
    if age > _MAX_EVENT_AGE_SECONDS:
        return {
            "classification": "EVIDENCE_STALE",
            "failure_category": None,
            "freshness": "STALE",
            "age_seconds": age,
        }

    return {
        "classification": event_classification,
        "failure_category": failure_category,
        "freshness": "FRESH",
        "age_seconds": age,
    }
