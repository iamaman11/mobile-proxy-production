from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"


def load_operational_observer():
    if str(CONTROLLER) not in sys.path:
        sys.path.insert(0, str(CONTROLLER))
    spec = importlib.util.spec_from_file_location(
        "runtime_operational_observer_transport_acceptance",
        CONTROLLER / "runtime_operational_observer.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_health_transport_has_independent_hard_timeout() -> None:
    module = load_operational_observer()
    script = module._operational_script("stage4-admin-token-safe-value")

    assert module._HEALTH_TRANSPORT_TIMEOUT_SECONDS == 5
    assert module._ROOT_SCRIPT_TIMEOUT_SECONDS == 12
    assert (
        b'"$BB_BIN" timeout -t 5 "$BB_BIN" nc -w 5 127.0.0.1 8088'
        in script
    )
    assert b"head -c 16384" in script
    assert b"command -v nc" not in script


def test_runtime_health_transport_keeps_bearer_out_of_process_arguments() -> None:
    module = load_operational_observer()
    source = (CONTROLLER / "runtime_operational_observer.py").read_text(encoding="utf-8")
    script = module._operational_script("stage4-admin-token-safe-value")

    assert b"Authorization: Bearer %s" in script
    assert b'"$ADMIN_TOKEN" |' in script
    assert "wget" not in source
    assert "curl" not in source
    assert "--header" not in source
    assert " -H " not in source
