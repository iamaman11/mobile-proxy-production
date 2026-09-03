#!/usr/bin/env python3
"""Instantiate the canonical APK transaction kernel from private execution inputs.

Stage C.0g deliberately does not wire this executable seam to a live workflow.
A later thin workflow may invoke it only after a separate physical-execution cursor.
"""

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

from apk_transaction_ports import (
    ApkTransactionContext,
    PRIVATE_REPOSITORY,
    PrivateApkTransactionPorts,
    RestIssueCommentClient,
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_TRANSACTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_TYPED_ARTIFACT = re.compile(r"^b3:[0-9a-f]{64}$")


class EntrypointFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EntrypointFailure(message)


def require_text(value: str, label: str, pattern: re.Pattern[str] | None = None) -> str:
    value = value.strip()
    require(bool(value), f"{label} is required")
    if pattern is not None:
        require(pattern.fullmatch(value) is not None, f"{label} is invalid")
    return value


def require_positive(value: int, label: str) -> int:
    require(not isinstance(value, bool) and value > 0, f"{label} must be positive")
    return value


def require_exact_canonical_checkout(root: Path, canonical_sha: str) -> Path:
    root = root.resolve()
    scripts = root / "scripts"
    require(scripts.is_dir(), "canonical scripts directory is unavailable")
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise EntrypointFailure("exact canonical checkout cannot be verified") from error
    require(completed.stdout.strip() == canonical_sha, "canonical checkout does not match admitted SHA")
    required = (
        scripts / "transaction_runner.py",
        scripts / "operation_state_machine.py",
        scripts / "control_state_machine.py",
        scripts / "run_private_phone_preflight.py",
        scripts / "operations" / "install_apk.py",
    )
    require(all(path.is_file() for path in required), "canonical APK transaction source bundle is incomplete")
    return scripts


def load_canonical(scripts: Path) -> tuple[Any, Any, Any, Any, Any]:
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    operation = importlib.import_module("operation_state_machine")
    control = importlib.import_module("control_state_machine")
    transaction = importlib.import_module("transaction_runner")
    preflight = importlib.import_module("run_private_phone_preflight")
    apk = importlib.import_module("operations.install_apk")
    require(transaction.TransactionRunner.__module__ == "transaction_runner", "canonical transaction runner import differs")
    require(apk.ApkInstallBinding.contract.operation_id == "android.apk-install.v1", "canonical APK binding contract differs")
    return operation, control, transaction, preflight, apk


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-root", type=Path, required=True)
    parser.add_argument("--canonical-sha", required=True)
    parser.add_argument("--quality-run-id", type=int, required=True)
    parser.add_argument("--private-sha", required=True)
    parser.add_argument("--request-ref", required=True)
    parser.add_argument("--transaction-id", required=True)
    parser.add_argument("--artifact-ref", required=True)
    parser.add_argument("--apk-path", type=Path, required=True)
    parser.add_argument("--typed-digest-tool", type=Path, required=True)
    parser.add_argument("--mutation-scope-ref", default="github-actions:production-phone-global-mutation")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    canonical_sha = require_text(args.canonical_sha, "canonical SHA", _SHA)
    private_sha = require_text(args.private_sha, "private SHA", _SHA)
    transaction_id = require_text(args.transaction_id, "APK transaction ID", _TRANSACTION)
    artifact_ref = require_text(args.artifact_ref, "APK artifact identity", _TYPED_ARTIFACT)
    quality_run_id = require_positive(args.quality_run_id, "canonical Quality run ID")

    require(os.environ.get("GITHUB_REPOSITORY", "") == PRIVATE_REPOSITORY, "private repository identity differs")
    require(os.environ.get("GITHUB_SHA", "") == private_sha, "private execution SHA differs")
    try:
        run_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
        run_attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
    except ValueError as error:
        raise EntrypointFailure("workflow run identity is invalid") from error
    run_id = require_positive(run_id, "workflow run ID")
    run_attempt = require_positive(run_attempt, "workflow run attempt")

    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    require(bool(token), "GitHub CONTROL evidence token is unavailable")

    scripts = require_exact_canonical_checkout(args.canonical_root, canonical_sha)
    operation, control, transaction, preflight, apk = load_canonical(scripts)
    context = ApkTransactionContext(
        canonical_sha=canonical_sha,
        canonical_quality_run_id=quality_run_id,
        private_sha=private_sha,
        request_ref=args.request_ref,
        transaction_id=transaction_id,
        admitted_artifact_ref=artifact_ref,
        serial=serial,
        target_binding_key=binding_key,
        workflow_run_id=run_id,
        workflow_run_attempt=run_attempt,
        mutation_scope_ref=args.mutation_scope_ref,
    )
    ports = PrivateApkTransactionPorts(
        transaction_module=transaction,
        operation_module=operation,
        control_module=control,
        preflight_module=preflight,
        comments=RestIssueCommentClient(token=token),
        context=context,
    )

    # Stable transaction identity is supplied by the immutable request. Rerun attempt
    # is used only for session/observation provenance and can never mint a new mutation.
    existing_evidence = ports.load_existing_evidence()
    commands = apk.SubprocessCommandEdge()
    digests = apk.ExternalTypedArtifactDigest(tool=args.typed_digest_tool.resolve(), commands=commands)
    executor = apk.CanonicalApkInstallExecutor(
        serial=serial,
        apk_path=args.apk_path.resolve(),
        admitted_artifact_ref=artifact_ref,
        commands=commands,
        digests=digests,
    )
    request = apk.ApkInstallRequest(transaction_id, artifact_ref)
    result = transaction.TransactionRunner().run(
        request,
        ports=ports,
        binding=apk.ApkInstallBinding(executor),
        existing_evidence=existing_evidence,
    )

    output = {
        "format_version": 1,
        "operation_id": "android.apk-install.v1",
        "operation_transaction_id": transaction_id,
        "canonical_sha": canonical_sha,
        "canonical_quality_run_id": quality_run_id,
        "private_sha": private_sha,
        "request_ref": args.request_ref,
        "artifact_ref": artifact_ref,
        "state": result.derived.get("state"),
        "next_step": result.derived.get("next_step"),
        "terminal_ref": result.terminal_ref,
        "dispatch_error": result.dispatch_error,
        "blind_retry_allowed": False,
        "raw_device_identifier_recorded": False,
    }
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 2 if result.derived.get("state") == "UNKNOWN_EXECUTION_OUTCOME" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"APK transaction integration failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
