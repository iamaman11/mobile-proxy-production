#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from control_request import RequestProvenance, build_request_envelope
from production_command_router import RouteRefused, classify


def expect_refused(**kwargs) -> None:
    try:
        classify(**kwargs)
    except RouteRefused:
        return
    raise AssertionError("command unexpectedly admitted")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        registry = Path(td) / "routes.json"
        registry.write_text(
            json.dumps(
                {
                    "schema": "production-command-routes.v1",
                    "authority_cursor": "issue179-comment-5529299637",
                    "routes": [
                        {
                            "command": "/observe",
                            "operation": "observe",
                            "workflow": "observe.yml",
                            "mutating": False,
                        },
                        {
                            "command": "/mutate",
                            "operation": "mutate",
                            "workflow": "mutate.yml",
                            "mutating": True,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        common = {
            "registry_path": registry,
            "repository": "iamaman11/mobile-proxy-production",
            "issue_number": 1,
            "actor": "iamaman11",
            "owner": "iamaman11",
        }

        route, first = classify(command_body="/mutate target generation", comment_id=101, **common)
        assert route.operation == "mutate"
        assert route.mutating is True
        assert first.arguments == ("target", "generation")

        # Provenance changes must never create a new semantic request.
        _, second = classify(command_body="/mutate target generation", comment_id=999, **common)
        assert first.request_id == second.request_id
        assert first.desired_generation == second.desired_generation
        assert first.provenance.comment_id != second.provenance.comment_id

        # Semantic changes and authority changes must create different request identities.
        _, changed_target = classify(command_body="/mutate other generation", comment_id=102, **common)
        assert changed_target.request_id != first.request_id

        alt = build_request_envelope(
            operation="mutate",
            arguments=("target", "generation"),
            authority_cursor="issue179-comment-5529299638",
            mutating=True,
            provenance=RequestProvenance(
                repository="iamaman11/mobile-proxy-production",
                issue_number=1,
                comment_id=101,
                actor="iamaman11",
            ),
        )
        assert alt.request_id != first.request_id

        # Strict single-command fail-closed truth table.
        for body in (
            "",
            " /mutate target generation",
            "/mutate target generation ",
            "/mutate target\n/observe target",
            "/mutate target /observe",
            "/unknown target",
            "mutate target",
        ):
            expect_refused(command_body=body, comment_id=103, **common)

        expect_refused(
            command_body="/mutate target generation",
            comment_id=104,
            registry_path=registry,
            repository="iamaman11/mobile-proxy-production",
            issue_number=2,
            actor="iamaman11",
            owner="iamaman11",
        )
        expect_refused(
            command_body="/mutate target generation",
            comment_id=105,
            registry_path=registry,
            repository="iamaman11/mobile-proxy-production",
            issue_number=1,
            actor="github-actions[bot]",
            owner="iamaman11",
        )

    print("PRODUCTION_COMMAND_ROUTER_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
