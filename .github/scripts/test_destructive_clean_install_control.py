#!/usr/bin/env python3
"""Hosted truth table for cursor-bound destructive clean-install control."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

PRIVATE_SCRIPTS = Path(__file__).resolve().parent
if str(PRIVATE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PRIVATE_SCRIPTS))

import clean_install_control as control

SHA = "d" * 40
PRIVATE_SHA = "e" * 40
CURSOR = 9001
WINNER_COMMENT = 9100
DUPLICATE_COMMENT = 9101
COMMAND = f"/phone-clean-install {SHA} cursor={CURSOR}"
TX = f"clean-install-cursor-{CURSOR}"
SEMANTIC = f"{control.OPERATION_ID}:{SHA}:tracker-comment:{CURSOR}"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def owner_comment(comment_id: int, body: str) -> dict[str, Any]:
    return {"id": comment_id, "body": body, "user": {"login": control.OWNER}}


def bot_evidence(comment_id: int, heading: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": heading + "\n\n```json\n" + json.dumps(dict(payload), sort_keys=True) + "\n```",
        "user": {"login": control.TRUSTED_BOT},
    }


def tracker_checkpoint(comment_id: int, *, authorize: bool = True, sha: str = SHA) -> dict[str, Any]:
    lines = ["## Progress checkpoint — test cursor", "", "**NEXT ALLOWED ITEM:** bounded test"]
    if authorize:
        lines.extend(("", control.AUTH_OPERATION_LINE, f"{control.AUTH_SHA_PREFIX}{sha}"))
    return owner_comment(comment_id, "\n".join(lines))


@dataclass
class FakeClient:
    public_sha: str = SHA
    private_sha: str = PRIVATE_SHA
    quality_id: int = 777
    tracker: list[Mapping[str, Any]] | None = None
    control_comments_value: list[Mapping[str, Any]] | None = None

    def public_main_sha(self) -> str:
        return self.public_sha

    def private_main_sha(self) -> str:
        return self.private_sha

    def quality_runs(self, canonical_sha: str) -> list[Mapping[str, Any]]:
        return [{
            "id": self.quality_id,
            "run_attempt": 1,
            "name": "Quality",
            "head_branch": "main",
            "head_sha": canonical_sha,
            "conclusion": "success",
        }]

    def tracker_comments(self) -> list[Mapping[str, Any]]:
        return list(self.tracker or [tracker_checkpoint(CURSOR)])

    def control_comments(self) -> list[Mapping[str, Any]]:
        return list(self.control_comments_value or [owner_comment(WINNER_COMMENT, COMMAND)])


def expect_refusal(fn, contains: str) -> None:
    try:
        fn()
    except control.AdmissionRefusal as error:
        require(contains in str(error), f"unexpected refusal: {error}")
    else:
        raise AssertionError("expected fail-closed admission refusal")


def test_success_and_same_comment_rerun_pre_intent() -> None:
    client = FakeClient()
    first = control.admit(client, command_body=COMMAND, command_comment_id=WINNER_COMMENT, workflow_sha=PRIVATE_SHA)
    require(first.request.transaction_id == TX, "transaction must derive from tracker cursor")
    require(first.request.semantic_request_key == SEMANTIC, "semantic request identity differs")
    second = control.admit(
        client,
        command_body=COMMAND,
        command_comment_id=WINNER_COMMENT,
        workflow_sha=PRIVATE_SHA,
        expected_quality_run_id=777,
        expected_transaction_id=TX,
        expected_semantic_request_key=SEMANTIC,
    )
    require(second == first, "same-comment pre-intent rerun must remain same semantic transaction")


def test_near_simultaneous_duplicate_owner_comments() -> None:
    comments = [owner_comment(WINNER_COMMENT, COMMAND), owner_comment(DUPLICATE_COMMENT, COMMAND)]
    client = FakeClient(control_comments_value=comments)
    winner = control.admit(client, command_body=COMMAND, command_comment_id=WINNER_COMMENT, workflow_sha=PRIVATE_SHA)
    require(winner.request.transaction_id == TX, "winner transaction differs")
    expect_refusal(
        lambda: control.admit(client, command_body=COMMAND, command_comment_id=DUPLICATE_COMMENT, workflow_sha=PRIVATE_SHA),
        "not the elected request",
    )


def test_existing_intent_and_terminal_block() -> None:
    for heading in (control.INTENT_HEADING, control.TERMINAL_HEADING):
        client = FakeClient(control_comments_value=[
            owner_comment(WINNER_COMMENT, COMMAND),
            bot_evidence(9200, heading, {"transaction_id": TX, "semantic_request_key": SEMANTIC}),
        ])
        expect_refusal(
            lambda: control.admit(client, command_body=COMMAND, command_comment_id=WINNER_COMMENT, workflow_sha=PRIVATE_SHA),
            "durable CONTROL evidence",
        )


def test_latest_tracker_cursor_and_explicit_authorization_required() -> None:
    client = FakeClient(tracker=[tracker_checkpoint(CURSOR), tracker_checkpoint(CURSOR + 1, authorize=False)])
    expect_refusal(
        lambda: control.admit(client, command_body=COMMAND, command_comment_id=WINNER_COMMENT, workflow_sha=PRIVATE_SHA),
        "not the latest authoritative tracker checkpoint",
    )
    latest_command = f"/phone-clean-install {SHA} cursor={CURSOR + 1}"
    client.control_comments_value = [owner_comment(WINNER_COMMENT, latest_command)]
    expect_refusal(
        lambda: control.admit(client, command_body=latest_command, command_comment_id=WINNER_COMMENT, workflow_sha=PRIVATE_SHA),
        "does not authorize destructive clean install",
    )


def test_authority_changes_fail_closed_at_phone_boundary() -> None:
    client = FakeClient()
    accepted = control.admit(client, command_body=COMMAND, command_comment_id=WINNER_COMMENT, workflow_sha=PRIVATE_SHA)
    client.private_sha = "f" * 40
    expect_refusal(
        lambda: control.admit(
            client,
            command_body=COMMAND,
            command_comment_id=WINNER_COMMENT,
            workflow_sha=PRIVATE_SHA,
            expected_quality_run_id=accepted.quality_run_id,
            expected_transaction_id=accepted.request.transaction_id,
            expected_semantic_request_key=accepted.request.semantic_request_key,
        ),
        "not current private main",
    )


def load_v2():
    return importlib.import_module("run_destructive_clean_install_v2")


class SimulatedFailure(RuntimeError):
    pass


class FakeClean:
    def __init__(self, fail_stage: str | None = None, present: bool = True, version_ok: bool = True):
        self.fail_stage = fail_stage
        self.present = present
        self.version_ok = version_ok

    def _fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise SimulatedFailure(stage)

    def prove_registered_device(self, serial: str) -> None:
        self._fail("registered_device_reproof")

    def package_present(self, serial: str) -> bool:
        self._fail("package_presence")
        return self.present

    def package_version(self, serial: str):
        self._fail("package_version")
        return (1004, "0.1.4") if self.version_ok else (1003, "0.1.3")

    def verify_installed_apk_digest(self, serial: str, expected_digest: str, digest_tool: Path) -> None:
        self._fail("installed_apk_digest")


def test_unknown_diagnostics_are_bounded() -> None:
    v2 = load_v2()
    for stage in ("registered_device_reproof", "package_presence", "package_version"):
        state, accepted, post = v2.classify_after_failure(
            FakeClean(fail_stage=stage),
            "registered-device",
            expected_digest="b3:" + "a" * 64,
            digest_tool=Path("tool"),
            expected_version_name="0.1.4",
            expected_version_code=1004,
        )
        require(state == "UNKNOWN_EXECUTION_OUTCOME" and accepted is False, "observation failure must be UNKNOWN")
        require(post["observation_failure_stage"] == stage, "UNKNOWN stage must be retained")
        require(post["observation_failure_code"] == "UNEXPECTED_ERROR", "only bounded code may be retained")
        require("registered-device" not in json.dumps(post), "diagnostic must not retain raw target")

    state, accepted, post = v2.classify_after_failure(
        FakeClean(fail_stage="installed_apk_digest"),
        "registered-device",
        expected_digest="b3:" + "a" * 64,
        digest_tool=Path("tool"),
        expected_version_name="0.1.4",
        expected_version_code=1004,
    )
    require(state == "QUARANTINED_PACKAGE_STATE" and accepted is False, "digest observation failure must quarantine known-present package")
    require(post["observation_failure_stage"] == "installed_apk_digest", "digest failure stage missing")


def test_static_contract() -> None:
    workflow = (PRIVATE_SCRIPTS.parent / "workflows" / "phone-clean-install.yml").read_text(encoding="utf-8")
    policy = (PRIVATE_SCRIPTS.parent / "workflows" / "phone-clean-install-policy.yml").read_text(encoding="utf-8")
    v2 = (PRIVATE_SCRIPTS / "run_destructive_clean_install_v2.py").read_text(encoding="utf-8")
    control_text = (PRIVATE_SCRIPTS / "clean_install_control.py").read_text(encoding="utf-8")
    require("cursor=([1-9][0-9]*)" in control_text, "cursor-bound command grammar must be enforced")
    require("Revalidate semantic admission inside global mutation lock" in workflow, "in-lock admission recheck missing")
    require(workflow.index("Revalidate semantic admission inside global mutation lock") < workflow.index("Execute one durable destructive clean-install transaction"), "semantic recheck must precede transaction execution")
    require(workflow.count("group: production-phone-global-mutation") == 1, "exactly one phone mutation lock required")
    require("clean-install-cursor-" in v2, "v2 wrapper must bind transaction to public cursor")
    require("failure_diagnostics" in v2 and "raw_error_text_recorded" in v2, "bounded UNKNOWN diagnostics required")
    require("test_destructive_clean_install_control.py" in policy, "policy must execute truth table")


def main() -> int:
    test_success_and_same_comment_rerun_pre_intent()
    test_near_simultaneous_duplicate_owner_comments()
    test_existing_intent_and_terminal_block()
    test_latest_tracker_cursor_and_explicit_authorization_required()
    test_authority_changes_fail_closed_at_phone_boundary()
    test_unknown_diagnostics_are_bounded()
    test_static_contract()
    print("destructive_clean_install_control_policy=accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
