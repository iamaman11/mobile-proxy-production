#!/usr/bin/env python3
"""Cursor-bound admission for destructive Android clean-install operations.

The public hardening tracker is the authorization cursor. One tracker checkpoint
can authorize at most one semantic clean-install transaction. Private Issue #1
remains the append-only CONTROL ledger; this module performs no phone access.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import re
import sys
from typing import Any, Mapping, Protocol
import urllib.parse
import urllib.request

PUBLIC_REPOSITORY = "iamaman11/mobile-proxy"
PRIVATE_REPOSITORY = "iamaman11/mobile-proxy-production"
TRACKER_ISSUE = 179
CONTROL_ISSUE = 1
OWNER = "iamaman11"
TRUSTED_BOT = "github-actions[bot]"
OPERATION_ID = "android.clean-install-signing-generation.v1"
INTENT_HEADING = "## CONTROL APK CLEAN INSTALL INTENT"
TERMINAL_HEADING = "## CONTROL APK CLEAN INSTALL TERMINAL"
AUTH_OPERATION_LINE = f"CONTROL_AUTHORIZED_OPERATION={OPERATION_ID}"
AUTH_SHA_PREFIX = "CONTROL_AUTHORIZED_CANONICAL_SHA="
COMMAND_RE = re.compile(r"^/phone-clean-install ([0-9a-f]{40}) cursor=([1-9][0-9]*)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TX_RE = re.compile(r"^clean-install-cursor-[1-9][0-9]*$")


class AdmissionRefusal(RuntimeError):
    """Fail-closed admission refusal before any phone access."""


@dataclass(frozen=True)
class CleanInstallRequest:
    canonical_sha: str
    cursor_id: int
    transaction_id: str
    semantic_request_key: str


@dataclass(frozen=True)
class Admission:
    request: CleanInstallRequest
    quality_run_id: int
    command_comment_id: int


class ControlClient(Protocol):
    def public_main_sha(self) -> str: ...
    def private_main_sha(self) -> str: ...
    def quality_runs(self, canonical_sha: str) -> list[Mapping[str, Any]]: ...
    def tracker_comments(self) -> list[Mapping[str, Any]]: ...
    def control_comments(self) -> list[Mapping[str, Any]]: ...


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AdmissionRefusal(message)


def parse_command(body: str) -> CleanInstallRequest:
    match = COMMAND_RE.fullmatch(body.strip())
    if match is None:
        raise AdmissionRefusal("invalid cursor-bound destructive clean-install command")
    canonical_sha = match.group(1)
    cursor_id = int(match.group(2))
    transaction_id = f"clean-install-cursor-{cursor_id}"
    semantic_request_key = f"{OPERATION_ID}:{canonical_sha}:tracker-comment:{cursor_id}"
    return CleanInstallRequest(canonical_sha, cursor_id, transaction_id, semantic_request_key)


def _user_login(comment: Mapping[str, Any]) -> str:
    user = comment.get("user")
    return str(user.get("login", "")) if isinstance(user, Mapping) else ""


def _comment_id(comment: Mapping[str, Any]) -> int | None:
    value = comment.get("id")
    return value if isinstance(value, int) and value > 0 else None


def _body(comment: Mapping[str, Any]) -> str:
    value = comment.get("body")
    return value if isinstance(value, str) else ""


def _trusted_payload(comment: Mapping[str, Any], heading: str) -> dict[str, Any] | None:
    if _user_login(comment) != TRUSTED_BOT:
        return None
    body = _body(comment)
    prefix = heading + "\n\n```json\n"
    suffix = "\n```"
    if not body.startswith(prefix) or not body.endswith(suffix):
        return None
    try:
        value = json.loads(body[len(prefix) : -len(suffix)])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _latest_tracker_checkpoint(comments: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    checkpoints = [
        item
        for item in comments
        if _user_login(item) == OWNER
        and _comment_id(item) is not None
        and _body(item).startswith("## Progress checkpoint")
    ]
    require(bool(checkpoints), "public tracker has no authoritative progress checkpoint")
    return max(checkpoints, key=lambda item: int(_comment_id(item) or 0))


def _validate_tracker_authorization(
    comments: list[Mapping[str, Any]], request: CleanInstallRequest
) -> None:
    latest = _latest_tracker_checkpoint(comments)
    latest_id = _comment_id(latest)
    require(latest_id == request.cursor_id, "clean-install cursor is not the latest authoritative tracker checkpoint")
    body = _body(latest)
    lines = {line.strip() for line in body.splitlines()}
    require(AUTH_OPERATION_LINE in lines, "latest tracker checkpoint does not authorize destructive clean install")
    require(
        f"{AUTH_SHA_PREFIX}{request.canonical_sha}" in lines,
        "latest tracker checkpoint does not authorize this canonical SHA",
    )


def _quality_run_id(runs: list[Mapping[str, Any]], canonical_sha: str) -> int:
    accepted: list[tuple[int, int]] = []
    for run in runs:
        run_id = run.get("id")
        attempt = run.get("run_attempt", 0)
        if (
            isinstance(run_id, int)
            and run_id > 0
            and run.get("name") == "Quality"
            and run.get("head_branch") == "main"
            and run.get("head_sha") == canonical_sha
            and run.get("conclusion") == "success"
        ):
            accepted.append((int(attempt) if isinstance(attempt, int) else 0, run_id))
    require(bool(accepted), "current canonical SHA has no exact successful main Quality run")
    accepted.sort(reverse=True)
    return accepted[0][1]


def _semantic_winner(
    comments: list[Mapping[str, Any]], request: CleanInstallRequest
) -> int:
    matches: list[int] = []
    for comment in comments:
        if _user_login(comment) != OWNER:
            continue
        comment_id = _comment_id(comment)
        if comment_id is None:
            continue
        try:
            observed = parse_command(_body(comment))
        except AdmissionRefusal:
            continue
        if observed.semantic_request_key == request.semantic_request_key:
            matches.append(comment_id)
    require(bool(matches), "semantic clean-install owner command is absent from CONTROL issue")
    return min(matches)


def _refuse_existing_durable_evidence(
    comments: list[Mapping[str, Any]], request: CleanInstallRequest
) -> None:
    for comment in comments:
        for heading in (INTENT_HEADING, TERMINAL_HEADING):
            payload = _trusted_payload(comment, heading)
            if payload is None:
                continue
            if payload.get("transaction_id") == request.transaction_id:
                raise AdmissionRefusal("cursor transaction already has durable CONTROL evidence; blind retry forbidden")
            if payload.get("semantic_request_key") == request.semantic_request_key:
                raise AdmissionRefusal("semantic clean-install request already has durable CONTROL evidence")


def admit(
    client: ControlClient,
    *,
    command_body: str,
    command_comment_id: int,
    workflow_sha: str,
    expected_quality_run_id: int | None = None,
    expected_transaction_id: str | None = None,
    expected_semantic_request_key: str | None = None,
) -> Admission:
    require(command_comment_id > 0, "owner command comment identity is invalid")
    require(SHA_RE.fullmatch(workflow_sha) is not None, "private workflow SHA is invalid")
    request = parse_command(command_body)
    require(TX_RE.fullmatch(request.transaction_id) is not None, "derived cursor transaction identity is invalid")

    require(client.public_main_sha() == request.canonical_sha, "requested SHA is not current canonical public main")
    require(client.private_main_sha() == workflow_sha, "workflow execution SHA is not current private main")
    quality_run_id = _quality_run_id(client.quality_runs(request.canonical_sha), request.canonical_sha)
    if expected_quality_run_id is not None:
        require(quality_run_id == expected_quality_run_id, "Quality authority changed after hosted admission")

    _validate_tracker_authorization(client.tracker_comments(), request)
    control_comments = client.control_comments()
    require(
        _semantic_winner(control_comments, request) == command_comment_id,
        "duplicate semantic clean-install owner command is not the elected request",
    )
    _refuse_existing_durable_evidence(control_comments, request)

    if expected_transaction_id is not None:
        require(request.transaction_id == expected_transaction_id, "cursor transaction changed after hosted admission")
    if expected_semantic_request_key is not None:
        require(
            request.semantic_request_key == expected_semantic_request_key,
            "semantic clean-install request changed after hosted admission",
        )
    return Admission(request, quality_run_id, command_comment_id)


@dataclass(frozen=True)
class RestControlClient:
    token: str

    def _get_json(self, url: str) -> Any:
        require(bool(self.token), "GitHub CONTROL token is unavailable")
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "mobile-proxy-production-clean-install-control",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def public_main_sha(self) -> str:
        value = self._get_json(f"https://api.github.com/repos/{PUBLIC_REPOSITORY}/commits/main")
        return str(value.get("sha", "")) if isinstance(value, Mapping) else ""

    def private_main_sha(self) -> str:
        value = self._get_json(f"https://api.github.com/repos/{PRIVATE_REPOSITORY}/commits/main")
        return str(value.get("sha", "")) if isinstance(value, Mapping) else ""

    def quality_runs(self, canonical_sha: str) -> list[Mapping[str, Any]]:
        query = urllib.parse.urlencode(
            {"head_sha": canonical_sha, "event": "push", "status": "completed", "per_page": 100}
        )
        value = self._get_json(f"https://api.github.com/repos/{PUBLIC_REPOSITORY}/actions/runs?{query}")
        runs = value.get("workflow_runs", []) if isinstance(value, Mapping) else []
        return [item for item in runs if isinstance(item, Mapping)]

    def _issue_comments(self, repository: str, issue_number: int) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        for page in range(1, 51):
            value = self._get_json(
                f"https://api.github.com/repos/{repository}/issues/{issue_number}/comments?per_page=100&page={page}"
            )
            require(isinstance(value, list), "GitHub issue comment inventory is invalid")
            result.extend(item for item in value if isinstance(item, Mapping))
            if len(value) < 100:
                return result
        raise AdmissionRefusal("GitHub issue comment pagination exceeded bound")

    def tracker_comments(self) -> list[Mapping[str, Any]]:
        return self._issue_comments(PUBLIC_REPOSITORY, TRACKER_ISSUE)

    def control_comments(self) -> list[Mapping[str, Any]]:
        return self._issue_comments(PRIVATE_REPOSITORY, CONTROL_ISSUE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("admit",))
    parser.add_argument("--command-body", required=True)
    parser.add_argument("--command-comment-id", type=int, required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--expected-quality-run-id", type=int)
    parser.add_argument("--expected-transaction-id")
    parser.add_argument("--expected-semantic-request-key")
    parser.add_argument("--github-output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    try:
        accepted = admit(
            RestControlClient(token),
            command_body=args.command_body,
            command_comment_id=args.command_comment_id,
            workflow_sha=args.workflow_sha,
            expected_quality_run_id=args.expected_quality_run_id,
            expected_transaction_id=args.expected_transaction_id,
            expected_semantic_request_key=args.expected_semantic_request_key,
        )
    except (AdmissionRefusal, OSError, ValueError) as error:
        print(f"clean-install admission refused: {type(error).__name__}: {error}", file=sys.stderr)
        return 2

    outputs = {
        "canonical_sha": accepted.request.canonical_sha,
        "cursor_id": str(accepted.request.cursor_id),
        "quality_run_id": str(accepted.quality_run_id),
        "transaction_id": accepted.request.transaction_id,
        "semantic_request_key": accepted.request.semantic_request_key,
    }
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")
    else:
        print(json.dumps(outputs, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
