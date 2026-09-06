from __future__ import annotations

import ast
import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"
sys.path.insert(0, str(CONTROLLER))

import github_projection as projection_module  # noqa: E402
from github_projection import ProjectionError, PublicDeploymentMatch, PublicDeploymentProjection  # noqa: E402


def load_observer():
    path = SCRIPTS / "observe_public_deployment_projection.py"
    spec = importlib.util.spec_from_file_location("projection_observer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def match(deployment_id: int = 77, latest_state: str | None = "queued") -> PublicDeploymentMatch:
    return PublicDeploymentMatch(
        deployment_id=deployment_id,
        source_sha="ff882e5ca5116fa90b80f15c6cc019f88e68ccfa",
        ref="ff882e5ca5116fa90b80f15c6cc019f88e68ccfa",
        environment="phone-production",
        payload={
            "schema": "mobile-proxy-deployment-projection.v1",
            "product_release": "v0.1.4",
            "release_id": 382429107,
        },
        latest_state=latest_state,
    )


def test_bounded_evidence_zero_matches_emits_count_only() -> None:
    observer = load_observer()
    assert observer.bounded_evidence(()) == {"exact_match_count": 0}


def test_bounded_evidence_one_match_emits_only_id_and_state() -> None:
    observer = load_observer()
    assert observer.bounded_evidence((match(),)) == {
        "exact_match_count": 1,
        "deployment_id": 77,
        "latest_state": "queued",
    }


def test_bounded_evidence_multiple_matches_emits_count_only() -> None:
    observer = load_observer()
    assert observer.bounded_evidence((match(77), match(78))) == {"exact_match_count": 2}


def test_observer_calls_only_find_exact_on_projection() -> None:
    path = SCRIPTS / "observe_public_deployment_projection.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    projection_calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "projection"
    ]
    assert projection_calls == ["find_exact"], projection_calls


def test_observer_has_exact_fixed_historical_identity() -> None:
    observer = load_observer()
    assert observer.SOURCE_SHA == "ff882e5ca5116fa90b80f15c6cc019f88e68ccfa"
    assert observer.ENVIRONMENT == "phone-production"
    assert observer.PRODUCT_RELEASE == "v0.1.4"
    assert observer.RELEASE_ID == 382429107


def test_observer_source_has_no_private_or_physical_writer_capability() -> None:
    source = (SCRIPTS / "observe_public_deployment_projection.py").read_text(encoding="utf-8")
    forbidden = (
        "IssueEvidenceStore",
        "persist_admission",
        "persist_intent",
        "persist_terminal",
        "dispatch_install_once",
        "android_target",
        "adb",
        "provider",
        "vultr",
        ".create(",
        ".status(",
    )
    present = [token for token in forbidden if token in source]
    assert not present, present


def test_observer_workflow_is_hosted_manual_read_only_only() -> None:
    workflow = (WORKFLOWS / "public-deployment-projection-observer.yml").read_text(encoding="utf-8")
    required = (
        "workflow_dispatch:",
        "permissions:\n  contents: read",
        "runs-on: ubuntu-latest",
        "PUBLIC_DEPLOYMENTS_TOKEN",
        ".github/scripts/observe_public_deployment_projection.py",
    )
    missing = [token for token in required if token not in workflow]
    assert not missing, missing
    forbidden = (
        "self-hosted",
        "android-production",
        "issue_comment:",
        "push:",
        "pull_request:",
        "contents: write",
        "issues: write",
        "deployments: write",
        "release-deployment.yml",
        "run_phone_release_deployment.py",
        "adb",
        "provider",
        "vultr",
    )
    present = [token for token in forbidden if token in workflow]
    assert not present, present


def test_projection_get_retries_transient_transport_with_fixed_bound() -> None:
    projection = PublicDeploymentProjection("token")
    calls = 0
    responses: list[object] = [
        urllib.error.URLError("temporary transport"),
        urllib.error.HTTPError("https://api.github.com", 504, "gateway timeout", None, None),
        io.BytesIO(json.dumps([]).encode("utf-8")),
    ]
    original = projection_module.urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):
        nonlocal calls
        assert timeout == 30
        calls += 1
        value = responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    projection_module.urllib.request.urlopen = fake_urlopen
    try:
        assert projection._request("/deployments", method="GET") == []
        assert calls == 3
    finally:
        projection_module.urllib.request.urlopen = original


def test_projection_get_transport_exhaustion_is_exactly_three_attempts() -> None:
    projection = PublicDeploymentProjection("token")
    calls = 0
    original = projection_module.urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):
        nonlocal calls
        assert timeout == 30
        calls += 1
        raise urllib.error.URLError("temporary transport")

    projection_module.urllib.request.urlopen = fake_urlopen
    try:
        try:
            projection._request("/deployments", method="GET")
        except ProjectionError as exc:
            assert str(exc) == "public GitHub Deployment projection failed"
        else:
            raise AssertionError("projection GET transport exhaustion unexpectedly succeeded")
        assert calls == 3
    finally:
        projection_module.urllib.request.urlopen = original


def test_projection_post_transport_failure_is_never_retried() -> None:
    projection = PublicDeploymentProjection("token")
    calls = 0
    original = projection_module.urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):
        nonlocal calls
        assert timeout == 30
        calls += 1
        raise urllib.error.URLError("ambiguous write transport")

    projection_module.urllib.request.urlopen = fake_urlopen
    try:
        try:
            projection._request("/deployments", method="POST", payload={"ref": "a" * 40})
        except ProjectionError:
            pass
        else:
            raise AssertionError("projection POST transport failure unexpectedly succeeded")
        assert calls == 1
    finally:
        projection_module.urllib.request.urlopen = original


def test_projection_invalid_json_is_not_retried() -> None:
    projection = PublicDeploymentProjection("token")
    calls = 0
    original = projection_module.urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):
        nonlocal calls
        assert timeout == 30
        calls += 1
        return io.BytesIO(b"not-json")

    projection_module.urllib.request.urlopen = fake_urlopen
    try:
        try:
            projection._request("/deployments", method="GET")
        except ProjectionError:
            pass
        else:
            raise AssertionError("invalid projection JSON unexpectedly succeeded")
        assert calls == 1
    finally:
        projection_module.urllib.request.urlopen = original


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"PROJECTION_OBSERVER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
