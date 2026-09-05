#!/usr/bin/env python3
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GITHUB_DIR = ROOT.parent
sys.path.insert(0, str(ROOT))

from deployment_request import RequestProvenance, build_deployment_request
from deployment_state_machine import (
    ACCEPTED, QUARANTINED, RECOVERED, REFUSED, UNKNOWN,
    DeploymentState, TransitionError, deployment_projection, recover_unknown, reduce_state,
)
from terminal_result import DeploymentTerminal, validate_terminal


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
    assert first.request_id == duplicate_comment.request_id
    assert first.request_id != other_release.request_id
    assert "authority_cursor" not in first.to_dict()


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
