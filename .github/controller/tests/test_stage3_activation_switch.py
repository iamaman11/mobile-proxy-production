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


def test_activation_treats_existing_current_symlink_as_file_destination() -> None:
    calls: list[tuple[bytes, int]] = []

    def fake_root_script(serial: str, script: bytes, timeout: int = 30):
        assert serial == "registered-serial"
        calls.append((script, timeout))
        return phone_target.RootScriptResult(
            status="completed",
            returncode=0,
            stdout=b"",
            stderr=b"",
        )

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

    assert len(calls) == 1
    script, timeout = calls[0]
    assert timeout == 150
    safe_switch = b'mv -Tf "$CURRENT_TMP" "$ROOT/current"'
    unsafe_switch = b'mv -f "$CURRENT_TMP" "$ROOT/current"'
    assert safe_switch in script
    assert unsafe_switch not in script
    assert script.index(b'ln -s "$TARGET" "$CURRENT_TMP"') < script.index(safe_switch)
    assert script.index(safe_switch) < script.index(b'cat > "$BOOT"')
    assert script.index(safe_switch) < script.rindex(b'sh "$ROOT/current/service.sh"')


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda item: item.__name__):
        test()
    print(f"STAGE3_ACTIVATION_SWITCH_TESTS_OK count={len(tests)}")


if __name__ == "__main__":
    main()
