from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / "workflows"
sys.path.insert(0, str(CONTROLLER))

from durable_release_identity import durable_release_identity  # noqa: E402
from phone_release_reconcile import (  # noqa: E402
    PhoneReleaseReconcileError,
    reconcile_healthy_exact_projection,
)


def _identity() -> object:
    return SimpleNamespace(
        tag="v0.1.7",
        release_id=383454833,
        source_sha="a" * 40,
        artifact_digest="b3:" + "b" * 64,
        phone_runtime_artifact_name="mobile-proxy-phone-production-runtime-v0.1.7.tar.gz",
        phone_runtime_artifact_digest="b3:" + "c" * 64,
        phone_runtime_inventory_path="phone-production-runtime/components.json",
        phone_runtime_inventory_digest="b3:" + "d" * 64,
    )


def _admitted() -> object:
    return SimpleNamespace(identity=_identity())


def _admission(deployment_id: int = 77) -> object:
    payload = {
        "schema": "production-deployment-admission.v2",
        **durable_release_identity(_identity(), target="phone-production"),
        "semantic_request_id": "req-sha256:" + "e" * 64,
        "execution_id": "gh-run:1:1",
        "controller_revision": "f" * 40,
        "deployment_id": deployment_id,
        "initial_projection_state": "queued",
        "mutation_authority": False,
        "dispatch_authority": False,
        "mutation_performed": False,
    }
    return SimpleNamespace(payload=payload)


class FakeEvidence:
    def __init__(self, records: list[object]) -> None:
        self.records = records
        self.reads = 0

    def list_records(self, heading: str) -> list[object]:
        assert heading == "## DEPLOYMENT ADMISSION V2"
        self.reads += 1
        return list(self.records)


class FakeProjection:
    def __init__(self, *, state: str | None, matches: int = 1) -> None:
        self.state = state
        self.matches = matches
        self.status_calls: list[tuple[int, str, str]] = []
        self.find_calls = 0

    def find_exact(self, **kwargs):
        assert kwargs == {
            "source_sha": "a" * 40,
            "environment": "phone-production",
            "release_tag": "v0.1.7",
            "release_id": 383454833,
        }
        self.find_calls += 1
        return tuple(
            SimpleNamespace(deployment_id=77 + index, latest_state=self.state)
            for index in range(self.matches)
        )

    def status(self, *, deployment_id: int, state: str, description: str) -> int:
        assert deployment_id == 77
        assert state == "success"
        assert "HEALTHY_EXACT" in description
        assert "canonical terminal unchanged" in description
        self.status_calls.append((deployment_id, state, description))
        self.state = "success"
        return 9001


def test_repair_uses_one_existing_durable_projection_then_reads_back() -> None:
    evidence = FakeEvidence([_admission()])
    projection = FakeProjection(state="error")
    result = reconcile_healthy_exact_projection(
        evidence=evidence,
        projection=projection,
        admitted=_admitted(),
        environment="phone-production",
    )
    assert result.deployment_id == 77
    assert result.previous_state == "error"
    assert result.final_state == "success"
    assert result.projection_updated is True
    assert result.durable_admission_count == 1
    assert len(projection.status_calls) == 1
    assert projection.find_calls == 2
    assert evidence.reads == 1
    assert not hasattr(projection, "create")


def test_second_identical_reconcile_is_readback_verified_noop() -> None:
    evidence = FakeEvidence([_admission()])
    projection = FakeProjection(state="success")
    result = reconcile_healthy_exact_projection(
        evidence=evidence,
        projection=projection,
        admitted=_admitted(),
        environment="phone-production",
    )
    assert result.projection_updated is False
    assert result.previous_state == "success"
    assert result.final_state == "success"
    assert projection.status_calls == []
    assert projection.find_calls == 2


def test_zero_or_multiple_public_matches_fail_closed_without_write() -> None:
    for count in (0, 2):
        projection = FakeProjection(state="error", matches=count)
        try:
            reconcile_healthy_exact_projection(
                evidence=FakeEvidence([_admission()]),
                projection=projection,
                admitted=_admitted(),
                environment="phone-production",
            )
        except PhoneReleaseReconcileError:
            pass
        else:
            raise AssertionError(f"public match count {count} was unexpectedly accepted")
        assert projection.status_calls == []


def test_public_match_without_matching_durable_admission_fails_closed() -> None:
    projection = FakeProjection(state="error")
    try:
        reconcile_healthy_exact_projection(
            evidence=FakeEvidence([]),
            projection=projection,
            admitted=_admitted(),
            environment="phone-production",
        )
    except PhoneReleaseReconcileError as exc:
        assert "durable admission" in str(exc)
    else:
        raise AssertionError("public Deployment without durable admission was accepted")
    assert projection.status_calls == []


def test_conflicting_compatible_durable_deployment_identity_fails_closed() -> None:
    projection = FakeProjection(state="error")
    try:
        reconcile_healthy_exact_projection(
            evidence=FakeEvidence([_admission(77), _admission(88)]),
            projection=projection,
            admitted=_admitted(),
            environment="phone-production",
        )
    except PhoneReleaseReconcileError as exc:
        assert "different public Deployments" in str(exc)
    else:
        raise AssertionError("conflicting durable Deployment identities were accepted")
    assert projection.status_calls == []


def test_reconcile_adapter_has_no_phone_mutation_or_durable_write_surface() -> None:
    source = (SCRIPTS / "reconcile_phone_release.py").read_text(encoding="utf-8")
    forbidden = (
        "dispatch_release_once",
        "dispatch_install_once",
        "persist_intent(",
        "persist_terminal(",
        "persist_admission(",
        "projection.create(",
        "adb install",
        "adb push",
    )
    present = [token for token in forbidden if token in source]
    assert not present, present
    for required in (
        '"operation_class": "RECONCILE"',
        '"target_access_mode": "read_only"',
        '"phone_mutation_performed": False',
        '"deployment_created": False',
        '"deployment_intent_created": False',
        '"private_terminal_written": False',
        '"private_evidence_written": False',
        "observe_exact_phone_release(",
        "reconcile_healthy_exact_projection(",
    ):
        assert required in source


def test_reconcile_workflow_is_issue1_only_and_mandatory_evidence() -> None:
    source = (WORKFLOWS / "phone-release-reconcile.yml").read_text(encoding="utf-8")
    required = (
        "workflow_call:",
        "command != '/reconcile-phone-release phone-production v0.1.7'",
        "SOURCE_ISSUE_NUMBER'] != '1'",
        "SOURCE_ACTOR'] != os.environ['EXPECTED_OWNER']",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "group: production-target-phone-production",
        ".github/scripts/reconcile_phone_release.py",
        "Preserve mandatory bounded Stage 4 reconcile evidence",
        "STAGE4_PHONE_RELEASE_RECONCILE_ACCEPTED",
        "historical_terminal_rewritten=false",
    )
    missing = [token for token in required if token not in source]
    assert not missing, missing
    evidence = source[
        source.index("- name: Preserve mandatory bounded Stage 4 reconcile evidence") :
        source.index("- name: Record bounded Stage 4 reconcile artifact transport outcome")
    ]
    assert "continue-on-error" not in evidence
    for forbidden in (
        "workflow_dispatch:",
        "issue_comment:",
        "/deploy ",
        "/retry-deploy ",
        "dispatch_release_once",
        "adb install",
        "adb push",
    ):
        assert forbidden not in source


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"PHONE_RELEASE_RECONCILE_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
