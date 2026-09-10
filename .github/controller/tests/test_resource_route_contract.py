from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
PRODUCTION = ROOT / "production"
SHA = "832c8b010efee97a6f5c9c587b766acbe65dd453"


def _load_router():
    name = "issue_command_router_resource_contract"
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / "issue_command_router.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_resource_route_has_independent_operation_identity() -> None:
    router = _load_router()
    target_value = json.loads((PRODUCTION / "targets.json").read_text(encoding="utf-8"))
    targets = router.validate_target_registry(target_value)
    command_value = json.loads(
        (PRODUCTION / "command-control-registry.json").read_text(encoding="utf-8")
    )
    router.validate_registry(command_value, targets)

    route = router.classify(
        repository="iamaman11/mobile-proxy-production",
        issue_number=1,
        author="iamaman11",
        command="/observe-phone-resource-sanity phone-production v0.1.7",
        event_sha=SHA,
        current_main_sha=SHA,
        run_attempt=1,
    )

    assert route.route_id == "observe-phone-resource-sanity"
    assert route.operation == "observe-phone-resource-sanity"
    assert route.operation != "observe-phone-operational"
    assert route.operation in targets["phone-production"]["allowed_operations"]
    assert route.target_capability_policy == "phone-production-resource-sanity-observation"
    assert route.workflow == ".github/workflows/phone-resource-sanity-observation.yml"
    assert route.read_only is True
    assert route.destructive is False
