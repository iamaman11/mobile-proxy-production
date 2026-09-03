#!/usr/bin/env python3
"""Hosted truth table for the private APK TransactionPorts seam."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
from pathlib import Path
import sys
from typing import Any, Mapping


PRIVATE_SCRIPTS = Path(__file__).resolve().parent
if str(PRIVATE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PRIVATE_SCRIPTS))

import apk_transaction_ports as PORTS


TX = "apk-request-4242"
REF = "b3:" + "a" * 64
SHA = "d" * 40
PRIVATE_SHA = "e" * 40
SERIAL = "registered-device-1"
KEY = "k" * 40
REQUEST_REF = "issue-comment:4242"


class FakeComments:
    def __init__(self) -> None:
        self.comments: list[dict[str, Any]] = []
        self.next_id = 1000

    def list_comments(self) -> list[Mapping[str, Any]]:
        return list(self.comments)

    def create_comment(self, body: str) -> int:
        comment_id = self.next_id
        self.next_id += 1
        self.comments.append({"id": comment_id, "body": body, "user": {"login": PORTS.TRUSTED_BOT}})
        return comment_id


@dataclass
class FakePreflight:
    control: Any
    mutation_attempted: bool = False
    calls: int = 0

    def build_report(self, canonical_sha: str, *, target_binding_id: str, session_id: str, observation_ref: str, transaction_id: str) -> dict[str, Any]:
        self.calls += 1
        return {
            "format_version": 1,
            "repository": PORTS.CANONICAL_REPOSITORY,
            "canonical_sha": canonical_sha,
            "mode": "read_only",
            "device": {"device_count": 1, "registered_device_match": True, "adb_state": "device", "shell_probe": True},
            "observed_facts": [{
                "subject": "phone",
                "predicate": "registered_phone_access_proven",
                "value": True,
                "target": PORTS.TARGET,
                "observation_ref": observation_ref,
                "source_ref": canonical_sha,
                "dependencies": [
                    {"scope": PORTS.TARGET_SCOPE, "identity": target_binding_id},
                    {"scope": PORTS.OBSERVER_SCOPE, "identity": PORTS.OBSERVER_ID},
                    {"scope": PORTS.SESSION_SCOPE, "identity": session_id},
                    {"scope": f"transaction/{transaction_id}", "identity": transaction_id},
                ],
                "authority": "CONTROL",
                "persisted": False,
            }],
            "causal_fact_envelope_emitted": True,
            "raw_device_identifier_recorded": False,
            "mutation_performed": False,
            "accepted": True,
        }


class FakeExecutor:
    def __init__(self, runner: Any, *, unknown: bool = False) -> None:
        self.runner = runner
        self.unknown = unknown
        self.dispatch_calls = 0
        self.postcondition_calls = 0

    def dispatch_once(self, request):
        self.dispatch_calls += 1
        if self.unknown:
            raise self.runner.DispatchOutcomeUnknown("simulated lost result")
        return self.runner.DispatchReceipt("apk-dispatch-result:known")

    def verify_postcondition(self, request):
        self.postcondition_calls += 1
        return self.runner.PostconditionProof(True, "installed-apk:" + request.artifact_ref)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_canonical(root: Path):
    scripts = root / "scripts"
    require(scripts.is_dir(), "canonical scripts directory missing")
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    operation = importlib.import_module("operation_state_machine")
    control = importlib.import_module("control_state_machine")
    runner = importlib.import_module("transaction_runner")
    apk = importlib.import_module("operations.install_apk")
    return operation, control, runner, apk


def context() -> PORTS.ApkTransactionContext:
    return PORTS.ApkTransactionContext(
        canonical_sha=SHA,
        canonical_quality_run_id=777,
        private_sha=PRIVATE_SHA,
        request_ref=REQUEST_REF,
        transaction_id=TX,
        admitted_artifact_ref=REF,
        serial=SERIAL,
        target_binding_key=KEY,
        workflow_run_id=888,
        workflow_run_attempt=3,
    )


def make_ports(operation, control, runner, comments):
    preflight = FakePreflight(control)
    ports = PORTS.PrivateApkTransactionPorts(
        transaction_module=runner,
        operation_module=operation,
        control_module=control,
        preflight_module=preflight,
        comments=comments,
        context=context(),
    )
    return ports, preflight


def headings(comments: FakeComments) -> list[str]:
    return [str(item["body"]).splitlines()[0] for item in comments.comments]


def test_success(operation, control, runner, apk) -> None:
    comments = FakeComments()
    ports, preflight = make_ports(operation, control, runner, comments)
    executor = FakeExecutor(runner)
    request = apk.ApkInstallRequest(TX, REF)
    result = runner.TransactionRunner().run(request, ports=ports, binding=apk.ApkInstallBinding(executor), existing_evidence=ports.load_existing_evidence())
    require(result.derived["state"] == "ACCEPTED", "success transaction must be ACCEPTED")
    require(executor.dispatch_calls == 1, "APK executor must dispatch exactly once")
    require(executor.postcondition_calls == 1, "APK postcondition must execute exactly once")
    require(preflight.calls == 1, "transaction-bound phone observer must execute exactly once")
    require(headings(comments) == [PORTS.BOUNDARY_RAW_HEADING, PORTS.BOUNDARY_FACT_HEADING, PORTS.INTENT_HEADING, PORTS.TERMINAL_HEADING], "durable CONTROL evidence order differs")
    raw = PORTS._parse(comments.comments[0], PORTS.BOUNDARY_RAW_HEADING)
    promoted = PORTS._parse(comments.comments[1], PORTS.BOUNDARY_FACT_HEADING)
    intent = PORTS._parse(comments.comments[2], PORTS.INTENT_HEADING)
    require(raw is not None and promoted is not None and intent is not None, "CONTROL records missing")
    raw_fact = raw["fact"]
    promoted_fact = promoted["fact"]
    require(raw_fact["persisted"] is False, "canonical raw boundary fact must be unpersisted")
    require(promoted_fact["persisted"] is True, "promoted boundary fact must be persisted")
    reverted = dict(promoted_fact)
    reverted["persisted"] = False
    require(reverted == raw_fact, "promotion must change only persistence")
    require(intent["affected_domain_generations"] == {PORTS.PACKAGE_DOMAIN: TX}, "package generation must advance to exact transaction")
    require(intent["dispatch_may_reach_target"] is True, "intent must conservatively mark may-have-reached")
    require(intent["blind_retry_allowed"] is False, "APK mutation intent must forbid blind retry")
    serialized = "\n".join(str(item["body"]) for item in comments.comments)
    require(SERIAL not in serialized and KEY not in serialized, "CONTROL evidence leaked raw target/key")


def test_unknown_rerun_block(operation, control, runner, apk) -> None:
    comments = FakeComments()
    first_ports, first_preflight = make_ports(operation, control, runner, comments)
    first_executor = FakeExecutor(runner, unknown=True)
    request = apk.ApkInstallRequest(TX, REF)
    first = runner.TransactionRunner().run(request, ports=first_ports, binding=apk.ApkInstallBinding(first_executor), existing_evidence=first_ports.load_existing_evidence())
    require(first.derived["state"] == "UNKNOWN_EXECUTION_OUTCOME", "lost post-dispatch result must be UNKNOWN_EXECUTION_OUTCOME")
    require(first.lifecycle_state == runner.TERMINAL_UNKNOWN, "post-dispatch ambiguity must classify as durable UNKNOWN")
    require(isinstance(first.terminal_ref, str) and first.terminal_ref.startswith("issue-comment:"), "UNKNOWN result must persist a durable terminal record")
    require(first_executor.dispatch_calls == 1, "first attempt must dispatch once")
    require(first_preflight.calls == 1, "first attempt boundary must be observed once")
    require(headings(comments) == [PORTS.BOUNDARY_RAW_HEADING, PORTS.BOUNDARY_FACT_HEADING, PORTS.INTENT_HEADING, PORTS.TERMINAL_HEADING], "UNKNOWN path must persist boundary, intent and terminal evidence")
    terminal = PORTS._parse(comments.comments[3], PORTS.TERMINAL_HEADING)
    require(terminal is not None, "durable UNKNOWN terminal is missing")
    require(terminal["derived"]["state"] == "UNKNOWN_EXECUTION_OUTCOME", "durable terminal must preserve UNKNOWN state")
    require(terminal["blind_retry_allowed"] is False, "durable UNKNOWN terminal must forbid blind retry")
    retry_ports, retry_preflight = make_ports(operation, control, runner, comments)
    retry_executor = FakeExecutor(runner)
    existing = retry_ports.load_existing_evidence()
    require(existing[-1].status == operation.DISPATCHED, "durable UNKNOWN terminal must reconstruct the dispatch boundary")
    try:
        runner.TransactionRunner().run(request, ports=retry_ports, binding=apk.ApkInstallBinding(retry_executor), existing_evidence=existing)
    except runner.BlindRetryForbidden:
        pass
    else:
        raise AssertionError("durable UNKNOWN transaction must forbid blind retry")
    require(retry_executor.dispatch_calls == 0, "rerun must be blocked before APK dispatch")
    require(retry_preflight.calls == 0, "rerun must be blocked before phone observation")
    require(len(comments.comments) == 4, "blocked rerun must not create new durable evidence or a new destructive identity")


def test_terminal_rerun_block(operation, control, runner, apk) -> None:
    comments = FakeComments()
    ports, _ = make_ports(operation, control, runner, comments)
    request = apk.ApkInstallRequest(TX, REF)
    runner.TransactionRunner().run(request, ports=ports, binding=apk.ApkInstallBinding(FakeExecutor(runner)))
    replay_ports, replay_preflight = make_ports(operation, control, runner, comments)
    existing = replay_ports.load_existing_evidence()
    replay_executor = FakeExecutor(runner)
    try:
        runner.TransactionRunner().run(request, ports=replay_ports, binding=apk.ApkInstallBinding(replay_executor), existing_evidence=existing)
    except runner.TransactionRefusal:
        pass
    else:
        raise AssertionError("terminal APK transaction must not be destructively resumed")
    require(replay_executor.dispatch_calls == 0, "terminal rerun must not dispatch")
    require(replay_preflight.calls == 0, "terminal rerun must not access phone")


def test_authority_refusal(operation, control, runner, apk) -> None:
    comments = FakeComments()
    ports, preflight = make_ports(operation, control, runner, comments)
    request = apk.ApkInstallRequest(TX, "b3:" + "b" * 64)
    result = runner.TransactionRunner().run(request, ports=ports, binding=apk.ApkInstallBinding(FakeExecutor(runner)))
    require(result.derived["state"] == "REFUSED", "artifact mismatch must refuse authority")
    require(preflight.calls == 0, "authority refusal must happen before phone observation")
    require(headings(comments) == [PORTS.TERMINAL_HEADING], "authority refusal may persist only terminal evidence")


def test_static_split() -> None:
    ports_text = (PRIVATE_SCRIPTS / "apk_transaction_ports.py").read_text(encoding="utf-8")
    entry_text = (PRIVATE_SCRIPTS / "run_apk_transaction.py").read_text(encoding="utf-8")
    workflows = PRIVATE_SCRIPTS.parent / "workflows"
    workflow_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(workflows.glob("*.yml")))
    require("TransactionRunner().run" in entry_text, "entrypoint must invoke canonical TransactionRunner")
    require("ApkInstallBinding" in entry_text, "entrypoint must instantiate canonical APK binding")
    require("CanonicalApkInstallExecutor" in entry_text, "entrypoint must instantiate canonical APK executor")
    require("run_private_phone_preflight" in entry_text, "entrypoint must use canonical phone observer")
    require("--transaction-id" in entry_text, "entrypoint must require explicit stable transaction identity")
    require("transaction_id = require_text(args.transaction_id" in entry_text, "transaction ID must come from explicit immutable request input")
    require("adb " not in ports_text and '["adb"' not in ports_text and '("adb"' not in ports_text, "private ports must contain no inline ADB semantics")
    require("subprocess.run" not in ports_text, "private ports must not implement APK command execution")
    require("domain/package" in ports_text, "private intent must persist package generation")
    require("blind_retry_allowed" in ports_text, "private evidence must encode no-blind-retry")
    require("production-phone-global-mutation" in ports_text, "private port must represent the global mutation scope")
    require("run_apk_transaction.py" not in workflow_text, "Stage C.0g must not wire the executable seam to any workflow")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-root", type=Path, required=True)
    args = parser.parse_args()
    operation, control, runner, apk = load_canonical(args.canonical_root.resolve())
    test_static_split()
    test_success(operation, control, runner, apk)
    test_unknown_rerun_block(operation, control, runner, apk)
    test_terminal_rerun_block(operation, control, runner, apk)
    test_authority_refusal(operation, control, runner, apk)
    print("apk_transaction_integration_policy=accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
