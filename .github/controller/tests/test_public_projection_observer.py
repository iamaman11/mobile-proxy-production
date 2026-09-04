from __future__ import annotations

import inspect
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
ROOT = CONTROLLER.parent
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"
sys.path.insert(0, str(CONTROLLER))
sys.path.insert(0, str(SCRIPTS))

from github_projection import PublicDeploymentMatch, PublicDeploymentProjection  # noqa: E402
from observe_public_deployment_projection import (  # noqa: E402
    ENVIRONMENT,
    PRODUCT_RELEASE,
    RELEASE_ID,
    SOURCE_SHA,
    build_observation,
    observe_projection,
)


PAYLOAD = {
    "schema": "mobile-proxy-deployment-projection.v1",
    "product_release": PRODUCT_RELEASE,
    "release_id": RELEASE_ID,
}


def match(*, deployment_id: int, state: str | None = "queued") -> PublicDeploymentMatch:
    return PublicDeploymentMatch(
        deployment_id=deployment_id,
        source_sha=SOURCE_SHA,
        ref=SOURCE_SHA,
        environment=ENVIRONMENT,
        payload=PAYLOAD,
        latest_state=state,
    )


class FakeProjection:
    def __init__(self, matches: list[PublicDeploymentMatch]) -> None:
        self.matches = tuple(matches)
        self.calls: list[dict[str, object]] = []

    def find_exact(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.matches


def test_stage2x_identity_is_exact_and_immutable() -> None:
    assert SOURCE_SHA == "ff882e5ca5116fa90b80f15c6cc019f88e68ccfa"
    assert ENVIRONMENT == "phone-production"
    assert PRODUCT_RELEASE == "v0.1.4"
    assert RELEASE_ID == 382429107


def test_zero_matches_emits_count_only() -> None:
    value = build_observation([])
    assert value["exact_match_count"] == 0
    assert "deployment_id" not in value
    assert "latest_state" not in value


def test_exactly_one_match_emits_id_and_latest_state() -> None:
    value = build_observation([match(deployment_id=77, state="in_progress")])
    assert value["exact_match_count"] == 1
    assert value["deployment_id"] == 77
    assert value["latest_state"] == "in_progress"


def test_multiple_matches_emit_count_without_selecting_one() -> None:
    value = build_observation([
        match(deployment_id=77, state=None),
        match(deployment_id=78, state=None),
    ])
    assert value["exact_match_count"] == 2
    assert "deployment_id" not in value
    assert "latest_state" not in value


def test_observer_uses_only_exact_find_path() -> None:
    projection = FakeProjection([match(deployment_id=77, state="queued")])
    value = observe_projection(projection)  # type: ignore[arg-type]
    assert value["deployment_id"] == 77
    assert projection.calls == [{
        "source_sha": SOURCE_SHA,
        "environment": ENVIRONMENT,
        "release_tag": PRODUCT_RELEASE,
        "release_id": RELEASE_ID,
    }]


def test_observer_refuses_one_match_without_latest_state() -> None:
    try:
        build_observation([match(deployment_id=77, state=None)])
    except RuntimeError as exc:
        assert "unavailable latest state" in str(exc)
    else:
        raise AssertionError("one exact Deployment without latest state was unexpectedly accepted")


def test_live_observer_source_has_no_write_or_physical_capability() -> None:
    source = (SCRIPTS / "observe_public_deployment_projection.py").read_text(encoding="utf-8")
    required = (
        "PublicDeploymentProjection",
        "projection.find_exact(",
        "PUBLIC_DEPLOYMENTS_TOKEN",
        SOURCE_SHA,
        ENVIRONMENT,
        PRODUCT_RELEASE,
        str(RELEASE_ID),
    )
    missing = [item for item in required if item not in source]
    assert missing == []

    forbidden = (
        ".create(",
        ".status(",
        "IssueEvidenceStore",
        "persist_admission(",
        "persist_intent(",
        "persist_terminal(",
        "dispatch_install_once(",
        "android_target",
        "adb ",
        "subprocess",
        "provider",
        "vultr",
    )
    present = [item for item in forbidden if item in source]
    assert present == []


def test_projection_lookup_implementation_is_get_only() -> None:
    source = inspect.getsource(PublicDeploymentProjection.find_exact)
    status_source = inspect.getsource(PublicDeploymentProjection._latest_status)
    assert 'method="GET"' in source
    assert 'method="GET"' in status_source
    assert 'method="POST"' not in source
    assert 'method="POST"' not in status_source


def test_workflow_is_hosted_owner_only_and_read_only() -> None:
    workflow = (WORKFLOWS / "public-deployment-projection-observer.yml").read_text(encoding="utf-8")
    required = (
        "workflow_dispatch:",
        "if: github.actor == github.repository_owner",
        "runs-on: ubuntu-latest",
        "contents: read",
        "PUBLIC_DEPLOYMENTS_TOKEN",
        ".github/scripts/observe_public_deployment_projection.py",
    )
    missing = [item for item in required if item not in workflow]
    assert missing == []

    forbidden = (
        "self-hosted",
        "android-production",
        "issue_comment:",
        "/deploy ",
        "issues: write",
        "deployments: write",
        "curl ",
        "gh api",
        "adb ",
    )
    present = [item for item in forbidden if item in workflow]
    assert present == []


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"PUBLIC_PROJECTION_OBSERVER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
