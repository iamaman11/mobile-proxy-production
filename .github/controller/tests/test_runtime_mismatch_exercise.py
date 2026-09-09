from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"
CONTROLLER = ROOT / "controller"


def load_module():
    sys.path.insert(0, str(CONTROLLER))
    spec = importlib.util.spec_from_file_location(
        "exercise_runtime_mismatch",
        SCRIPTS / "exercise_runtime_mismatch.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_fixed_scripts_only_switch_current_between_equivalent_v017_paths() -> None:
    module = load_module()
    inject = module._transition_script(restore=False).decode("utf-8")
    restore = module._transition_script(restore=True).decode("utf-8")
    canonical = "/data/adb/mobile-proxy-node/releases/v0.1.7"
    equivalent = "/data/adb/mobile-proxy-node/releases/../releases/v0.1.7"
    current = "/data/adb/mobile-proxy-node/current"

    assert canonical in inject and canonical in restore
    assert equivalent in inject and equivalent in restore
    assert current in inject and current in restore
    assert inject.count('ln -sfn "$PROBE" "$CURRENT"') == 1
    assert restore.count('ln -sfn "$TARGET" "$CURRENT"') == 1
    assert 'readlink -f "$PROBE"' in inject
    assert 'readlink -f "$CURRENT"' in inject
    assert 'readlink -f "$CURRENT"' in restore
    for forbidden in (
        "kill ",
        "setprop sys.powerctl",
        "service.sh",
        "adb install",
        "adb push",
        "rm -rf",
        "cp ",
        "mv ",
    ):
        assert forbidden not in inject
        assert forbidden not in restore


def test_transition_protocol_is_typed_and_fail_closed(monkeypatch) -> None:
    module = load_module()

    def result(*, status="completed", returncode=0, stdout=b"", stderr=b""):
        return SimpleNamespace(
            status=status,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    inject_ok = (
        b"stage4_mismatch=inject_dispatched\n"
        b"stage4_mismatch=noncanonical_current_verified\n"
    )
    restore_ok = (
        b"stage4_mismatch=restore_dispatched\n"
        b"stage4_mismatch=canonical_current_restored\n"
    )

    monkeypatch.setattr(module.phone_target, "_run_root_script", lambda *a, **k: result(stdout=inject_ok))
    injected = module._run_transition("serial", restore=False)
    assert injected.outcome == "INJECTED"
    assert injected.failure_code is None
    assert injected.dispatched is True and injected.verified is True

    monkeypatch.setattr(module.phone_target, "_run_root_script", lambda *a, **k: result(stdout=restore_ok))
    restored = module._run_transition("serial", restore=True)
    assert restored.outcome == "RESTORED"
    assert restored.failure_code is None
    assert restored.dispatched is True and restored.verified is True

    monkeypatch.setattr(
        module.phone_target,
        "_run_root_script",
        lambda *a, **k: result(returncode=40, stdout=b"stage4_mismatch=inject_precondition_refused\n"),
    )
    refused = module._run_transition("serial", restore=False)
    assert refused.outcome == "REFUSED"
    assert refused.dispatched is False and refused.verified is False

    monkeypatch.setattr(
        module.phone_target,
        "_run_root_script",
        lambda *a, **k: result(status="timeout", returncode=None),
    )
    unknown = module._run_transition("serial", restore=False)
    assert unknown.outcome == "UNKNOWN"
    assert unknown.dispatched is None and unknown.verified is None

    monkeypatch.setattr(
        module.phone_target,
        "_run_root_script",
        lambda *a, **k: result(returncode=41, stdout=b"stage4_mismatch=inject_dispatched\n"),
    )
    ambiguous = module._run_transition("serial", restore=False)
    assert ambiguous.outcome == "UNKNOWN"
    assert ambiguous.dispatched is True and ambiguous.verified is None

    monkeypatch.setattr(
        module.phone_target,
        "_run_root_script",
        lambda *a, **k: result(stdout=inject_ok, stderr=b"unexpected"),
    )
    protocol = module._run_transition("serial", restore=False)
    assert protocol.outcome == "UNKNOWN"
    assert protocol.failure_code == "LINK_TRANSITION_PROTOCOL_MISMATCH"


def test_mismatch_detection_requires_real_degraded_release_observation() -> None:
    module = load_module()
    canonical = "/data/adb/mobile-proxy-node/releases/v0.1.7"
    equivalent = "/data/adb/mobile-proxy-node/releases/../releases/v0.1.7"

    positive = SimpleNamespace(
        classification="DEGRADED",
        apk=SimpleNamespace(desired=True),
        runtime=SimpleNamespace(
            desired=False,
            target_release_exists=True,
            target_release=canonical,
            current_target=equivalent,
            exact_files_verified=False,
        ),
    )
    observed = module._mismatch_condition(positive)
    assert observed == {
        "classification": "DEGRADED",
        "apk_exact": True,
        "runtime_desired": False,
        "target_release_exists": True,
        "current_matches_canonical_target": False,
        "exact_files_verified": False,
        "detected": True,
    }

    healthy = SimpleNamespace(
        classification="HEALTHY_EXACT",
        apk=SimpleNamespace(desired=True),
        runtime=SimpleNamespace(
            desired=True,
            target_release_exists=True,
            target_release=canonical,
            current_target=canonical,
            exact_files_verified=True,
        ),
    )
    assert module._mismatch_condition(healthy)["detected"] is False

    unknown = SimpleNamespace(classification="UNKNOWN", apk=None, runtime=None)
    safe = module._mismatch_condition(unknown)
    assert safe["classification"] == "UNKNOWN"
    assert safe["detected"] is False
    assert safe["current_matches_canonical_target"] is None


def test_adapter_has_only_two_fixed_root_transition_calls_and_no_generic_mutator() -> None:
    source = (SCRIPTS / "exercise_runtime_mismatch.py").read_text(encoding="utf-8")
    assert source.count("_run_transition(serial, restore=False)") == 1
    assert source.count("_run_transition(serial, restore=True)") == 1
    assert source.count("phone_target._run_root_script(") == 1
    assert "_ROOT_SCRIPT_TIMEOUT_SECONDS = 15" in source
    assert "current-symlink-noncanonical-equivalent-once" in source
    assert "same_release_bytes" in source
    assert "MISMATCH_DETECTED_AND_RESTORED" in source
    for forbidden in (
        "--path",
        "--selector",
        "--process",
        "--signal",
        "--pid",
        "subprocess.run",
        "subprocess.Popen",
        "os.system",
        "shell=True",
        "dispatch_release_once",
        "dispatch_install_once",
    ):
        assert forbidden not in source


def test_workflow_is_single_ingress_reusable_bounded_and_preserves_evidence() -> None:
    source = (WORKFLOWS / "stage4-runtime-mismatch-exercise.yml").read_text(encoding="utf-8")
    required = (
        "workflow_call:",
        "command != '/exercise-runtime-mismatch phone-production v0.1.7'",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-target-phone-production",
        "cancel-in-progress: false",
        "environment: phone-production",
        ".github/scripts/exercise_runtime_mismatch.py",
        "stage4-runtime-mismatch-exercise.v1",
        "MISMATCH_DETECTED_AND_RESTORED",
        "stage4-runtime-mismatch-exercise-${{ github.run_id }}-${{ github.run_attempt }}",
        "retention-days: 90",
        "if-no-files-found: error",
    )
    missing = [item for item in required if item not in source]
    assert not missing, missing
    for forbidden in (
        "workflow_dispatch:",
        "issue_comment:",
        "run_phone_release_deployment.py",
        "dispatch_release_once",
        "dispatch_install_once",
        "systemctl restart",
        "service.sh",
        "adb install",
        "adb push",
        "/deploy ",
        "/retry-deploy ",
    ):
        assert forbidden not in source
