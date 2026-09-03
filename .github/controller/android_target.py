from __future__ import annotations

import hashlib
import hmac
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

_PACKAGE = "com.example.mobileproxy"
_VERSION_CODE = re.compile(r"versionCode=([0-9]+)")
_VERSION_NAME = re.compile(r"versionName=([^\s]+)")


class AndroidObservationUnavailable(RuntimeError):
    pass


class AndroidArtifactRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class AndroidObservation:
    target: str
    target_binding_id: str
    package_name: str
    installed: bool
    version_name: str | None
    version_code: int | None
    desired: bool
    mode: str = "read_only"
    raw_device_identifier_recorded: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DispatchResult:
    confirmed: bool
    outcome_unknown: bool
    error_class: str | None


def target_binding_id(serial: str, key: str) -> str:
    serial = serial.strip()
    if not serial or len(serial) > 128 or any(ch.isspace() for ch in serial):
        raise AndroidObservationUnavailable("registered Android target binding is invalid")
    if len(key) < 32 or key == serial:
        raise AndroidObservationUnavailable("independent Android target binding key is unavailable")
    digest = hmac.new(
        key.encode("utf-8"),
        b"phone-production\0" + serial.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return "tb-hmac-sha256:" + digest


def _adb() -> str:
    value = shutil.which("adb")
    if value is None:
        raise AndroidObservationUnavailable("ADB tooling is unavailable")
    return value


def _run(command: list[str], *, timeout: int, check: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        raise AndroidObservationUnavailable("Android read-only observation transport failed") from exc


def _adb_read(serial: str, arguments: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    adb = _adb()
    state = _run([adb, "-s", serial, "get-state"], timeout=15)
    if state.returncode != 0 or state.stdout.strip() != "device":
        raise AndroidObservationUnavailable("registered Android target is not in device state")
    return _run([adb, "-s", serial, *arguments], timeout=timeout)


def observe(
    *,
    serial: str,
    binding_key: str,
    expected_version_name: str,
    expected_version_code: int,
) -> AndroidObservation:
    binding = target_binding_id(serial, binding_key)
    package_path = _adb_read(serial, ["shell", "pm", "path", _PACKAGE])
    output = package_path.stdout.strip()
    if package_path.returncode != 0 or not output.startswith("package:"):
        return AndroidObservation(
            target="phone-production",
            target_binding_id=binding,
            package_name=_PACKAGE,
            installed=False,
            version_name=None,
            version_code=None,
            desired=False,
        )
    dump = _adb_read(serial, ["shell", "dumpsys", "package", _PACKAGE], timeout=45)
    if dump.returncode != 0:
        raise AndroidObservationUnavailable("installed package metadata observation failed")
    code_match = _VERSION_CODE.search(dump.stdout)
    name_match = _VERSION_NAME.search(dump.stdout)
    if code_match is None or name_match is None:
        raise AndroidObservationUnavailable("installed package version metadata is unavailable")
    code = int(code_match.group(1))
    name = name_match.group(1)
    return AndroidObservation(
        target="phone-production",
        target_binding_id=binding,
        package_name=_PACKAGE,
        installed=True,
        version_name=name,
        version_code=code,
        desired=name == expected_version_name and code == expected_version_code,
    )


def verify_artifact(
    *,
    apk: Path,
    expected_sha256: str,
    expected_version_name: str,
    expected_version_code: int,
) -> dict[str, object]:
    if not apk.is_file():
        raise AndroidArtifactRefused("admitted Android artifact is unavailable")
    actual = hashlib.sha256(apk.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise AndroidArtifactRefused("downloaded Android artifact digest differs")
    aapt = shutil.which("aapt2") or shutil.which("aapt")
    if aapt is None:
        raise AndroidArtifactRefused("Android package metadata tool is unavailable")
    try:
        result = subprocess.run(
            [aapt, "dump", "badging", str(apk)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        raise AndroidArtifactRefused("Android artifact metadata verification failed") from exc
    match = re.search(
        r"^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']+)'",
        result.stdout,
        re.MULTILINE,
    )
    if match is None:
        raise AndroidArtifactRefused("Android artifact package identity is unreadable")
    package, code, name = match.group(1), int(match.group(2)), match.group(3)
    if package != _PACKAGE or code != expected_version_code or name != expected_version_name:
        raise AndroidArtifactRefused("Android artifact package/version differs from Release manifest")
    apksigner = shutil.which("apksigner")
    if apksigner is None:
        raise AndroidArtifactRefused("APK signature verifier is unavailable")
    try:
        subprocess.run(
            [apksigner, "verify", "--verbose", str(apk)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        raise AndroidArtifactRefused("Android artifact signature verification failed") from exc
    return {
        "package_name": package,
        "version_name": name,
        "version_code": code,
        "sha256": actual,
        "signature_verified": True,
    }


def dispatch_install_once(*, serial: str, apk: Path) -> DispatchResult:
    """Invoke exactly one destructive package-manager command.

    Any timeout, transport failure, or non-zero adb result is classified as an
    unknown physical outcome. The caller must never call this function again for
    the same durable mutation intent; it may only perform read-only recovery.
    """
    adb = _adb()
    try:
        result = subprocess.run(
            [adb, "-s", serial, "install", "-r", str(apk)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return DispatchResult(False, True, "ADB_INSTALL_TRANSPORT_UNKNOWN")
    if result.returncode != 0:
        return DispatchResult(False, True, "ADB_INSTALL_RESULT_NOT_ACCEPTED")
    if "Success" not in result.stdout:
        return DispatchResult(False, True, "ADB_INSTALL_SUCCESS_NOT_PROVEN")
    return DispatchResult(True, False, None)
