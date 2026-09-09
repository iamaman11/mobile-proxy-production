from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"
PRODUCTION = ROOT / "production"

if str(CONTROLLER) not in sys.path:
    sys.path.insert(0, str(CONTROLLER))


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "stage4_runtime_mismatch_exercise_test",
        SCRIPTS / "exercise_runtime_mismatch.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _admitted():
    return SimpleNamespace(
        identity=SimpleNamespace(
            tag="v0.1.7",
            immutable=True,
        )
    )


def _exact_snapshot():
    return SimpleNamespace(classification="HEALTHY_EXACT", desired=True)


def test_fixed_shadow_expectation_detects_managed_current_without_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_script()
    sha = "a" * 40
    monkeypatch.setenv("GITHUB_SHA", sha)
    monkeypatch.setenv("ANDROID_PRODUCTION_SERIAL", "registered-phone-secret")
    monkeypatch.setenv("ANDROID_TARGET_BINDING_KEY", "b" * 32)
    monkeypatch.setattr(module, "resolve_release", lambda **kwargs: _admitted())
    materialized = SimpleNamespace(
        release_root=tmp_path,
        required_live_release_paths=("bin/runtime-supervisor",),
    )
    monkeypatch.setattr(
        module,
        "prepare_verified_release_runtime",
        lambda *args, **kwargs: materialized,
    )
    exact_calls = []

    def exact(**kwargs):
        exact_calls.append(kwargs)
        return _exact_snapshot()

    monkeypatch.setattr(module, "observe_exact_phone_release", exact)
    shadow_calls = []

    def shadow(**kwargs):
        shadow_calls.append(kwargs)
        assert kwargs["release_id"] == "v9999.9999.9999"
        return SimpleNamespace(
            target_release="/data/adb/mobile-proxy-node/releases/v9999.9999.9999",
            target_release_exists=False,
            current_target="/data/adb/mobile-proxy-node/releases/v0.1.7",
            exact_files_verified=False,
            desired=False,
            admissible_for_new_dispatch=True,
        )

    monkeypatch.setattr(module, "observe_runtime", shadow)
    output = tmp_path / "evidence.json"
    payload, code = module.exercise(
        target="phone-production",
        release_tag="v0.1.7",
        controller_revision=sha,
        product_root=tmp_path,
        runtime_manifest=tmp_path / "runtime.json",
        output=output,
    )

    assert code == 0
    assert payload["classification"] == "MISMATCH_DETECTED"
    assert payload["failure_code"] is None
    assert payload["baseline"] == {"classification": "HEALTHY_EXACT", "exact": True}
    assert payload["postcondition"] == {"classification": "HEALTHY_EXACT", "exact": True}
    assert payload["mismatch_detection"]["detected"] is True
    assert payload["mismatch_detection"]["current_state_relative_to_shadow"] == "other_managed_release"
    assert len(exact_calls) == 2
    assert len(shadow_calls) == 1
    safety = payload["safety"]
    assert safety["phone_access_performed"] is True
    for field in (
        "phone_mutation_performed",
        "runtime_mutation_performed",
        "current_link_mutation_performed",
        "deployment_created",
        "deployment_intent_created",
        "recovery_dispatched",
        "reconciliation_dispatched",
        "provider_access_performed",
        "vm_access_performed",
        "runner_proxy_mutation_performed",
        "raw_device_identifier_recorded",
        "raw_current_target_path_recorded",
        "raw_runtime_release_paths_recorded",
        "raw_config_recorded",
        "secret_values_recorded",
    ):
        assert safety[field] is False
    text = output.read_text(encoding="utf-8")
    assert "registered-phone-secret" not in text
    assert "b" * 32 not in text


def test_shadow_release_collision_refuses_without_changing_phone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_script()
    sha = "c" * 40
    monkeypatch.setenv("GITHUB_SHA", sha)
    monkeypatch.setenv("ANDROID_PRODUCTION_SERIAL", "registered-phone")
    monkeypatch.setenv("ANDROID_TARGET_BINDING_KEY", "d" * 32)
    monkeypatch.setattr(module, "resolve_release", lambda **kwargs: _admitted())
    monkeypatch.setattr(
        module,
        "prepare_verified_release_runtime",
        lambda *args, **kwargs: SimpleNamespace(
            release_root=tmp_path,
            required_live_release_paths=("bin/runtime-supervisor",),
        ),
    )
    monkeypatch.setattr(module, "observe_exact_phone_release", lambda **kwargs: _exact_snapshot())
    monkeypatch.setattr(
        module,
        "observe_runtime",
        lambda **kwargs: SimpleNamespace(
            target_release="/data/adb/mobile-proxy-node/releases/v9999.9999.9999",
            target_release_exists=True,
            current_target="/data/adb/mobile-proxy-node/releases/v0.1.7",
            exact_files_verified=False,
            desired=False,
            admissible_for_new_dispatch=True,
        ),
    )
    payload, code = module.exercise(
        target="phone-production",
        release_tag="v0.1.7",
        controller_revision=sha,
        product_root=tmp_path,
        runtime_manifest=tmp_path / "runtime.json",
        output=tmp_path / "evidence.json",
    )
    assert code == 2
    assert payload["classification"] == "REFUSED"
    assert payload["failure_code"] == "SHADOW_RELEASE_COLLISION"
    assert payload["safety"]["phone_mutation_performed"] is False


def test_runtime_mismatch_route_is_read_only_and_declarative() -> None:
    registry = json.loads((PRODUCTION / "command-control-registry.json").read_text(encoding="utf-8"))
    targets = json.loads((PRODUCTION / "targets.json").read_text(encoding="utf-8"))
    routes = [item for item in registry["routes"] if item["id"] == "exercise-runtime-mismatch"]
    assert len(routes) == 1
    route = routes[0]
    assert route["handler"] == "dispatch_workflow"
    assert route["operation_class"] == "DIAGNOSTIC"
    assert route["read_only"] is True
    assert route["destructive"] is False
    assert route["physical_domains"] == []
    assert route["requires_phone"] is True
    assert route["requires_vm"] is False
    assert route["ref"] == "main"
    assert route["ref_policy"] == "controller-main-exact"
    assert route["dispatch_inputs"] == {"release_tag": "release", "target": "target"}
    assert "exercise-runtime-mismatch" in targets["targets"]["phone-production"]["allowed_operations"]

    workflow = (WORKFLOWS / "stage4-runtime-mismatch-exercise.yml").read_text(encoding="utf-8")
    script = (SCRIPTS / "exercise_runtime_mismatch.py").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "Exercise managed-current mismatch detection read-only" in workflow
    assert "phone_mutation_performed" in workflow
    assert "MISMATCH_DETECTED" in workflow
    assert "_SHADOW_EXPECTED_RELEASE = \"v9999.9999.9999\"" in script
    assert "observe_runtime(" in script
    assert "_run_root_script" not in script
    assert "ln -s" not in script
    assert "dispatch_release_once" not in script
