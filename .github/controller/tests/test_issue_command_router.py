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


def load_router():
    loaded = sys.modules.get("issue_command_router")
    if loaded is not None:
        return loaded
    path = SCRIPTS / "issue_command_router.py"
    spec = importlib.util.spec_from_file_location("issue_command_router", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
    assert {item["id"] for item in routes if item["enabled"]} == {
        "observe-public-deployment-projection",
        "deploy-product-release",
        "runner-android-build-tools-bootstrap",
    }
    assert set(targets) == {"phone-production", "vm-production"}
    assert targets["phone-production"]["active"] is True
    assert targets["vm-production"]["active"] is False


def test_observer_route_is_exact_read_only_and_rerun_safe() -> None:
    route = accepted()
    assert route.route_id == "observe-public-deployment-projection"
    assert route.handler == "dispatch_workflow"
    assert route.workflow == ".github/workflows/public-deployment-projection-observer.yml"
    assert route.ref == "main"
    assert route.operation_class == "OBSERVE"
    assert route.read_only is True and route.destructive is False
    assert route.idempotency_policy == "single-run-attempt"
    refused("/observe-public-deployment-projection extra")
    refused(run_attempt=2)


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


def test_destructive_contract_requires_semantic_idempotency_recovery_and_evidence() -> None:
    router = load_router()
    command_value, _, targets = registries()
    broken = json.loads(json.dumps(command_value))
    deploy = next(item for item in broken["routes"] if item["id"] == "deploy-product-release")
    deploy["recovery_policy"] = "retry"
    try:
        router.validate_registry(broken, targets)
    except router.RouteRefused:
        pass
    else:
        raise AssertionError("destructive route without UNKNOWN/no-blind-retry recovery was accepted")


def test_dynamic_workflow_or_ref_is_rejected() -> None:
    router = load_router()
    command_value, _, targets = registries()
    for field, value in (("workflow", ".github/workflows/${command}.yml"), ("ref", "${ref}")):
        broken = json.loads(json.dumps(command_value))
        broken["routes"][0][field] = value
        try:
            router.validate_registry(broken, targets)
        except router.RouteRefused:
            continue
        raise AssertionError(f"dynamic {field} was accepted")


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


def test_exactly_one_issue_comment_ingress_and_literal_mappings() -> None:
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
        "repos/iamaman11/mobile-proxy-production/actions/workflows/public-deployment-projection-observer.yml/dispatches",
        "-f ref=main",
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
