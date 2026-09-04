from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

_PACKAGE = "com.example.mobileproxy"
_VERSION_CODE = re.compile(r"versionCode=([0-9]+)")
_VERSION_NAME = re.compile(r"versionName=([^\s]+)")
_SHA256 = re.compile(r"[0-9a-f]{64}")
ANDROID_BUILD_TOOLS_VERSION = "36.0.0"
_ANDROID_SDK_USER_RELATIVE_ROOT = Path(".local/share/mobile-proxy/android-sdk")


class AndroidObservationUnavailable(RuntimeError):
    pass


class AndroidArtifactRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class AndroidBuildTools:
    version: str
    root: Path
    aapt2: Path
    apksigner: Path

    @property
    def identity(self) -> str:
        return f"android-build-tools:{self.version}:user-local-v1"


@dataclass(frozen=True)
class AndroidObservation:
    target: str
    target_binding_id: str
    package_name: str
    installed: bool
    version_name: str | None
    version_code: int | None
    desired: bool
    artifact_sha256: str | None = None
    exact_artifact_verified: bool = False
    mode: str = "read_only"
    raw_device_identifier_recorded: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DispatchResult:
    confirmed: bool
    outcome_unknown: bool
    error_class: str | None


def _user_android_sdk_root(*, home: Path | None = None) -> Path:
    home_root = (home if home is not None else Path.home()).expanduser().resolve()
    if not home_root.is_absolute():
        raise AndroidArtifactRefused("runner home for Android build-tools is invalid")
    root = (home_root / _ANDROID_SDK_USER_RELATIVE_ROOT).resolve()
    try:
        root.relative_to(home_root)
    except ValueError as exc:
        raise AndroidArtifactRefused("Android build-tools root escaped runner home") from exc
    return root


def resolve_android_build_tools(*, home: Path | None = None) -> AndroidBuildTools:
    """Resolve the exact user-local verifier toolchain without consulting PATH.

    Installation is deliberately outside this controller function. Production
    execution is admitted only when the pinned Build Tools revision is already
    present at the controller-owned user-local location.
    """
    sdk_root = _user_android_sdk_root(home=home)
    build_root = sdk_root / "build-tools" / ANDROID_BUILD_TOOLS_VERSION
    aapt2 = build_root / "aapt2"
    apksigner = build_root / "apksigner"
    required = (("aapt2", aapt2), ("apksigner", apksigner))
    missing = [name for name, path in required if not path.is_file() or not os.access(path, os.X_OK)]
    if missing:
        raise AndroidArtifactRefused(
            f"pinned Android build-tools {ANDROID_BUILD_TOOLS_VERSION} are unavailable"
        )
    return AndroidBuildTools(
        version=ANDROID_BUILD_TOOLS_VERSION,
        root=build_root,
        aapt2=aapt2,
        apksigner=apksigner,
    )


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AndroidArtifactRefused("Android artifact bytes are unreadable") from exc
    return digest.hexdigest()


def verify_local_artifact_bytes(*, apk: Path, expected_sha256: str) -> str:
    """Prove the local APK still equals the exact admitted Release asset bytes."""
    if _SHA256.fullmatch(expected_sha256) is None:
        raise AndroidArtifactRefused("Android Release transport digest is invalid")
    if not apk.is_file() or apk.stat().st_size <= 0:
        raise AndroidArtifactRefused("admitted Android artifact is unavailable")
    actual = _sha256_file(apk)
    if not hmac.compare_digest(actual, expected_sha256):
        raise AndroidArtifactRefused("local Android artifact bytes differ from admitted Release")
    return actual


def _installed_apk_path(output: str) -> str | None:
    paths: list[str] = []
    for line in output.splitlines():
        value = line.strip()
        if not value:
            continue
        prefix, separator, path = value.partition(":")
        if prefix != "package" or separator != ":" or not path.startswith("/"):
            raise AndroidObservationUnavailable("installed APK path observation is invalid")
        paths.append(path)
    if not paths:
        return None
    if len(paths) != 1:
        raise AndroidObservationUnavailable("installed APK path observation is ambiguous")
    return paths[0]


def observe(
    *,
    serial: str,
    binding_key: str,
    expected_version_name: str,
    expected_version_code: int,
    expected_artifact_sha256: str,
) -> AndroidObservation:
    if _SHA256.fullmatch(expected_artifact_sha256) is None:
        raise AndroidObservationUnavailable("admitted Android artifact identity is invalid")
    binding = target_binding_id(serial, binding_key)
    package_path = _adb_read(serial, ["shell", "pm", "path", _PACKAGE])
    if package_path.returncode != 0:
        return AndroidObservation(
            target="phone-production",
            target_binding_id=binding,
            package_name=_PACKAGE,
            installed=False,
            version_name=None,
            version_code=None,
            desired=False,
        )
    remote_path = _installed_apk_path(package_path.stdout)
    if remote_path is None:
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

    with tempfile.TemporaryDirectory(prefix="mobile-proxy-installed-apk-") as raw:
        local = Path(raw) / "installed-base.apk"
        pull = _adb_read(serial, ["pull", remote_path, str(local)], timeout=120)
        if pull.returncode != 0 or not local.is_file() or local.stat().st_size <= 0:
            raise AndroidObservationUnavailable("installed APK capture is unavailable")
        try:
            observed_sha256 = _sha256_file(local)
        except AndroidArtifactRefused as exc:
            raise AndroidObservationUnavailable("installed APK content observation failed") from exc

    exact_artifact_verified = hmac.compare_digest(observed_sha256, expected_artifact_sha256)
    return AndroidObservation(
        target="phone-production",
        target_binding_id=binding,
        package_name=_PACKAGE,
        installed=True,
        version_name=name,
        version_code=code,
        desired=(
            name == expected_version_name
            and code == expected_version_code
            and exact_artifact_verified
        ),
        artifact_sha256=observed_sha256,
        exact_artifact_verified=exact_artifact_verified,
    )


def verify_artifact(
    *,
    apk: Path,
    expected_sha256: str,
    expected_version_name: str,
    expected_version_code: int,
    build_tools: AndroidBuildTools | None = None,
) -> dict[str, object]:
    actual = verify_local_artifact_bytes(apk=apk, expected_sha256=expected_sha256)
    tools = build_tools if build_tools is not None else resolve_android_build_tools()
    if tools.version != ANDROID_BUILD_TOOLS_VERSION:
        raise AndroidArtifactRefused("Android verifier build-tools identity differs")
    try:
        result = subprocess.run(
            [str(tools.aapt2), "dump", "badging", str(apk)],
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
    try:
        subprocess.run(
            [str(tools.apksigner), "verify", "--verbose", str(apk)],
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
        "build_tools_identity": tools.identity,
        "build_tools_version": tools.version,
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
