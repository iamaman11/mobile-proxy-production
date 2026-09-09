from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
WORKFLOWS = ROOT / "workflows"
if str(CONTROLLER) not in sys.path:
    sys.path.insert(0, str(CONTROLLER))

import runtime_operational_observer as runtime_observer  # noqa: E402


def test_generated_phone_script_observes_only_bounded_reverse_tunnel_fields() -> None:
    script = runtime_observer._operational_script("bounded-test-token").decode("utf-8")
    for field in (
        "reverse_tunnel_connected",
        "reverse_tunnel_freshness",
        "reverse_tunnel_active_transport",
        "reverse_tunnel_failover_reason",
    ):
        assert f"printf '{field}=%s" in script
    assert "reverse_tunnel_last_error" not in script


def test_reverse_tunnel_enums_match_immutable_product_v017_contract() -> None:
    assert runtime_observer._ALLOWED_REVERSE_TUNNEL_FRESHNESS == {
        "none",
        "unknown",
        "fresh",
        "stale",
    }
    assert runtime_observer._ALLOWED_REVERSE_TUNNEL_TRANSPORT == {
        "none",
        "tcp",
        "quic",
        "tls_tcp",
    }
    assert runtime_observer._ALLOWED_REVERSE_TUNNEL_FAILOVER_REASON == {
        "none",
        "connect_timeout",
        "connect_failed",
        "authentication_failed",
        "session_closed",
        "session_error",
    }


def test_workflow_validates_bounded_tunnel_fields_and_rejects_raw_error() -> None:
    source = (WORKFLOWS / "phone-operational-observation.yml").read_text(encoding="utf-8")
    for field in (
        "reverse_tunnel_connected",
        "reverse_tunnel_freshness",
        "reverse_tunnel_active_transport",
        "reverse_tunnel_failover_reason",
    ):
        assert field in source
    assert "operational evidence exposed unbounded reverse tunnel error" in source
