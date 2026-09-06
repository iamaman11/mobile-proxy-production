from __future__ import annotations

import hashlib
import importlib.util
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

fake_android = ModuleType("android_target")


class DispatchResult:
    def __init__(self, confirmed: bool, outcome_unknown: bool, error_class: str | None):
        self.confirmed = confirmed
        self.outcome_unknown = outcome_unknown
        self.error_class = error_class


fake_android.DispatchResult = DispatchResult
fake_android.dispatch_install_once = lambda **kwargs: DispatchResult(True, False, None)
sys.modules["android_target"] = fake_android

spec = importlib.util.spec_from_file_location("phone_target", Path(__file__).resolve().parents[1] / "phone_target.py")
phone_target = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["phone_target"] = phone_target
spec.loader.exec_module(phone_target)


def runtime_release(root: Path) -> tuple[Path, tuple[str, ...]]:
    release = root / "release"
    files = {
        "service.sh": b"service\n",
        "bin/runtime-supervisor": b"runtime\n",
        "config/host-daemon.json": b"{}\n",
        "config/sing-box.json": b"{}\n",
    }
    for relative, body in files.items():
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return release, tuple(files)


def test_observe_runtime_root_precondition_and_empty_layout_are_exact_and_read_only() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        target = "/data/adb/mobile-proxy-node/releases/v0.1.6"
        expected_layout = (
            f"if [ -e '{target}' ] || [ -L '{target}' ]; then echo target=present; else echo target=absent; fi; "
            "if [ -L '/data/adb/mobile-proxy-node/current' ]; then printf 'current='; readlink '/data/adb/mobile-proxy-node/current'; "
            "elif [ -e '/data/adb/mobile-proxy-node/current' ]; then echo current=invalid; else echo current=absent; fi"
        )
        calls: list[list[str]] = []

        def fake_read(serial, args, timeout=30):
            calls.append(list(args))
            if args == ["shell", "su", "0", "sh", "-c", "true"]:
                return SimpleNamespace(returncode=0, stdout="")
            if args == ["shell", "su", "0", "sh", "-c", expected_layout]:
                return SimpleNamespace(returncode=0, stdout="target=absent\ncurrent=absent\n")
            raise AssertionError(f"unexpected read: {args!r}")

        original = phone_target._read
        phone_target._read = fake_read
        try:
            observed = phone_target.observe_runtime(
                serial="serial", release_root=release, release_id="v0.1.6", required_paths=required
            )
        finally:
            phone_target._read = original
        assert calls == [
            ["shell", "su", "0", "sh", "-c", "true"],
            ["shell", "su", "0", "sh", "-c", expected_layout],
        ]
        assert observed.target_release_exists is False
        assert observed.current_target is None
        assert observed.desired is False
        assert observed.admissible_for_new_dispatch is True
        assert observed.mode == "read_only"


def test_observe_runtime_classifies_root_capability_nonzero_before_layout() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        calls: list[list[str]] = []

        def fake_read(serial, args, timeout=30):
            calls.append(list(args))
            return SimpleNamespace(returncode=7, stdout="ignored", stderr="ignored")

        original = phone_target._read
        phone_target._read = fake_read
        try:
            try:
                phone_target.observe_runtime(
                    serial="serial", release_root=release, release_id="v0.1.6", required_paths=required
                )
            except phone_target.PhoneTargetUnavailable as error:
                assert str(error) == "rooted runtime capability unavailable"
            else:
                raise AssertionError("root capability failure was not classified")
        finally:
            phone_target._read = original
        assert calls == [["shell", "su", "0", "sh", "-c", "true"]]


def test_observe_runtime_classifies_layout_probe_nonzero_after_root_success() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        calls: list[list[str]] = []

        def fake_read(serial, args, timeout=30):
            calls.append(list(args))
            if len(calls) == 1:
                return SimpleNamespace(returncode=0, stdout="")
            return SimpleNamespace(returncode=9, stdout="ignored", stderr="ignored")

        original = phone_target._read
        phone_target._read = fake_read
        try:
            try:
                phone_target.observe_runtime(
                    serial="serial", release_root=release, release_id="v0.1.6", required_paths=required
                )
            except phone_target.PhoneTargetUnavailable as error:
                assert str(error) == "rooted runtime layout observation failed"
            else:
                raise AssertionError("layout probe failure was not classified")
        finally:
            phone_target._read = original
        assert calls[0] == ["shell", "su", "0", "sh", "-c", "true"]
        assert calls[1][:5] == ["shell", "su", "0", "sh", "-c"]
        assert len(calls) == 2


def test_observe_runtime_classifies_malformed_layout_without_exposing_output() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        calls = 0

        def fake_read(serial, args, timeout=30):
            nonlocal calls
            calls += 1
            if calls == 1:
                return SimpleNamespace(returncode=0, stdout="")
            return SimpleNamespace(returncode=0, stdout="arbitrary-root-output\n")

        original = phone_target._read
        phone_target._read = fake_read
        try:
            try:
                phone_target.observe_runtime(
                    serial="serial", release_root=release, release_id="v0.1.6", required_paths=required
                )
            except phone_target.PhoneTargetUnavailable as error:
                assert str(error) == "rooted runtime state observation is malformed"
                assert "arbitrary-root-output" not in str(error)
            else:
                raise AssertionError("malformed layout was not classified")
        finally:
            phone_target._read = original
        assert calls == 2


def test_observe_runtime_requires_exact_current_and_all_bytes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        target = "/data/adb/mobile-proxy-node/releases/v0.1.6"

        def fake_read(serial, args, timeout=30):
            if args[:4] == ["shell", "su", "0", "sh"]:
                return SimpleNamespace(returncode=0, stdout=f"target=present\ncurrent={target}\n")
            relative = args[-1].split(target + "/", 1)[1]
            digest = hashlib.sha256((release / relative).read_bytes()).hexdigest()
            return SimpleNamespace(returncode=0, stdout=f"{digest}  file\n")

        original = phone_target._read
        phone_target._read = fake_read
        try:
            observed = phone_target.observe_runtime(serial="serial", release_root=release, release_id="v0.1.6", required_paths=required)
        finally:
            phone_target._read = original
        assert observed.desired is True
        assert observed.exact_files_verified is True
        assert observed.admissible_for_new_dispatch is True


def test_observe_runtime_rejects_existing_noncurrent_target_for_new_dispatch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))

        def fake_read(serial, args, timeout=30):
            return SimpleNamespace(returncode=0, stdout="target=present\ncurrent=/data/adb/mobile-proxy-node/releases/v0.1.5\n")

        original = phone_target._read
        phone_target._read = fake_read
        try:
            observed = phone_target.observe_runtime(serial="serial", release_root=release, release_id="v0.1.6", required_paths=required)
        finally:
            phone_target._read = original
        assert observed.desired is False
        assert observed.admissible_for_new_dispatch is False


def test_observe_runtime_rejects_unmanaged_current_even_when_target_absent() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))

        def fake_read(serial, args, timeout=30):
            return SimpleNamespace(returncode=0, stdout="target=absent\ncurrent=/data/local/tmp/foreign-runtime\n")

        original = phone_target._read
        phone_target._read = fake_read
        try:
            observed = phone_target.observe_runtime(serial="serial", release_root=release, release_id="v0.1.6", required_paths=required)
        finally:
            phone_target._read = original
        assert observed.desired is False
        assert observed.admissible_for_new_dispatch is False


def test_composite_dispatch_calls_apk_then_runtime_once() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        apk = Path(raw) / "app.apk"
        apk.write_bytes(b"apk")
        calls: list[str] = []
        originals = (phone_target._stage_runtime, phone_target.dispatch_install_once, phone_target._materialize_inactive, phone_target._activate)

        def install(**kwargs):
            calls.append("apk")
            return DispatchResult(True, False, None)

        def stage(**kwargs):
            calls.append("stage")
            return "/stage", tuple()

        def materialize(**kwargs):
            calls.append("runtime")
            return "/data/adb/mobile-proxy-node/releases/v0.1.6"

        phone_target.dispatch_install_once = install
        phone_target._stage_runtime = stage
        phone_target._materialize_inactive = materialize
        phone_target._activate = lambda **kwargs: calls.append("activate")
        try:
            result = phone_target.dispatch_release_once(
                serial="serial", apk=apk, release_root=release, release_id="v0.1.6",
                required_paths=required, install_apk=True, install_runtime=True,
            )
        finally:
            (phone_target._stage_runtime, phone_target.dispatch_install_once, phone_target._materialize_inactive, phone_target._activate) = originals
        assert result.confirmed is True and result.outcome_unknown is False
        assert calls == ["apk", "stage", "runtime", "activate"]


def test_apk_unknown_stops_before_any_rooted_runtime_mutation() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        apk = Path(raw) / "app.apk"
        apk.write_bytes(b"apk")
        mutated: list[str] = []
        originals = (phone_target._stage_runtime, phone_target.dispatch_install_once, phone_target._materialize_inactive)
        phone_target.dispatch_install_once = lambda **kwargs: DispatchResult(False, True, "ADB_UNKNOWN")
        phone_target._stage_runtime = lambda **kwargs: mutated.append("stage")
        phone_target._materialize_inactive = lambda **kwargs: mutated.append("runtime")
        try:
            result = phone_target.dispatch_release_once(
                serial="serial", apk=apk, release_root=release, release_id="v0.1.6",
                required_paths=required, install_apk=True, install_runtime=True,
            )
        finally:
            (phone_target._stage_runtime, phone_target.dispatch_install_once, phone_target._materialize_inactive) = originals
        assert result.outcome_unknown is True
        assert mutated == []


def test_composite_dispatch_can_skip_already_desired_apk_domain() -> None:
    with tempfile.TemporaryDirectory() as raw:
        release, required = runtime_release(Path(raw))
        apk = Path(raw) / "app.apk"
        apk.write_bytes(b"apk")
        calls: list[str] = []
        originals = (phone_target._stage_runtime, phone_target.dispatch_install_once, phone_target._materialize_inactive, phone_target._activate)
        phone_target.dispatch_install_once = lambda **kwargs: calls.append("apk")
        phone_target._stage_runtime = lambda **kwargs: (calls.append("stage") or ("/stage", tuple()))
        phone_target._materialize_inactive = lambda **kwargs: (calls.append("runtime") or "/data/adb/mobile-proxy-node/releases/v0.1.6")
        phone_target._activate = lambda **kwargs: calls.append("activate")
        try:
            result = phone_target.dispatch_release_once(
                serial="serial", apk=apk, release_root=release, release_id="v0.1.6",
                required_paths=required, install_apk=False, install_runtime=True,
            )
        finally:
            (phone_target._stage_runtime, phone_target.dispatch_install_once, phone_target._materialize_inactive, phone_target._activate) = originals
        assert result.confirmed is True and result.outcome_unknown is False
        assert calls == ["stage", "runtime", "activate"]


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
    print(f"PHONE_TARGET_TESTS_OK count={len(tests)}")


if __name__ == "__main__":
    main()
