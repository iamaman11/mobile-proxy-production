#!/usr/bin/env python3
"""Read-only recovery observer for one already-durable scratch UNKNOWN terminal."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

from apk_transaction_ports import PRIVATE_REPOSITORY, RestIssueCommentClient
from filesystem_scratch_edges import AdbScratchAbsenceObserver, RecoveryDispatchForbiddenEdge
from filesystem_scratch_transaction_ports import (
    OPERATION_ID,
    SEMANTIC_OPERATION,
    PrivateScratchTransactionPorts,
    context_from_prior_terminal,
)
from run_filesystem_scratch_transaction import exact_checkout, load_canonical, require

_SHA = re.compile(r"^[0-9a-f]{40}$")
_ISSUE_REF = re.compile(r"^issue-comment:[1-9][0-9]*$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-root", type=Path, required=True)
    parser.add_argument("--recovery-kernel-sha", required=True)
    parser.add_argument("--recovery-quality-run-id", type=int, required=True)
    parser.add_argument("--private-sha", required=True)
    parser.add_argument("--prior-terminal-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require(_SHA.fullmatch(args.recovery_kernel_sha) is not None, "recovery Kernel SHA is invalid")
    require(_SHA.fullmatch(args.private_sha) is not None, "private SHA is invalid")
    require(args.recovery_quality_run_id > 0, "recovery Kernel Quality run is invalid")
    require(_ISSUE_REF.fullmatch(args.prior_terminal_ref) is not None, "prior terminal ref is invalid")
    require(os.environ.get("GITHUB_REPOSITORY", "") == PRIVATE_REPOSITORY, "private repository identity differs")
    require(os.environ.get("GITHUB_SHA", "") == args.private_sha, "private execution SHA differs")
    run_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
    run_attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
    require(run_id > 0 and run_attempt > 0, "workflow provenance is invalid")

    scripts = exact_checkout(args.canonical_root, args.recovery_kernel_sha)
    operation, control, transaction, preflight, filesystem = load_canonical(scripts)
    recover_observe = getattr(transaction.TransactionRunner(), "recover_observe", None)
    require(callable(recover_observe), "accepted recovery Kernel has no read-only recovery path")

    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    require(bool(serial) and bool(binding_key) and bool(token), "recovery execution inputs are unavailable")
    comments = RestIssueCommentClient(token=token)
    context = context_from_prior_terminal(
        comments,
        args.prior_terminal_ref,
        private_sha=args.private_sha,
        serial=serial,
        target_binding_key=binding_key,
        workflow_run_id=run_id,
        workflow_run_attempt=run_attempt,
        recovery_kernel_sha=args.recovery_kernel_sha,
        recovery_kernel_quality_run_id=args.recovery_quality_run_id,
    )
    semantic = transaction.routed_semantic_request_identity(
        request_id=context.control_request_id,
        operation=SEMANTIC_OPERATION,
        arguments=(context.canonical_sha,),
        authority_cursor=context.authority_cursor,
        desired_generation=context.desired_generation,
    )
    expected_transaction_id = transaction.derive_physical_transaction_id(semantic, OPERATION_ID)
    require(expected_transaction_id == context.transaction_id, "prior terminal physical transaction identity differs")
    request_digest = context.control_request_id.removeprefix("req-sha256:")
    require(
        context.scratch_ref == "/data/local/tmp/mobile-proxy-kernel-" + request_digest[:32],
        "prior terminal scratch namespace differs from semantic request",
    )
    require(context.payload_ref == "payload/" + context.desired_generation, "prior terminal payload identity differs")
    request = filesystem.FilesystemScratchRoundtripRequest(
        semantic_request=semantic,
        scratch_ref=context.scratch_ref,
        payload_ref=context.payload_ref,
    )
    ports = PrivateScratchTransactionPorts(
        transaction_module=transaction,
        operation_module=operation,
        control_module=control,
        preflight_module=preflight,
        comments=comments,
        context=context,
    )
    prior = ports.load_terminal_by_ref(args.prior_terminal_ref)
    observer = AdbScratchAbsenceObserver(serial, transaction)
    executor = filesystem.FilesystemScratchRoundtripExecutor(
        filesystem=RecoveryDispatchForbiddenEdge(transaction),
        observer=observer,
    )
    binding = filesystem.FilesystemScratchRoundtripBinding(executor)
    require(binding.transaction_id(request) == context.transaction_id, "recovery binding transaction differs")

    result = recover_observe(
        request,
        ports=ports,
        binding=binding,
        prior_terminal=prior,
        prior_terminal_ref=args.prior_terminal_ref,
    )
    output = {
        "format_version": 1,
        "schema": "filesystem-scratch-recovery-result.v1",
        "operation_id": OPERATION_ID,
        "semantic_operation": SEMANTIC_OPERATION,
        "control_request_id": context.control_request_id,
        "original_authority_cursor": context.authority_cursor,
        "desired_generation": context.desired_generation,
        "operation_transaction_id": context.transaction_id,
        "original_canonical_sha": context.canonical_sha,
        "original_canonical_quality_run_id": context.canonical_quality_run_id,
        "recovery_kernel_sha": args.recovery_kernel_sha,
        "recovery_kernel_quality_run_id": args.recovery_quality_run_id,
        "private_sha": args.private_sha,
        "prior_terminal_ref": args.prior_terminal_ref,
        "state": result.derived.get("state"),
        "lifecycle_state": result.lifecycle_state,
        "recovery_terminal_ref": result.terminal_ref,
        "recovery_error_class": None if result.recovery_error is None else "OBSERVATION_UNAVAILABLE",
        "blind_retry_allowed": False,
        "phone_mutation_performed": False,
        "cleanup_performed": False,
        "original_transaction_accepted": False,
        "raw_device_identifier_recorded": False,
    }
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result.derived.get("state") in {"RECOVERED", "QUARANTINED"} else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"filesystem scratch recovery failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
