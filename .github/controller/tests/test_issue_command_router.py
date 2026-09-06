from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
PRODUCTION = ROOT / "production"
WORKFLOWS = ROOT / "workflows"
CONTROLLER = ROOT / "controller"
SHA = "832c8b010efee97a6f5c9c587b766acbe65dd453"


def _load_module(name: str, path: Path):
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_router():
    return _load_module("issue_command_router", SCRIPTS / "issue_command_router.py")


def load_dispatcher():
    load_router()
    return _load_module("dispatch_allowlisted_workflow", SCRIPTS / "dispatch_allowlisted_workflow.py")


def accepted(command: str = "/observe-public-deployment-projection", **overrides):
    router = load_router()
    facts = {
        "repository": "iamaman11/mobile-proxy-production",
        "issue_number": 1,
        "author": "iamaman11",
        "command": command,
        "event_sha": SHA,
        "current_main_sha": SHA,
        "run_attempt": 1,
    }
    facts.update(overrides)
    return router.classify(**facts)


def refused(command: str = "/observe-public-deployment-projection", **overrides) -> None:
    router = load_router()
    try:
        accepted(command, **overrides)
    except router.RouteRefused:
        return
    raise AssertionError(f"route unexpectedly accepted: command={command!r}, overrides={overrides!r}")


def registries():
    router = load_router()
    target_value = json.loads((PRODUCTION / "targets.json").read_text(encoding="utf-8"))
    targets = router.validate_target_registry(target_value)
    command_value = json.loads((PRODUCTION / "command-control-registry.json").read_text(encoding="utf-8"))
    routes = router.validate_registry(command_value, targets)
    return command_value, routes, targets


def test_registry_and_target_contracts_are_complete() -> None:
    command_value, routes, targets = registries()
    assert command_value["repository"] == "iamaman11/mobile-proxy-production"
    assert command_value["issue_number"] == 1
    assert command_value["allowed_authors"] == ["iamaman11"]
    assert command_value["extension_rules"]["dispatch_workflow_must_be_read_only"] is True
    assert {item["id"] for item in routes if item["enabled"]} == {
        "observe-public-deployment-projection",
        "verify-product-release",
        "deploy-product-release",
        "runner-android-build-tools-bootstrap",
    }
    assert set(targets) == {"phone-production", "vm-production"}
    assert targets["phone-production"]["active"] is True
    assert targets["phone-production"]["allowed_operations"] == [
        "deploy-product-release",
        "verify-product-release",
    ]
    assert targets["vm-production"]["active"] is False
    assert targets["vm-production"]["allowed_operations"] == ["deploy-product-release"]
    for route in routes:
        assert route["authority_policy"]
        assert route["target_capability_policy"]
        assert isinstance(route["arguments"], list)
        assert isinstance(route["dispatch_inputs"], dict)


def test_observer_route_is_exact_read_only_and_rerun_safe() -> None:
    route = accepted()
    assert route.route_id == "observe-public-deployment-projection"
    assert route.handler == "dispatch_workflow"
    assert route.workflow == ".github/workflows/public-deployment-projection-observer.yml"
    assert route.ref == "main"
    assert route.operation_class == "OBSERVE"
    assert route.read_only is True and route.destructive is False
    assert route.idempotency_policy == "single-run-attempt"
    assert route.arguments_json == "{}"
    refused("/observe-public-deployment-projection extra")
    refused(run_attempt=2)


def test_release_verify_route_is_hosted_read_only_and_bounded() -> None:
    route = accepted("/verify-release phone-production v0.1.7")
    assert route.route_id == "verify-product-release"
    assert route.handler == "dispatch_workflow"
    assert route.workflow == ".github/workflows/product-release-admission-proof.yml"
    assert route.ref == "main"
    assert route.operation == "verify-product-release"
    assert route.operation_class == "RELEASE_VERIFY"
    assert route.target == "phone-production"
    assert route.release_tag == "v0.1.7"
    assert route.read_only is True and route.destructive is False
    assert route.idempotency_policy == "single-run-attempt"
    assert json.loads(route.arguments_json) == {
        "release": "v0.1.7",
        "target": "phone-production",
    }
    refused("/verify-release vm-production v0.1.7")
    refused("/verify-release phone-production 0.1.7")
    refused("/verify-release phone-production v0.1")
    refused("/verify-release phone-production v0.1.7 extra")
    refused("/verify-release phone-production v0.1.7;echo")
    refused("/verify-release phone-production v0.1.7\n/deploy phone-production v0.1.7")
    refused("/verify-release phone-production v0.1.7", run_attempt=2)

    dispatcher = load_dispatcher()
    workflow, ref, inputs = dispatcher.build_dispatch(
        "verify-product-release",
        '{"release":"v0.1.7","target":"phone-production"}',
    )
    assert workflow == "product-release-admission-proof.yml"
    assert ref == "main"
    assert inputs == {"release_tag": "v0.1.7", "target": "phone-production"}

    source = (WORKFLOWS / "product-release-admission-proof.yml").read_text(encoding="utf-8")
    for required in (
        "workflow_dispatch:",
        "runs-on: ubuntu-latest",
        "from release_resolver import resolve_release",
        "resolve_release(tag=release_tag, target=target)",
        "PRODUCT_RELEASE_ADMISSION_ACCEPTED",
        '"phone_access": False',
        '"deployment_created": False',
        '"deployment_intent_created": False',
    ):
        assert required in source
    for forbidden in (
        "environment:",
        "secrets.",
        "self-hosted",
        "android-production",
        "adb ",
        "prepare_release_deployment",
        "finalize_deployment_projection",
        "release-deployment.yml",
        "api.github.com/repos/iamaman11/mobile-proxy-production/deployments",
    ):
        assert forbidden not in source


def test_generic_dispatcher_resolves_only_registry_read_only_routes() -> None:
    dispatcher = load_dispatcher()
    workflow, ref, inputs = dispatcher.build_dispatch("observe-public-deployment-projection", "{}")
    assert workflow == "public-deployment-projection-observer.yml"
    assert ref == "main"
    assert inputs == {}
    try:
        dispatcher.build_dispatch(
            "deploy-product-release",
            '{"release":"v0.1.4","target":"phone-production"}',
        )
    except dispatcher.DispatchRefused:
        pass
    else:
        raise AssertionError("generic dispatcher accepted destructive deployment route")
    try:
        dispatcher.build_dispatch("observe-public-deployment-projection", '{"extra":"x"}')
    except dispatcher.DispatchRefused:
        pass
    else:
        raise AssertionError("generic dispatcher accepted unknown arguments")


def test_deploy_route_preserves_existing_semantic_identity() -> None:
    phone = accepted("/deploy phone-production v0.1.4")
    assert phone.route_id == "deploy-product-release"
    assert phone.handler == "deployment"
    assert phone.workflow == ".github/workflows/release-deployment.yml"
    assert phone.target == "phone-production"
    assert phone.release_tag == "v0.1.4"
    assert phone.destructive is True and phone.read_only is False
    assert phone.semantic_identity_policy == "existing-deployment-request-v2"
    assert "semantic" in phone.idempotency_policy
    assert "UNKNOWN" in phone.recovery_policy and "no-blind-retry" in phone.recovery_policy
    assert json.loads(phone.arguments_json) == {"release": "v0.1.4", "target": "phone-production"}
    vm = accepted("/deploy vm-production v12.34.56")
    assert vm.target == "vm-production" and vm.release_tag == "v12.34.56"


def test_invalid_deploy_inputs_are_refused() -> None:
    for command in (
        "/deploy phone-production 0.1.4",
        "/deploy staging v0.1.4",
        "/deploy phone-production v0.1",
        "/deploy phone-production v0.1.4 extra",
        "/deploy phone-production v0.1.4;echo",
        "/deploy phone-production v0.1.4\n/observe-public-deployment-projection",
    ):
        refused(command)


def test_runner_tooling_route_is_exact_and_bounded() -> None:
    route = accepted(f"/runner-android-build-tools-bootstrap {SHA}")
    assert route.route_id == "runner-android-build-tools-bootstrap"
    assert route.handler == "workflow_call"
    assert route.workflow == ".github/workflows/production-runner-android-build-tools-bootstrap.yml"
    assert route.operation_class == "RUNNER_TOOLING"
    assert route.destructive is False
    assert route.canonical_sha == SHA
    assert json.loads(route.arguments_json) == {"canonical_sha": SHA}
    refused("/runner-android-build-tools-bootstrap main")
    refused(f"/runner-android-build-tools-bootstrap {SHA} extra")


def test_repository_issue_author_sha_and_clean_line_are_fail_closed() -> None:
    refused(repository="iamaman11/mobile-proxy")
    refused(issue_number=2)
    refused(author="someone-else")
    refused(event_sha="not-a-sha")
    refused(current_main_sha="not-a-sha")
    refused(current_main_sha="7939a0d3cc6d4fca676779a9581af5275877029e")
    refused(" /observe-public-deployment-projection")
    refused("/observe-public-deployment-projection ")
    refused("/observe-public-deployment-projection\r")
    refused("/unknown")
    refused("/" + "x" * 1100)


def test_destructive_contract_requires_semantic_idempotency_recovery_and_evidence() -> None:
    router = load_router()
    command_value, _, targets = registries()
    for field, value in (
        ("recovery_policy", "retry"),
        ("idempotency_policy", "run-id"),
        ("concurrency_domain", "none"),
        ("evidence_policy", "green-workflow"),
    ):
        broken = json.loads(json.dumps(command_value))
        deploy = next(item for item in broken["routes"] if item["id"] == "deploy-product-release")
        deploy[field] = value
        try:
            router.validate_registry(broken, targets)
        except router.RouteRefused:
            continue
        raise AssertionError(f"incomplete destructive contract accepted after changing {field}")


def test_dynamic_workflow_ref_or_dispatch_mutation_is_rejected() -> None:
    router = load_router()
    command_value, _, targets = registries()
    mutations = (
        ("workflow", ".github/workflows/${command}.yml"),
        ("ref", "${ref}"),
        ("read_only", False),
        ("destructive", True),
    )
    for field, value in mutations:
        broken = json.loads(json.dumps(command_value))
        observer = next(item for item in broken["routes"] if item["id"] == "observe-public-deployment-projection")
        observer[field] = value
        try:
            router.validate_registry(broken, targets)
        except router.RouteRefused:
            continue
        raise AssertionError(f"unsafe generic dispatch mutation accepted: {field}")


def test_argument_schema_must_exactly_match_regex_captures() -> None:
    router = load_router()
    command_value, _, targets = registries()
    broken = json.loads(json.dumps(command_value))
    deploy = next(item for item in broken["routes"] if item["id"] == "deploy-product-release")
    deploy["arguments"] = [{"name": "target", "type": "target"}]
    try:
        router.validate_registry(broken, targets)
    except router.RouteRefused:
        pass
    else:
        raise AssertionError("route with argument/pattern drift was accepted")

    broken = json.loads(json.dumps(command_value))
    bootstrap = next(item for item in broken["routes"] if item["id"] == "runner-android-build-tools-bootstrap")
    bootstrap["arguments"][0]["type"] = "shell"
    try:
        router.validate_registry(broken, targets)
    except router.RouteRefused:
        return
    raise AssertionError("unsupported argument type was accepted")


def test_read_only_contract_cannot_claim_physical_domain() -> None:
    router = load_router()
    command_value, _, targets = registries()
    broken = json.loads(json.dumps(command_value))
    broken["routes"][0]["physical_domains"] = ["phone"]
    try:
        router.validate_registry(broken, targets)
    except router.RouteRefused:
        return
    raise AssertionError("read-only route with physical domain was accepted")


def test_unknown_target_reference_is_rejected() -> None:
    router = load_router()
    command_value, _, targets = registries()
    broken = json.loads(json.dumps(command_value))
    deploy = next(item for item in broken["routes"] if item["id"] == "deploy-product-release")
    deploy["allowed_targets"].append("unknown-production")
    try:
        router.validate_registry(broken, targets)
    except router.RouteRefused:
        return
    raise AssertionError("route referencing an unknown target was accepted")


def test_exactly_one_issue_comment_ingress_and_generic_safe_dispatch_adapter() -> None:
    ingress = sorted(
        path.name
        for path in WORKFLOWS.glob("*.yml")
        if "  issue_comment:\n" in path.read_text(encoding="utf-8")
    )
    assert ingress == ["production-control-router.yml"], ingress
    workflow = (WORKFLOWS / "production-control-router.yml").read_text(encoding="utf-8")
    required = (
        "issue_comment:\n    types: [created]",
        ".github/scripts/issue_command_router.py",
        ".github/production/command-control-registry.json",
        ".github/production/targets.json",
        ".github/scripts/dispatch_allowlisted_workflow.py",
        "needs.route.outputs.handler == 'dispatch_workflow'",
        "needs.route.outputs.read_only == 'true'",
        "needs.route.outputs.destructive == 'false'",
        "./.github/workflows/release-deployment.yml",
        "./.github/workflows/production-runner-android-build-tools-bootstrap.yml",
        "actions: write",
        "issues: write",
    )
    missing = [token for token in required if token not in workflow]
    assert not missing, missing
    forbidden = (
        "actions/workflows/${{",
        "-f ref=${{",
        "secrets.",
        "adb ",
        "runs-on: [self-hosted",
    )
    present = [token for token in forbidden if token in workflow]
    assert not present, present


def test_dispatch_adapter_accepts_no_issue_workflow_ref_or_shell_values() -> None:
    source = (SCRIPTS / "dispatch_allowlisted_workflow.py").read_text(encoding="utf-8")
    required = (
        'route.get("handler") != "dispatch_workflow"',
        'route.get("read_only") is not True',
        'route.get("destructive") is not False',
        'route.get("ref") != "main"',
        "controller main drifted before workflow dispatch",
        "urllib.parse.quote(workflow_name, safe=\"\")",
    )
    missing = [token for token in required if token not in source]
    assert not missing, missing
    forbidden = ("subprocess", "os.system", "shell=True", "eval(", "exec(", "adb", "self-hosted")
    present = [token for token in forbidden if token in source]
    assert not present, present


def test_deployment_request_identity_remains_cursor_free_and_semantic() -> None:
    sys.path.insert(0, str(CONTROLLER))
    from deployment_request import RequestProvenance, build_deployment_request

    first = build_deployment_request(
        target="phone-production",
        product_release_tag="v0.1.4",
        provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 100, "iamaman11"),
    )
    second = build_deployment_request(
        target="phone-production",
        product_release_tag="v0.1.4",
        provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 200, "iamaman11"),
    )
    assert first.request_id == second.request_id
    assert first.semantic_payload() == second.semantic_payload()


def test_unknown_recovered_quarantine_and_no_blind_retry_invariants_remain() -> None:
    state_machine = (CONTROLLER / "deployment_state_machine.py").read_text(encoding="utf-8")
    evidence = (CONTROLLER / "evidence_store.py").read_text(encoding="utf-8")
    for token in ("UNKNOWN", "RECOVERED", "QUARANTINED"):
        assert token in state_machine
    assert "state in {UNKNOWN, RECOVERED}" in state_machine
    for token in ("ADMISSION_HEADING", "INTENT_HEADING", "TERMINAL_HEADING", "DUPLICATE_HEADING"):
        assert token in evidence


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"ISSUE_COMMAND_ROUTER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
