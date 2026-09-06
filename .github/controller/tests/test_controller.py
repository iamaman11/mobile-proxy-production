#!/usr/bin/env python3
from __future__ import annotations

import ast
import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
GITHUB_DIR = ROOT.parent
sys.path.insert(0, str(ROOT))

import product_runtime_renderer as renderer
from deployment_request import (
    RequestProvenance,
    build_deployment_request,
    build_retry_deployment_request,
)
from deployment_state_machine import (
    ACCEPTED, QUARANTINED, RECOVERED, REFUSED, UNKNOWN,
    DeploymentState, TransitionError, deployment_projection, recover_unknown, reduce_state,
)
from evidence_store import EvidenceRecord, INTENT_HEADING, TERMINAL_HEADING
from phone_runtime import PhoneRuntimeRefused
from projection_admission import ProjectionAdmissionError, resolve_projection_admission
from terminal_result import DeploymentTerminal, validate_terminal


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def advance_to_dispatch() -> DeploymentState:
    state = DeploymentState()
    for event in ("request_received", "authorized", "observed", "intent_persisted"):
        state = reduce_state(state, event)
    return state


def expect_transition_error(callable_) -> None:
    try:
        callable_()
    except TransitionError:
        return
    raise AssertionError("illegal transition unexpectedly admitted")


def test_duplicate_command_semantic_identity() -> None:
    first = build_deployment_request(target="phone-production", product_release_tag="v0.1.4", provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 100, "iamaman11"))
    duplicate_comment = build_deployment_request(target="phone-production", product_release_tag="v0.1.4", provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 999, "iamaman11"))
    other_release = build_deployment_request(target="phone-production", product_release_tag="v0.1.5", provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 101, "iamaman11"))
    assert first.request_id == "req-sha256:3c1280e9aa922a8f77e003e73fcbdb92903d46ea68f1fa2d3bc7d9dbeb1c3173"
    assert first.request_id == duplicate_comment.request_id
    assert first.request_id != other_release.request_id
    assert "retry_of_request_id" not in first.to_dict()
    assert "authority_cursor" not in first.to_dict()


def test_explicit_refused_retry_request_identity_is_lineage_bound_not_comment_bound() -> None:
    prior = "req-sha256:" + "4" * 64
    first = build_retry_deployment_request(
        target="phone-production", product_release_tag="v0.1.7", retry_of_request_id=prior,
        provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 200, "iamaman11"),
    )
    duplicate_comment = build_retry_deployment_request(
        target="phone-production", product_release_tag="v0.1.7", retry_of_request_id=prior,
        provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 201, "iamaman11"),
    )
    other_prior = build_retry_deployment_request(
        target="phone-production", product_release_tag="v0.1.7",
        retry_of_request_id="req-sha256:" + "5" * 64,
        provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 202, "iamaman11"),
    )
    ordinary = build_deployment_request(
        target="phone-production", product_release_tag="v0.1.7",
        provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 203, "iamaman11"),
    )
    assert first.request_id == duplicate_comment.request_id
    assert first.request_id != other_prior.request_id
    assert first.request_id != ordinary.request_id
    assert first.retry_of_request_id == prior
    assert first.to_dict()["retry_of_request_id"] == prior


def test_retry_command_is_exactly_one_existing_destructive_route() -> None:
    issue_router = _load_script("controller_test_issue_router", GITHUB_DIR / "scripts" / "issue_command_router.py")
    command_router = _load_script("controller_test_deployment_command_router", GITHUB_DIR / "scripts" / "deployment_command_router.py")
    sha = "a" * 40
    prior = "req-sha256:" + "4" * 64
    route = issue_router.classify(
        repository="iamaman11/mobile-proxy-production", issue_number=1, author="iamaman11",
        command=f"/retry-deploy phone-production v0.1.7 {prior}",
        event_sha=sha, current_main_sha=sha, run_attempt=1,
    )
    assert route.route_id == "deploy-product-release"
    assert route.operation == "deploy-product-release"
    assert route.destructive is True and route.read_only is False
    request = command_router.classify(
        command_body=f"/retry-deploy phone-production v0.1.7 {prior}",
        repository="iamaman11/mobile-proxy-production", issue_number=1, comment_id=300,
        actor="iamaman11", owner="iamaman11",
    )
    assert request.retry_of_request_id == prior
    invalid = (
        "/retry-deploy phone-production v0.1.7",
        f"/retry-deploy vm-production v0.1.7 {prior}",
        f"/deploy phone-production v0.1.7 {prior}",
        "/retry-deploy phone-production v0.1.7 req-sha256:bad",
    )
    for command in invalid:
        try:
            issue_router.classify(
                repository="iamaman11/mobile-proxy-production", issue_number=1, author="iamaman11",
                command=command, event_sha=sha, current_main_sha=sha, run_attempt=1,
            )
        except issue_router.RouteRefused:
            continue
        raise AssertionError(f"invalid retry command reached deployment route: {command}")


def _refused_terminal(prior: str, *, target: str = "phone-production", release: str = "v0.1.7", mutation: bool = False, recovery: bool = False, state: str = REFUSED) -> dict[str, object]:
    terminal = DeploymentTerminal(
        operation="deploy-product-release", semantic_request_id=prior,
        execution_id="gh-run:123:1", controller_revision="a" * 40, target=target,
        product_release=release, release_id=1, release_source_sha="b" * 40,
        artifact_digest="b3:" + "c" * 64, deployment_id=2, state=state, current_step="OBSERVE",
        blocking_predicates=("bounded_test_refusal",), mutation_performed=mutation,
        postcondition_verified=False, recovery_required=recovery,
        next_allowed_operation="fix_blocking_predicates_then_reissue",
    ).to_dict()
    if state == REFUSED:
        validate_terminal(terminal)
    return terminal


class _FakeEvidence:
    def __init__(self, *, intents=(), terminals=()):
        self.intents = list(intents)
        self.terminals = list(terminals)

    def list_records(self, heading: str):
        if heading == INTENT_HEADING:
            return list(self.intents)
        if heading == TERMINAL_HEADING:
            return list(self.terminals)
        return []


def test_refused_retry_lineage_admission_is_pre_release_and_fail_closed() -> None:
    prepare = _load_script("controller_test_prepare_release", GITHUB_DIR / "scripts" / "prepare_release_deployment.py")
    prior = "req-sha256:" + "4" * 64
    request = build_retry_deployment_request(
        target="phone-production", product_release_tag="v0.1.7", retry_of_request_id=prior,
        provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 400, "iamaman11"),
    ).to_dict()
    terminal = EvidenceRecord(10, TERMINAL_HEADING, _refused_terminal(prior))
    admitted = prepare._retry_lineage_terminal(_FakeEvidence(terminals=[terminal]), request)
    assert admitted.ref == "issue-comment:10"

    bad_cases = (
        _FakeEvidence(),
        _FakeEvidence(terminals=[terminal, EvidenceRecord(11, TERMINAL_HEADING, _refused_terminal(prior))]),
        _FakeEvidence(
            intents=[EvidenceRecord(12, INTENT_HEADING, {"semantic_request_id": prior})],
            terminals=[terminal],
        ),
        _FakeEvidence(terminals=[EvidenceRecord(13, TERMINAL_HEADING, _refused_terminal(prior, mutation=True))]),
        _FakeEvidence(terminals=[EvidenceRecord(14, TERMINAL_HEADING, _refused_terminal(prior, recovery=True))]),
        _FakeEvidence(terminals=[EvidenceRecord(15, TERMINAL_HEADING, _refused_terminal(prior, release="v0.1.6"))]),
    )
    for evidence in bad_cases:
        try:
            prepare._retry_lineage_terminal(evidence, request)
        except Exception:
            continue
        raise AssertionError("ineligible prior request was admitted for destructive retry")

    source = (GITHUB_DIR / "scripts" / "prepare_release_deployment.py").read_text(encoding="utf-8")
    lineage = source.index("_retry_lineage_terminal(evidence, request)")
    release = source.index("admitted = resolve_release(")
    projection = source.index("projection = PublicDeploymentProjection(")
    retry_projection = source.index("retry_authorized=retry_terminal is not None", projection)
    assert lineage < release < projection < retry_projection


class _FakeProjection:
    def __init__(self, matches=()):
        self.matches = tuple(matches)
        self.status_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []

    def find_exact(self, **kwargs):
        return self.matches

    def status(self, **kwargs):
        self.status_calls.append(dict(kwargs))
        return 1

    def create(self, **kwargs):
        self.create_calls.append(dict(kwargs))
        return 99


def _projection_match(state: str | None, deployment_id: int = 2):
    return SimpleNamespace(
        deployment_id=deployment_id,
        source_sha="b" * 40,
        ref="b" * 40,
        environment="phone-production",
        payload={},
        latest_state=state,
    )


def _projection_admit(projection: _FakeProjection, **overrides):
    values = {
        "projection": projection,
        "source_sha": "b" * 40,
        "environment": "phone-production",
        "release_tag": "v0.1.7",
        "release_id": 7,
        "durable_deployment_id": None,
    }
    values.update(overrides)
    return resolve_projection_admission(**values)


def test_terminal_public_projection_remains_fail_closed_for_ordinary_deploy() -> None:
    for state in ("failure", "error", "success", "inactive"):
        projection = _FakeProjection([_projection_match(state)])
        try:
            _projection_admit(projection)
        except ProjectionAdmissionError:
            pass
        else:
            raise AssertionError(f"ordinary deploy reused terminal public projection {state}")
        assert projection.status_calls == []
        assert projection.create_calls == []


def test_explicit_refused_retry_reopens_only_failure_or_error_projection_generation() -> None:
    for state in ("failure", "error"):
        projection = _FakeProjection([_projection_match(state)])
        decision = _projection_admit(
            projection,
            retry_authorized=True,
            retry_expected_deployment_id=2,
        )
        assert decision.deployment_id == 2
        assert decision.reused is True
        assert decision.observed_state == "queued"
        assert projection.create_calls == []
        assert projection.status_calls == [{
            "deployment_id": 2,
            "state": "queued",
            "description": "v0.1.7 explicit REFUSED retry admitted by production controller",
        }]


def test_explicit_retry_does_not_reopen_success_or_inactive_projection() -> None:
    for state in ("success", "inactive"):
        projection = _FakeProjection([_projection_match(state)])
        try:
            _projection_admit(projection, retry_authorized=True)
        except ProjectionAdmissionError:
            pass
        else:
            raise AssertionError(f"retry reopened forbidden terminal public projection {state}")
        assert projection.status_calls == []


def test_retry_projection_requires_exact_known_deployment_identity_when_present() -> None:
    projection = _FakeProjection([_projection_match("failure", deployment_id=2)])
    try:
        _projection_admit(
            projection,
            retry_authorized=True,
            retry_expected_deployment_id=3,
        )
    except ProjectionAdmissionError as exc:
        assert "different public Deployment" in str(exc)
    else:
        raise AssertionError("retry reused a public Deployment outside its prior lineage")
    assert projection.status_calls == []


def test_projection_admission_rejects_ambiguous_or_mismatched_public_identity() -> None:
    multiple = _FakeProjection([
        _projection_match("failure", deployment_id=2),
        _projection_match("failure", deployment_id=3),
    ])
    try:
        _projection_admit(multiple, retry_authorized=True)
    except ProjectionAdmissionError as exc:
        assert "multiple exact public Deployments" in str(exc)
    else:
        raise AssertionError("ambiguous public projection admitted")
    assert multiple.status_calls == []

    mismatched_durable = _FakeProjection([_projection_match("queued", deployment_id=2)])
    try:
        _projection_admit(mismatched_durable, durable_deployment_id=3)
    except ProjectionAdmissionError as exc:
        assert "different public Deployment" in str(exc)
    else:
        raise AssertionError("mismatched durable public Deployment admitted")


def test_projection_retry_identity_cannot_be_supplied_without_lineage_authority() -> None:
    projection = _FakeProjection([_projection_match("failure", deployment_id=2)])
    try:
        _projection_admit(projection, retry_expected_deployment_id=2)
    except ProjectionAdmissionError as exc:
        assert "without admitted retry lineage" in str(exc)
    else:
        raise AssertionError("retry identity bypassed retry lineage authority")
    assert projection.status_calls == []


def test_safe_nonterminal_projection_reuse_is_unchanged() -> None:
    for state in ("queued", "in_progress"):
        projection = _FakeProjection([_projection_match(state)])
        decision = _projection_admit(projection)
        assert decision.deployment_id == 2
        assert decision.reused is True
        assert decision.observed_state == state
        assert projection.status_calls == []


def test_product_subprocess_failure_classes_are_bounded_and_secret_free() -> None:
    secret = "SHOULD_NEVER_APPEAR_IN_DURABLE_EVIDENCE"
    original = renderer._run_checked

    def leaking_failure(*args, **kwargs):
        raise PhoneRuntimeRefused("raw stderr token=" + secret)

    renderer._run_checked = leaking_failure
    try:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "runtime.tar.gz"
            artifact.write_bytes(b"x")
            try:
                renderer._product_digest(product_root=root, asset_name="runtime.tar.gz", path=artifact)
            except renderer.ProductReleaseComponentDigestRefused as exc:
                assert str(exc) == "PRODUCT_RELEASE_COMPONENT_DIGEST_COMMAND_FAILED"
                assert secret not in str(exc)
            else:
                raise AssertionError("digest subprocess failure was not classified")

            materialized = SimpleNamespace(
                source_root=root / "source",
                release_root=root / "release",
                required_live_release_paths=(),
            )
            materialized.source_root.mkdir()
            materialized.release_root.mkdir()
            try:
                renderer.render_required_runtime_configs(
                    materialized, product_root=root, manifest_json="{}",
                    release_id="v0.1.7", environment={"SECRET": secret},
                )
            except renderer.ProductRuntimeRenderRefused as exc:
                assert str(exc) == "PRODUCT_RUNTIME_RENDER_COMMAND_FAILED"
                assert secret not in str(exc)
            else:
                raise AssertionError("runtime render subprocess failure was not classified")
    finally:
        renderer._run_checked = original


def test_failure_before_dispatch() -> None:
    state = reduce_state(reduce_state(reduce_state(DeploymentState(), "request_received"), "authorized"), "observed")
    state = reduce_state(state, "intent_persistence_failed", reason="durable intent write failed")
    assert state.state == REFUSED and state.dispatch_attempted is False and state.mutation_performed is False


def test_transport_loss_before_dispatch() -> None:
    state = reduce_state(reduce_state(DeploymentState(), "request_received"), "authorized")
    state = reduce_state(state, "observation_refused", reason="phone observer unavailable")
    assert state.state == REFUSED and state.mutation_performed is False


def test_transport_loss_after_durable_dispatch() -> None:
    state = reduce_state(advance_to_dispatch(), "dispatch_outcome_unknown")
    assert state.state == UNKNOWN and state.dispatch_attempted is True and state.recovery_required is True
    expect_transition_error(lambda: reduce_state(state, "dispatch_confirmed"))
    recovered = recover_unknown(state, "recovery_observed_desired")
    assert recovered.state == RECOVERED and deployment_projection(recovered.state) == "error" and recovered.mutation_performed is True


def test_controller_rerun_after_intent_is_observation_only() -> None:
    unknown = DeploymentState(
        state=UNKNOWN, current_step="DISPATCH", intent_persisted=True, dispatch_attempted=True,
        mutation_performed=True, recovery_required=True,
        blocking_predicates=("durable_intent_exists_without_terminal",),
    )
    expect_transition_error(lambda: reduce_state(unknown, "dispatch_confirmed"))
    recovered = recover_unknown(unknown, "recovery_observed_desired")
    assert recovered.state == RECOVERED and recovered.postcondition_verified is True


def test_postcondition_failure() -> None:
    state = reduce_state(reduce_state(advance_to_dispatch(), "dispatch_confirmed"), "verify_mismatch")
    assert state.state == QUARANTINED and state.postcondition_verified is True and deployment_projection(state.state) == "failure"


def test_evidence_persistence_failure() -> None:
    before = reduce_state(reduce_state(DeploymentState(), "request_received"), "evidence_persistence_failed")
    assert before.state == REFUSED
    after = reduce_state(reduce_state(advance_to_dispatch(), "dispatch_confirmed"), "evidence_persistence_failed")
    assert after.state == UNKNOWN and after.recovery_required is True


def test_verified_noop_is_accepted_without_mutation() -> None:
    state = reduce_state(reduce_state(reduce_state(DeploymentState(), "request_received"), "authorized"), "already_desired")
    assert state.state == ACCEPTED and state.mutation_performed is False and state.postcondition_verified is True


def test_success_and_terminal_projection() -> None:
    state = reduce_state(reduce_state(advance_to_dispatch(), "dispatch_confirmed"), "verify_match")
    assert state.state == ACCEPTED and deployment_projection(state.state) == "success"
    terminal = DeploymentTerminal(
        operation="deploy-product-release", semantic_request_id="req-sha256:" + "1" * 64,
        execution_id="gh-run:123:1", controller_revision="a" * 40, target="phone-production",
        product_release="v0.1.4", release_id=1, release_source_sha="b" * 40,
        artifact_digest="b3:" + "c" * 64, deployment_id=2, state=ACCEPTED, current_step="VERIFY",
        facts={"installed_release":"v0.1.4"}, mutation_performed=True, postcondition_verified=True,
        next_allowed_operation="deploy-product-release", evidence_refs=("issue-comment:1",),
    ).to_dict()
    validate_terminal(terminal)
    assert terminal["deployment_projection"] == "success"


def test_refused_terminal_can_precede_release_resolution() -> None:
    terminal = DeploymentTerminal(
        operation="deploy-product-release", semantic_request_id="req-sha256:" + "2" * 64,
        execution_id="gh-run:124:1", controller_revision="a" * 40, target="phone-production",
        product_release="v0.1.4", release_id=None, release_source_sha=None, artifact_digest=None,
        deployment_id=None, state=REFUSED, current_step="AUTHORIZE",
        blocking_predicates=("release_not_admissible",), mutation_performed=False,
    ).to_dict()
    validate_terminal(terminal)
    assert terminal["deployment_projection"] == "failure"


def test_recovered_never_projects_success() -> None:
    assert deployment_projection(RECOVERED) == "error"
    assert deployment_projection(UNKNOWN) == "error"
    assert deployment_projection(QUARANTINED) == "failure"


def test_exactly_one_composite_destructive_adapter_callsite() -> None:
    path = GITHUB_DIR / "scripts" / "run_phone_release_deployment.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    composite = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dispatch_release_once"]
    direct_apk = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dispatch_install_once"]
    assert len(composite) == 1, f"expected one composite dispatch callsite, got {len(composite)}"
    assert direct_apk == [], "transaction owner must not bypass composite dispatch adapter"


def test_recovery_binds_full_release_identity_before_any_observation() -> None:
    source = (GITHUB_DIR / "scripts" / "run_phone_release_deployment.py").read_text(encoding="utf-8")
    recovery = source.split('if args.recovery_only == "true":', 1)[1].split('    if existing_intent is not None:', 1)[0]
    identity_check = recovery.index("payload_matches_release_identity")
    materialize = recovery.index("_materialize_verified_release_apk")
    observation = recovery.index("_observe_composite")
    assert identity_check < materialize < observation
    assert "recovery lacks matching full durable deployment admission" in recovery
    assert "dispatch_release_once(" not in recovery


def test_composite_postcondition_requires_apk_and_runtime() -> None:
    source = (GITHUB_DIR / "scripts" / "run_phone_release_deployment.py").read_text(encoding="utf-8")
    assert "apk_post.desired and runtime_post.desired" in source
    assert "apk_recovery.desired and runtime_recovery.desired" in source
    assert '"dispatch_operation": "android-release-composite-v1"' in source


def test_recovery_api_has_no_dispatch_parameter() -> None:
    import inspect
    signature = inspect.signature(recover_unknown)
    assert "dispatch" not in signature.parameters and "executor" not in signature.parameters


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"DEPLOYMENT_CONTROLLER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
