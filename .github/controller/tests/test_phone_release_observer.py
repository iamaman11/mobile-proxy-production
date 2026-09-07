from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"


def load_observer():
    spec = importlib.util.spec_from_file_location(
        "observe_phone_release_acceptance",
        SCRIPTS / "observe_phone_release.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bounded_runtime_summary_never_records_current_path() -> None:
    module = load_observer()
    expected = "/data/adb/mobile-proxy-node/releases/v0.1.7"
    runtime = SimpleNamespace(
        target_release=expected,
        target_release_exists=True,
        current_target=expected,
        exact_files_verified=True,
        required_file_count=7,
        desired=True,
        admissible_for_new_dispatch=True,
        mode="read_only",
    )
    value = module._bounded_runtime(runtime)
    assert value["current_state"] == "expected_release"
    assert value["current_matches_expected_release"] is True
    assert value["raw_current_target_path_recorded"] is False
    assert expected not in repr(value)

    runtime.current_target = "/data/adb/mobile-proxy-node/releases/v0.1.6"
    value = module._bounded_runtime(runtime)
    assert value["current_state"] == "other_managed_release"
    assert value["current_matches_expected_release"] is False
    assert "v0.1.6" not in repr(value)


def test_expected_materialization_suppresses_secret_binding_ids_and_rendered_paths() -> None:
    module = load_observer()
    value = module._bounded_materialization(
        {
            "exact_release_runtime": True,
            "artifact_name": "runtime.tar.gz",
            "product_content_digest": "b3:" + "a" * 64,
            "transport_sha256": "b" * 64,
            "component_inventory_digest": "b3:" + "c" * 64,
            "component_count": 6,
            "required_live_file_count": 7,
            "derived_files_rendered": ["config/host-daemon.json", "config/sing-box.json"],
            "renderer_source_sha": "d" * 40,
            "runtime_manifest_sha256": "e" * 64,
            "secret_binding_ids": {
                "SECRET_A": "hmac-sha256:" + "f" * 64,
                "SECRET_B": "hmac-sha256:" + "1" * 64,
            },
        }
    )
    assert value["derived_file_count"] == 2
    assert value["secret_binding_count"] == 2
    assert value["secret_binding_ids_recorded"] is False
    assert value["secret_values_recorded"] is False
    assert value["raw_rendered_config_recorded"] is False
    assert "SECRET_A" not in repr(value)
    assert "host-daemon.json" not in repr(value)


def test_observer_has_no_destructive_callsite() -> None:
    source = (SCRIPTS / "observe_phone_release.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {
        "dispatch_release_once",
        "dispatch_install_once",
        "_stage_runtime",
        "_materialize_inactive",
        "_activate",
    }
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (calls & forbidden), calls & forbidden
    for token in (
        '"phone_mutation_performed": False',
        '"provider_mutation_performed": False',
        '"raw_device_identifier_recorded": False',
        '"raw_current_target_path_recorded": False',
        '"raw_config_recorded": False',
        '"secret_values_recorded": False',
    ):
        assert token in source


def test_workflow_is_exact_issue1_read_only_observation_under_target_lock() -> None:
    source = (WORKFLOWS / "phone-release-observation.yml").read_text(encoding="utf-8")
    required = (
        "workflow_call:",
        "command:",
        "source_issue_number:",
        "source_comment_id:",
        "source_actor:",
        "source_comment_url:",
        "command != '/observe-phone-release phone-production v0.1.7'",
        "SOURCE_ISSUE_NUMBER'] != '1'",
        "SOURCE_ACTOR'] != os.environ['EXPECTED_OWNER']",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-target-phone-production",
        "cancel-in-progress: false",
        "environment: phone-production",
        ".github/scripts/observe_phone_release.py",
        "--release-tag \"$RELEASE_TAG\"",
        "STAGE4_PHONE_RELEASE_BASELINE_HEALTHY_EXACT",
        "rust_toolchain=1.95.0",
        "user_local_tooling=true",
        "system_path_mutation=false",
    )
    missing = [token for token in required if token not in source]
    assert not missing, missing
    forbidden = (
        "workflow_dispatch:",
        "issue_comment:",
        "/deploy ",
        "/retry-deploy ",
        "dispatch_release_once",
        "dispatch_install_once",
        "adb shell rm",
        "adb install",
    )
    present = [token for token in forbidden if token in source]
    assert not present, present
    assert "provider_access=false" in source
    assert "provider_mutation_performed" in source


def test_only_production_router_references_phone_observer_workflow() -> None:
    callers = []
    for path in WORKFLOWS.glob("*.yml"):
        if path.name == "phone-release-observation.yml":
            continue
        if "./.github/workflows/phone-release-observation.yml" in path.read_text(encoding="utf-8"):
            callers.append(path.name)
    assert callers == ["production-control-router.yml"], callers
    router = (WORKFLOWS / "production-control-router.yml").read_text(encoding="utf-8")
    for required in (
        "needs.route.outputs.handler == 'workflow_call'",
        "needs.route.outputs.route_id == 'observe-phone-release'",
        "needs.route.outputs.operation_class == 'OBSERVE'",
        "needs.route.outputs.read_only == 'true'",
        "needs.route.outputs.destructive == 'false'",
        "command: ${{ github.event.comment.body }}",
        "source_comment_id: ${{ github.event.comment.id }}",
        "secrets: inherit",
    ):
        assert required in router


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"PHONE_RELEASE_OBSERVER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
