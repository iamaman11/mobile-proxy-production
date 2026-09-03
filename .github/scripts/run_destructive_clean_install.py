#!/usr/bin/env python3
"""Private execution entrypoint for one destructive Android signing-generation clean install.

Canonical phone mechanics stay in the public clean_install_android_production.py primitive.
This wrapper owns only private execution evidence/replay control around that primitive:
- refuse any transaction that already has durable intent/terminal evidence;
- prove the registered phone read-only before durable intent;
- persist mutation intent before the canonical primitive may dispatch uninstall/install;
- invoke the canonical clean-install primitive exactly once;
- persist a bounded terminal classification, including conservative post-failure recovery
  observation without blindly retrying mutation.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


PRIVATE_REPOSITORY = "iamaman11/mobile-proxy-production"
CONTROL_ISSUE = 1
TRUSTED_BOT = "github-actions[bot]"
OPERATION_ID = "android.clean-install-signing-generation.v1"
PACKAGE = "com.example.mobileproxy"
INTENT_HEADING = "## CONTROL APK CLEAN INSTALL INTENT"
TERMINAL_HEADING = "## CONTROL APK CLEAN INSTALL TERMINAL"
TRANSACTION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ExecutionRefusal(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ExecutionRefusal(message)


def _request(url: str, token: str, *, method: str = "GET", data: bytes | None = None):
    request = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "mobile-proxy-production-clean-install",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return urllib.request.urlopen(request, timeout=20)


def list_control_comments(token: str) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        url = f"https://api.github.com/repos/{PRIVATE_REPOSITORY}/issues/{CONTROL_ISSUE}/comments?{query}"
        with _request(url, token) as response:
            batch = json.load(response)
        if not isinstance(batch, list):
            raise ExecutionRefusal("CONTROL comment inventory is invalid")
        comments.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            return comments
        page += 1
        if page > 50:
            raise ExecutionRefusal("CONTROL comment inventory exceeds bounded pagination")


def parse_control_payload(comment: dict[str, Any], heading: str) -> dict[str, Any] | None:
    user = comment.get("user")
    if not isinstance(user, dict) or user.get("login") != TRUSTED_BOT:
        return None
    body = comment.get("body")
    if not isinstance(body, str) or not body.startswith(heading + "\n\n```json\n"):
        return None
    prefix = heading + "\n\n```json\n"
    if not body.endswith("\n```"):
        return None
    raw = body[len(prefix) : -4]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def refuse_prior_transaction(token: str, transaction_id: str) -> None:
    matches: list[tuple[str, int | None]] = []
    for comment in list_control_comments(token):
        for heading in (INTENT_HEADING, TERMINAL_HEADING):
            payload = parse_control_payload(comment, heading)
            if payload is not None and payload.get("transaction_id") == transaction_id:
                matches.append((heading, comment.get("id") if isinstance(comment.get("id"), int) else None))
    if matches:
        raise ExecutionRefusal(
            "destructive clean-install transaction already has durable CONTROL evidence; blind retry forbidden"
        )


def post_control(token: str, heading: str, payload: dict[str, Any]) -> int:
    body = heading + "\n\n```json\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n```"
    data = json.dumps({"body": body}).encode("utf-8")
    url = f"https://api.github.com/repos/{PRIVATE_REPOSITORY}/issues/{CONTROL_ISSUE}/comments"
    with _request(url, token, method="POST", data=data) as response:
        if response.status != 201:
            raise ExecutionRefusal("CONTROL evidence persistence failed")
        created = json.load(response)
    comment_id = created.get("id") if isinstance(created, dict) else None
    require(isinstance(comment_id, int), "CONTROL evidence comment identity is unavailable")
    return comment_id


def load_canonical(canonical_root: Path, canonical_sha: str):
    canonical_root = canonical_root.resolve()
    scripts = canonical_root / "scripts"
    require(scripts.is_dir(), "canonical scripts root is unavailable")
    required = (
        "clean_install_android_production.py",
        "run_private_phone_preflight.py",
        "verify_android_installed_signer.py",
    )
    for name in required:
        require((scripts / name).is_file(), f"canonical clean-install dependency is unavailable: {name}")
    sys.path.insert(0, str(canonical_root))
    try:
        module = importlib.import_module("scripts.clean_install_android_production")
    finally:
        try:
            sys.path.remove(str(canonical_root))
        except ValueError:
            pass
    require(module.require_canonical_sha(canonical_sha) == canonical_sha, "canonical SHA is invalid")
    return module


def classify_after_failure(
    clean,
    serial: str,
    *,
    expected_digest: str,
    digest_tool: Path,
    expected_version_name: str,
    expected_version_code: int,
) -> tuple[str, bool, dict[str, Any]]:
    """Observe only; never dispatch a recovery mutation here."""
    try:
        clean.prove_registered_device(serial)
        if not clean.package_present(serial):
            return (
                "PACKAGE_ABSENT_AFTER_FAILED_CLEAN_INSTALL",
                False,
                {
                    "package_present": False,
                    "exact_candidate_installed": False,
                    "recovery_mutation_performed": False,
                },
            )
        installed_code, installed_name = clean.package_version(serial)
        exact_version = (installed_code, installed_name) == (
            expected_version_code,
            expected_version_name,
        )
        exact_digest = False
        if exact_version:
            try:
                clean.verify_installed_apk_digest(serial, expected_digest, digest_tool)
                exact_digest = True
            except Exception:
                exact_digest = False
        if exact_version and exact_digest:
            return (
                "INSTALLED_EXACT_CANDIDATE_RECOVERED_POSTCONDITION",
                True,
                {
                    "package_present": True,
                    "exact_candidate_installed": True,
                    "exact_version_verified": True,
                    "exact_digest_verified": True,
                    "recovery_mutation_performed": False,
                },
            )
        return (
            "QUARANTINED_PACKAGE_STATE",
            False,
            {
                "package_present": True,
                "exact_candidate_installed": False,
                "exact_version_verified": exact_version,
                "exact_digest_verified": exact_digest,
                "recovery_mutation_performed": False,
            },
        )
    except Exception:
        return (
            "UNKNOWN_EXECUTION_OUTCOME",
            False,
            {
                "package_present": None,
                "exact_candidate_installed": False,
                "recovery_mutation_performed": False,
            },
        )


def execute(args: argparse.Namespace) -> dict[str, Any]:
    require(os.environ.get("GITHUB_REPOSITORY") == PRIVATE_REPOSITORY, "wrong private execution repository")
    require(os.environ.get("GITHUB_SHA") == args.private_sha, "private execution SHA differs")
    require(TRANSACTION_PATTERN.fullmatch(args.transaction_id) is not None, "transaction ID is invalid")
    token = os.environ.get("GH_TOKEN", "")
    require(bool(token), "GitHub CONTROL token is unavailable")

    clean = load_canonical(args.canonical_root, args.canonical_sha)
    refuse_prior_transaction(token, args.transaction_id)

    # Re-validate exact candidate off the phone before any destructive intent exists.
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

    # SAME-JOB registered-device proof before durable destructive intent.
    clean.prove_registered_device(serial)
    old_package_present = clean.package_present(serial)

    intent = {
        "format_version": 1,
        "operation_id": OPERATION_ID,
        "transaction_id": args.transaction_id,
        "request_ref": args.request_ref,
        "canonical_sha": args.canonical_sha,
        "quality_run_id": args.quality_run_id,
        "private_execution_sha": args.private_sha,
        "target": "android-production",
        "application_id": PACKAGE,
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
    intent_comment_id = post_control(token, INTENT_HEADING, intent)

    state = "UNKNOWN_EXECUTION_OUTCOME"
    accepted = False
    postcondition: dict[str, Any] = {
        "package_present": None,
        "exact_candidate_installed": False,
        "recovery_mutation_performed": False,
    }
    dispatch_error = False

    try:
        report = clean.clean_install(
            canonical_sha=args.canonical_sha,
            apk=args.apk,
            release_evidence=args.release_evidence,
            digest_tool=args.digest_tool,
            expected_version_name=args.expected_version_name,
            expected_version_code=args.expected_version_code,
        )
        require(report.get("accepted") is True, "canonical clean-install did not accept terminal state")
        require(report.get("new_apk_installed") is True, "canonical clean-install did not prove installed APK")
        require(report.get("new_apk_identity_verified") is True, "canonical clean-install did not prove APK identity")
        require(report.get("new_apk_digest_verified") is True, "canonical clean-install did not prove APK digest")
        state = "INSTALLED_EXACT_CANDIDATE"
        accepted = True
        postcondition = {
            "package_present": True,
            "exact_candidate_installed": True,
            "exact_version_verified": True,
            "exact_digest_verified": True,
            "recovery_mutation_performed": False,
        }
    except Exception:
        dispatch_error = True
        state, accepted, postcondition = classify_after_failure(
            clean,
            serial,
            expected_digest=expected_digest,
            digest_tool=args.digest_tool,
            expected_version_name=args.expected_version_name,
            expected_version_code=args.expected_version_code,
        )

    terminal = {
        "format_version": 1,
        "operation_id": OPERATION_ID,
        "transaction_id": args.transaction_id,
        "request_ref": args.request_ref,
        "canonical_sha": args.canonical_sha,
        "quality_run_id": args.quality_run_id,
        "private_execution_sha": args.private_sha,
        "intent_comment_id": intent_comment_id,
        "target": "android-production",
        "application_id": PACKAGE,
        "expected_version_name": args.expected_version_name,
        "expected_version_code": args.expected_version_code,
        "affected_domain_generations": {"domain/package": args.transaction_id},
        "state": state,
        "accepted": accepted,
        "dispatch_error_observed": dispatch_error,
        "postcondition": postcondition,
        "blind_retry_allowed": False,
        "old_generation_preservation_required": False,
        "raw_device_identifier_recorded": False,
        "artifact_digest_recorded": False,
        "phone_mutation_may_have_occurred": True,
    }
    terminal_comment_id = post_control(token, TERMINAL_HEADING, terminal)

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
    except (ExecutionRefusal, OSError, ValueError) as error:
        print(f"destructive clean-install refused before accepted terminal: {error}", file=sys.stderr)
        return 2
    return 0 if result.get("accepted") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
