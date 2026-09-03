#!/usr/bin/env python3
"""Cursor-bound destructive clean-install transaction wrapper.

This hardens the C.0l transport without duplicating its GitHub evidence helpers.
It adds deterministic cursor semantics and bounded diagnostics for UNKNOWN results.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

import run_destructive_clean_install as legacy

SAFE_ERROR_CODES = {
    "CleanInstallFailure": "CLEAN_INSTALL_FAILURE",
    "PreflightFailure": "PHONE_PREFLIGHT_FAILURE",
    "ExecutionRefusal": "EXECUTION_REFUSAL",
    "OSError": "OS_ERROR",
    "ValueError": "VALUE_ERROR",
}


def safe_error_code(error: BaseException) -> str:
    return SAFE_ERROR_CODES.get(type(error).__name__, "UNEXPECTED_ERROR")


def classify_after_failure(
    clean: Any,
    serial: str,
    *,
    expected_digest: str,
    digest_tool: Path,
    expected_version_name: str,
    expected_version_code: int,
) -> tuple[str, bool, dict[str, Any]]:
    """Observe only and preserve a bounded failure stage/code; never mutate."""
    postcondition: dict[str, Any] = {
        "package_present": None,
        "exact_candidate_installed": False,
        "recovery_mutation_performed": False,
        "observation_failure_stage": None,
        "observation_failure_code": None,
    }

    try:
        clean.prove_registered_device(serial)
    except Exception as error:
        postcondition["observation_failure_stage"] = "registered_device_reproof"
        postcondition["observation_failure_code"] = safe_error_code(error)
        return "UNKNOWN_EXECUTION_OUTCOME", False, postcondition

    try:
        package_present = clean.package_present(serial)
    except Exception as error:
        postcondition["observation_failure_stage"] = "package_presence"
        postcondition["observation_failure_code"] = safe_error_code(error)
        return "UNKNOWN_EXECUTION_OUTCOME", False, postcondition

    postcondition["package_present"] = package_present
    if not package_present:
        return "PACKAGE_ABSENT_AFTER_FAILED_CLEAN_INSTALL", False, postcondition

    try:
        installed_code, installed_name = clean.package_version(serial)
    except Exception as error:
        postcondition["observation_failure_stage"] = "package_version"
        postcondition["observation_failure_code"] = safe_error_code(error)
        return "UNKNOWN_EXECUTION_OUTCOME", False, postcondition

    exact_version = (installed_code, installed_name) == (
        expected_version_code,
        expected_version_name,
    )
    postcondition["exact_version_verified"] = exact_version
    if not exact_version:
        postcondition["exact_digest_verified"] = False
        return "QUARANTINED_PACKAGE_STATE", False, postcondition

    try:
        clean.verify_installed_apk_digest(serial, expected_digest, digest_tool)
    except Exception as error:
        postcondition["exact_digest_verified"] = False
        postcondition["observation_failure_stage"] = "installed_apk_digest"
        postcondition["observation_failure_code"] = safe_error_code(error)
        return "QUARANTINED_PACKAGE_STATE", False, postcondition

    postcondition["exact_digest_verified"] = True
    postcondition["exact_candidate_installed"] = True
    return "INSTALLED_EXACT_CANDIDATE_RECOVERED_POSTCONDITION", True, postcondition


def execute(args: argparse.Namespace) -> dict[str, Any]:
    legacy.require(
        os.environ.get("GITHUB_REPOSITORY") == legacy.PRIVATE_REPOSITORY,
        "wrong private execution repository",
    )
    legacy.require(os.environ.get("GITHUB_SHA") == args.private_sha, "private execution SHA differs")
    legacy.require(
        legacy.TRANSACTION_PATTERN.fullmatch(args.transaction_id) is not None,
        "transaction ID is invalid",
    )
    legacy.require(args.transaction_id == f"clean-install-cursor-{args.control_cursor_id}", "cursor transaction identity differs")
    expected_semantic = (
        f"{legacy.OPERATION_ID}:{args.canonical_sha}:tracker-comment:{args.control_cursor_id}"
    )
    legacy.require(args.semantic_request_key == expected_semantic, "semantic clean-install request identity differs")

    token = os.environ.get("GH_TOKEN", "")
    legacy.require(bool(token), "GitHub CONTROL token is unavailable")

    clean = legacy.load_canonical(args.canonical_root, args.canonical_sha)
    legacy.refuse_prior_transaction(token, args.transaction_id)

    evidence = clean.load_json(args.release_evidence)
    expected_digest = clean.verify_release_evidence(
        evidence,
        args.canonical_sha,
        args.apk,
        args.digest_tool,
        args.expected_version_name,
        args.expected_version_code,
    )
    serial = clean.require_expected_serial()

    clean.prove_registered_device(serial)
    old_package_present = clean.package_present(serial)

    intent = {
        "format_version": 2,
        "operation_id": legacy.OPERATION_ID,
        "transaction_id": args.transaction_id,
        "semantic_request_key": args.semantic_request_key,
        "control_cursor_id": args.control_cursor_id,
        "request_ref": args.request_ref,
        "canonical_sha": args.canonical_sha,
        "quality_run_id": args.quality_run_id,
        "private_execution_sha": args.private_sha,
        "target": "android-production",
        "application_id": legacy.PACKAGE,
        "expected_version_name": args.expected_version_name,
        "expected_version_code": args.expected_version_code,
        "affected_domain_generations": {"domain/package": args.transaction_id},
        "old_package_observed": old_package_present,
        "old_generation_preservation_required": False,
        "dispatch_may_reach_target": True,
        "adapter_invocation_allowed_only_after_persistence": True,
        "blind_retry_allowed": False,
        "phone_mutation_intended": True,
        "raw_device_identifier_recorded": False,
        "artifact_digest_recorded": False,
    }
    intent_comment_id = legacy.post_control(token, legacy.INTENT_HEADING, intent)

    state = "UNKNOWN_EXECUTION_OUTCOME"
    accepted = False
    dispatch_error = False
    dispatch_failure_stage: str | None = None
    dispatch_failure_code: str | None = None
    postcondition: dict[str, Any] = {
        "package_present": None,
        "exact_candidate_installed": False,
        "recovery_mutation_performed": False,
        "observation_failure_stage": None,
        "observation_failure_code": None,
    }

    try:
        report = clean.clean_install(
            canonical_sha=args.canonical_sha,
            apk=args.apk,
            release_evidence=args.release_evidence,
            digest_tool=args.digest_tool,
            expected_version_name=args.expected_version_name,
            expected_version_code=args.expected_version_code,
        )
        legacy.require(report.get("accepted") is True, "canonical clean-install did not accept terminal state")
        legacy.require(report.get("new_apk_installed") is True, "canonical clean-install did not prove installed APK")
        legacy.require(report.get("new_apk_identity_verified") is True, "canonical clean-install did not prove APK identity")
        legacy.require(report.get("new_apk_digest_verified") is True, "canonical clean-install did not prove APK digest")
        state = "INSTALLED_EXACT_CANDIDATE"
        accepted = True
        postcondition = {
            "package_present": True,
            "exact_candidate_installed": True,
            "exact_version_verified": True,
            "exact_digest_verified": True,
            "recovery_mutation_performed": False,
            "observation_failure_stage": None,
            "observation_failure_code": None,
        }
    except Exception as error:
        dispatch_error = True
        dispatch_failure_stage = "canonical_clean_install"
        dispatch_failure_code = safe_error_code(error)
        state, accepted, postcondition = classify_after_failure(
            clean,
            serial,
            expected_digest=expected_digest,
            digest_tool=args.digest_tool,
            expected_version_name=args.expected_version_name,
            expected_version_code=args.expected_version_code,
        )

    terminal = {
        "format_version": 2,
        "operation_id": legacy.OPERATION_ID,
        "transaction_id": args.transaction_id,
        "semantic_request_key": args.semantic_request_key,
        "control_cursor_id": args.control_cursor_id,
        "request_ref": args.request_ref,
        "canonical_sha": args.canonical_sha,
        "quality_run_id": args.quality_run_id,
        "private_execution_sha": args.private_sha,
        "intent_comment_id": intent_comment_id,
        "target": "android-production",
        "application_id": legacy.PACKAGE,
        "expected_version_name": args.expected_version_name,
        "expected_version_code": args.expected_version_code,
        "affected_domain_generations": {"domain/package": args.transaction_id},
        "state": state,
        "accepted": accepted,
        "dispatch_error_observed": dispatch_error,
        "failure_diagnostics": {
            "dispatch_failure_stage": dispatch_failure_stage,
            "dispatch_failure_code": dispatch_failure_code,
            "observation_failure_stage": postcondition.get("observation_failure_stage"),
            "observation_failure_code": postcondition.get("observation_failure_code"),
            "raw_error_text_recorded": False,
        },
        "postcondition": postcondition,
        "blind_retry_allowed": False,
        "old_generation_preservation_required": False,
        "raw_device_identifier_recorded": False,
        "artifact_digest_recorded": False,
        "phone_mutation_may_have_occurred": True,
    }
    terminal_comment_id = legacy.post_control(token, legacy.TERMINAL_HEADING, terminal)
    result = dict(terminal)
    result["terminal_comment_id"] = terminal_comment_id
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-root", type=Path, required=True)
    parser.add_argument("--canonical-sha", required=True)
    parser.add_argument("--quality-run-id", type=int, required=True)
    parser.add_argument("--private-sha", required=True)
    parser.add_argument("--transaction-id", required=True)
    parser.add_argument("--semantic-request-key", required=True)
    parser.add_argument("--control-cursor-id", type=int, required=True)
    parser.add_argument("--request-ref", required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--release-evidence", type=Path, required=True)
    parser.add_argument("--digest-tool", type=Path, required=True)
    parser.add_argument("--expected-version-name", required=True)
    parser.add_argument("--expected-version-code", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = execute(args)
    except (legacy.ExecutionRefusal, OSError, ValueError) as error:
        print(
            f"cursor-bound destructive clean-install refused before accepted terminal: {type(error).__name__}",
            file=sys.stderr,
        )
        return 2
    return 0 if result.get("accepted") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
