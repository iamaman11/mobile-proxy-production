#!/usr/bin/env python3
"""Shared semantic request identity for production-control operations.

Comment IDs, workflow run IDs and run attempts are provenance only.  They are
intentionally excluded from the semantic request identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

SCHEMA = "production-control-request.v1"
_CURSOR_RE = re.compile(r"issue179-comment-[1-9][0-9]*")
_OPERATION_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}")


class RequestContractError(ValueError):
    """Fail-closed semantic request contract violation."""


def _digest(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_arguments(arguments: Iterable[str]) -> tuple[str, ...]:
    result = tuple(str(value).strip() for value in arguments)
    if any(not value for value in result):
        raise RequestContractError("empty semantic argument")
    if any("\n" in value or "\r" in value or "\x00" in value for value in result):
        raise RequestContractError("multiline/NUL semantic argument")
    return result


def desired_generation(operation: str, arguments: Iterable[str]) -> str:
    args = normalize_arguments(arguments)
    return "gen-sha256:" + _digest({"operation": operation, "arguments": list(args)})


@dataclass(frozen=True)
class RequestProvenance:
    repository: str
    issue_number: int
    comment_id: int
    actor: str
    event_name: str = "issue_comment"

    def validate(self) -> None:
        if self.issue_number != 1:
            raise RequestContractError("production command must originate from private Issue #1")
        if self.comment_id <= 0:
            raise RequestContractError("invalid source comment id")
        if not self.repository or not self.actor:
            raise RequestContractError("incomplete provenance")
        if self.event_name != "issue_comment":
            raise RequestContractError("unsupported source event")


@dataclass(frozen=True)
class RequestEnvelope:
    schema: str
    request_id: str
    operation: str
    arguments: tuple[str, ...]
    authority_cursor: str
    desired_generation: str
    mutating: bool
    provenance: RequestProvenance

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "operation": self.operation,
            "arguments": list(self.arguments),
            "authority_cursor": self.authority_cursor,
            "desired_generation": self.desired_generation,
        }

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["arguments"] = list(self.arguments)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build_request_envelope(
    *,
    operation: str,
    arguments: Iterable[str],
    authority_cursor: str,
    mutating: bool,
    provenance: RequestProvenance,
    generation: str | None = None,
) -> RequestEnvelope:
    if _OPERATION_RE.fullmatch(operation) is None:
        raise RequestContractError("invalid operation name")
    if _CURSOR_RE.fullmatch(authority_cursor) is None:
        raise RequestContractError("invalid authority cursor")
    provenance.validate()
    args = normalize_arguments(arguments)
    generation_value = generation or desired_generation(operation, args)
    if not generation_value.startswith("gen-") or len(generation_value) > 160:
        raise RequestContractError("invalid desired generation")

    semantic = {
        "schema": SCHEMA,
        "operation": operation,
        "arguments": list(args),
        "authority_cursor": authority_cursor,
        "desired_generation": generation_value,
    }
    request_id = "req-sha256:" + _digest(semantic)
    return RequestEnvelope(
        schema=SCHEMA,
        request_id=request_id,
        operation=operation,
        arguments=args,
        authority_cursor=authority_cursor,
        desired_generation=generation_value,
        mutating=bool(mutating),
        provenance=provenance,
    )
