#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from android_target import (  # noqa: E402
    ANDROID_BUILD_TOOLS_VERSION,
    AndroidArtifactRefused,
    resolve_android_build_tools,
    verify_artifact,
)


def _tool_root(home: Path) -> Path:
    return home / ".local/share/mobile-proxy/android-sdk/build-tools" / ANDROID_BUILD_TOOLS_VERSION


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o755)


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


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"ANDROID_TARGET_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
