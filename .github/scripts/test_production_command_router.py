#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from control_request import RequestProvenance, build_request_envelope
from production_command_router import RouteRefused, classify, load_registry


GLOBAL_CURSOR = "issue179-comment-5531154097"
RECOVERY_CURSOR = "issue179-comment-5533040841"


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
                    "authority_cursor": GLOBAL_CURSOR,
                    "routes": [
                        {
                            "command": "/observe",
                            "operation": "observe",
                            "workflow": "observe.yml",
                            "mutating": False,
                        },
                        {
                            "command": "/recover",
                            "operation": "recover",
                            "workflow": "mixed.yml",
                            "mutating": False,
                            "authority_cursor": RECOVERY_CURSOR,
                        },
                        {
                            "command": "/mutate-a",
                            "operation": "mutate-a",
                            "workflow": "physical-transaction-readiness.yml",
                            "mutating": True,
                        },
                        {
                            "command": "/mutate-b",
                            "operation": "mutate-b",
                            "workflow": "physical-transaction-readiness.yml",
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

        global_cursor, loaded = load_registry(registry)
        assert global_cursor == GLOBAL_CURSOR
        assert loaded["/mutate-a"].workflow == loaded["/mutate-b"].workflow
        assert loaded["/mutate-a"].operation != loaded["/mutate-b"].operation
        assert loaded["/mutate-a"].authority_cursor == GLOBAL_CURSOR
        assert loaded["/observe"].authority_cursor == GLOBAL_CURSOR
        assert loaded["/recover"].authority_cursor == RECOVERY_CURSOR
        assert loaded["/recover"].mutating is False

        route, first = classify(command_body="/mutate-a target generation", comment_id=101, **common)
        assert route.operation == "mutate-a"
        assert route.mutating is True
        assert route.authority_cursor == GLOBAL_CURSOR
        assert first.arguments == ("target", "generation")
        assert first.authority_cursor == GLOBAL_CURSOR

        route_b, other_operation = classify(
            command_body="/mutate-b target generation",
            comment_id=101,
            **common,
        )
        assert route_b.workflow == route.workflow
        assert other_operation.request_id != first.request_id

        # Provenance changes must never create a new semantic request.
        _, second = classify(command_body="/mutate-a target generation", comment_id=999, **common)
        assert first.request_id == second.request_id
        assert first.desired_generation == second.desired_generation
        assert first.provenance.comment_id != second.provenance.comment_id

        # The bounded recovery route uses its own authority while legacy routes remain unchanged.
        recovery_route, recovery_first = classify(
            command_body="/recover issue-comment:5532752064",
            comment_id=201,
            **common,
        )
        _, recovery_second = classify(
            command_body="/recover issue-comment:5532752064",
            comment_id=202,
            **common,
        )
        assert recovery_route.authority_cursor == RECOVERY_CURSOR
        assert recovery_first.authority_cursor == RECOVERY_CURSOR
        assert recovery_first.arguments == ("issue-comment:5532752064",)
        assert recovery_first.request_id == recovery_second.request_id
        assert recovery_first.provenance.comment_id != recovery_second.provenance.comment_id

        default_recovery_identity = build_request_envelope(
            operation="recover",
            arguments=("issue-comment:5532752064",),
            authority_cursor=GLOBAL_CURSOR,
            mutating=False,
            provenance=RequestProvenance(
                repository="iamaman11/mobile-proxy-production",
                issue_number=1,
                comment_id=201,
                actor="iamaman11",
            ),
        )
        assert recovery_first.request_id != default_recovery_identity.request_id

        # Semantic changes and authority changes must create different request identities.
        _, changed_target = classify(command_body="/mutate-a other generation", comment_id=102, **common)
        assert changed_target.request_id != first.request_id

        alt = build_request_envelope(
            operation="mutate-a",
            arguments=("target", "generation"),
            authority_cursor="issue179-comment-5531154098",
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
            " /mutate-a target generation",
            "/mutate-a target generation ",
            "/mutate-a target\n/observe target",
            "/mutate-a target /observe",
            "/unknown target",
            "mutate-a target",
        ):
            expect_refused(command_body=body, comment_id=103, **common)

        expect_refused(
            command_body="/mutate-a target generation",
            comment_id=104,
            registry_path=registry,
            repository="iamaman11/mobile-proxy-production",
            issue_number=2,
            actor="iamaman11",
            owner="iamaman11",
        )
        expect_refused(
            command_body="/mutate-a target generation",
            comment_id=105,
            registry_path=registry,
            repository="iamaman11/mobile-proxy-production",
            issue_number=1,
            actor="github-actions[bot]",
            owner="iamaman11",
        )

        duplicate_operation = Path(td) / "duplicate-operation.json"
        duplicate_operation.write_text(
            json.dumps(
                {
                    "schema": "production-command-routes.v1",
                    "authority_cursor": GLOBAL_CURSOR,
                    "routes": [
                        {"command": "/a", "operation": "same", "workflow": "shared.yml", "mutating": True},
                        {"command": "/b", "operation": "same", "workflow": "shared.yml", "mutating": True},
                    ],
                }
            ),
            encoding="utf-8",
        )
        try:
            load_registry(duplicate_operation)
        except RouteRefused:
            pass
        else:
            raise AssertionError("duplicate semantic operation unexpectedly admitted")

        malformed_route_cursor = Path(td) / "malformed-route-cursor.json"
        malformed_route_cursor.write_text(
            json.dumps(
                {
                    "schema": "production-command-routes.v1",
                    "authority_cursor": GLOBAL_CURSOR,
                    "routes": [
                        {
                            "command": "/recover",
                            "operation": "recover",
                            "workflow": "mixed.yml",
                            "mutating": False,
                            "authority_cursor": "issue179-comment-NOT-NUMERIC",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        try:
            load_registry(malformed_route_cursor)
        except RouteRefused:
            pass
        else:
            raise AssertionError("malformed per-route authority cursor unexpectedly admitted")

    print("PRODUCTION_COMMAND_ROUTER_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())