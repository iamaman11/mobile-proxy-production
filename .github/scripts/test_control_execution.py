from __future__ import annotations

import copy
import json
import unittest

from control_execution import (
    ExecutionContractError,
    build_execution_ack,
    execution_id,
    render_execution_ack,
    validate_execution_ack,
)


PRIVATE_SHA = "8" * 40
REQUEST_ID = "req-sha256:" + "1" * 64
GENERATION = "gen-sha256:" + "2" * 64


def request(*, comment_id: int = 101, mutating: bool = False) -> dict[str, object]:
    return {
        "schema": "production-control-request.v1",
        "request_id": REQUEST_ID,
        "operation": "phone-preflight",
        "arguments": ["3" * 40],
        "authority_cursor": "issue179-comment-5531491187",
        "desired_generation": GENERATION,
        "mutating": mutating,
        "provenance": {
            "repository": "iamaman11/mobile-proxy-production",
            "issue_number": 1,
            "comment_id": comment_id,
            "actor": "iamaman11",
            "event_name": "issue_comment",
        },
    }


class ExecutionIdentityTests(unittest.TestCase):
    def test_run_attempt_is_exact_execution_identity(self) -> None:
        self.assertEqual(execution_id(33807528604, 1), "gh-run:33807528604:1")
        self.assertNotEqual(execution_id(33807528604, 1), execution_id(33807528604, 2))
        self.assertNotEqual(execution_id(33807528604, 1), execution_id(33807528605, 1))

    def test_same_semantic_request_can_have_distinct_execution_attempts(self) -> None:
        first = build_execution_ack(
            request(comment_id=101), run_id=700, run_attempt=1, private_sha=PRIVATE_SHA
        )
        rerun = build_execution_ack(
            request(comment_id=101), run_id=700, run_attempt=2, private_sha=PRIVATE_SHA
        )
        later = build_execution_ack(
            request(comment_id=102), run_id=701, run_attempt=1, private_sha=PRIVATE_SHA
        )
        self.assertEqual(first["semantic_request_id"], rerun["semantic_request_id"])
        self.assertEqual(first["semantic_request_id"], later["semantic_request_id"])
        self.assertNotEqual(first["execution_id"], rerun["execution_id"])
        self.assertNotEqual(first["execution_id"], later["execution_id"])
        self.assertEqual(first["source_comment_id"], 101)
        self.assertEqual(later["source_comment_id"], 102)

    def test_ack_is_non_terminal_and_does_not_invent_physical_transaction(self) -> None:
        record = build_execution_ack(
            request(), run_id=700, run_attempt=1, private_sha=PRIVATE_SHA
        )
        self.assertEqual(record["state"], "ROUTED")
        self.assertIs(record["terminal"], False)
        self.assertIs(record["mutating"], False)
        self.assertNotIn("physical_transaction_id", record)
        self.assertEqual(record["requested_canonical_sha"], "3" * 40)
        self.assertNotIn("canonical_sha", record)

    def test_mutating_semantics_do_not_turn_execution_id_into_transaction_id(self) -> None:
        record = build_execution_ack(
            request(mutating=True), run_id=700, run_attempt=1, private_sha=PRIVATE_SHA
        )
        self.assertIs(record["mutating"], True)
        self.assertNotIn("physical_transaction_id", record)
        self.assertEqual(record["execution_id"], "gh-run:700:1")

    def test_ack_validation_fails_closed_on_identity_mismatch(self) -> None:
        record = build_execution_ack(
            request(), run_id=700, run_attempt=1, private_sha=PRIVATE_SHA
        )
        broken = copy.deepcopy(record)
        broken["execution_id"] = "gh-run:700:2"
        with self.assertRaises(ExecutionContractError):
            validate_execution_ack(broken)

    def test_renderer_contains_bounded_machine_readable_record(self) -> None:
        record = build_execution_ack(
            request(), run_id=700, run_attempt=1, private_sha=PRIVATE_SHA
        )
        rendered = render_execution_ack(record)
        self.assertIn("## CONTROL EXECUTION ACK", rendered)
        self.assertIn("```json", rendered)
        self.assertIn(json.dumps(record, indent=2, sort_keys=True), rendered)
        self.assertNotIn("ANDROID_PRODUCTION_SERIAL", rendered)
        self.assertNotIn("ANDROID_TARGET_BINDING_KEY", rendered)

    def test_invalid_run_or_request_provenance_is_refused(self) -> None:
        with self.assertRaises(ExecutionContractError):
            build_execution_ack(
                request(), run_id=0, run_attempt=1, private_sha=PRIVATE_SHA
            )
        malformed = request()
        malformed["provenance"] = {"issue_number": 2, "comment_id": 101}
        with self.assertRaises(ExecutionContractError):
            build_execution_ack(
                malformed, run_id=700, run_attempt=1, private_sha=PRIVATE_SHA
            )


if __name__ == "__main__":
    unittest.main()
