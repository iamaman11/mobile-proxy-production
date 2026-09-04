from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"
SHA = "5715c839957af781a84baa2ae89482c2a7c89e05"


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


def accepted(**overrides):
    router = load_router()
    facts = {
        "repository": "iamaman11/mobile-proxy-production",
        "issue_number": 1,
        "author": "iamaman11",
        "command": "/observe-public-deployment-projection",
        "event_sha": SHA,
        "current_main_sha": SHA,
    }
    facts.update(overrides)
    return router.classify(**facts)


def refused(**overrides) -> None:
    router = load_router()
    try:
        accepted(**overrides)
    except router.RouteRefused:
        return
    raise AssertionError(f"route unexpectedly accepted: {overrides!r}")


def test_only_one_exact_route_is_admitted() -> None:
    router = load_router()
    route = accepted()
    assert route.workflow == ".github/workflows/public-deployment-projection-observer.yml"
    assert route.ref == "main"
    assert router.ALLOWED_AUTHORS == frozenset({"iamaman11"})


def test_wrong_issue_repository_author_or_command_is_refused() -> None:
    refused(issue_number=2)
    refused(repository="iamaman11/mobile-proxy")
    refused(author="someone-else")
    refused(command="/deploy phone-production v0.1.4")
    refused(command="/observe-public-deployment-projection extra")
    refused(command="/observe-public-deployment-projection\n/deploy phone-production v0.1.4")


def test_authoritative_main_drift_or_malformed_sha_is_refused() -> None:
    refused(current_main_sha="ff882e5ca5116fa90b80f15c6cc019f88e68ccfa")
    refused(event_sha="not-a-sha")
    refused(current_main_sha="not-a-sha")


def test_router_workflow_is_native_exact_and_bounded() -> None:
    workflow = (WORKFLOWS / "issue-command-router.yml").read_text(encoding="utf-8")
    required = (
        "issue_comment:\n    types: [created]",
        "contents: read",
        "issues: read",
        "actions: write",
        "github.event.repository.full_name == 'iamaman11/mobile-proxy-production'",
        "repos/iamaman11/mobile-proxy-production/commits/main",
        ".github/scripts/issue_command_router.py",
        "GH_TOKEN: ${{ github.token }}",
        "repos/iamaman11/mobile-proxy-production/actions/workflows/public-deployment-projection-observer.yml/dispatches",
        "-f ref=main",
    )
    missing = [token for token in required if token not in workflow]
    assert not missing, missing

    forbidden = (
        "workflow_call:",
        "workflow_run:",
        "workflow_dispatch:",
        "pull_request:",
        "push:",
        "schedule:",
        "secrets.",
        "PUBLIC_DEPLOYMENTS_TOKEN",
        "adb",
        "android-production",
        "self-hosted",
        "vultr",
        "provider",
        "release-deployment.yml",
        "run_phone_release_deployment.py",
        "/deploy",
        "actions/workflows/${",
        "-f ref=${",
    )
    present = [token for token in forbidden if token in workflow]
    assert not present, present


def test_router_source_has_no_dynamic_dispatch_or_mutation_capability() -> None:
    source = (SCRIPTS / "issue_command_router.py").read_text(encoding="utf-8")
    required = (
        'AUTHORITATIVE_REPOSITORY = "iamaman11/mobile-proxy-production"',
        "AUTHORITATIVE_ISSUE_NUMBER = 1",
        'ALLOWED_COMMAND = "/observe-public-deployment-projection"',
        'ALLOWED_WORKFLOW = ".github/workflows/public-deployment-projection-observer.yml"',
        'ALLOWED_REF = "main"',
        "event_sha != current_main_sha",
    )
    missing = [token for token in required if token not in source]
    assert not missing, missing
    forbidden = (
        "subprocess",
        "requests",
        "urllib",
        "adb",
        "install",
        "delete",
        "secret",
        "provider",
        "vultr",
    )
    present = [token for token in forbidden if token in source.lower()]
    assert not present, present


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"ISSUE_COMMAND_ROUTER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
