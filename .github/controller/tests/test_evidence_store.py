#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evidence_store import (  # noqa: E402
    EvidenceError,
    EvidenceRecord,
    EvidenceTransportError,
    EvidenceWriteAmbiguous,
    INTENT_HEADING,
    IssueEvidenceStore,
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


def terminal_payload(*, state: str = "REFUSED") -> dict[str, object]:
    return {
        "schema": "production-deployment-terminal.v2",
        "semantic_request_id": "req-sha256:" + "a" * 64,
        "execution_id": "gh-run:123:1",
        "state": state,
        "mutation_performed": False,
    }


def test_get_transport_transient_failure_recovers_with_bounded_retry() -> None:
    store = IssueEvidenceStore("test-token")
    calls: list[str] = []
    sleeps: list[float] = []
    sentinel = object()
    original_urlopen = urllib.request.urlopen
    original_sleep = time.sleep

    def fake_urlopen(request, timeout=30):
        calls.append(request.get_method())
        if len(calls) < 3:
            raise urllib.error.URLError("transient TLS EOF")
        return sentinel

    try:
        urllib.request.urlopen = fake_urlopen
        time.sleep = sleeps.append
        result = store._open("https://api.github.com/example")
    finally:
        urllib.request.urlopen = original_urlopen
        time.sleep = original_sleep

    assert result is sentinel
    assert calls == ["GET", "GET", "GET"]
    assert sleeps == [0.25, 0.5]


def test_get_transport_exhaustion_fails_after_exact_bound() -> None:
    store = IssueEvidenceStore("test-token")
    calls: list[str] = []
    sleeps: list[float] = []
    original_urlopen = urllib.request.urlopen
    original_sleep = time.sleep

    def fake_urlopen(request, timeout=30):
        calls.append(request.get_method())
        raise urllib.error.URLError("persistent TLS EOF")

    try:
        urllib.request.urlopen = fake_urlopen
        time.sleep = sleeps.append
        try:
            store._open("https://api.github.com/example")
        except EvidenceTransportError:
            pass
        else:
            raise AssertionError("exhausted GET transport unexpectedly succeeded")
    finally:
        urllib.request.urlopen = original_urlopen
        time.sleep = original_sleep

    assert calls == ["GET", "GET", "GET"]
    assert sleeps == [0.25, 0.5]


def test_post_transport_failure_has_zero_automatic_retry() -> None:
    store = IssueEvidenceStore("test-token")
    calls: list[str] = []
    sleeps: list[float] = []
    original_urlopen = urllib.request.urlopen
    original_sleep = time.sleep

    def fake_urlopen(request, timeout=30):
        calls.append(request.get_method())
        raise urllib.error.URLError("ambiguous POST transport failure")

    try:
        urllib.request.urlopen = fake_urlopen
        time.sleep = sleeps.append
        try:
            store.create(TERMINAL_HEADING, terminal_payload())
        except EvidenceWriteAmbiguous:
            pass
        else:
            raise AssertionError("ambiguous POST transport unexpectedly succeeded")
    finally:
        urllib.request.urlopen = original_urlopen
        time.sleep = original_sleep

    assert calls == ["POST"]
    assert sleeps == []


def test_terminal_identity_is_deterministic() -> None:
    first = terminal_payload()
    reordered = dict(reversed(list(first.items())))
    assert evidence_identity(TERMINAL_HEADING, first) == evidence_identity(TERMINAL_HEADING, reordered)
    assert evidence_identity(TERMINAL_HEADING, first) != evidence_identity(INTENT_HEADING, first)


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
