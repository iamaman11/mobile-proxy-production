from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping

ACK_SCHEMA = "control-execution-ack.v1"
EXECUTION_RE = re.compile(r"gh-run:([1-9][0-9]*):([1-9][0-9]*)")
REQUEST_RE = re.compile(r"req-sha256:[0-9a-f]{64}")
CURSOR_RE = re.compile(r"issue179-comment-[1-9][0-9]*")
SHA_RE = re.compile(r"[0-9a-f]{40}")


class ExecutionContractError(ValueError):
    pass


def _positive_int(value: object, *, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionContractError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ExecutionContractError(f"{field} must be a positive integer")
    return parsed


def execution_id(run_id: object, run_attempt: object) -> str:
    run = _positive_int(run_id, field="workflow_run_id")
    attempt = _positive_int(run_attempt, field="workflow_run_attempt")
    return f"gh-run:{run}:{attempt}"


def _request_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionContractError("control request must be an object")
    return value


def build_execution_ack(
    request: object,
    *,
    run_id: object,
    run_attempt: object,
    private_sha: str,
) -> dict[str, object]:
    envelope = _request_mapping(request)
    if envelope.get("schema") != "production-control-request.v1":
        raise ExecutionContractError("unexpected control request schema")

    semantic_request_id = str(envelope.get("request_id", ""))
    if REQUEST_RE.fullmatch(semantic_request_id) is None:
        raise ExecutionContractError("invalid semantic request id")

    operation = str(envelope.get("operation", "")).strip()
    if not operation or any(character.isspace() for character in operation):
        raise ExecutionContractError("invalid operation")

    request_authority_cursor = str(envelope.get("authority_cursor", ""))
    if CURSOR_RE.fullmatch(request_authority_cursor) is None:
        raise ExecutionContractError("invalid request authority cursor")

    provenance = envelope.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ExecutionContractError("control request provenance is missing")
    source_comment_id = _positive_int(
        provenance.get("comment_id"), field="source_comment_id"
    )
    if int(provenance.get("issue_number", 0)) != 1:
        raise ExecutionContractError("execution ACK is only valid for private Issue #1")

    normalized_private_sha = str(private_sha).strip()
    if SHA_RE.fullmatch(normalized_private_sha) is None:
        raise ExecutionContractError("invalid private execution SHA")

    arguments = envelope.get("arguments")
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        raise ExecutionContractError("invalid control request arguments")
    requested_canonical_sha: str | None = None
    if arguments and SHA_RE.fullmatch(arguments[0]):
        requested_canonical_sha = arguments[0]

    run = _positive_int(run_id, field="workflow_run_id")
    attempt = _positive_int(run_attempt, field="workflow_run_attempt")
    record: dict[str, object] = {
        "schema": ACK_SCHEMA,
        "source_comment_id": source_comment_id,
        "semantic_request_id": semantic_request_id,
        "execution_id": execution_id(run, attempt),
        "workflow_run_id": run,
        "workflow_run_attempt": attempt,
        "operation": operation,
        "requested_canonical_sha": requested_canonical_sha,
        "private_sha": normalized_private_sha,
        "request_authority_cursor": request_authority_cursor,
        "mutating": envelope.get("mutating") is True,
        "state": "ROUTED",
        "terminal": False,
    }
    validate_execution_ack(record)
    return record


def validate_execution_ack(record: object) -> Mapping[str, object]:
    value = _request_mapping(record)
    if value.get("schema") != ACK_SCHEMA:
        raise ExecutionContractError("invalid execution ACK schema")
    run = _positive_int(value.get("workflow_run_id"), field="workflow_run_id")
    attempt = _positive_int(
        value.get("workflow_run_attempt"), field="workflow_run_attempt"
    )
    expected_execution_id = execution_id(run, attempt)
    if value.get("execution_id") != expected_execution_id:
        raise ExecutionContractError("execution id does not match run attempt")
    if EXECUTION_RE.fullmatch(str(value.get("execution_id", ""))) is None:
        raise ExecutionContractError("invalid execution id")
    if REQUEST_RE.fullmatch(str(value.get("semantic_request_id", ""))) is None:
        raise ExecutionContractError("invalid semantic request id")
    if _positive_int(value.get("source_comment_id"), field="source_comment_id") <= 0:
        raise AssertionError("unreachable")
    if SHA_RE.fullmatch(str(value.get("private_sha", ""))) is None:
        raise ExecutionContractError("invalid private SHA")
    requested = value.get("requested_canonical_sha")
    if requested is not None and SHA_RE.fullmatch(str(requested)) is None:
        raise ExecutionContractError("invalid requested canonical SHA")
    if CURSOR_RE.fullmatch(str(value.get("request_authority_cursor", ""))) is None:
        raise ExecutionContractError("invalid request authority cursor")
    if value.get("state") != "ROUTED" or value.get("terminal") is not False:
        raise ExecutionContractError("execution ACK must be non-terminal ROUTED state")
    if "physical_transaction_id" in value:
        raise ExecutionContractError("execution ACK must not invent physical transaction identity")
    return value


def render_execution_ack(record: Mapping[str, object]) -> str:
    validate_execution_ack(record)
    requested = record.get("requested_canonical_sha") or "n/a"
    return "\n".join(
        [
            "## CONTROL EXECUTION ACK",
            "",
            f"- operation: `{record['operation']}`",
            f"- source_comment_id: `{record['source_comment_id']}`",
            f"- semantic_request_id: `{record['semantic_request_id']}`",
            f"- execution_id: `{record['execution_id']}`",
            f"- workflow_run_id: `{record['workflow_run_id']}`",
            f"- workflow_run_attempt: `{record['workflow_run_attempt']}`",
            f"- requested_canonical_sha: `{requested}`",
            f"- private_sha: `{record['private_sha']}`",
            f"- request_authority_cursor: `{record['request_authority_cursor']}`",
            f"- state: `{record['state']}`",
            "- terminal: `false`",
            "",
            "Live execution state authority: the exact GitHub Actions run/jobs identified above.",
            "This Issue comment is an immutable operator projection, not terminal authority.",
            "",
            "```json",
            json.dumps(record, indent=2, sort_keys=True),
            "```",
        ]
    )


def _load_json(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExecutionContractError("invalid control request JSON") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--private-sha", required=True)
    parser.add_argument("--record-path", required=True)
    parser.add_argument("--body-path", required=True)
    args = parser.parse_args()

    record = build_execution_ack(
        _load_json(args.request_json),
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        private_sha=args.private_sha,
    )
    Path(args.record_path).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    Path(args.body_path).write_text(render_execution_ack(record) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
