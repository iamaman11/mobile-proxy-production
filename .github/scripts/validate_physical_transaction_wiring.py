#!/usr/bin/env python3
"""Hosted fail-closed verifier for private -> canonical physical transaction wiring."""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path


EXPECTED_SCHEMA = "physical-transaction-wiring.v1"
EXPECTED_CURSOR = "issue179-comment-5531154097"
EXPECTED_REPOSITORY = "iamaman11/mobile-proxy"
EXPECTED_SHA = "8e994aafcf1b56a86cffef1d99f380393fa71f17"
EXPECTED_QUALITY_RUN = 33797487865


class WiringError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise WiringError(message)


def load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        fail(f"{path}: expected JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-root", type=Path, default=Path("."))
    parser.add_argument("--canonical-root", type=Path, required=True)
    args = parser.parse_args(argv)

    private_root = args.private_root.resolve()
    canonical_root = args.canonical_root.resolve()
    binding_path = private_root / ".github/production/physical-transaction-wiring.json"
    routes_path = private_root / ".github/production/command-routes.json"
    router_workflow_path = private_root / ".github/workflows/production-control-router.yml"

    binding = load_json(binding_path)
    routes = load_json(routes_path)

    if binding.get("schema") != EXPECTED_SCHEMA:
        fail("physical wiring schema differs")
    if binding.get("authority_cursor") != EXPECTED_CURSOR:
        fail("physical wiring authority cursor differs")
    if binding.get("canonical_repository") != EXPECTED_REPOSITORY:
        fail("canonical repository differs")
    if binding.get("canonical_sha") != EXPECTED_SHA:
        fail("canonical SHA differs")
    if binding.get("canonical_quality_run") != EXPECTED_QUALITY_RUN:
        fail("canonical Quality run differs")
    if binding.get("phone_execution_enabled") is not False:
        fail("phone execution must remain disabled in this stage")

    shared_workflow = str(binding.get("shared_mutation_workflow", ""))
    if shared_workflow != "physical-transaction-readiness.yml":
        fail("unexpected shared mutation workflow")

    actual_sha = subprocess.check_output(
        ["git", "-C", str(canonical_root), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if actual_sha != EXPECTED_SHA:
        fail(f"canonical checkout SHA differs: {actual_sha}")

    scripts = canonical_root / "scripts"
    sys.path.insert(0, str(scripts))
    plan = importlib.import_module("physical_operation_plan")
    registry = importlib.import_module("operations.registry")
    transaction = importlib.import_module("transaction_runner")

    inventory_errors = tuple(plan.validate_private_mutator_inventory())
    if inventory_errors:
        fail("canonical private mutator inventory invalid: " + "; ".join(inventory_errors))
    if not hasattr(transaction, "TransactionRunner"):
        fail("canonical Universal TransactionRunner missing")

    inventory = {item.workflow: item for item in plan.PRIVATE_MUTATOR_INVENTORY}
    binding_items = binding.get("bindings")
    if not isinstance(binding_items, list) or not binding_items:
        fail("physical wiring bindings missing")
    private_map: dict[str, str] = {}
    for item in binding_items:
        if not isinstance(item, dict):
            fail("physical wiring binding entry malformed")
        operation = str(item.get("operation", ""))
        workflow = str(item.get("legacy_workflow", ""))
        if not operation or not workflow:
            fail("physical wiring binding entry incomplete")
        if operation in private_map:
            fail(f"duplicate private operation binding: {operation}")
        private_map[operation] = workflow

    if set(private_map.values()) != set(inventory):
        fail("private legacy workflow mapping differs from canonical mutator inventory")

    canonical_atomic = set(registry.CANONICAL_BINDING_TYPES)
    if canonical_atomic != set(registry.CANONICAL_EXECUTOR_TYPES):
        fail("canonical binding/executor registry key sets differ")

    for operation, workflow in sorted(private_map.items()):
        item = inventory[workflow]
        if item.disposition == "hard_blocked":
            if item.operation_id is not None or item.plan_id is not None:
                fail(f"{workflow}: canonical hard block unexpectedly maps to execution")
            continue
        if item.disposition == "atomic":
            if item.operation_id not in canonical_atomic:
                fail(f"{workflow}: canonical atomic operation lacks executor binding")
            continue
        if item.disposition == "composite":
            composite = plan.composite_plan(item.plan_id)
            errors = tuple(plan.validate_plan(composite))
            if errors:
                fail(f"{workflow}: canonical composite plan invalid: {'; '.join(errors)}")
            missing = sorted(
                step.operation_id
                for step in composite.steps
                if step.operation_id not in canonical_atomic
            )
            if missing:
                fail(f"{workflow}: composite step lacks canonical executor: {missing}")
            continue
        fail(f"{workflow}: unsupported canonical disposition {item.disposition}")

    if routes.get("schema") != "production-command-routes.v1":
        fail("route registry schema differs")
    if routes.get("authority_cursor") != EXPECTED_CURSOR:
        fail("route registry authority cursor differs")
    route_items = routes.get("routes")
    if not isinstance(route_items, list) or not route_items:
        fail("route registry empty")

    mutating_routes = [item for item in route_items if item.get("mutating") is True]
    route_operations = {str(item.get("operation", "")) for item in mutating_routes}
    if route_operations != set(private_map):
        fail("mutating route operation set differs from physical wiring bindings")
    if any(str(item.get("workflow", "")) != shared_workflow for item in mutating_routes):
        fail("all mutating Issue #1 routes must use the shared canonical readiness workflow")
    if any(
        item.get("mutating") is not True
        and str(item.get("workflow", "")) == shared_workflow
        for item in route_items
    ):
        fail("non-mutating route points at physical mutation readiness workflow")

    shared_path = private_root / ".github/workflows" / shared_workflow
    if not shared_path.is_file():
        fail("shared canonical readiness workflow missing")
    shared_text = shared_path.read_text(encoding="utf-8")
    forbidden_shared = (
        "self-hosted",
        "android-production",
        "adb ",
        "adb\n",
        "secrets.",
        "secrets: inherit",
    )
    for token in forbidden_shared:
        if token in shared_text:
            fail(f"shared readiness workflow contains forbidden phone execution token: {token}")
    required_shared = (
        "runs-on: ubuntu-latest",
        "PHONE_EXECUTION_ENABLED: 'false'",
        "validate_physical_transaction_wiring.py",
        "PHYSICAL_EXECUTION_NOT_AUTHORIZED",
        f"ref: {EXPECTED_SHA}",
    )
    for token in required_shared:
        if token not in shared_text:
            fail(f"shared readiness workflow contract missing: {token}")

    router_text = router_workflow_path.read_text(encoding="utf-8")
    for legacy_workflow in inventory:
        if f"uses: ./.github/workflows/{legacy_workflow}" in router_text:
            fail(f"Issue #1 router still calls legacy mutator workflow: {legacy_workflow}")
    if router_text.count(f"uses: ./.github/workflows/{shared_workflow}") != 1:
        fail("Issue #1 router must have exactly one shared mutating workflow call")
    for operation in private_map:
        if operation not in router_text:
            fail(f"Issue #1 router shared mutation selector missing operation: {operation}")

    print(
        "PHYSICAL_TRANSACTION_WIRING_OK "
        f"canonical_sha={EXPECTED_SHA} mutating_routes={len(mutating_routes)} "
        f"atomic_bindings={len(canonical_atomic)} phone_execution_enabled=false"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (WiringError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"PHYSICAL_TRANSACTION_WIRING_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
