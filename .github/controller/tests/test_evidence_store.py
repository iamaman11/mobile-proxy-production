#!/usr/bin/env python3
from __future__ import annotations

import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from durable_release_identity import PHONE_RUNTIME_IDENTITY_FIELDS  # noqa: E402
from evidence_store import (  # noqa: E402
    ADMISSION_HEADING,
    EvidenceError,
    EvidenceRecord,
    EvidenceTransportError,
    EvidenceWriteAmbiguous,
    INTENT_HEADING,
    IssueEvidenceStore,
    READ_TRANSPORT_RETRY_DELAYS_SECONDS,
    TERMINAL_HEADING,
    evidence_identity,
)


class FakeStore(IssueEvidenceStore):
    def __init__(
        self,
        *,
        initial: list[EvidenceRecord] | None = None,
        create_outcomes: list[str] | None = None,
    ) -> None:
        self.token = "test-token"
        self.records = list(initial or [])
        self.create_outcomes = list(create_outcomes or [])
        self.create_calls = 0
        self.read_calls = 0
        self.next_comment_id = 1000

    def list_records(self, heading: str) -> list[EvidenceRecord]:
        self.read_calls += 1
        return [record for record in self.records if record.heading == heading]

    def create(self, heading: str, payload: dict[str, object]) -> EvidenceRecord:
        self.create_calls += 1
        outcome = self.create_outcomes.pop(0) if self.create_outcomes else "success"
        if outcome == "pre_commit_failure":
            raise EvidenceWriteAmbiguous("simulated pre-commit transport failure")
        record = EvidenceRecord(self.next_comment_id, heading, dict(payload))
        self.next_comment_id += 1
        self.records.append(record)
        if outcome == "response_loss":
            raise EvidenceWriteAmbiguous("simulated response loss after commit")
        if outcome != "success":
            raise AssertionError(f"unknown fake outcome: {outcome}")
        return record


class FakeResponse:
    def close(self) -> None:
        pass

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def request_id() -> str:
    return "req-sha256:" + "a" * 64


def runtime_identity(*, digest_char: str = "e") -> dict[str, object]:
    return {
        "phone_runtime_artifact_name": "mobile-proxy-phone-production-runtime-v0.1.4.tar.gz",
        "phone_runtime_artifact_digest": "b3:" + digest_char * 64,
        "phone_runtime_inventory_path": "phone-production-runtime/components.json",
        "phone_runtime_inventory_digest": "b3:" + "f" * 64,
    }


def admission_payload(
    *,
    execution_id: str = "gh-run:123:1",
    deployment_id: int = 77,
    target: str = "phone-production",
    include_runtime: bool | None = None,
) -> dict[str, object]:
    if include_runtime is None:
        include_runtime = target == "phone-production"
    payload: dict[str, object] = {
        "schema": "production-deployment-admission.v2",
        "semantic_request_id": request_id(),
        "execution_id": execution_id,
        "controller_revision": "b" * 40,
        "target": target,
        "product_release": "v0.1.4",
        "release_id": 382429107,
        "release_source_sha": "c" * 40,
        "artifact_digest": "b3:" + "d" * 64,
        "deployment_id": deployment_id,
        "initial_projection_state": "queued",
        "mutation_authority": False,
        "dispatch_authority": False,
        "mutation_performed": False,
    }
    if include_runtime:
        payload.update(runtime_identity())
    return payload


def intent_payload(
    *,
    execution_id: str = "gh-run:123:1",
    target: str = "phone-production",
    include_runtime: bool | None = None,
) -> dict[str, object]:
    if include_runtime is None:
        include_runtime = target == "phone-production"
    payload: dict[str, object] = {
        "schema": "production-deployment-intent.v2",
        "semantic_request_id": request_id(),
        "execution_id": execution_id,
        "controller_revision": "b" * 40,
        "target": target,
        "target_binding_id": "android-target-binding:test",
        "product_release": "v0.1.4",
        "release_id": 382429107,
        "release_source_sha": "c" * 40,
        "artifact_digest": "b3:" + "d" * 64,
        "deployment_id": 77,
        "dispatch_operation": "adb-install-replace",
        "dispatch_may_reach_target": True,
        "blind_retry_allowed": False,
        "mutation_performed": False,
    }
    if include_runtime:
        payload.update(runtime_identity())
    return payload


def terminal_payload(*, state: str = "REFUSED") -> dict[str, object]:
    return {
        "schema": "production-deployment-terminal.v2",
        "semantic_request_id": request_id(),
        "execution_id": "gh-run:123:1",
        "state": state,
        "mutation_performed": False,
    }


def test_terminal_identity_is_deterministic() -> None:
    first = terminal_payload()
    reordered = dict(reversed(list(first.items())))
    assert evidence_identity(TERMINAL_HEADING, first) == evidence_identity(TERMINAL_HEADING, reordered)
    assert evidence_identity(TERMINAL_HEADING, first) != evidence_identity(INTENT_HEADING, first)


def test_transient_get_transport_failure_recovers_with_bounded_retry() -> None:
    outcomes: list[object] = [
        urllib.error.URLError("transient-1"),
        urllib.error.URLError("transient-2"),
        FakeResponse(),
    ]

    def fake_urlopen(*args, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    sleeps: list[float] = []
    store = IssueEvidenceStore("test-token")
    with patch("evidence_store.urllib.request.urlopen", side_effect=fake_urlopen) as opened:
        with patch("evidence_store.time.sleep", side_effect=sleeps.append):
            response = store._open("https://api.github.test/read")
    assert isinstance(response, FakeResponse)
    assert opened.call_count == 3
    assert sleeps == list(READ_TRANSPORT_RETRY_DELAYS_SECONDS[:2])


def test_exhausted_get_transport_retries_fail_closed() -> None:
    sleeps: list[float] = []
    store = IssueEvidenceStore("test-token")
    with patch(
        "evidence_store.urllib.request.urlopen",
        side_effect=urllib.error.URLError("still-offline"),
    ) as opened:
        with patch("evidence_store.time.sleep", side_effect=sleeps.append):
            try:
                store._open("https://api.github.test/read")
            except EvidenceTransportError as exc:
                assert "transport failed" in str(exc)
            else:
                raise AssertionError("exhausted GET transport retries unexpectedly succeeded")
    assert opened.call_count == len(READ_TRANSPORT_RETRY_DELAYS_SECONDS) + 1
    assert sleeps == list(READ_TRANSPORT_RETRY_DELAYS_SECONDS)


def test_http_rejection_is_not_retried() -> None:
    store = IssueEvidenceStore("test-token")
    error = urllib.error.HTTPError(
        "https://api.github.test/read",
        403,
        "forbidden",
        hdrs=None,
        fp=None,
    )
    with patch("evidence_store.urllib.request.urlopen", side_effect=error) as opened:
        with patch("evidence_store.time.sleep") as slept:
            try:
                store._open("https://api.github.test/read")
            except EvidenceError as exc:
                assert "HTTP 403" in str(exc)
            else:
                raise AssertionError("HTTP rejection unexpectedly succeeded")
    assert opened.call_count == 1
    slept.assert_not_called()


def test_post_transport_ambiguity_is_not_blindly_retried_by_open() -> None:
    store = IssueEvidenceStore("test-token")
    with patch(
        "evidence_store.urllib.request.urlopen",
        side_effect=urllib.error.URLError("write-response-unknown"),
    ) as opened:
        with patch("evidence_store.time.sleep") as slept:
            try:
                store.create(TERMINAL_HEADING, terminal_payload())
            except EvidenceWriteAmbiguous as exc:
                assert "write outcome is ambiguous" in str(exc)
            else:
                raise AssertionError("ambiguous POST unexpectedly succeeded")
    assert opened.call_count == 1
    slept.assert_not_called()


def test_admission_is_non_mutating_and_idempotent_for_one_execution() -> None:
    store = FakeStore()
    payload = admission_payload()
    first = store.persist_admission(payload)
    second = store.persist_admission(dict(payload))
    assert first.comment_id == second.comment_id
    assert first.heading == ADMISSION_HEADING
    assert first.payload["mutation_authority"] is False
    assert first.payload["dispatch_authority"] is False
    assert store.create_calls == 1


def test_admission_can_link_later_execution_to_same_public_deployment() -> None:
    store = FakeStore()
    first = store.persist_admission(admission_payload(execution_id="gh-run:123:1"))
    second = store.persist_admission(admission_payload(execution_id="gh-run:124:1"))
    reusable = store.reusable_admission(request_id())
    assert first.comment_id != second.comment_id
    assert reusable is not None
    assert reusable.payload["deployment_id"] == 77
    assert store.create_calls == 2


def test_conflicting_admission_public_deployment_fails_closed() -> None:
    store = FakeStore()
    store.persist_admission(admission_payload(deployment_id=77))
    try:
        store.persist_admission(admission_payload(execution_id="gh-run:124:1", deployment_id=78))
    except EvidenceError as exc:
        assert "different public Deployment admission" in str(exc) or "conflicting durable deployment admissions" in str(exc)
    else:
        raise AssertionError("conflicting public Deployment admission was unexpectedly accepted")
    assert store.create_calls == 1


def test_admission_cannot_grant_dispatch_authority() -> None:
    store = FakeStore()
    payload = admission_payload()
    payload["dispatch_authority"] = True
    try:
        store.persist_admission(payload)
    except EvidenceError as exc:
        assert "must not grant dispatch authority" in str(exc)
    else:
        raise AssertionError("dispatch-authorizing admission was unexpectedly accepted")
    assert store.create_calls == 0


def test_admission_response_loss_reconciles_without_duplicate_write() -> None:
    store = FakeStore(create_outcomes=["response_loss"])
    payload = admission_payload()
    record = store.persist_admission(payload)
    assert record.identity == evidence_identity(ADMISSION_HEADING, payload)
    assert store.create_calls == 1
    admissions = [item for item in store.records if item.heading == ADMISSION_HEADING]
    assert len(admissions) == 1


def test_historical_phone_admission_without_runtime_identity_remains_readable() -> None:
    legacy = admission_payload(include_runtime=False)
    record = EvidenceRecord(40, ADMISSION_HEADING, legacy)
    store = FakeStore(initial=[record])
    reusable = store.reusable_admission(request_id())
    assert reusable is not None
    assert reusable.comment_id == 40
    assert all(field not in reusable.payload for field in PHONE_RUNTIME_IDENTITY_FIELDS)


def test_historical_phone_admission_cannot_authorize_current_full_identity() -> None:
    legacy = EvidenceRecord(41, ADMISSION_HEADING, admission_payload(include_runtime=False))
    store = FakeStore(initial=[legacy])
    try:
        store.persist_admission(admission_payload(execution_id="gh-run:124:1"))
    except EvidenceError as exc:
        assert "different public Deployment admission" in str(exc)
    else:
        raise AssertionError("legacy APK-only admission unexpectedly authorized current phone identity")
    assert store.create_calls == 0


def test_current_phone_admission_requires_runtime_identity() -> None:
    store = FakeStore()
    try:
        store.persist_admission(admission_payload(include_runtime=False))
    except EvidenceError as exc:
        assert "lacks rooted-phone runtime identity" in str(exc)
    else:
        raise AssertionError("new APK-only phone admission unexpectedly persisted")
    assert store.create_calls == 0


def test_runtime_identity_change_breaks_admission_reuse() -> None:
    store = FakeStore()
    store.persist_admission(admission_payload())
    changed = admission_payload(execution_id="gh-run:124:1")
    changed["phone_runtime_artifact_digest"] = "b3:" + "9" * 64
    try:
        store.persist_admission(changed)
    except EvidenceError as exc:
        assert "different public Deployment admission" in str(exc)
    else:
        raise AssertionError("changed phone runtime identity unexpectedly reused durable admission")
    assert store.create_calls == 1


def test_vm_admission_is_runtime_free_and_rejects_phone_identity_leakage() -> None:
    store = FakeStore()
    vm = admission_payload(target="vm-production", include_runtime=False)
    record = store.persist_admission(vm)
    assert all(field not in record.payload for field in PHONE_RUNTIME_IDENTITY_FIELDS)

    leaked = admission_payload(
        execution_id="gh-run:124:1",
        target="vm-production",
        include_runtime=True,
    )
    try:
        store.persist_admission(leaked)
    except EvidenceError as exc:
        assert "leaked to another target" in str(exc)
    else:
        raise AssertionError("phone runtime identity unexpectedly leaked into VM admission")


def test_current_phone_intent_requires_runtime_identity() -> None:
    store = FakeStore()
    try:
        store.persist_intent(intent_payload(include_runtime=False))
    except EvidenceError as exc:
        assert "lacks rooted-phone runtime identity" in str(exc)
    else:
        raise AssertionError("new APK-only phone mutation intent unexpectedly persisted")
    assert store.create_calls == 0


def test_intent_runtime_mismatch_fails_closed() -> None:
    store = FakeStore()
    first = store.persist_intent(intent_payload())
    changed = intent_payload(execution_id="gh-run:124:1")
    changed["phone_runtime_inventory_digest"] = "b3:" + "8" * 64
    try:
        store.persist_intent(changed)
    except EvidenceError as exc:
        assert "different durable mutation intent" in str(exc)
    else:
        raise AssertionError("changed phone runtime identity unexpectedly reused mutation intent")
    assert first.comment_id == 1000
    assert store.create_calls == 1


def test_historical_intent_is_readable_but_not_reusable_for_current_phone_identity() -> None:
    legacy_payload = intent_payload(include_runtime=False)
    legacy = EvidenceRecord(42, INTENT_HEADING, legacy_payload)
    store = FakeStore(initial=[legacy])
    existing, terminal = store.request_history(request_id())
    assert existing is not None and existing.comment_id == 42
    assert terminal is None
    try:
        store.persist_intent(intent_payload(execution_id="gh-run:124:1"))
    except EvidenceError as exc:
        assert "different durable mutation intent" in str(exc)
    else:
        raise AssertionError("legacy APK-only intent unexpectedly reused for current phone identity")
    assert store.create_calls == 0


def test_normal_terminal_write_success() -> None:
    store = FakeStore(create_outcomes=["success"])
    payload = terminal_payload()
    record = store.persist_terminal(payload)
    assert record.identity == evidence_identity(TERMINAL_HEADING, payload)
    assert store.create_calls == 1


def test_pre_commit_transport_failure_reconciles_before_bounded_retry() -> None:
    store = FakeStore(create_outcomes=["pre_commit_failure", "success"])
    payload = terminal_payload()
    record = store.persist_terminal(payload)
    assert record.identity == evidence_identity(TERMINAL_HEADING, payload)
    assert store.create_calls == 2
    assert store.read_calls >= 4
    terminals = [item for item in store.records if item.heading == TERMINAL_HEADING]
    assert len(terminals) == 1


def test_post_commit_response_loss_accepts_matching_existing_terminal_without_second_write() -> None:
    store = FakeStore(create_outcomes=["response_loss"])
    payload = terminal_payload()
    record = store.persist_terminal(payload)
    assert record.identity == evidence_identity(TERMINAL_HEADING, payload)
    assert store.create_calls == 1
    terminals = [item for item in store.records if item.heading == TERMINAL_HEADING]
    assert len(terminals) == 1


def test_matching_existing_terminal_is_idempotent() -> None:
    payload = terminal_payload()
    existing = EvidenceRecord(55, TERMINAL_HEADING, dict(payload))
    store = FakeStore(initial=[existing])
    record = store.persist_terminal(payload)
    assert record.comment_id == 55
    assert store.create_calls == 0


def test_conflicting_existing_terminal_fails_closed() -> None:
    existing = EvidenceRecord(56, TERMINAL_HEADING, terminal_payload(state="UNKNOWN"))
    store = FakeStore(initial=[existing])
    try:
        store.persist_terminal(terminal_payload(state="REFUSED"))
    except EvidenceError as exc:
        assert "different canonical terminal" in str(exc)
    else:
        raise AssertionError("conflicting durable terminal was unexpectedly accepted")
    assert store.create_calls == 0


def test_second_ambiguous_write_without_observed_terminal_fails_closed() -> None:
    store = FakeStore(create_outcomes=["pre_commit_failure", "pre_commit_failure"])
    try:
        store.persist_terminal(terminal_payload())
    except EvidenceError as exc:
        assert "canonical terminal remains absent" in str(exc)
    else:
        raise AssertionError("unreconciled terminal ambiguity was unexpectedly accepted")
    assert store.create_calls == 2


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"EVIDENCE_STORE_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
