#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import android_target as TARGET  # noqa: E402
from android_target import (  # noqa: E402
    ANDROID_BUILD_TOOLS_VERSION,
    AndroidArtifactRefused,
    AndroidObservationUnavailable,
    observe,
    resolve_android_build_tools,
    verify_artifact,
    verify_local_artifact_bytes,
)
from deployment_state_machine import (  # noqa: E402
    DeploymentState,
    recover_unknown,
    reduce_state,
)

SERIAL = "registered-device-1"
BINDING_KEY = "k" * 32
EXPECTED_BYTES = b"exact-product-release-apk"
OTHER_BYTES = b"same-version-different-apk"
EXPECTED_SHA256 = hashlib.sha256(EXPECTED_BYTES).hexdigest()


def _tool_root(home: Path) -> Path:
    return home / ".local/share/mobile-proxy/android-sdk/build-tools" / ANDROID_BUILD_TOOLS_VERSION


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o755)


def _completed(stdout: str = "", *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


def _observe_installed(
    installed_bytes: bytes,
    *,
    package_stdout: str = "package:/data/app/example/base.apk\n",
    package_returncode: int = 0,
    pull_returncode: int = 0,
    materialize_pull: bool = True,
):
    calls: list[tuple[str, ...]] = []

    def fake_adb_read(serial: str, arguments: list[str], *, timeout: int = 30):
        assert serial == SERIAL
        argv = tuple(arguments)
        calls.append(argv)
        if argv == ("shell", "pm", "path", "com.example.mobileproxy"):
            return _completed(package_stdout, returncode=package_returncode)
        if argv == ("shell", "dumpsys", "package", "com.example.mobileproxy"):
            return _completed("versionCode=1004\nversionName=0.1.4\n")
        if len(argv) == 3 and argv[0] == "pull":
            if materialize_pull:
                Path(argv[2]).write_bytes(installed_bytes)
            return _completed("1 file pulled\n", returncode=pull_returncode)
        raise AssertionError(f"unexpected read-only adb call: {argv!r}")

    with mock.patch.object(TARGET, "_adb_read", side_effect=fake_adb_read):
        observation = observe(
            serial=SERIAL,
            binding_key=BINDING_KEY,
            expected_version_name="0.1.4",
            expected_version_code=1004,
            expected_artifact_sha256=EXPECTED_SHA256,
        )
    return observation, calls


def _observe_state() -> DeploymentState:
    state = reduce_state(DeploymentState(), "request_received")
    return reduce_state(state, "authorized")


def _dispatch_state() -> DeploymentState:
    state = reduce_state(_observe_state(), "observed")
    return reduce_state(state, "intent_persisted")


def test_pinned_build_tools_are_user_local_and_not_path_resolved() -> None:
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        root = _tool_root(home)
        root.mkdir(parents=True)
        _write_executable(root / "aapt2", "#!/bin/sh\nexit 0\n")
        _write_executable(root / "apksigner", "#!/bin/sh\nexit 0\n")
        tools = resolve_android_build_tools(home=home)
        assert tools.version == "36.0.0"
        assert tools.root == root.resolve()
        assert tools.aapt2 == root.resolve() / "aapt2"
        assert tools.apksigner == root.resolve() / "apksigner"
        assert tools.identity == "android-build-tools:36.0.0:user-local-v1"


def test_missing_pinned_build_tools_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        root = _tool_root(home)
        root.mkdir(parents=True)
        _write_executable(root / "aapt2", "#!/bin/sh\nexit 0\n")
        try:
            resolve_android_build_tools(home=home)
        except AndroidArtifactRefused as exc:
            assert "pinned Android build-tools 36.0.0 are unavailable" in str(exc)
        else:
            raise AssertionError("incomplete Android build-tools contract was unexpectedly accepted")


def test_artifact_verification_uses_exact_pinned_tools_and_keeps_full_checks() -> None:
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw) / "home"
        root = _tool_root(home)
        root.mkdir(parents=True)
        _write_executable(
            root / "aapt2",
            "#!/bin/sh\nprintf \"%s\\n\" \"package: name='com.example.mobileproxy' versionCode='1004' versionName='0.1.4'\"\n",
        )
        _write_executable(root / "apksigner", "#!/bin/sh\nexit 0\n")
        tools = resolve_android_build_tools(home=home)

        apk = Path(raw) / "release.apk"
        apk.write_bytes(b"bounded-test-apk")
        digest = hashlib.sha256(apk.read_bytes()).hexdigest()
        result = verify_artifact(
            apk=apk,
            expected_sha256=digest,
            expected_version_name="0.1.4",
            expected_version_code=1004,
            build_tools=tools,
        )
        assert result["package_name"] == "com.example.mobileproxy"
        assert result["version_name"] == "0.1.4"
        assert result["version_code"] == 1004
        assert result["sha256"] == digest
        assert result["signature_verified"] is True
        assert result["build_tools_identity"] == "android-build-tools:36.0.0:user-local-v1"


def test_exact_installed_bytes_are_desired() -> None:
    observation, calls = _observe_installed(EXPECTED_BYTES)
    assert observation.desired is True
    assert observation.exact_artifact_verified is True
    assert observation.artifact_sha256 == EXPECTED_SHA256
    assert calls[0] == ("shell", "pm", "path", "com.example.mobileproxy")
    assert calls[1] == ("shell", "dumpsys", "package", "com.example.mobileproxy")
    assert calls[2][0:2] == ("pull", "/data/app/example/base.apk")


def test_same_version_different_installed_bytes_cannot_take_noop_acceptance() -> None:
    observation, _ = _observe_installed(OTHER_BYTES)
    assert observation.version_name == "0.1.4"
    assert observation.version_code == 1004
    assert observation.exact_artifact_verified is False
    assert observation.desired is False

    state = _observe_state()
    state = reduce_state(state, "already_desired" if observation.desired else "observed")
    assert state.state == "INTENT"
    assert state.state != "ACCEPTED"


def test_post_dispatch_byte_mismatch_is_quarantined() -> None:
    observation, _ = _observe_installed(OTHER_BYTES)
    state = reduce_state(_dispatch_state(), "dispatch_confirmed")
    state = reduce_state(state, "verify_match" if observation.desired else "verify_mismatch")
    assert state.state == "QUARANTINED"
    assert state.postcondition_verified is True
    assert state.recovery_required is True


def test_unknown_recovery_uses_exact_bytes_for_terminal_classification() -> None:
    exact, _ = _observe_installed(EXPECTED_BYTES)
    mismatch, _ = _observe_installed(OTHER_BYTES)

    unknown = reduce_state(_dispatch_state(), "dispatch_outcome_unknown")
    recovered = recover_unknown(
        unknown,
        "recovery_observed_desired" if exact.desired else "recovery_observed_other",
    )
    assert recovered.state == "RECOVERED"
    assert recovered.postcondition_verified is True

    unknown = reduce_state(_dispatch_state(), "dispatch_outcome_unknown")
    quarantined = recover_unknown(
        unknown,
        "recovery_observed_desired" if mismatch.desired else "recovery_observed_other",
    )
    assert quarantined.state == "QUARANTINED"
    assert quarantined.postcondition_verified is True


def test_ambiguous_installed_apk_paths_are_observation_unavailable() -> None:
    try:
        _observe_installed(
            EXPECTED_BYTES,
            package_stdout=(
                "package:/data/app/example/base.apk\n"
                "package:/data/app/example/split_config.apk\n"
            ),
        )
    except AndroidObservationUnavailable as exc:
        assert "ambiguous" in str(exc)
    else:
        raise AssertionError("ambiguous installed APK paths were unexpectedly accepted")


def test_pull_capture_failure_is_observation_unavailable() -> None:
    try:
        _observe_installed(EXPECTED_BYTES, materialize_pull=False)
    except AndroidObservationUnavailable as exc:
        assert "capture is unavailable" in str(exc)
    else:
        raise AssertionError("missing installed APK capture was unexpectedly accepted")


def test_local_release_bytes_are_reproved_before_dispatch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        apk = Path(raw) / "release.apk"
        apk.write_bytes(EXPECTED_BYTES)
        assert verify_local_artifact_bytes(
            apk=apk,
            expected_sha256=EXPECTED_SHA256,
        ) == EXPECTED_SHA256

        apk.write_bytes(OTHER_BYTES)
        try:
            verify_local_artifact_bytes(apk=apk, expected_sha256=EXPECTED_SHA256)
        except AndroidArtifactRefused as exc:
            assert "differ from admitted Release" in str(exc)
        else:
            raise AssertionError("changed local Release APK was unexpectedly accepted")


def test_pre_dispatch_artifact_refusal_is_terminal_without_dispatch() -> None:
    state = reduce_state(
        _dispatch_state(),
        "dispatch_refused",
        reason="local Android artifact bytes differ from admitted Release",
    )
    assert state.state == "REFUSED"
    assert state.intent_persisted is True
    assert state.dispatch_attempted is False
    assert state.mutation_performed is False
    assert state.recovery_required is False


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"ANDROID_TARGET_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
