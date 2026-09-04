#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evidence_store import ADMISSION_HEADING, EvidenceRecord, INTENT_HEADING, TERMINAL_HEADING  # noqa: E402
from github_projection import PublicDeploymentMatch, PublicDeploymentProjection  # noqa: E402
from projection_admission import ProjectionAdmissionError, resolve_projection_admission  # noqa: E402
from projection_reconciler import ProjectionReconciliationError, decide_projection, reconcile_projection  # noqa: E402
from terminal_result import DeploymentTerminal  # noqa: E402


REQUEST_ID = "req-sha256:" + "a" * 64
EXECUTION_ID = "gh-run:123:1"
CONTROLLER_SHA = "b" * 40
SOURCE_SHA = "c" * 40
ARTIFACT_DIGEST = "b3:" + "d" * 64
DEPLOYMENT_ID = 77
PUBLIC_PAYLOAD = {
    "schema": "mobile-proxy-deployment-projection.v1",
    "product_release": "v0.1.4",
    "release_id": 382429107,
}


def admission_record(*, execution_id: str = EXECUTION_ID, deployment_id: int = DEPLOYMENT_ID) -> EvidenceRecord:
    return EvidenceRecord(
        10,
        ADMISSION_HEADING,
        {
            "schema": "production-deployment-admission.v2",
            "semantic_request_id": REQUEST_ID,
            "execution_id": execution_id,
            "controller_revision": CONTROLLER_SHA,
            "target": "phone-production",
            "product_release": "v0.1.4",
            "release_id": 382429107,
            "release_source_sha": SOURCE_SHA,
            "artifact_digest": ARTIFACT_DIGEST,
            "deployment_id": deployment_id,
            "initial_projection_state": "queued",
            "mutation_authority": False,
            "dispatch_authority": False,
            "mutation_performed": False,
        },
    )


def intent_record(*, deployment_id: int = DEPLOYMENT_ID) -> EvidenceRecord:
    return EvidenceRecord(
        11,
        INTENT_HEADING,
        {
            "schema": "production-deployment-intent.v2",
            "semantic_request_id": REQUEST_ID,
            "execution_id": EXECUTION_ID,
            "deployment_id": deployment_id,
            "dispatch_may_reach_target": True,
            "blind_retry_allowed": False,
            "mutation_performed": False,
        },
    )


def terminal_record(*, state: str, deployment_id: int = DEPLOYMENT_ID) -> EvidenceRecord:
    projection_postcondition = state in {"ACCEPTED", "RECOVERED", "QUARANTINED"}
    payload = DeploymentTerminal(
        operation="deploy-product-release",
        semantic_request_id=REQUEST_ID,
        execution_id=EXECUTION_ID,
        controller_revision=CONTROLLER_SHA,
        target="phone-production",
        product_release="v0.1.4",
        release_id=382429107,
        release_source_sha=SOURCE_SHA,
        artifact_digest=ARTIFACT_DIGEST,
        deployment_id=deployment_id,
        state=state,
        current_step="VERIFY" if state == "ACCEPTED" else "RECOVERY",
        facts={},
        blocking_predicates=(),
        mutation_performed=state in {"UNKNOWN", "RECOVERED", "QUARANTINED"},
        postcondition_verified=projection_postcondition,
        recovery_required=state == "UNKNOWN",
        recovery_state="test" if state in {"UNKNOWN", "RECOVERED", "QUARANTINED"} else None,
        next_allowed_operation="none",
        evidence_refs=("issue-comment:1",),
    ).to_dict()
    return EvidenceRecord(12, TERMINAL_HEADING, payload)


def public_match(*, deployment_id: int = DEPLOYMENT_ID, state: str | None = "queued") -> PublicDeploymentMatch:
    return PublicDeploymentMatch(
        deployment_id=deployment_id,
        source_sha=SOURCE_SHA,
        ref=SOURCE_SHA,
        environment="phone-production",
        payload=PUBLIC_PAYLOAD,
        latest_state=state,
    )


class FakeEvidence:
    def __init__(self, *, admission: EvidenceRecord | None = None, intent: EvidenceRecord | None = None, terminal: EvidenceRecord | None = None) -> None:
        self.admission = admission
        self.intent = intent
        self.terminal = terminal
        self.private_write_calls = 0

    def admission_for_execution(self, semantic_request_id: str, execution_id: str):
        if self.admission is None:
            return None
        if self.admission.payload.get("semantic_request_id") == semantic_request_id and self.admission.payload.get("execution_id") == execution_id:
            return self.admission
        return None

    def reusable_admission(self, semantic_request_id: str):
        if self.admission is not None and self.admission.payload.get("semantic_request_id") == semantic_request_id:
            return self.admission
        return None

    def request_history(self, semantic_request_id: str):
        return self.intent, self.terminal

    def persist_admission(self, *args, **kwargs):
        self.private_write_calls += 1
        raise AssertionError("projection reconciliation must not write private canonical evidence")

    def persist_intent(self, *args, **kwargs):
        self.private_write_calls += 1
        raise AssertionError("projection reconciliation must not write private canonical evidence")

    def persist_terminal(self, *args, **kwargs):
        self.private_write_calls += 1
        raise AssertionError("projection reconciliation must not write private canonical evidence")


class FakeProjection:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def status(self, *, deployment_id: int, state: str, description: str) -> int:
        self.calls.append({"deployment_id": deployment_id, "state": state, "description": description})
        if self.fail:
            raise RuntimeError("simulated public projection failure")
        return 9001


class FakeAdmissionProjection:
    def __init__(self, matches: list[PublicDeploymentMatch]) -> None:
        self.matches = tuple(matches)
        self.find_calls = 0
        self.create_calls: list[dict[str, object]] = []
        self.status_calls: list[dict[str, object]] = []

    def find_exact(self, **kwargs):
        self.find_calls += 1
        return self.matches

    def create(self, **kwargs) -> int:
        self.create_calls.append(dict(kwargs))
        return DEPLOYMENT_ID

    def status(self, **kwargs) -> int:
        self.status_calls.append(dict(kwargs))
        return 9001


class FakeLookupProjection(PublicDeploymentProjection):
    def __init__(self, deployments: list[object], statuses: list[object]) -> None:
        self.token = "test"
        self.deployments = deployments
        self.statuses = statuses
        self.calls: list[tuple[str, str]] = []

    def _request(self, path: str, *, method: str, payload=None):
        self.calls.append((path, method))
        if path.startswith("/deployments?"):
            return self.deployments
        if path.startswith(f"/deployments/{DEPLOYMENT_ID}/statuses?"):
            return self.statuses
        raise AssertionError("unexpected lookup path: " + path)


def test_pre_intent_target_failure_projects_error_never_success() -> None:
    decision = decide_projection(
        admission=admission_record(),
        semantic_request_id=REQUEST_ID,
        intent=None,
        terminal=None,
        target_job_result="failure",
    )
    assert decision.state == "error"
    assert decision.canonical_terminal is False
    assert "before durable mutation intent/terminal" in decision.description


def test_intent_without_terminal_projects_error_and_recovery_required() -> None:
    decision = decide_projection(
        admission=admission_record(),
        semantic_request_id=REQUEST_ID,
        intent=intent_record(),
        terminal=None,
        target_job_result="failure",
    )
    assert decision.state == "error"
    assert decision.canonical_terminal is False
    assert "recovery required" in decision.description


def test_canonical_accepted_terminal_is_only_success_source() -> None:
    decision = decide_projection(
        admission=admission_record(),
        semantic_request_id=REQUEST_ID,
        intent=intent_record(),
        terminal=terminal_record(state="ACCEPTED"),
        target_job_result="success",
    )
    assert decision.state == "success"
    assert decision.canonical_terminal is True
    assert decision.description == "canonical controller terminal: ACCEPTED"


def test_canonical_refused_terminal_projects_failure() -> None:
    decision = decide_projection(
        admission=admission_record(),
        semantic_request_id=REQUEST_ID,
        intent=None,
        terminal=terminal_record(state="REFUSED"),
        target_job_result="failure",
    )
    assert decision.state == "failure"
    assert decision.canonical_terminal is True


def test_recovered_terminal_never_projects_original_success() -> None:
    decision = decide_projection(
        admission=admission_record(),
        semantic_request_id=REQUEST_ID,
        intent=intent_record(),
        terminal=terminal_record(state="RECOVERED"),
        target_job_result="success",
    )
    assert decision.state == "error"
    assert decision.canonical_terminal is True


def test_terminal_with_different_public_deployment_fails_closed() -> None:
    try:
        decide_projection(
            admission=admission_record(),
            semantic_request_id=REQUEST_ID,
            intent=None,
            terminal=terminal_record(state="ACCEPTED", deployment_id=78),
            target_job_result="success",
        )
    except ProjectionReconciliationError as exc:
        assert "public Deployment id differs" in str(exc)
    else:
        raise AssertionError("mismatched public Deployment terminal was unexpectedly accepted")


def test_public_projection_failure_cannot_rewrite_private_canonical_evidence() -> None:
    evidence = FakeEvidence(admission=admission_record(), intent=None, terminal=None)
    projection = FakeProjection(fail=True)
    try:
        reconcile_projection(
            evidence=evidence,
            projection=projection,
            semantic_request_id=REQUEST_ID,
            execution_id=EXECUTION_ID,
            target_job_result="failure",
        )
    except RuntimeError as exc:
        assert "simulated public projection failure" in str(exc)
    else:
        raise AssertionError("simulated public projection failure unexpectedly succeeded")
    assert evidence.private_write_calls == 0
    assert projection.calls[0]["state"] == "error"


def test_public_lookup_filters_exact_identity_and_reads_latest_state() -> None:
    other_payload = dict(PUBLIC_PAYLOAD)
    other_payload["release_id"] = 1
    projection = FakeLookupProjection(
        deployments=[
            {
                "id": DEPLOYMENT_ID,
                "sha": SOURCE_SHA,
                "ref": SOURCE_SHA,
                "environment": "phone-production",
                "payload": PUBLIC_PAYLOAD,
            },
            {
                "id": 88,
                "sha": SOURCE_SHA,
                "ref": SOURCE_SHA,
                "environment": "phone-production",
                "payload": other_payload,
            },
        ],
        statuses=[
            {"id": 100, "state": "queued"},
            {"id": 101, "state": "in_progress"},
        ],
    )
    matches = projection.find_exact(
        source_sha=SOURCE_SHA,
        environment="phone-production",
        release_tag="v0.1.4",
        release_id=382429107,
    )
    assert len(matches) == 1
    assert matches[0].deployment_id == DEPLOYMENT_ID
    assert matches[0].latest_state == "in_progress"
    assert projection.calls[0][1] == "GET"
    assert projection.calls[1][1] == "GET"


def test_zero_match_creates_exactly_one_and_queues() -> None:
    projection = FakeAdmissionProjection([])
    decision = resolve_projection_admission(
        projection=projection,
        source_sha=SOURCE_SHA,
        environment="phone-production",
        release_tag="v0.1.4",
        release_id=382429107,
        durable_deployment_id=None,
    )
    assert decision.deployment_id == DEPLOYMENT_ID
    assert decision.reused is False
    assert projection.find_calls == 1
    assert len(projection.create_calls) == 1
    assert projection.status_calls == [{
        "deployment_id": DEPLOYMENT_ID,
        "state": "queued",
        "description": "v0.1.4 admitted by production controller",
    }]


def test_one_matching_safe_projection_reuses_without_public_write() -> None:
    projection = FakeAdmissionProjection([public_match(state="in_progress")])
    decision = resolve_projection_admission(
        projection=projection,
        source_sha=SOURCE_SHA,
        environment="phone-production",
        release_tag="v0.1.4",
        release_id=382429107,
        durable_deployment_id=DEPLOYMENT_ID,
    )
    assert decision.reused is True
    assert decision.observed_state == "in_progress"
    assert projection.create_calls == []
    assert projection.status_calls == []


def test_multiple_exact_projection_matches_refuse_before_create() -> None:
    projection = FakeAdmissionProjection([
        public_match(deployment_id=DEPLOYMENT_ID),
        public_match(deployment_id=78),
    ])
    try:
        resolve_projection_admission(
            projection=projection,
            source_sha=SOURCE_SHA,
            environment="phone-production",
            release_tag="v0.1.4",
            release_id=382429107,
            durable_deployment_id=None,
        )
    except ProjectionAdmissionError as exc:
        assert "multiple exact public Deployments" in str(exc)
    else:
        raise AssertionError("multiple exact public Deployments were unexpectedly accepted")
    assert projection.create_calls == []
    assert projection.status_calls == []


def test_ack_only_queued_projection_reuses_without_durable_admission() -> None:
    projection = FakeAdmissionProjection([public_match(state="queued")])
    decision = resolve_projection_admission(
        projection=projection,
        source_sha=SOURCE_SHA,
        environment="phone-production",
        release_tag="v0.1.4",
        release_id=382429107,
        durable_deployment_id=None,
    )
    assert decision.deployment_id == DEPLOYMENT_ID
    assert decision.reused is True
    assert projection.create_calls == []
    assert projection.status_calls == []


def test_public_success_without_private_terminal_fails_closed() -> None:
    projection = FakeAdmissionProjection([public_match(state="success")])
    try:
        resolve_projection_admission(
            projection=projection,
            source_sha=SOURCE_SHA,
            environment="phone-production",
            release_tag="v0.1.4",
            release_id=382429107,
            durable_deployment_id=None,
        )
    except ProjectionAdmissionError as exc:
        assert "missing private canonical terminal" in str(exc)
    else:
        raise AssertionError("public success without private terminal was unexpectedly accepted")
    assert projection.create_calls == []
    assert projection.status_calls == []


def test_prepare_has_no_direct_create_bypass_around_read_before_create() -> None:
    source = (ROOT.parent / "scripts" / "prepare_release_deployment.py").read_text(encoding="utf-8")
    assert "resolve_projection_admission(" in source
    assert "projection.create(" not in source


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"PROJECTION_RECONCILER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
