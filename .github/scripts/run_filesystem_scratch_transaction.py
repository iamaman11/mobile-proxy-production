#!/usr/bin/env python3
"""Invoke the exact public Universal TransactionRunner for one real scratch transaction."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from apk_transaction_ports import PRIVATE_REPOSITORY, RestIssueCommentClient
from control_request import RequestProvenance, build_request_envelope
from filesystem_scratch_edges import AdbScratchAbsenceObserver, AdbScratchRoundtripEdge
from filesystem_scratch_transaction_ports import (
    OPERATION_ID,
    SEMANTIC_OPERATION,
    PrivateScratchTransactionPorts,
    ScratchTransactionContext,
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_CURSOR = re.compile(r"^issue179-comment-[1-9][0-9]*$")


class ScratchEntrypointFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ScratchEntrypointFailure(message)


def exact_checkout(root: Path, expected_sha: str) -> Path:
    root = root.resolve()
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    require(result.stdout.strip() == expected_sha, "canonical checkout SHA differs")
    scripts = root / "scripts"
    required = (
        scripts / "transaction_runner.py",
        scripts / "operation_state_machine.py",
        scripts / "control_state_machine.py",
        scripts / "run_private_phone_preflight.py",
        scripts / "atomic_physical_contracts.py",
        scripts / "operations" / "filesystem.py",
    )
    require(all(path.is_file() for path in required), "canonical scratch transaction bundle incomplete")
    return scripts


def load_canonical(scripts: Path) -> tuple[Any, Any, Any, Any, Any]:
    sys.path.insert(0, str(scripts))
    operation = importlib.import_module("operation_state_machine")
    control = importlib.import_module("control_state_machine")
    transaction = importlib.import_module("transaction_runner")
    preflight = importlib.import_module("run_private_phone_preflight")
    filesystem = importlib.import_module("operations.filesystem")
    require(transaction.TransactionRunner.__module__ == "transaction_runner", "canonical TransactionRunner import differs")
    require(
        filesystem.FilesystemScratchRoundtripBinding.contract.operation_id == OPERATION_ID,
        "canonical scratch binding contract differs",
    )
    requirements = tuple(filesystem.FilesystemScratchRoundtripBinding.contract.fact_requirements)
    require(len(requirements) == 1, "scratch bootstrap must have exactly one preflight requirement")
    require(
        requirements[0].subject == "phone"
        and requirements[0].predicate == "registered_phone_access_proven"
        and requirements[0].freshness == operation.SAME_TRANSACTION,
        "scratch bootstrap preflight contract differs",
    )
    return operation, control, transaction, preflight, filesystem


def parse_control_request(raw: str, *, expected_id: str, canonical_sha: str, authority_cursor: str):
    payload = json.loads(raw)
    require(isinstance(payload, dict), "control request envelope is invalid")
    provenance_raw = payload.get("provenance")
    require(isinstance(provenance_raw, dict), "control request provenance is invalid")
    provenance = RequestProvenance(
        repository=str(provenance_raw.get("repository", "")),
        issue_number=int(provenance_raw.get("issue_number", 0)),
        comment_id=int(provenance_raw.get("comment_id", 0)),
        actor=str(provenance_raw.get("actor", "")),
        event_name=str(provenance_raw.get("event_name", "")),
    )
    arguments = tuple(str(item) for item in payload.get("arguments", ()))
    rebuilt = build_request_envelope(
        operation=str(payload.get("operation", "")),
        arguments=arguments,
        authority_cursor=str(payload.get("authority_cursor", "")),
        mutating=bool(payload.get("mutating", False)),
        provenance=provenance,
        generation=str(payload.get("desired_generation", "")),
    )
    require(rebuilt.to_dict() == payload, "control request envelope does not recompute exactly")
    require(rebuilt.request_id == expected_id, "control request ID differs")
    require(rebuilt.operation == SEMANTIC_OPERATION, "semantic operation differs")
    require(rebuilt.mutating is True, "filesystem certification request is not mutating")
    require(rebuilt.authority_cursor == authority_cursor, "control request authority cursor differs")
    require(rebuilt.arguments == (canonical_sha,), "filesystem certification request must target exact canonical SHA")
    require(provenance.repository == PRIVATE_REPOSITORY and provenance.issue_number == 1, "control request provenance differs")
    return rebuilt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-root", type=Path, required=True)
    parser.add_argument("--canonical-sha", required=True)
    parser.add_argument("--quality-run-id", type=int, required=True)
    parser.add_argument("--private-sha", required=True)
    parser.add_argument("--authority-cursor", required=True)
    parser.add_argument("--request-ref", required=True)
    parser.add_argument("--control-request-id", required=True)
    parser.add_argument("--control-request-json", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require(_SHA.fullmatch(args.canonical_sha) is not None, "canonical SHA is invalid")
    require(_SHA.fullmatch(args.private_sha) is not None, "private SHA is invalid")
    require(_CURSOR.fullmatch(args.authority_cursor) is not None, "authority cursor is invalid")
    require(args.quality_run_id > 0, "Quality run ID is invalid")
    require(os.environ.get("GITHUB_REPOSITORY", "") == PRIVATE_REPOSITORY, "private repository identity differs")
    require(os.environ.get("GITHUB_SHA", "") == args.private_sha, "private execution SHA differs")
    run_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
    run_attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
    require(run_id > 0 and run_attempt > 0, "workflow provenance is invalid")

    envelope = parse_control_request(
        args.control_request_json,
        expected_id=args.control_request_id,
        canonical_sha=args.canonical_sha,
        authority_cursor=args.authority_cursor,
    )
    scripts = exact_checkout(args.canonical_root, args.canonical_sha)
    operation, control, transaction, preflight, filesystem = load_canonical(scripts)
    semantic = transaction.routed_semantic_request_identity(
        request_id=envelope.request_id,
        operation=envelope.operation,
        arguments=envelope.arguments,
        authority_cursor=envelope.authority_cursor,
        desired_generation=envelope.desired_generation,
    )
    transaction_id = transaction.derive_physical_transaction_id(semantic, OPERATION_ID)
    request_digest = envelope.request_id.removeprefix("req-sha256:")
    scratch_ref = "/data/local/tmp/mobile-proxy-kernel-" + request_digest[:32]
    payload_ref = "payload/" + envelope.desired_generation
    request = filesystem.FilesystemScratchRoundtripRequest(
        semantic_request=semantic,
        scratch_ref=scratch_ref,
        payload_ref=payload_ref,
    )

    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    require(bool(serial) and bool(binding_key) and bool(token), "production execution inputs are unavailable")

    context = ScratchTransactionContext(
        canonical_sha=args.canonical_sha,
        canonical_quality_run_id=args.quality_run_id,
        private_sha=args.private_sha,
        request_ref=args.request_ref,
        control_request_id=envelope.request_id,
        authority_cursor=envelope.authority_cursor,
        desired_generation=envelope.desired_generation,
        transaction_id=transaction_id,
        scratch_ref=scratch_ref,
        payload_ref=payload_ref,
        serial=serial,
        target_binding_key=binding_key,
        workflow_run_id=run_id,
        workflow_run_attempt=run_attempt,
    )
    ports = PrivateScratchTransactionPorts(
        transaction_module=transaction,
        operation_module=operation,
        control_module=control,
        preflight_module=preflight,
        comments=RestIssueCommentClient(token=token),
        context=context,
    )
    executor = filesystem.FilesystemScratchRoundtripExecutor(
        filesystem=AdbScratchRoundtripEdge(serial, transaction),
        observer=AdbScratchAbsenceObserver(serial, transaction),
    )
    binding = filesystem.FilesystemScratchRoundtripBinding(executor)
    require(binding.transaction_id(request) == transaction_id, "binding physical transaction identity differs")

    result = transaction.TransactionRunner().run(
        request,
        ports=ports,
        binding=binding,
        existing_evidence=ports.load_existing_evidence(),
    )
    output = {
        "format_version": 1,
        "schema": "filesystem-scratch-kernel-result.v1",
        "operation_id": OPERATION_ID,
        "semantic_operation": SEMANTIC_OPERATION,
        "control_request_id": envelope.request_id,
        "authority_cursor": envelope.authority_cursor,
        "desired_generation": envelope.desired_generation,
        "operation_transaction_id": transaction_id,
        "canonical_sha": args.canonical_sha,
        "canonical_quality_run_id": args.quality_run_id,
        "private_sha": args.private_sha,
        "state": result.derived.get("state"),
        "lifecycle_state": result.lifecycle_state,
        "terminal_ref": result.terminal_ref,
        "dispatch_error": result.dispatch_error,
        "postcondition_error": result.postcondition_error,
        "blind_retry_allowed": False,
        "raw_device_identifier_recorded": False,
        "scratch_namespace_absent_required": True,
    }
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result.derived.get("state") == "ACCEPTED" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"filesystem scratch transaction failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
