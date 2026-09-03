from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

PRIVATE_REPOSITORY = "iamaman11/mobile-proxy-production"
ISSUE_NUMBER = 1
TRUSTED_ACTOR = "github-actions[bot]"
INTENT_HEADING = "## DEPLOYMENT MUTATION INTENT V2"
TERMINAL_HEADING = "## DEPLOYMENT TERMINAL V2"
DUPLICATE_HEADING = "## DEPLOYMENT DUPLICATE V2"


class EvidenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvidenceRecord:
    comment_id: int
    heading: str
    payload: Mapping[str, Any]

    @property
    def ref(self) -> str:
        return f"issue-comment:{self.comment_id}"


def _body(heading: str, payload: Mapping[str, Any]) -> str:
    return "\n".join((heading, "", "```json", json.dumps(payload, indent=2, sort_keys=True), "```"))


def _parse_body(body: str, heading: str) -> Mapping[str, Any] | None:
    prefix = heading + "\n\n```json\n"
    suffix = "\n```"
    normalized = body.rstrip("\n")
    if not normalized.startswith(prefix):
        return None
    if not normalized.endswith(suffix):
        raise EvidenceError(f"trusted {heading} comment is malformed")
    raw = normalized[len(prefix) : -len(suffix)]
    if len(raw) > 100_000:
        raise EvidenceError(f"trusted {heading} payload exceeds bound")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"trusted {heading} JSON is invalid") from exc
    if not isinstance(value, Mapping):
        raise EvidenceError(f"trusted {heading} JSON is not an object")
    return value


class IssueEvidenceStore:
    def __init__(self, token: str) -> None:
        if not token:
            raise EvidenceError("private evidence token is unavailable")
        self.token = token

    def _open(self, url: str, *, method: str = "GET", payload: bytes | None = None):
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "mobile-proxy-production-controller-v2",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            return urllib.request.urlopen(request, timeout=30)
        except (OSError, urllib.error.URLError) as exc:
            raise EvidenceError("private durable evidence transport failed") from exc

    def list_records(self, heading: str) -> list[EvidenceRecord]:
        result: list[EvidenceRecord] = []
        for page in range(1, 101):
            url = (
                f"https://api.github.com/repos/{PRIVATE_REPOSITORY}/issues/{ISSUE_NUMBER}"
                f"/comments?per_page=100&page={page}"
            )
            with self._open(url) as response:
                payload = json.load(response)
            if not isinstance(payload, list):
                raise EvidenceError("private comment inventory is invalid")
            for item in payload:
                if not isinstance(item, Mapping):
                    continue
                user = item.get("user")
                if not isinstance(user, Mapping) or user.get("login") != TRUSTED_ACTOR:
                    continue
                body = item.get("body")
                if not isinstance(body, str):
                    continue
                parsed = _parse_body(body, heading)
                if parsed is None:
                    continue
                comment_id = item.get("id")
                if not isinstance(comment_id, int) or comment_id <= 0:
                    raise EvidenceError("trusted evidence comment id is invalid")
                result.append(EvidenceRecord(comment_id, heading, parsed))
            if len(payload) < 100:
                return result
        raise EvidenceError("private Issue #1 evidence inventory exceeds bounded scan")

    def create(self, heading: str, payload: Mapping[str, Any]) -> EvidenceRecord:
        url = f"https://api.github.com/repos/{PRIVATE_REPOSITORY}/issues/{ISSUE_NUMBER}/comments"
        encoded = json.dumps({"body": _body(heading, payload)}).encode("utf-8")
        with self._open(url, method="POST", payload=encoded) as response:
            value = json.load(response)
        comment_id = value.get("id") if isinstance(value, Mapping) else None
        if not isinstance(comment_id, int) or comment_id <= 0:
            raise EvidenceError("durable private evidence comment was not created")
        return EvidenceRecord(comment_id, heading, dict(payload))

    def request_history(self, semantic_request_id: str) -> tuple[EvidenceRecord | None, EvidenceRecord | None]:
        intents = [
            item for item in self.list_records(INTENT_HEADING)
            if item.payload.get("semantic_request_id") == semantic_request_id
        ]
        terminals = [
            item for item in self.list_records(TERMINAL_HEADING)
            if item.payload.get("semantic_request_id") == semantic_request_id
        ]
        if len(intents) > 1:
            first = json.dumps(intents[0].payload, sort_keys=True)
            if any(json.dumps(item.payload, sort_keys=True) != first for item in intents[1:]):
                raise EvidenceError("semantic request has conflicting durable mutation intents")
        if len(terminals) > 1:
            first = json.dumps(terminals[0].payload, sort_keys=True)
            if any(json.dumps(item.payload, sort_keys=True) != first for item in terminals[1:]):
                raise EvidenceError("semantic request has conflicting durable terminals")
        return (intents[0] if intents else None, terminals[0] if terminals else None)

    def persist_intent(self, payload: Mapping[str, Any]) -> EvidenceRecord:
        request_id = str(payload.get("semantic_request_id", ""))
        existing_intent, existing_terminal = self.request_history(request_id)
        if existing_terminal is not None:
            raise EvidenceError("terminal already exists; mutation intent cannot be created")
        if existing_intent is not None:
            return existing_intent
        if payload.get("blind_retry_allowed") is not False or payload.get("dispatch_may_reach_target") is not True:
            raise EvidenceError("mutation intent lacks no-blind-retry boundary")
        return self.create(INTENT_HEADING, payload)

    def persist_terminal(self, payload: Mapping[str, Any]) -> EvidenceRecord:
        request_id = str(payload.get("semantic_request_id", ""))
        _, existing_terminal = self.request_history(request_id)
        if existing_terminal is not None:
            if json.dumps(existing_terminal.payload, sort_keys=True) != json.dumps(payload, sort_keys=True):
                raise EvidenceError("semantic request already has a different canonical terminal")
            return existing_terminal
        return self.create(TERMINAL_HEADING, payload)

    def persist_duplicate_projection(self, payload: Mapping[str, Any]) -> EvidenceRecord:
        return self.create(DUPLICATE_HEADING, payload)
