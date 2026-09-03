#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deployment_request import RequestProvenance, build_deployment_request
from deployment_state_machine import (
    ACCEPTED,
    QUARANTINED,
    RECOVERED,
    REFUSED,
    UNKNOWN,
    DeploymentState,
    TransitionError,
    deployment_projection,
    recover_unknown,
    reduce_state,
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


def test_request_identity() -> None:
    first = build_deployment_request(
        target="phone-production",
        product_release_tag="v0.1.4",
        provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 100, "iamaman11"),
    )
    duplicate_comment = build_deployment_request(
        target="phone-production",
        product_release_tag="v0.1.4",
        provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 999, "iamaman11"),
    )
    other_release = build_deployment_request(
        target="phone-production",
        product_release_tag="v0.1.5",
        provenance=RequestProvenance("iamaman11/mobile-proxy-production", 1, 101, "iamaman11"),
    )
    assert first.request_id == duplicate_comment.request_id
    assert first.request_id != other_release.request_id
    assert "authority_cursor" not in first.to_dict()


def test_failure_before_dispatch() -> None:
    state = DeploymentState()
    state = reduce_state(state, "request_received")
    state = reduce_state(state, "authorized")
    state = reduce_state(state, "observed")
    state = reduce_state(state, "intent_persistence_failed", reason="durable intent write failed")
    assert state.state == REFUSED
    assert state.dispatch_attempted is False
    assert state.mutation_performed is False


def test_transport_loss_before_dispatch() -> None:
    state = DeploymentState()
    state = reduce_state(state, "request_received")
    state = reduce_state(state, "authorized")
    state = reduce_state(state, "observation_refused", reason="phone observer unavailable")
    assert state.state == REFUSED
    assert state.mutation_performed is False


def test_transport_loss_after_durable_dispatch() -> None:
    state = advance_to_dispatch()
    state = reduce_state(state, "dispatch_outcome_unknown")
    assert state.state == UNKNOWN
    assert state.dispatch_attempted is True
    assert state.recovery_required is True
    expect_transition_error(lambda: reduce_state(state, "dispatch_confirmed"))
    recovered = recover_unknown(state, "recovery_observed_desired")
    assert recovered.state == RECOVERED
    assert deployment_projection(recovered.state) == "error"
    assert recovered.mutation_performed is True


def test_postcondition_failure() -> None:
    state = reduce_state(advance_to_dispatch(), "dispatch_confirmed")
    state = reduce_state(state, "verify_mismatch")
    assert state.state == QUARANTINED
    assert state.postcondition_verified is True
    assert deployment_projection(state.state) == "failure"


def test_evidence_persistence_failure() -> None:
    before = DeploymentState()
    before = reduce_state(before, "request_received")
    before = reduce_state(before, "evidence_persistence_failed")
    assert before.state == REFUSED

    after = reduce_state(advance_to_dispatch(), "dispatch_confirmed")
    after = reduce_state(after, "evidence_persistence_failed")
    assert after.state == UNKNOWN
    assert after.recovery_required is True


def test_success_and_terminal_projection() -> None:
    state = reduce_state(advance_to_dispatch(), "dispatch_confirmed")
    state = reduce_state(state, "verify_match")
    assert state.state == ACCEPTED
    assert deployment_projection(state.state) == "success"

    terminal = DeploymentTerminal(
        operation="deploy-product-release",
        semantic_request_id="req-sha256:" + "1" * 64,
        execution_id="gh-run:123:1",
        controller_revision="a" * 40,
        target="phone-production",
        product_release="v0.1.4",
        release_id=1,
        release_source_sha="b" * 40,
        artifact_digest="c" * 64,
        deployment_id=2,
        state=ACCEPTED,
        current_step="VERIFY",
        facts={"installed_release": "v0.1.4"},
        mutation_performed=True,
        postcondition_verified=True,
        next_allowed_operation="deploy-product-release",
        evidence_refs=("issue-comment:1",),
    )
    value = terminal.to_dict()
    validate_terminal(value)
    assert value["deployment_projection"] == "success"


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"DEPLOYMENT_CONTROLLER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
