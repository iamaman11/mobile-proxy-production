from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT.parent / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

from quarantine_recovery import (  # noqa: E402
    RECOVERY_INTENT_SCHEMA,
    RECOVERY_OPERATION,
    RECOVERY_TARGET,
    RECOVERY_TERMINAL_SCHEMA,
    QuarantineRecoveryError,
    managed_release_tag,
    quarantined_current_release,
    recovery_semantic_id,
    validate_quarantined_deployment_intent,
    validate_quarantined_deployment_terminal,
    validate_recovery_intent,
    validate_recovery_terminal,
)
import quarantine_phone_observer  # noqa: E402

RUNNER = SCRIPTS / "run_quarantine_recovery.py"
PREPARE = SCRIPTS / "prepare_quarantine_recovery.py"
POSTCONDITION = SCRIPTS / "verify_quarantine_recovery_postconditions.py"
CORE = ROOT / "quarantine_recovery.py"


def expect_error(fn) -> None:
    try:
        fn()
    except QuarantineRecoveryError:
        return
    raise AssertionError("expected QuarantineRecoveryError")


def request(seed: str) -> str:
    return "req-sha256:" + seed * 64


def recovery_id(*, release: str, request_id: str, terminal_ref: str, parent_ref: str | None = None) -> str:
    return recovery_semantic_id(
        target=RECOVERY_TARGET,
        release=release,
        quarantined_request_id=request_id,
        quarantined_terminal_ref=terminal_ref,
        parent_recovery_terminal_ref=parent_ref or terminal_ref,
    )


def deployment_intent(*, release: str, release_id: int, request_id: str) -> dict[str, object]:
    return {
        "schema": "production-deployment-intent.v2",
        "semantic_request_id": request_id,
        "target": RECOVERY_TARGET,
        "product_release": release,
        "release_id": release_id,
        "target_binding_id": "tb-hmac-sha256:" + "a" * 64,
        "blind_retry_allowed": False,
        "dispatch_may_reach_target": True,
        "mutation_performed": False,
        "physical_domains": {"apk": True, "runtime": True},
    }


def deployment_terminal(
    *, release: str, release_id: int, request_id: str, current_release: str
) -> dict[str, object]:
    return {
        "schema": "production-deployment-terminal.v2",
        "semantic_request_id": request_id,
        "target": RECOVERY_TARGET,
        "product_release": release,
        "release_id": release_id,
        "state": "QUARANTINED",
        "mutation_performed": True,
        "postcondition_verified": True,
        "next_allowed_operation": "read-only-observation-or-approved-recovery",
        "facts": {
            "recovery_observation": {
                "apk": {"desired": True, "exact_artifact_verified": True},
                "runtime": {
                    "target_release": f"/data/adb/mobile-proxy-node/releases/{release}",
                    "target_release_exists": True,
                    "current_target": f"/data/adb/mobile-proxy-node/releases/{current_release}",
                    "desired": False,
                    "admissible_for_new_dispatch": False,
                },
            }
        },
    }


def recovery_intent(
    *, release: str, release_id: int, request_id: str, quarantined_ref: str, current_release: str
) -> dict[str, object]:
    return {
        "schema": RECOVERY_INTENT_SCHEMA,
        "semantic_recovery_id": recovery_id(
            release=release,
            request_id=request_id,
            terminal_ref=quarantined_ref,
        ),
        "operation": RECOVERY_OPERATION,
        "execution_id": "gh-run:1:1",
        "controller_revision": "b" * 40,
        "target": RECOVERY_TARGET,
        "target_binding_id": "tb-hmac-sha256:" + "c" * 64,
        "product_release": release,
        "release_id": release_id,
        "quarantined_request_id": request_id,
        "quarantined_intent_ref": "issue-comment:11",
        "quarantined_terminal_ref": quarantined_ref,
        "parent_recovery_terminal_ref": quarantined_ref,
        "apk_exact": True,
        "inactive_runtime_exact": True,
        "current_before_release": current_release,
        "activation_may_reach_target": True,
        "blind_retry_allowed": False,
        "mutation_performed": False,
    }


def recovery_terminal(
    *,
    release: str,
    release_id: int,
    request_id: str,
    quarantined_ref: str,
    state: str = "ACCEPTED",
    mutation_performed: bool = True,
    postcondition_verified: bool = True,
) -> dict[str, object]:
    return {
        "schema": RECOVERY_TERMINAL_SCHEMA,
        "semantic_recovery_id": recovery_id(
            release=release,
            request_id=request_id,
            terminal_ref=quarantined_ref,
        ),
        "operation": RECOVERY_OPERATION,
        "execution_id": "gh-run:1:1",
        "controller_revision": "d" * 40,
        "target": RECOVERY_TARGET,
        "product_release": release,
        "release_id": release_id,
        "quarantined_request_id": request_id,
        "quarantined_terminal_ref": quarantined_ref,
        "parent_recovery_terminal_ref": quarantined_ref,
        "recovery_intent_ref": "issue-comment:13" if mutation_performed else None,
        "state": state,
        "mutation_performed": mutation_performed,
        "postcondition_verified": postcondition_verified,
        "blocking_predicate": None if state == "ACCEPTED" else "BOUNDED_TEST_STATE",
        "facts": {"postcondition": {}},
        "blind_retry_allowed": False,
    }


def test_semantic_identity_is_release_agnostic_and_lineage_bound() -> None:
    first = recovery_id(
        release="v1.2.3",
        request_id=request("1"),
        terminal_ref="issue-comment:101",
    )
    second = recovery_id(
        release="v9.8.7",
        request_id=request("2"),
        terminal_ref="issue-comment:202",
    )
    assert first.startswith("recovery-sha256:")
    assert second.startswith("recovery-sha256:")
    assert first != second
    assert first != recovery_id(
        release="v1.2.3",
        request_id=request("1"),
        terminal_ref="issue-comment:101",
        parent_ref="issue-comment:102",
    )
    expect_error(lambda: recovery_id(
        release="not-semver",
        request_id=request("1"),
        terminal_ref="issue-comment:101",
    ))


def test_deployment_eligibility_works_for_multiple_release_identities() -> None:
    cases = (
        ("v1.2.3", 123, request("3"), "v1.2.2"),
        ("v9.8.7", 987, request("4"), "v9.8.6"),
    )
    for release, release_id, request_id, current_release in cases:
        intent = deployment_intent(release=release, release_id=release_id, request_id=request_id)
        terminal = deployment_terminal(
            release=release,
            release_id=release_id,
            request_id=request_id,
            current_release=current_release,
        )
        validate_quarantined_deployment_intent(
            intent,
            target=RECOVERY_TARGET,
            release=release,
            request_id=request_id,
            release_id=release_id,
        )
        validate_quarantined_deployment_terminal(
            terminal,
            target=RECOVERY_TARGET,
            release=release,
            request_id=request_id,
            release_id=release_id,
        )
        assert quarantined_current_release(terminal, release=release) == current_release


def test_release_ids_reject_bool_and_nonpositive_values() -> None:
    release = "v2.3.4"
    request_id = request("5")
    intent = deployment_intent(release=release, release_id=234, request_id=request_id)
    terminal = deployment_terminal(
        release=release,
        release_id=234,
        request_id=request_id,
        current_release="v2.3.3",
    )
    expect_error(lambda: validate_quarantined_deployment_intent(
        dict(intent, release_id=True),
        target=RECOVERY_TARGET,
        release=release,
        request_id=request_id,
        release_id=True,
    ))
    expect_error(lambda: validate_quarantined_deployment_terminal(
        dict(terminal, release_id=0),
        target=RECOVERY_TARGET,
        release=release,
        request_id=request_id,
        release_id=0,
    ))


def test_quarantine_eligibility_rejects_nonrecoverable_or_changed_shapes() -> None:
    release = "v2.3.4"
    request_id = request("5")
    terminal = deployment_terminal(
        release=release,
        release_id=234,
        request_id=request_id,
        current_release="v2.3.3",
    )
    same = deployment_terminal(
        release=release,
        release_id=234,
        request_id=request_id,
        current_release=release,
    )
    expect_error(lambda: validate_quarantined_deployment_terminal(
        same,
        target=RECOVERY_TARGET,
        release=release,
        request_id=request_id,
        release_id=234,
    ))
    terminal["next_allowed_operation"] = "none"
    expect_error(lambda: validate_quarantined_deployment_terminal(
        terminal,
        target=RECOVERY_TARGET,
        release=release,
        request_id=request_id,
        release_id=234,
    ))


def test_recovery_intent_contract_is_generic_and_exactly_once() -> None:
    for release, release_id, request_id, current in (
        ("v3.4.5", 345, request("6"), "v3.4.4"),
        ("v7.8.9", 789, request("7"), "v7.8.8"),
    ):
        payload = recovery_intent(
            release=release,
            release_id=release_id,
            request_id=request_id,
            quarantined_ref="issue-comment:301",
            current_release=current,
        )
        validate_recovery_intent(payload)
        expect_error(lambda payload=payload: validate_recovery_intent(dict(payload, blind_retry_allowed=True)))
        expect_error(lambda payload=payload, release=release: validate_recovery_intent(
            dict(payload, current_before_release=release)
        ))


def test_recovery_terminal_contract_preserves_ambiguity_boundary() -> None:
    base = dict(
        release="v4.5.6",
        release_id=456,
        request_id=request("8"),
        quarantined_ref="issue-comment:401",
    )
    validate_recovery_terminal(recovery_terminal(**base))
    validate_recovery_terminal(recovery_terminal(
        **base,
        state="UNKNOWN",
        mutation_performed=True,
        postcondition_verified=False,
    ))
    validate_recovery_terminal(recovery_terminal(
        **base,
        state="QUARANTINED",
        mutation_performed=True,
        postcondition_verified=True,
    ))
    validate_recovery_terminal(recovery_terminal(
        **base,
        state="REFUSED",
        mutation_performed=False,
        postcondition_verified=False,
    ))
    expect_error(lambda: validate_recovery_terminal(recovery_terminal(
        **base,
        state="UNKNOWN",
        mutation_performed=False,
        postcondition_verified=False,
    )))
    expect_error(lambda: validate_recovery_terminal(recovery_terminal(
        **base,
        state="ACCEPTED",
        mutation_performed=True,
        postcondition_verified=False,
    )))
    refused = recovery_terminal(
        **base,
        state="REFUSED",
        mutation_performed=False,
        postcondition_verified=False,
    )
    refused["recovery_intent_ref"] = "issue-comment:99"
    expect_error(lambda: validate_recovery_terminal(refused))


def test_managed_release_tag_is_bounded_to_semver_release_paths() -> None:
    assert managed_release_tag("/data/adb/mobile-proxy-node/releases/v1.2.3") == "v1.2.3"
    assert managed_release_tag("/data/adb/mobile-proxy-node/releases/not-a-release") is None
    assert managed_release_tag("/data/local/tmp/v1.2.3") is None
    assert managed_release_tag(None) is None


def _fake_root_result(stdout: bytes):
    return SimpleNamespace(status="completed", returncode=0, stdout=stdout, stderr=b"")


def test_quarantine_observer_reports_bounded_current_release_tag() -> None:
    expected = "1" * 64
    captured: list[bytes] = []
    old_files = quarantine_phone_observer._files
    old_run = quarantine_phone_observer._run_root_script
    try:
        quarantine_phone_observer._files = lambda release_root, required_paths: (
            ("service.sh", Path("/tmp/service.sh"), expected),
        )

        def run(serial, script, timeout):
            captured.append(script)
            return _fake_root_result(
                ("target=present\ncurrent=/data/adb/mobile-proxy-node/releases/v6.5.4\nh0=" + expected + "\n").encode()
            )

        quarantine_phone_observer._run_root_script = run
        observed = quarantine_phone_observer.observe_exact_inactive_runtime(
            serial="registered",
            release_root=Path("/tmp/release"),
            release_id="v6.5.5",
            required_paths=("service.sh",),
        )
    finally:
        quarantine_phone_observer._files = old_files
        quarantine_phone_observer._run_root_script = old_run

    assert observed["target_release_exists"] is True
    assert observed["inactive_exact_files_verified"] is True
    assert observed["current_relation"] == "other-managed"
    assert observed["current_release_tag"] == "v6.5.4"
    assert observed["desired"] is False
    text = captured[0].decode()
    for forbidden in ("mkdir ", "cp ", "rm ", "mv ", "ln ", "kill ", "chmod ", "pm install"):
        assert forbidden not in text


def test_generic_recovery_implementation_contains_no_current_incident_identity() -> None:
    forbidden = (
        "v0.1.7",
        "v0.1.8",
        "386594506",
        "063412d66b1b05a6649ee44dc1f5261696cd7e6be6facbc9a7faf84335f6a95a",
        "5624973342",
        "5624980359",
    )
    for path in (CORE, PREPARE, RUNNER, POSTCONDITION):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, (path.name, token)


def test_runner_is_activation_only_and_reuses_existing_observers() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    for required in (
        "prepare_verified_release_runtime(",
        "observe_exact_inactive_runtime(",
        "current_release_matches_quarantined_terminal",
        "_activate(",
        "recovery intent already exists; activation will not be repeated",
        '"blind_retry_performed": False',
        '"phone_runtime_bytes_rematerialized": False',
        '"apk_mutation_performed": False',
        '"exact_release_desired": exact_release_desired',
    ):
        assert required in source
    assert source.count("_activate(") == 1
    assert "observe_runtime_operational_health(" not in source
    for forbidden in (
        "dispatch_install_once(",
        "dispatch_release_once(",
        "_stage_runtime(",
        "_materialize_inactive(",
        "pm install",
        "adb install",
    ):
        assert forbidden not in source


def test_prepare_refuses_any_existing_request_recovery_lineage() -> None:
    source = PREPARE.read_text(encoding="utf-8")
    assert "_request_recovery_records(" in source
    assert "quarantined request already has recovery lineage" in source


def test_independent_postcondition_adapter_has_no_mutation_capability() -> None:
    source = POSTCONDITION.read_text(encoding="utf-8")
    for required in (
        "observe_exact_phone_release(",
        "observe_runtime_operational_health(",
        '"phone_mutation_performed": False',
        '"provider_mutation_performed": False',
    ):
        assert required in source
    for forbidden in (
        "_activate(",
        "dispatch_install_once(",
        "dispatch_release_once(",
        "_stage_runtime(",
        "_materialize_inactive(",
        "pm install",
        "adb install",
    ):
        assert forbidden not in source


if __name__ == "__main__":
    tests = [name for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for name in tests:
        globals()[name]()
    print(f"QUARANTINE_RECOVERY_TESTS_OK count={len(tests)}")
