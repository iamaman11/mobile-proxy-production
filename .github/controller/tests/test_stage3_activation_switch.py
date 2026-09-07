from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

fake_android = ModuleType("android_target")


class DispatchResult:
    def __init__(self, confirmed: bool, outcome_unknown: bool, error_class: str | None):
        self.confirmed = confirmed
        self.outcome_unknown = outcome_unknown
        self.error_class = error_class


fake_android.DispatchResult = DispatchResult
fake_android.dispatch_install_once = lambda **kwargs: DispatchResult(True, False, None)
sys.modules["android_target"] = fake_android

spec = importlib.util.spec_from_file_location(
    "phone_target_stage3_activation_test",
    Path(__file__).resolve().parents[1] / "phone_target.py",
)
phone_target = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = phone_target
spec.loader.exec_module(phone_target)


def _result(*, status: str = "completed", returncode: int | None = 0):
    return phone_target.RootScriptResult(
        status=status,
        returncode=returncode,
        stdout=b"",
        stderr=b"",
    )


def _call_with(result):
    calls: list[tuple[bytes, int]] = []

    def fake_root_script(serial: str, script: bytes, timeout: int = 30):
        assert serial == "registered-serial"
        calls.append((script, timeout))
        return result

    original = phone_target._run_root_script
    phone_target._run_root_script = fake_root_script
    try:
        phone_target._activate(
            serial="registered-serial",
            release_id="v0.1.7",
            target="/data/adb/mobile-proxy-node/releases/v0.1.7",
        )
    finally:
        phone_target._run_root_script = original
    return calls


def test_activation_uses_android11_compatible_symlink_cutover_and_asserts_it() -> None:
    calls = _call_with(_result())
    assert len(calls) == 1
    script, timeout = calls[0]
    assert timeout == 150
    switch = b'ln -sfn "$TARGET" "$ROOT/current"'
    verify = b'[ "$(readlink "$ROOT/current")" = "$TARGET" ]'
    assert switch in script
    assert verify in script
    assert b"mv -T" not in script
    assert b"CURRENT_TMP" not in script
    assert script.index(switch) < script.index(verify)
    assert script.index(verify) < script.index(b'cat > "$BOOT"')
    assert script.index(verify) < script.rindex(b'sh "$ROOT/current/service.sh"')


def test_completed_nonzero_is_known_activation_failure_not_transport_unknown() -> None:
    original = phone_target._run_root_script
    phone_target._run_root_script = lambda *args, **kwargs: _result(returncode=17)
    try:
        try:
            phone_target._activate(
                serial="registered-serial",
                release_id="v0.1.7",
                target="/data/adb/mobile-proxy-node/releases/v0.1.7",
            )
        except phone_target.PhoneTargetMutationOutcomeUnknown as exc:
            raise AssertionError("completed nonzero must not be mutation-outcome UNKNOWN") from exc
        except phone_target.PhoneTargetUnavailable:
            pass
        else:
            raise AssertionError("completed nonzero activation must fail")
    finally:
        phone_target._run_root_script = original


def test_transport_timeout_remains_mutation_outcome_unknown() -> None:
    original = phone_target._run_root_script
    phone_target._run_root_script = lambda *args, **kwargs: _result(status="timeout", returncode=None)
    try:
        try:
            phone_target._activate(
                serial="registered-serial",
                release_id="v0.1.7",
                target="/data/adb/mobile-proxy-node/releases/v0.1.7",
            )
        except phone_target.PhoneTargetMutationOutcomeUnknown:
            pass
        else:
            raise AssertionError("transport timeout must remain mutation-outcome UNKNOWN")
    finally:
        phone_target._run_root_script = original


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
    print(f"STAGE3_ACTIVATION_SWITCH_TESTS_OK count={len(tests)}")


if __name__ == "__main__":
    main()
