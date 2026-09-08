from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT.parent / "scripts"
sys.path.insert(0, str(ROOT))

from phone_target import PhoneTargetUnavailable  # noqa: E402

SERIAL = "registered-production-target"


def load_preflight():
    spec = importlib.util.spec_from_file_location(
        "phone_transport_preflight_s43_acceptance",
        SCRIPTS / "phone_transport_preflight.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(module, *, device_effect=None, root_effect=None):
    with tempfile.TemporaryDirectory() as raw:
        output = Path(raw) / "preflight.json"
        events: list[str] = []

        def require_device(serial: str):
            events.append("registered_device_state")
            if device_effect is not None:
                if isinstance(device_effect, BaseException):
                    raise device_effect
                return device_effect(serial)
            return "/usr/bin/adb"

        def probe_root(serial: str):
            events.append("root_contract")
            if root_effect is not None:
                if isinstance(root_effect, BaseException):
                    raise root_effect
                return root_effect(serial)
            return None

        with (
            mock.patch.dict(
                module.os.environ,
                {"ANDROID_PRODUCTION_SERIAL": SERIAL, "RUNNER_TEMP": raw},
                clear=False,
            ),
            mock.patch.object(module, "collect_runner_transport_evidence", return_value={}),
            mock.patch.object(module, "classify_preflight", return_value="READY"),
            mock.patch.object(module, "_require_device", side_effect=require_device),
            mock.patch.object(module, "_probe_root_capability", side_effect=probe_root),
        ):
            rc = module.main(
                [
                    "--output",
                    str(output),
                    "--controller-revision",
                    "a" * 40,
                    "--source-comment-created-at",
                    datetime.now(timezone.utc).isoformat(),
                ]
            )
        return rc, json.loads(output.read_text(encoding="utf-8")), events


def test_preflight_keeps_exact_s43_success_order_and_contract() -> None:
    module = load_preflight()
    rc, payload, events = _run(module)

    assert rc == 0
    assert events == ["registered_device_state", "root_contract"]
    assert payload["classification"] == "READY"
    assert "failure_phase" not in payload
    assert payload["safety"]["phone_access_performed"] is True


def test_preflight_device_failure_adds_only_safe_phase_without_root_attempt() -> None:
    module = load_preflight()
    rc, payload, events = _run(
        module,
        device_effect=PhoneTargetUnavailable("registered phone target is not in device state"),
    )

    assert rc == 0
    assert events == ["registered_device_state"]
    assert payload["classification"] == "NOT_READY"
    assert payload["failure_phase"] == "registered_device_state"
    assert payload["failure_class"] == "PhoneTargetUnavailable"
    assert payload["safety"]["phone_access_performed"] is False


def test_preflight_root_failure_preserves_s43_device_success_then_reports_root_phase() -> None:
    module = load_preflight()
    rc, payload, events = _run(
        module,
        root_effect=PhoneTargetUnavailable("rooted runtime capability unavailable"),
    )

    assert rc == 0
    assert events == ["registered_device_state", "root_contract"]
    assert payload["classification"] == "NOT_READY"
    assert payload["failure_phase"] == "root_contract"
    assert payload["safety"]["phone_access_performed"] is True


def test_preflight_does_not_introduce_second_adb_target_path() -> None:
    source = (SCRIPTS / "phone_transport_preflight.py").read_text(encoding="utf-8")
    assert "phone_transport_readiness" not in source
    assert "_registered_target_state" not in source
    assert "_ensure_adb_server" not in source
    assert source.index("_require_device(serial)") < source.index("_probe_root_capability(serial)")
