from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
import tempfile
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


def test_bounded_runtime_file_drift_records_only_index_class_and_status() -> None:
    module = load_observer()
    required = (
        "bin/host-daemon",
        "bin/runtime-supervisor",
        "bin/sing-box",
        "config/host-daemon.json",
        "config/sing-box.json",
        "module.prop",
        "service.sh",
    )
    statuses = (
        "exact",
        "digest_mismatch",
        "missing",
        "wrong_type",
        "unreadable",
        "exact",
        "exact",
    )
    value = module._bounded_runtime_file_drift(
        statuses,
        required_paths=required,
        rendered_paths=["config/host-daemon.json", "config/sing-box.json"],
    )
    assert value["required_file_count"] == 7
    assert value["exact_file_count"] == 3
    assert value["drift_file_count"] == 4
    assert value["files"] == [
        {"required_file_index": 0, "source_class": "static_release", "status": "exact"},
        {"required_file_index": 1, "source_class": "static_release", "status": "digest_mismatch"},
        {"required_file_index": 2, "source_class": "static_release", "status": "missing"},
        {"required_file_index": 3, "source_class": "sensitive_derived", "status": "wrong_type"},
        {"required_file_index": 4, "source_class": "sensitive_derived", "status": "unreadable"},
        {"required_file_index": 5, "source_class": "static_release", "status": "exact"},
        {"required_file_index": 6, "source_class": "static_release", "status": "exact"},
    ]
    assert value["raw_release_paths_recorded"] is False
    assert value["expected_file_digests_recorded"] is False
    assert value["observed_file_digests_recorded"] is False
    assert value["secret_derived_identifiers_recorded"] is False
    rendered = repr(value)
    for path in required:
        assert path not in rendered
    assert not any(len(token) == 64 for token in rendered.replace("'", " ").replace('"', " ").split())


def test_runtime_required_file_probe_is_complete_bounded_and_read_only() -> None:
    module = load_observer()
    phone_target = sys.modules["phone_target"]
    with tempfile.TemporaryDirectory() as raw:
        release = Path(raw) / "release"
        bodies = {
            "bin/a": b"a\n",
            "config/b": b"b\n",
            "service.sh": b"service\n",
        }
        for relative, body in bodies.items():
            target = release / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        required = tuple(bodies)
        captured: list[bytes] = []

        def fake_root_script(serial, script, timeout=30):
            captured.append(script)
            return phone_target.RootScriptResult(
                status="completed",
                returncode=0,
                stdout=b"0=exact\n1=digest_mismatch\n2=missing\n",
                stderr=b"",
            )

        originals = (phone_target._probe_root_capability, phone_target._run_root_script)
        phone_target._probe_root_capability = lambda serial: None
        phone_target._run_root_script = fake_root_script
        try:
            observed = module.observe_runtime_required_file_statuses(
                serial="serial",
                release_root=release,
                release_id="v0.1.7",
                required_paths=required,
            )
        finally:
            phone_target._probe_root_capability, phone_target._run_root_script = originals

    assert observed == ("exact", "digest_mismatch", "missing")
    assert len(captured) == 1
    script = captured[0]
    for index, relative in enumerate(required):
        expected = hashlib.sha256(bodies[relative]).hexdigest().encode()
        assert f"check_file {index} ".encode() in script
        assert relative.encode() in script
        assert expected in script
    for forbidden in (
        b"rm -rf",
        b"adb push",
        b"ln -s",
        b"ln -sfn",
        b"chmod",
        b"kill -TERM",
        b"service.sh\"",
    ):
        assert forbidden not in script


def test_runtime_required_file_probe_rejects_malformed_output_without_leaking_it() -> None:
    module = load_observer()
    phone_target = sys.modules["phone_target"]
    with tempfile.TemporaryDirectory() as raw:
        release = Path(raw) / "release"
        target = release / "service.sh"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"service\n")
        originals = (phone_target._probe_root_capability, phone_target._run_root_script)
        phone_target._probe_root_capability = lambda serial: None
        phone_target._run_root_script = lambda *args, **kwargs: phone_target.RootScriptResult(
            status="completed",
            returncode=0,
            stdout=b"raw/device/path=secret-derived-output\n",
            stderr=b"",
        )
        try:
            try:
                module.observe_runtime_required_file_statuses(
                    serial="serial",
                    release_root=release,
                    release_id="v0.1.7",
                    required_paths=("service.sh",),
                )
            except phone_target.PhoneTargetUnavailable as error:
                assert str(error) == "rooted runtime required-file observation is malformed"
                assert "raw/device/path" not in str(error)
                assert "secret-derived-output" not in str(error)
            else:
                raise AssertionError("malformed required-file output was accepted")
        finally:
            phone_target._probe_root_capability, phone_target._run_root_script = originals


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
        '"raw_runtime_release_paths_recorded": False',
        '"expected_file_digests_recorded": False',
        '"observed_file_digests_recorded": False',
        '"credential_derived_identifiers_recorded": False',
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
        lines = path.read_text(encoding="utf-8").splitlines()
        if any(line.strip() == "uses: ./.github/workflows/phone-release-observation.yml" for line in lines):
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
