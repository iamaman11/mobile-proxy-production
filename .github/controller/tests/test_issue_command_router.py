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
        "observe-phone-release",
        "observe-phone-operational",
        "exercise-runtime-recovery",
        "exercise-runtime-restart",
        "reconcile-phone-release",
        "phone-transport-preflight",
        "observe-runner-transport",
        "deploy-product-release",
        "runner-android-build-tools-bootstrap",
        "recover-quarantined-product-release",
    }
    assert set(targets) == {"phone-production", "vm-production"}
    assert targets["phone-production"]["active"] is True
    assert targets["phone-production"]["allowed_operations"] == [
        "deploy-product-release",
        "verify-product-release",
        "observe-phone-release",
        "observe-phone-operational",
        "exercise-runtime-recovery",
        "exercise-runtime-restart",
        "reconcile-phone-release",
        "phone-transport-preflight",
        "recover-quarantined-product-release",
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


def test_stage4_phone_observation_route_is_exact_read_only_and_bounded() -> None:
    route = accepted("/observe-phone-release phone-production v0.1.7")
    assert route.route_id == "observe-phone-release"
    assert route.handler == "workflow_call"
    assert route.workflow == ".github/workflows/phone-release-observation.yml"
    assert route.ref == "main"
    assert route.operation == "observe-phone-release"
    assert route.operation_class == "OBSERVE"
    assert route.target == "phone-production"
    assert route.release_tag == "v0.1.7"
    assert route.read_only is True and route.destructive is False
    assert route.concurrency_domain == "production-target-phone-production"
    assert route.idempotency_policy == "single-run-attempt"
    assert route.ref_policy == "controller-event-sha-exact"
    assert json.loads(route.arguments_json) == {
        "release": "v0.1.7",
        "target": "phone-production",
    }
    refused("/observe-phone-release phone-production v0.1.8")
    refused("/observe-phone-release vm-production v0.1.7")
    refused("/observe-phone-release phone-production v0.1.7 extra")
    refused("/observe-phone-release phone-production v0.1.7;echo")
    refused("/observe-phone-release phone-production v0.1.7\n/deploy phone-production v0.1.7")
    refused("/observe-phone-release phone-production v0.1.7", run_attempt=2)

    dispatcher = load_dispatcher()
    try:
        dispatcher.build_dispatch(
            "observe-phone-release",
            '{"release":"v0.1.7","target":"phone-production"}',
        )
    except dispatcher.DispatchRefused:
        pass
    else:
        raise AssertionError("generic hosted dispatcher accepted phone-access workflow_call route")


def test_stage4_phone_operational_route_is_independent_read_only_and_bounded() -> None:
    route = accepted("/observe-phone-operational phone-production v0.1.7")
    assert route.route_id == "observe-phone-operational"
    assert route.handler == "workflow_call"
    assert route.workflow == ".github/workflows/phone-operational-observation.yml"
    assert route.ref == "main"
    assert route.operation == "observe-phone-operational"
    assert route.operation_class == "OBSERVE"
    assert route.target == "phone-production"
    assert route.release_tag == "v0.1.7"
    assert route.read_only is True and route.destructive is False
    assert route.concurrency_domain == "production-target-phone-production"
    assert route.idempotency_policy == "single-run-attempt"
    assert route.ref_policy == "controller-event-sha-exact"
    assert route.target_capability_policy == "phone-production-operational-observation"
    assert json.loads(route.arguments_json) == {
        "release": "v0.1.7",
        "target": "phone-production",
    }
    refused("/observe-phone-operational phone-production v0.1.8")
    refused("/observe-phone-operational vm-production v0.1.7")
    refused("/observe-phone-operational phone-production v0.1.7 extra")
    refused("/observe-phone-operational phone-production v0.1.7;echo")
    refused("/observe-phone-operational phone-production v0.1.7\n/deploy phone-production v0.1.7")
    refused("/observe-phone-operational phone-production v0.1.7", run_attempt=2)

    dispatcher = load_dispatcher()
    try:
        dispatcher.build_dispatch(
            "observe-phone-operational",
            '{"release":"v0.1.7","target":"phone-production"}',
        )
    except dispatcher.DispatchRefused:
        pass
    else:
        raise AssertionError("generic hosted dispatcher accepted operational phone workflow_call route")


def test_stage4_runtime_recovery_route_is_exact_destructive_and_no_retry() -> None:
    route = accepted("/exercise-runtime-recovery phone-production v0.1.7")
    assert route.route_id == "exercise-runtime-recovery"
    assert route.handler == "workflow_call"
    assert route.workflow == ".github/workflows/stage4-runtime-recovery-exercise.yml"
    assert route.ref == "main"
    assert route.operation == "exercise-runtime-recovery"
    assert route.operation_class == "RECOVER"
    assert route.target == "phone-production"
    assert route.release_tag == "v0.1.7"
    assert route.read_only is False and route.destructive is True
    assert route.concurrency_domain == "production-target-phone-production"
    assert route.ref_policy == "controller-event-sha-exact"
    assert "semantic" in route.idempotency_policy
    assert "single-run-attempt" in route.idempotency_policy
    assert "UNKNOWN" in route.recovery_policy and "no-blind-retry" in route.recovery_policy
    assert route.target_capability_policy == "phone-production-fixed-runtime-recovery-exercise"
    assert json.loads(route.arguments_json) == {
        "release": "v0.1.7",
        "target": "phone-production",
    }
    refused("/exercise-runtime-recovery phone-production v0.1.8")
    refused("/exercise-runtime-recovery vm-production v0.1.7")
    refused("/exercise-runtime-recovery phone-production v0.1.7 host-daemon")
    refused("/exercise-runtime-recovery phone-production v0.1.7;echo")
    refused("/exercise-runtime-recovery phone-production v0.1.7\n/deploy phone-production v0.1.7")
    refused("/exercise-runtime-recovery phone-production v0.1.7", run_attempt=2)

    dispatcher = load_dispatcher()
    try:
        dispatcher.build_dispatch(
            "exercise-runtime-recovery",
            '{"release":"v0.1.7","target":"phone-production"}',
        )
    except dispatcher.DispatchRefused:
        pass
    else:
        raise AssertionError("generic hosted dispatcher accepted destructive lifecycle workflow_call route")


def test_stage4_runtime_restart_route_is_exact_destructive_and_no_retry() -> None:
    route = accepted("/exercise-runtime-restart phone-production v0.1.7")
    assert route.route_id == "exercise-runtime-restart"
    assert route.handler == "workflow_call"
    assert route.workflow == ".github/workflows/stage4-runtime-restart-exercise.yml"
    assert route.ref == "main"
    assert route.operation == "exercise-runtime-restart"
    assert route.operation_class == "RECOVER"
    assert route.target == "phone-production"
    assert route.release_tag == "v0.1.7"
    assert route.read_only is False and route.destructive is True
    assert route.concurrency_domain == "production-target-phone-production"
    assert route.ref_policy == "controller-event-sha-exact"
    assert "semantic" in route.idempotency_policy
    assert "single-run-attempt" in route.idempotency_policy
    assert "UNKNOWN" in route.recovery_policy and "no-blind-retry" in route.recovery_policy
    assert route.target_capability_policy == "phone-production-fixed-runtime-restart-exercise"
    assert json.loads(route.arguments_json) == {
        "release": "v0.1.7",
        "target": "phone-production",
    }
    refused("/exercise-runtime-restart phone-production v0.1.8")
    refused("/exercise-runtime-restart vm-production v0.1.7")
    refused("/exercise-runtime-restart phone-production v0.1.7 runtime-supervisor")
    refused("/exercise-runtime-restart phone-production v0.1.7;echo")
    refused("/exercise-runtime-restart phone-production v0.1.7\n/deploy phone-production v0.1.7")
    refused("/exercise-runtime-restart phone-production v0.1.7", run_attempt=2)

    dispatcher = load_dispatcher()
    try:
        dispatcher.build_dispatch(
            "exercise-runtime-restart",
            '{"release":"v0.1.7","target":"phone-production"}',
        )
    except dispatcher.DispatchRefused:
        pass
    else:
        raise AssertionError("generic hosted dispatcher accepted destructive runtime restart workflow_call route")


def test_stage4_phone_reconcile_route_is_distinct_target_read_only_control_plane_write() -> None:
    route = accepted("/reconcile-phone-release phone-production v0.1.7")
    assert route.route_id == "reconcile-phone-release"
    assert route.handler == "workflow_call"
    assert route.workflow == ".github/workflows/phone-release-reconcile.yml"
    assert route.ref == "main"
    assert route.operation == "reconcile-phone-release"
    assert route.operation_class == "RECONCILE"
    assert route.target == "phone-production"
    assert route.release_tag == "v0.1.7"
    assert route.read_only is False and route.destructive is False
    assert route.concurrency_domain == "production-target-phone-production"
    assert route.idempotency_policy == "projection-readback-noop"
    assert route.ref_policy == "controller-event-sha-exact"
    assert route.semantic_identity_policy == "command-arguments+existing-admitted-projection"
    assert json.loads(route.arguments_json) == {
        "release": "v0.1.7",
        "target": "phone-production",
    }
    refused("/reconcile-phone-release phone-production v0.1.8")
    refused("/reconcile-phone-release vm-production v0.1.7")
    refused("/reconcile-phone-release phone-production v0.1.7 extra")
    refused("/reconcile-phone-release phone-production v0.1.7;echo")
    refused("/reconcile-phone-release phone-production v0.1.7\n/deploy phone-production v0.1.7")


def test_phone_transport_preflight_route_is_exact_read_only_and_bounded() -> None:
    route = accepted("/phone-transport-preflight")
    assert route.route_id == "phone-transport-preflight"
    assert route.handler == "workflow_call"
    assert route.workflow == ".github/workflows/phone-transport-preflight.yml"
    assert route.operation_class == "DIAGNOSTIC"
    assert route.read_only is True and route.destructive is False
    assert route.concurrency_domain == "production-phone-read-only-preflight"
    assert route.arguments_json == "{}"
    refused("/phone-transport-preflight extra")
    refused("/phone-transport-preflight\n/deploy phone-production v0.1.7")
    refused("/phone-transport-preflight", run_attempt=2)

    source = (WORKFLOWS / "phone-release-observation.yml").read_text(encoding="utf-8")
    for required in (
        "workflow_call:",
        "source_issue_number:",
        "source_comment_id:",
        "source_actor:",
        "source_comment_url:",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-target-phone-production",
        "cancel-in-progress: false",
        "environment: phone-production",
        ".github/scripts/observe_phone_release.py",
        "STAGE4_PHONE_RELEASE_OBSERVATION_CLASSIFIED",
    ):
        assert required in source
    for forbidden in (
        "workflow_dispatch:",
        "issue_comment:",
        "dispatch_release_once",
        "dispatch_install_once",
        "/deploy ",
        "/retry-deploy ",
    ):
        assert forbidden not in source


def test_runner_transport_observer_route_is_exact_no_argument_and_phone_independent() -> None:
    route = accepted("/observe-runner-transport")
    assert route.route_id == "observe-runner-transport"
    assert route.handler == "workflow_call"
    assert route.workflow == ".github/workflows/runner-transport-observation.yml"
    assert route.ref == "main"
    assert route.operation == "observe-runner-transport"
    assert route.operation_class == "DIAGNOSTIC"
    assert route.read_only is True and route.destructive is False
    assert route.concurrency_domain == "production-runner-transport-observation"
    assert route.idempotency_policy == "single-run-attempt"
    assert route.ref_policy == "controller-event-sha-exact"
    assert route.arguments_json == "{}"
    refused("/observe-runner-transport extra")
    refused("/observe-runner-transport phone-production")
    refused("/observe-runner-transport v0.1.7")
    refused("/observe-runner-transport\n/deploy phone-production v0.1.7")
    refused("/observe-runner-transport", run_attempt=2)

    source = (WORKFLOWS / "runner-transport-observation.yml").read_text(encoding="utf-8")
    for required in (
        "workflow_call:",
        "inputs.command == '/observe-runner-transport'",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-runner-transport-observation",
        "cancel-in-progress: false",
        "Run bounded runner transport observer",
        ".github/scripts/observe_runner_transport.py",
        "continue-on-error: true",
        "Retry same core evidence transfer once",
        "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
        ".github/scripts/finalize_runner_transport_observation.py",
        "runner-transport-observation-final.json",
    ):
        assert required in source
    assert source.count(".github/scripts/observe_runner_transport.py") == 1
    for forbidden in (
        "workflow_dispatch:",
        "issue_comment:",
        "ANDROID_PRODUCTION_SERIAL",
        "environment: phone-production",
        "secrets:",
        "adb ",
        "systemctl restart",
        "recover-runner-transport",
        "ip route add",
        "ip route replace",
    ):
        assert forbidden not in source


def test_generic_dispatcher_resolves_only_registry_read_only_routes() -> None:
    dispatcher = load_dispatcher()
    workflow, ref, inputs = dispatcher.build_dispatch("observe-public-deployment-projection", "{}")
    assert workflow == "public-deployment-projection-observer.yml"
    assert ref == "main"
    assert inputs == {}
    for route_id, arguments in (
        ("observe-phone-release", '{"release":"v0.1.7","target":"phone-production"}'),
        ("observe-phone-operational", '{"release":"v0.1.7","target":"phone-production"}'),
        ("exercise-runtime-recovery", '{"release":"v0.1.7","target":"phone-production"}'),
        ("exercise-runtime-restart", '{"release":"v0.1.7","target":"phone-production"}'),
        ("reconcile-phone-release", '{"release":"v0.1.7","target":"phone-production"}'),
        ("observe-runner-transport", '{}'),
        ("deploy-product-release", '{"release":"v0.1.4","target":"phone-production"}'),
        ("recover-quarantined-product-release", '{"quarantined_request_id":"req-sha256:74489a27b4c845b9060056af498090beded81db009e05f0290091af846c4e5d7","release":"v0.1.7","target":"phone-production"}'),
    ):
        try:
            dispatcher.build_dispatch(route_id, arguments)
        except dispatcher.DispatchRefused:
            pass
        else:
            raise AssertionError("generic dispatcher accepted non-generic route")
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


def test_quarantine_recovery_route_is_exact_and_destructive() -> None:
    request_id = "req-sha256:74489a27b4c845b9060056af498090beded81db009e05f0290091af846c4e5d7"
    route = accepted(f"/recover-quarantined phone-production v0.1.7 {request_id}")
    assert route.route_id == "recover-quarantined-product-release"
    assert route.handler == "workflow_call"
    assert route.workflow == ".github/workflows/quarantined-release-recovery.yml"
    assert route.operation == "recover-quarantined-product-release"
    assert route.operation_class == "RECOVER"
    assert route.target == "phone-production"
    assert route.release_tag == "v0.1.7"
    assert route.destructive is True and route.read_only is False
    assert "semantic" in route.idempotency_policy
    assert "UNKNOWN" in route.recovery_policy and "no-blind-retry" in route.recovery_policy
    assert json.loads(route.arguments_json) == {
        "quarantined_request_id": request_id,
        "release": "v0.1.7",
        "target": "phone-production",
    }
    refused(f"/recover-quarantined phone-production v0.1.8 {request_id}")
    refused(f"/recover-quarantined vm-production v0.1.7 {request_id}")
    refused("/recover-quarantined phone-production v0.1.7 req-sha256:" + "0" * 64)
    refused(f"/recover-quarantined phone-production v0.1.7 {request_id} extra")
    refused(f"/recover-quarantined phone-production v0.1.7 {request_id}", run_attempt=2)


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
        "needs.route.outputs.handler == 'workflow_call'",
        "needs.route.outputs.route_id == 'observe-phone-release'",
        "needs.route.outputs.route_id == 'observe-phone-operational'",
        "needs.route.outputs.route_id == 'exercise-runtime-recovery'",
        "needs.route.outputs.route_id == 'exercise-runtime-restart'",
        "needs.route.outputs.route_id == 'reconcile-phone-release'",
        "needs.route.outputs.route_id == 'observe-runner-transport'",
        "needs.route.outputs.operation_class == 'RECONCILE'",
        "needs.route.outputs.operation_class == 'RECOVER'",
        "needs.route.outputs.read_only == 'false'",
        "./.github/workflows/phone-release-observation.yml",
        "./.github/workflows/phone-operational-observation.yml",
        "./.github/workflows/stage4-runtime-recovery-exercise.yml",
        "./.github/workflows/stage4-runtime-restart-exercise.yml",
        "./.github/workflows/phone-release-reconcile.yml",
        "./.github/workflows/runner-transport-observation.yml",
        "./.github/workflows/release-deployment.yml",
        "./.github/workflows/production-runner-android-build-tools-bootstrap.yml",
        "./.github/workflows/quarantined-release-recovery.yml",
        "recover-quarantined-product-release",
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
