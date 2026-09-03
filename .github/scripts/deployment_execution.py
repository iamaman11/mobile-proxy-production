#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))
from deployment_request import validate_deployment_request  # noqa: E402

_SHA = re.compile(r"[0-9a-f]{40}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--record-path", type=Path, required=True)
    parser.add_argument("--body-path", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request_json)
    validate_deployment_request(request)
    if args.run_id <= 0 or args.run_attempt <= 0 or _SHA.fullmatch(args.controller_revision) is None:
        raise SystemExit("invalid execution provenance")
    execution_id = f"gh-run:{args.run_id}:{args.run_attempt}"
    record = {
        "schema": "production-deployment-ack.v2",
        "operation": request["operation"],
        "semantic_request_id": request["request_id"],
        "execution_id": execution_id,
        "controller_revision": args.controller_revision,
        "target": request["target"],
        "product_release": request["product_release_tag"],
        "source_comment_id": request["provenance"]["comment_id"],
        "state": "REQUEST",
        "terminal": False,
    }
    args.record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    body = "\n".join([
        "## DEPLOYMENT EXECUTION ACK",
        "",
        f"- operation: `{record['operation']}`",
        f"- semantic_request_id: `{record['semantic_request_id']}`",
        f"- execution_id: `{execution_id}`",
        f"- controller_revision: `{args.controller_revision}`",
        f"- target: `{record['target']}`",
        f"- product_release: `{record['product_release']}`",
        "- state: `REQUEST`",
        "- terminal: `false`",
        "",
        "```json",
        json.dumps(record, indent=2, sort_keys=True),
        "```",
    ])
    args.body_path.write_text(body + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
