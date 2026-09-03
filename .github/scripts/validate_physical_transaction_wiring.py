#!/usr/bin/env python3
"""Hosted fail-closed verifier for private -> canonical physical transaction wiring."""

from __future__ import annotations

import argparse
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path


EXPECTED_SCHEMA = "physical-transaction-wiring.v1"
EXPECTED_CURSOR = "issue179-comment-5531491187"
EXPECTED_REPOSITORY = "iamaman11/mobile-proxy"
EXPECTED_SHA = "374073b4e666d71981d5ccf9169c30e0979845e6"
EXPECTED_QUALITY_RUN = 33802912939
EXPECTED_REHEARSAL_OPERATION = "phone-filesystem-certification"
EXPECTED_REHEARSAL_WORKFLOW = "filesystem-scratch-kernel-rehearsal.yml"
EXPECTED_PHYSICAL_OPERATION = "android.filesystem-scratch-roundtrip.v1"
EXPECTED_PLAN = "android.filesystem-certification.plan.v1"


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
        fail("global phone execution default must remain disabled")

    shared_workflow = str(binding.get("shared_mutation_workflow", ""))
    if shared_workflow != "physical-transaction-readiness.yml":
        fail("unexpected shared mutation workflow")

    rehearsal = binding.get("authorized_rehearsal")
    if not isinstance(rehearsal, dict):
        fail("authorized rehearsal is missing")
    expected_rehearsal = {
        "semantic_operation": EXPECTED_REHEARSAL_OPERATION,
        "workflow": EXPECTED_REHEARSAL_WORKFLOW,
        "physical_operation_id": EXPECTED_PHYSICAL_OPERATION,
        "plan_id": EXPECTED_PLAN,
        "plan_step_index": 0,
        "phone_execution_enabled": True,
        "single_atomic_transaction": True,
        "continuation_authorized": False,
    }
    if rehearsal != expected_rehearsal:
        fail("authorized rehearsal differs from exact Stage C.0t grant")

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
    operation = importlib.import_module("operation_state_machine")

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
        semantic_operation = str(item.get("operation", ""))
        workflow = str(item.get("legacy_workflow", ""))
        if not semantic_operation or not workflow:
            fail("physical wiring binding entry incomplete")
        if semantic_operation in private_map:
            fail(f"duplicate private operation binding: {semantic_operation}")
        private_map[semantic_operation] = workflow

    if set(private_map.values()) != set(inventory):
        fail("private legacy workflow mapping differs from canonical mutator inventory")

    canonical_atomic = set(registry.CANONICAL_BINDING_TYPES)
    if canonical_atomic != set(registry.CANONICAL_EXECUTOR_TYPES):
        fail("canonical binding/executor registry key sets differ")

    for semantic_operation, workflow in sorted(private_map.items()):
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

    rehearsal_inventory = inventory[private_map[EXPECTED_REHEARSAL_OPERATION]]
    if rehearsal_inventory.disposition != "composite" or rehearsal_inventory.plan_id != EXPECTED_PLAN:
        fail("filesystem certification canonical inventory is not the expected composite plan")
    rehearsal_plan = plan.composite_plan(EXPECTED_PLAN)
    if len(rehearsal_plan.steps) <= 0:
        fail("filesystem certification plan is empty")
    if rehearsal_plan.steps[0].operation_id != EXPECTED_PHYSICAL_OPERATION:
        fail("authorized rehearsal is not canonical filesystem plan step zero")
    if EXPECTED_PHYSICAL_OPERATION not in canonical_atomic:
        fail("authorized scratch operation lacks canonical executor binding")

    scratch_binding_type = registry.CANONICAL_BINDING_TYPES[EXPECTED_PHYSICAL_OPERATION]
    scratch_contract = scratch_binding_type.contract
    requirements = tuple(scratch_contract.fact_requirements)
    if len(requirements) != 1:
        fail("scratch bootstrap must have exactly one causal preflight requirement")
    requirement = requirements[0]
    if not (
        requirement.subject == "phone"
        and requirement.predicate == "registered_phone_access_proven"
        and requirement.freshness == operation.SAME_TRANSACTION
    ):
        fail("scratch bootstrap SAME_TRANSACTION phone admission differs")

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
    for item in mutating_routes:
        semantic_operation = str(item.get("operation", ""))
        expected_workflow = (
            EXPECTED_REHEARSAL_WORKFLOW
            if semantic_operation == EXPECTED_REHEARSAL_OPERATION
            else shared_workflow
        )
        if str(item.get("workflow", "")) != expected_workflow:
            fail(f"{semantic_operation}: mutating route workflow differs from bounded wiring")
    if any(
        item.get("mutating") is not True
        and str(item.get("workflow", "")) in {shared_workflow, EXPECTED_REHEARSAL_WORKFLOW}
        for item in route_items
    ):
        fail("non-mutating route points at physical mutation workflow")

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

    rehearsal_path = private_root / ".github/workflows" / EXPECTED_REHEARSAL_WORKFLOW
    if not rehearsal_path.is_file():
        fail("scratch rehearsal workflow missing")
    rehearsal_text = rehearsal_path.read_text(encoding="utf-8")
    required_rehearsal = (
        "workflow_call:",
        "execute-scratch-transaction:",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-phone-global-mutation",
        "cancel-in-progress: false",
        "queue: max",
        f"CANONICAL_SHA: {EXPECTED_SHA}",
        f"CANONICAL_QUALITY_RUN_ID: '{EXPECTED_QUALITY_RUN}'",
        f"AUTHORITY_CURSOR: {EXPECTED_CURSOR}",
        "run_filesystem_scratch_transaction.py",
        EXPECTED_PHYSICAL_OPERATION,
        "continue-on-error: true",
        "do not retry and do not continue to another physical step",
    )
    for token in required_rehearsal:
        if token not in rehearsal_text:
            fail(f"scratch rehearsal workflow contract missing: {token}")
    forbidden_rehearsal = (
        "run_android_filesystem_certification.py",
        "run_destructive_clean_install",
        "pm install",
        "pm uninstall",
        "service.sh",
        "reboot",
        "provider",
    )
    for token in forbidden_rehearsal:
        if token in rehearsal_text.lower():
            fail(f"scratch rehearsal workflow contains forbidden continuation/effect token: {token}")
    if rehearsal_text.count("group: production-phone-global-mutation") != 1:
        fail("scratch rehearsal must represent the global mutation scope exactly once")
    if rehearsal_text.count("run_filesystem_scratch_transaction.py") != 1:
        fail("scratch rehearsal must invoke exactly one private Kernel entrypoint")

    edge_text = (private_root / ".github/scripts/filesystem_scratch_edges.py").read_text(encoding="utf-8")
    entry_text = (private_root / ".github/scripts/run_filesystem_scratch_transaction.py").read_text(encoding="utf-8")
    ports_text = (private_root / ".github/scripts/filesystem_scratch_transaction_ports.py").read_text(encoding="utf-8")
    if edge_text.count("subprocess.run(") != 1:
        fail("scratch edge must centralize controller->phone transport in one subprocess call site")
    if "FilesystemScratchRoundtripBinding" not in entry_text:
        fail("scratch entrypoint does not bind exact canonical operation")
    if not (
        "runner = transaction.TransactionRunner()" in entry_text
        and "runner.run(" in entry_text
    ):
        fail("scratch entrypoint does not invoke the Universal Kernel through the named runner")
    if "run_android_filesystem_certification.py" in entry_text:
        fail("scratch entrypoint references legacy composite certification")
    if "blind_retry_allowed\": False" not in ports_text and '"blind_retry_allowed": False' not in ports_text:
        fail("scratch private CONTROL evidence does not forbid blind retry")

    router_text = router_workflow_path.read_text(encoding="utf-8")
    for legacy_workflow in inventory:
        if f"uses: ./.github/workflows/{legacy_workflow}" in router_text:
            fail(f"Issue #1 router still calls legacy mutator workflow: {legacy_workflow}")
    if router_text.count(f"uses: ./.github/workflows/{shared_workflow}") != 1:
        fail("Issue #1 router must have exactly one shared fail-closed mutator call")
    if router_text.count(f"uses: ./.github/workflows/{EXPECTED_REHEARSAL_WORKFLOW}") != 1:
        fail("Issue #1 router must have exactly one scratch rehearsal call")
    shared_selector_match = re.search(
        r"(?ms)^  op_canonical_physical_transaction:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        router_text,
    )
    if shared_selector_match is None:
        fail("Issue #1 shared mutator router job missing")
    shared_selector = shared_selector_match.group("body")
    if EXPECTED_REHEARSAL_OPERATION in shared_selector:
        fail("filesystem certification remains in shared fail-closed mutator selector")
    for semantic_operation in set(private_map) - {EXPECTED_REHEARSAL_OPERATION}:
        if semantic_operation not in shared_selector:
            fail(f"Issue #1 shared mutation selector missing operation: {semantic_operation}")

    print(
        "PHYSICAL_TRANSACTION_WIRING_OK "
        f"canonical_sha={EXPECTED_SHA} mutating_routes={len(mutating_routes)} "
        f"atomic_bindings={len(canonical_atomic)} default_phone_execution=false "
        f"authorized_rehearsal={EXPECTED_PHYSICAL_OPERATION} continuation_authorized=false"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (WiringError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"PHYSICAL_TRANSACTION_WIRING_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)