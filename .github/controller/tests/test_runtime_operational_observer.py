#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
GITHUB_DIR = ROOT.parent
sys.path.insert(0, str(ROOT))

import phone_target
import runtime_operational_observer as operational


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _output(
    *,
    watchdog: int = 1,
    supervisor: int = 1,
    host_daemon: int = 1,
    sing_box: int = 1,
    authenticated: str = "true",
    readiness: str = "healthy",
    serving: str = "true",
    proxy_status: str = "running",
    cellular: str = "true",
    proxy_bind: str = "true",
    local_serving: str = "true",
    tunnel_owner: str = "first_party_reverse_tunnel",
    degradation: str = "none",
) -> bytes:
    return (
        f"watchdog_count={watchdog}\n"
        f"runtime_supervisor_count={supervisor}\n"
        f"host_daemon_count={host_daemon}\n"
        f"sing_box_count={sing_box}\n"
        f"health_api_authenticated={authenticated}\n"
        f"readiness_state={readiness}\n"
        f"serving={serving}\n"
        f"proxy_status={proxy_status}\n"
        f"cellular_route_ready={cellular}\n"
        f"proxy_bind_ready={proxy_bind}\n"
        f"local_serving_ready={local_serving}\n"
        f"tunnel_owner={tunnel_owner}\n"
        f"degradation_reason_code={degradation}\n"
    ).encode()


def _healthy_observation() -> operational.RuntimeOperationalObservation:
    return operational._parse_output(_output())


def test_healthy_operational_projection_is_bounded_and_desired() -> None:
    observed = _healthy_observation()
    assert observed.desired is True
    assert observed.readiness_state == "healthy"
    assert observed.tunnel_owner == "first_party_reverse_tunnel"
    bounded = observed.to_bounded_dict()
    assert bounded["localhost_only"] is True
    assert bounded["raw_health_json_recorded"] is False
    assert bounded["raw_config_recorded"] is False
    assert bounded["secret_values_recorded"] is False
    assert bounded["process_ids_recorded"] is False
    assert bounded["process_cmdlines_recorded"] is False
    assert bounded["provider_access_performed"] is False
    assert bounded["phone_mutation_performed"] is False
    assert "admin_token" not in bounded
    assert "pid" not in bounded


def test_runtime_degraded_states_parse_without_becoming_unknown() -> None:
    for readiness in ("waiting_tunnel", "waiting_cellular", "recovering", "quarantined"):
        observed = operational._parse_output(
            _output(
                readiness=readiness,
                serving="false",
                proxy_status="degraded",
                cellular="false",
                local_serving="false",
                degradation="cellular_network_unvalidated",
            )
        )
        assert observed.readiness_state == readiness
        assert observed.desired is False


def test_unavailable_health_is_bounded_not_fabricated() -> None:
    observed = operational._parse_output(
        _output(
            authenticated="false",
            readiness="unknown",
            serving="unknown",
            proxy_status="unknown",
            cellular="unknown",
            proxy_bind="unknown",
            local_serving="unknown",
            tunnel_owner="unknown",
            degradation="unknown",
        )
    )
    assert observed.health_api_authenticated is False
    assert observed.desired is False
    assert observed.serving is None


def test_malformed_operational_output_fails_closed_without_echoing_payload() -> None:
    secretish = "do-not-echo-this-value"
    for raw in (
        _output(readiness=secretish),
        _output() + b"unexpected_key=value\n",
        b"not-key-value\n",
    ):
        try:
            operational._parse_output(raw)
        except phone_target.PhoneTargetUnavailable as exc:
            assert str(exc) == "runtime operational observation is malformed"
            assert secretish not in str(exc)
            continue
        raise AssertionError("malformed operational evidence was admitted")


def test_device_probe_is_localhost_only_and_has_no_mutation_primitives() -> None:
    source = operational._OPERATIONAL_SCRIPT.decode("utf-8")
    assert "GET /v1/health HTTP/1.1" in source
    assert "Authorization: Bearer %s" in source
    assert "127.0.0.1" in source
    assert "admin_token" in source
    assert "api.ipify.org" not in source
    assert "vultr" not in source.lower()
    assert "provider" not in source.lower()
    for forbidden in (
        "kill ",
        "pkill ",
        "killall ",
        "reboot",
        "setprop ",
        "chmod ",
        "chown ",
        "rm -",
        "mv ",
        "cp ",
        "svc ",
        "settings put",
        "am force-stop",
        "pm install",
        "pm uninstall",
        "start ",
        "stop ",
    ):
        assert forbidden not in source, forbidden
    emitted = [line for line in source.splitlines() if line.startswith("printf '") and "=%s" in line]
    assert emitted
    assert all("admin_token" not in line for line in emitted)
    assert all("health_raw" not in line for line in emitted)


def test_observe_uses_existing_root_transport_once(monkeypatch=None) -> None:
    calls: list[tuple[str, object]] = []
    original_probe = phone_target._probe_root_capability
    original_run = phone_target._run_root_script
    try:
        phone_target._probe_root_capability = lambda serial: calls.append(("probe", serial))

        def fake_run(serial: str, script: bytes, *, timeout: int):
            calls.append(("run", (serial, script, timeout)))
            return SimpleNamespace(status="completed", returncode=0, stderr=b"", stdout=_output())

        phone_target._run_root_script = fake_run
        observed = operational.observe_runtime_operational_health("registered-target")
        assert observed.desired is True
        assert calls[0] == ("probe", "registered-target")
        assert calls[1][0] == "run"
        serial, script, timeout = calls[1][1]
        assert serial == "registered-target"
        assert script is operational._OPERATIONAL_SCRIPT
        assert timeout == 20
    finally:
        phone_target._probe_root_capability = original_probe
        phone_target._run_root_script = original_run


def _base_evidence(*, exact_runtime: bool = True) -> dict[str, object]:
    runtime = {
        "current_state": "expected_release" if exact_runtime else "missing_current",
        "current_matches_expected_release": exact_runtime,
        "exact_files_verified": exact_runtime,
    }
    return {
        "schema": "stage4-phone-release-observation.v1",
        "classification": "HEALTHY_EXACT" if exact_runtime else "DEGRADED",
        "observation": {"desired": exact_runtime, "runtime": runtime},
        "safety": {
            "phone_access_performed": True,
            "phone_mutation_performed": False,
            "provider_access_performed": False,
            "provider_mutation_performed": False,
        },
    }


def test_enrichment_preserves_exact_state_and_adds_operational_health() -> None:
    enrich = _load_script(
        "runtime_operational_enrichment_test",
        GITHUB_DIR / "scripts" / "enrich_phone_runtime_operational.py",
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "evidence.json"
        path.write_text(json.dumps(_base_evidence()), encoding="utf-8")
        original = enrich.observe_runtime_operational_health
        try:
            enrich.observe_runtime_operational_health = lambda serial: _healthy_observation()
            assert enrich.enrich(path, serial="registered-target") == 0
        finally:
            enrich.observe_runtime_operational_health = original
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["classification"] == "HEALTHY_EXACT"
        assert payload["observation"]["desired"] is True
        assert payload["observation"]["operational"]["desired"] is True
        assert payload["safety"]["operational_probe_localhost_only"] is True
        assert payload["safety"]["raw_health_json_recorded"] is False
        assert payload["safety"]["process_ids_recorded"] is False


def test_operational_degradation_and_unavailability_are_distinct() -> None:
    enrich = _load_script(
        "runtime_operational_enrichment_degraded_test",
        GITHUB_DIR / "scripts" / "enrich_phone_runtime_operational.py",
    )
    with tempfile.TemporaryDirectory() as directory:
        degraded_path = Path(directory) / "degraded.json"
        degraded_path.write_text(json.dumps(_base_evidence()), encoding="utf-8")
        degraded = operational._parse_output(
            _output(
                readiness="recovering",
                serving="false",
                proxy_status="degraded",
                local_serving="false",
                degradation="local_probe_failed",
            )
        )
        original = enrich.observe_runtime_operational_health
        try:
            enrich.observe_runtime_operational_health = lambda serial: degraded
            assert enrich.enrich(degraded_path, serial="registered-target") == 0
        finally:
            enrich.observe_runtime_operational_health = original
        payload = json.loads(degraded_path.read_text(encoding="utf-8"))
        assert payload["classification"] == "DEGRADED"
        assert payload["observation"]["operational"]["evaluated"] is True
        assert payload["observation"]["operational"]["desired"] is False

        unknown_path = Path(directory) / "unknown.json"
        unknown_path.write_text(json.dumps(_base_evidence()), encoding="utf-8")
        try:
            def unavailable(_serial: str):
                raise phone_target.PhoneTargetUnavailable("bounded failure")

            enrich.observe_runtime_operational_health = unavailable
            assert enrich.enrich(unknown_path, serial="registered-target") == 2
        finally:
            enrich.observe_runtime_operational_health = original
        payload = json.loads(unknown_path.read_text(encoding="utf-8"))
        assert payload["classification"] == "UNKNOWN"
        assert payload["failure_code"] == "RUNTIME_OPERATIONAL_OBSERVATION_UNAVAILABLE"
        assert payload["observation"]["operational"]["reason"] == "observation_unavailable"


def test_non_exact_runtime_does_not_probe_operational_surface() -> None:
    enrich = _load_script(
        "runtime_operational_enrichment_not_exact_test",
        GITHUB_DIR / "scripts" / "enrich_phone_runtime_operational.py",
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "evidence.json"
        path.write_text(json.dumps(_base_evidence(exact_runtime=False)), encoding="utf-8")
        original = enrich.observe_runtime_operational_health
        try:
            def unexpected(_serial: str):
                raise AssertionError("operational surface was probed for non-exact runtime")

            enrich.observe_runtime_operational_health = unexpected
            assert enrich.enrich(path, serial="registered-target") == 0
        finally:
            enrich.observe_runtime_operational_health = original
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["classification"] == "DEGRADED"
        assert payload["observation"]["operational"]["evaluated"] is False
        assert payload["observation"]["operational"]["reason"] == "runtime_not_exact"


def main() -> int:
    test_healthy_operational_projection_is_bounded_and_desired()
    test_runtime_degraded_states_parse_without_becoming_unknown()
    test_unavailable_health_is_bounded_not_fabricated()
    test_malformed_operational_output_fails_closed_without_echoing_payload()
    test_device_probe_is_localhost_only_and_has_no_mutation_primitives()
    test_observe_uses_existing_root_transport_once()
    test_enrichment_preserves_exact_state_and_adds_operational_health()
    test_operational_degradation_and_unavailability_are_distinct()
    test_non_exact_runtime_does_not_probe_operational_surface()
    print("RUNTIME_OPERATIONAL_OBSERVER_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
