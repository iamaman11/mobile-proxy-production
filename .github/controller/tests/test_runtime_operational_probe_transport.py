from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


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


def test_runtime_probe_emits_only_allowlisted_phase_markers() -> None:
    module = load_operational_observer()
    script = module._operational_script("stage4-admin-token-safe-value")

    for phase in module._PHASES:
        assert f"{module._PHASE_PREFIX}{phase}".encode("utf-8") in script
    assert b"ADMIN_TOKEN" not in module._PHASE_PREFIX.encode("utf-8")
    assert b"health_raw" not in module._PHASE_PREFIX.encode("utf-8")


def test_runtime_probe_timeout_projects_last_allowlisted_phase(monkeypatch) -> None:
    module = load_operational_observer()
    stdout = (
        b"__stage4_phase=script_started\n"
        b"__stage4_phase=process_counts_done\n"
        b"__stage4_phase=busybox_selection_done\n"
        b"__stage4_phase=health_transport_start\n"
    )
    result = module.phone_target.RootScriptResult(
        status="timeout",
        returncode=None,
        stdout=stdout,
        stderr=b"",
    )
    monkeypatch.setattr(module.phone_target, "_probe_root_capability", lambda serial: None)
    monkeypatch.setattr(
        module.phone_target,
        "_run_root_script",
        lambda serial, script, timeout: result,
    )

    with pytest.raises(
        module.phone_target.PhoneTargetUnavailable,
        match=r"runtime operational observation is unavailable; phase=health_transport_start",
    ):
        module.observe_runtime_operational_health(
            "registered-production-phone",
            admin_token="stage4-admin-token-safe-value",
        )


def test_runtime_probe_success_parser_ignores_phase_markers() -> None:
    module = load_operational_observer()
    raw = b"\n".join(
        [
            b"__stage4_phase=script_started",
            b"__stage4_phase=process_counts_done",
            b"__stage4_phase=busybox_selection_done",
            b"__stage4_phase=health_transport_start",
            b"__stage4_phase=health_transport_done",
            b"__stage4_phase=health_parse_done",
            b"watchdog_count=1",
            b"runtime_supervisor_count=1",
            b"host_daemon_count=1",
            b"sing_box_count=1",
            b"health_api_authenticated=true",
            b"readiness_state=healthy",
            b"serving=true",
            b"proxy_status=running",
            b"cellular_route_ready=true",
            b"proxy_bind_ready=true",
            b"local_serving_ready=true",
            b"tunnel_owner=first_party_reverse_tunnel",
            b"degradation_reason_code=none",
            b"__stage4_phase=output_done",
            b"",
        ]
    )

    observation = module._parse_output(raw)
    assert observation.desired is True
    assert observation.tunnel_owner_matches_expected is True
