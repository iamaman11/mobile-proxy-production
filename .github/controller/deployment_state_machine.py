from __future__ import annotations

from dataclasses import dataclass, replace

REQUEST = "REQUEST"
AUTHORIZE = "AUTHORIZE"
OBSERVE = "OBSERVE"
INTENT = "INTENT"
DISPATCH = "DISPATCH"
VERIFY = "VERIFY"
RECOVERY = "RECOVERY"

ACCEPTED = "ACCEPTED"
REFUSED = "REFUSED"
UNKNOWN = "UNKNOWN"
RECOVERED = "RECOVERED"
QUARANTINED = "QUARANTINED"
TERMINALS = frozenset({ACCEPTED, REFUSED, UNKNOWN, RECOVERED, QUARANTINED})


class TransitionError(ValueError):
    pass


@dataclass(frozen=True)
class DeploymentState:
    state: str = REQUEST
    current_step: str = REQUEST
    intent_persisted: bool = False
    dispatch_attempted: bool = False
    mutation_performed: bool = False
    postcondition_verified: bool = False
    recovery_required: bool = False
    recovery_state: str | None = None
    blocking_predicates: tuple[str, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.state in TERMINALS


def _only(current: DeploymentState, *allowed: str) -> None:
    if current.state not in allowed:
        raise TransitionError(f"event is illegal from {current.state}")


def reduce_state(current: DeploymentState, event: str, *, reason: str | None = None) -> DeploymentState:
    """Pure deterministic deployment reducer.

    Physical adapters are outside this function. The reducer never retries a dispatch.
    An UNKNOWN terminal can only continue through the read-only recovery reducer below.
    """
    if current.terminal:
        raise TransitionError("terminal deployment state is immutable")

    if event == "request_received":
        _only(current, REQUEST)
        return replace(current, state=AUTHORIZE, current_step=AUTHORIZE)
    if event == "authorize_refused":
        _only(current, AUTHORIZE)
        return replace(current, state=REFUSED, current_step=AUTHORIZE, blocking_predicates=((reason or "authorization_refused"),))
    if event == "authorized":
        _only(current, AUTHORIZE)
        return replace(current, state=OBSERVE, current_step=OBSERVE)
    if event == "observation_refused":
        _only(current, OBSERVE)
        return replace(current, state=REFUSED, current_step=OBSERVE, blocking_predicates=((reason or "precondition_refused"),))
    if event == "already_desired":
        _only(current, OBSERVE)
        return replace(
            current,
            state=ACCEPTED,
            current_step=OBSERVE,
            mutation_performed=False,
            postcondition_verified=True,
            blocking_predicates=(),
        )
    if event == "observed":
        _only(current, OBSERVE)
        return replace(current, state=INTENT, current_step=INTENT)
    if event == "intent_persistence_failed":
        _only(current, INTENT)
        return replace(current, state=REFUSED, current_step=INTENT, blocking_predicates=((reason or "intent_not_durable"),))
    if event == "intent_persisted":
        _only(current, INTENT)
        if current.dispatch_attempted:
            raise TransitionError("dispatch already attempted before intent")
        return replace(current, state=DISPATCH, current_step=DISPATCH, intent_persisted=True)
    if event == "dispatch_confirmed":
        _only(current, DISPATCH)
        if not current.intent_persisted or current.dispatch_attempted:
            raise TransitionError("exactly-one dispatch invariant violated")
        return replace(current, state=VERIFY, current_step=VERIFY, dispatch_attempted=True, mutation_performed=True)
    if event == "dispatch_outcome_unknown":
        _only(current, DISPATCH)
        if not current.intent_persisted or current.dispatch_attempted:
            raise TransitionError("exactly-one dispatch invariant violated")
        return replace(
            current,
            state=UNKNOWN,
            current_step=DISPATCH,
            dispatch_attempted=True,
            mutation_performed=True,
            recovery_required=True,
            blocking_predicates=((reason or "dispatch_outcome_unknown"),),
        )
    if event == "verify_match":
        _only(current, VERIFY)
        if not current.dispatch_attempted:
            raise TransitionError("postcondition cannot precede dispatch")
        return replace(current, state=ACCEPTED, current_step=VERIFY, postcondition_verified=True)
    if event == "verify_mismatch":
        _only(current, VERIFY)
        return replace(
            current,
            state=QUARANTINED,
            current_step=VERIFY,
            postcondition_verified=True,
            recovery_required=True,
            blocking_predicates=((reason or "postcondition_mismatch"),),
        )
    if event == "verify_unavailable":
        _only(current, VERIFY)
        return replace(
            current,
            state=UNKNOWN,
            current_step=VERIFY,
            recovery_required=True,
            blocking_predicates=((reason or "postcondition_unavailable"),),
        )
    if event == "evidence_persistence_failed":
        if current.dispatch_attempted:
            return replace(
                current,
                state=UNKNOWN,
                current_step=current.current_step,
                recovery_required=True,
                blocking_predicates=((reason or "post_dispatch_evidence_not_durable"),),
            )
        return replace(
            current,
            state=REFUSED,
            current_step=current.current_step,
            blocking_predicates=((reason or "pre_dispatch_evidence_not_durable"),),
        )
    raise TransitionError(f"unknown reducer event: {event}")


def recover_unknown(current: DeploymentState, event: str, *, reason: str | None = None) -> DeploymentState:
    """Classify one UNKNOWN using observation only; no dispatch event exists here."""
    if current.state != UNKNOWN or not current.recovery_required:
        raise TransitionError("read-only recovery requires UNKNOWN terminal")
    if event == "recovery_observed_desired":
        return replace(
            current,
            state=RECOVERED,
            current_step=RECOVERY,
            postcondition_verified=True,
            recovery_required=False,
            recovery_state="desired_state_observed_after_unknown",
            blocking_predicates=(),
        )
    if event == "recovery_observed_other":
        return replace(
            current,
            state=QUARANTINED,
            current_step=RECOVERY,
            postcondition_verified=True,
            recovery_required=False,
            recovery_state="unexpected_state_observed_after_unknown",
            blocking_predicates=((reason or "recovery_postcondition_mismatch"),),
        )
    if event == "recovery_unavailable":
        return replace(
            current,
            state=UNKNOWN,
            current_step=RECOVERY,
            recovery_required=True,
            recovery_state="observation_unavailable",
            blocking_predicates=((reason or "recovery_observation_unavailable"),),
        )
    raise TransitionError("recovery is strictly read-only")


def deployment_projection(state: str) -> str:
    if state == ACCEPTED:
        return "success"
    if state in {REFUSED, QUARANTINED}:
        return "failure"
    if state in {UNKNOWN, RECOVERED}:
        return "error"
    return "in_progress"
