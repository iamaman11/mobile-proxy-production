#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
GITHUB_DIR = ROOT.parent
sys.path.insert(0, str(ROOT))

from deployment_request import RequestProvenance, build_deployment_request
from release_handoff import parse_admitted_release
from release_resolver import ReleaseAdmissionError

TAG = "v0.1.7"
APK = f"mobile-proxy-android-{TAG}.apk"
RUNTIME = f"mobile-proxy-phone-production-runtime-{TAG}.tar.gz"
PREFIX = f"https://github.com/iamaman11/mobile-proxy/releases/download/{TAG}/"
CONTROLLER_SHA = "a" * 40


def _load_runner(name: str):
    path = GITHUB_DIR / "scripts" / "run_phone_release_deployment.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fixture() -> dict[str, object]:
    return {
        "identity": {
            "tag": TAG,
            "release_id": 383454833,
            "source_sha": "a" * 40,
            "artifact_name": APK,
            "artifact_digest": "b3:" + "1" * 64,
            "manifest_digest": "b3:" + "2" * 64,
            "provenance_digest": "b3:" + "3" * 64,
            "immutable": True,
            "phone_runtime_artifact_name": RUNTIME,
            "phone_runtime_artifact_digest": "b3:" + "4" * 64,
            "phone_runtime_inventory_path": "phone-production-runtime/components.json",
            "phone_runtime_inventory_digest": "b3:" + "5" * 64,
        },
        "artifact_asset_id": 11,
        "artifact_download_url": PREFIX + APK,
        "artifact_transport_sha256": "6" * 64,
        "manifest_asset_id": 12,
        "provenance_asset_id": 13,
        "digests_asset_id": 14,
        "immutability_control": "github-immutable-release",
        "android_package": "com.example.mobileproxy",
        "android_version_name": "0.1.7",
        "android_version_code": 17,
        "phone_runtime_asset_id": 15,
        "phone_runtime_download_url": PREFIX + RUNTIME,
        "phone_runtime_transport_sha256": "7" * 64,
    }


def _request() -> dict[str, object]:
    return build_deployment_request(
        target="phone-production",
        product_release_tag=TAG,
        provenance=RequestProvenance(
            "iamaman11/mobile-proxy-production", 1, 900, "iamaman11"
        ),
    ).to_dict()


def expect_refused(mutator) -> None:
    value = fixture()
    mutator(value)
    try:
        parse_admitted_release(value, tag=TAG, target="phone-production")
    except ReleaseAdmissionError:
        return
    raise AssertionError("invalid admitted Release handoff unexpectedly accepted")


class _FakeEvidence:
    def __init__(self) -> None:
        self.terminals: list[dict[str, object]] = []

    def request_history(self, _request_id: str):
        return None, None

    def persist_terminal(self, payload: dict[str, object]):
        self.terminals.append(payload)
        return SimpleNamespace(ref="issue-comment:999")


class _FakeProjection:
    def __init__(self, *, fail: bool = False, error_type=RuntimeError) -> None:
        self.fail = fail
        self.error_type = error_type
        self.status_calls: list[dict[str, object]] = []

    def status(self, **kwargs):
        self.status_calls.append(dict(kwargs))
        if self.fail:
            raise self.error_type("bounded public projection failure")
        return 1


def _run_target_main(
    runner,
    *,
    admitted: dict[str, object],
    evidence: _FakeEvidence,
    projection: _FakeProjection,
    serial: str | None,
    binding_key: str | None,
) -> tuple[int, dict[str, object] | None]:
    original_argv = list(sys.argv)
    original_env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="handoff-test-") as td:
        root = Path(td)
        output = root / "terminal.json"
        runner.IssueEvidenceStore = lambda _token: evidence
        runner.PublicDeploymentProjection = lambda _token: projection
        runner.observe = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("phone observe called"))
        runner.observe_runtime = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("runtime observe called"))
        runner.dispatch_release_once = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("dispatch called"))
        runner._materialize_verified_release_apk = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("APK materialization called"))
        runner._materialize_verified_release_runtime = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("runtime materialization called"))
        try:
            os.environ.clear()
            os.environ.update(original_env)
            os.environ["GITHUB_SHA"] = CONTROLLER_SHA
            os.environ.pop("ANDROID_PRODUCTION_SERIAL", None)
            os.environ.pop("ANDROID_TARGET_BINDING_KEY", None)
            if serial is not None:
                os.environ["ANDROID_PRODUCTION_SERIAL"] = serial
            if binding_key is not None:
                os.environ["ANDROID_TARGET_BINDING_KEY"] = binding_key
            sys.argv = [
                "run_phone_release_deployment.py",
                "--request-json", json.dumps(_request(), separators=(",", ":")),
                "--admitted-release-json", json.dumps(admitted, separators=(",", ":")),
                "--deployment-id", "42",
                "--execution-id", "gh-run:123:1",
                "--controller-revision", CONTROLLER_SHA,
                "--recovery-only", "false",
                "--product-root", str(root / "product"),
                "--runtime-manifest", str(root / "runtime.json"),
                "--output", str(output),
            ]
            rc = runner.main()
            terminal = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
            return rc, terminal
        finally:
            sys.argv = original_argv
            os.environ.clear()
            os.environ.update(original_env)


def test_exact_hosted_envelope_round_trips_without_network_resolution() -> None:
    value = fixture()
    admitted = parse_admitted_release(value, tag=TAG, target="phone-production")
    assert admitted.to_dict() == value
    assert admitted.identity.tag == TAG
    assert admitted.identity.artifact_name == APK
    assert admitted.identity.phone_runtime_artifact_name == RUNTIME


def test_missing_or_extra_top_level_field_is_refused() -> None:
    expect_refused(lambda value: value.pop("manifest_asset_id"))
    expect_refused(lambda value: value.__setitem__("unexpected", True))


def test_missing_or_extra_identity_field_is_refused() -> None:
    expect_refused(lambda value: value["identity"].pop("source_sha"))
    expect_refused(lambda value: value["identity"].__setitem__("unexpected", True))


def test_request_tag_and_exact_phone_artifacts_are_bound() -> None:
    value = fixture()
    try:
        parse_admitted_release(value, tag="v0.1.8", target="phone-production")
    except ReleaseAdmissionError:
        pass
    else:
        raise AssertionError("wrong request tag was accepted")
    expect_refused(lambda value: value["identity"].__setitem__("artifact_name", f"mobile-proxy-other-{TAG}.apk"))
    expect_refused(lambda value: value["identity"].__setitem__("phone_runtime_artifact_name", f"mobile-proxy-phone-production-runtime-v0.1.8.tar.gz"))


def test_release_download_urls_must_be_exact_immutable_asset_shapes() -> None:
    expect_refused(lambda value: value.__setitem__("artifact_download_url", "https://example.invalid/mobile-proxy.apk"))
    expect_refused(lambda value: value.__setitem__("artifact_download_url", PREFIX + "../" + APK))
    expect_refused(lambda value: value.__setitem__("phone_runtime_download_url", PREFIX + APK))


def test_transport_digests_and_asset_ids_are_strict() -> None:
    expect_refused(lambda value: value.__setitem__("artifact_transport_sha256", "sha256:" + "6" * 64))
    expect_refused(lambda value: value.__setitem__("phone_runtime_transport_sha256", "not-a-digest"))
    expect_refused(lambda value: value.__setitem__("artifact_asset_id", True))
    expect_refused(lambda value: value.__setitem__("manifest_asset_id", 0))
    expect_refused(lambda value: value.__setitem__("phone_runtime_asset_id", value["artifact_asset_id"]))


def test_android_package_and_version_fields_are_exact() -> None:
    expect_refused(lambda value: value.__setitem__("android_package", "other.package"))
    expect_refused(lambda value: value.__setitem__("android_version_name", "0.1.8"))
    expect_refused(lambda value: value.__setitem__("android_version_code", "17"))


def test_identity_primitives_are_not_coerced() -> None:
    expect_refused(lambda value: value["identity"].__setitem__("release_id", "383454833"))
    expect_refused(lambda value: value["identity"].__setitem__("immutable", 1))
    expect_refused(lambda value: value["identity"].__setitem__("source_sha", 123))


def test_non_phone_target_is_not_admitted_by_phone_handoff_parser() -> None:
    try:
        parse_admitted_release(copy.deepcopy(fixture()), tag=TAG, target="vm-production")
    except ReleaseAdmissionError:
        return
    raise AssertionError("phone handoff parser accepted VM target")


def test_target_runner_has_no_second_live_release_resolution() -> None:
    source = (GITHUB_DIR / "scripts" / "run_phone_release_deployment.py").read_text(encoding="utf-8")
    assert "resolve_release(" not in source
    parse_at = source.index("admitted = parse_admitted_release(")
    serial_at = source.index('serial = os.environ.get("ANDROID_PRODUCTION_SERIAL"')
    assert parse_at < serial_at


def test_invalid_handoff_stops_before_evidence_or_phone_access() -> None:
    runner = _load_runner("handoff_invalid_target_runner")
    runner.IssueEvidenceStore = lambda _token: (_ for _ in ()).throw(AssertionError("evidence accessed"))
    runner.PublicDeploymentProjection = lambda _token: (_ for _ in ()).throw(AssertionError("projection accessed"))
    runner.observe = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("phone observe called"))
    runner.observe_runtime = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("runtime observe called"))
    runner.dispatch_release_once = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("dispatch called"))
    bad = fixture()
    bad["unexpected"] = True
    original_argv = list(sys.argv)
    original_env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="handoff-invalid-") as td:
        root = Path(td)
        try:
            os.environ["GITHUB_SHA"] = CONTROLLER_SHA
            sys.argv = [
                "run_phone_release_deployment.py",
                "--request-json", json.dumps(_request(), separators=(",", ":")),
                "--admitted-release-json", json.dumps(bad, separators=(",", ":")),
                "--deployment-id", "42",
                "--execution-id", "gh-run:123:1",
                "--controller-revision", CONTROLLER_SHA,
                "--recovery-only", "false",
                "--product-root", str(root / "product"),
                "--runtime-manifest", str(root / "runtime.json"),
                "--output", str(root / "terminal.json"),
            ]
            try:
                runner.main()
            except SystemExit as exc:
                assert "hosted admitted Release envelope is invalid" in str(exc)
            else:
                raise AssertionError("invalid admitted envelope reached target execution")
        finally:
            sys.argv = original_argv
            os.environ.clear()
            os.environ.update(original_env)


def test_missing_target_binding_emits_canonical_pre_intent_refused_terminal() -> None:
    runner = _load_runner("handoff_binding_target_runner")
    evidence = _FakeEvidence()
    projection = _FakeProjection(error_type=runner.ProjectionError)
    rc, terminal = _run_target_main(
        runner,
        admitted=fixture(),
        evidence=evidence,
        projection=projection,
        serial=None,
        binding_key=None,
    )
    assert rc == 2
    assert terminal is not None
    assert len(evidence.terminals) == 1
    assert terminal == evidence.terminals[0]
    assert terminal["state"] == "REFUSED"
    assert terminal["mutation_performed"] is False
    assert terminal["postcondition_verified"] is False
    assert terminal["recovery_required"] is False
    assert terminal["deployment_id"] == 42
    assert len(projection.status_calls) == 1


def test_pre_intent_projection_failure_still_persists_canonical_refused_terminal() -> None:
    runner = _load_runner("handoff_projection_target_runner")
    evidence = _FakeEvidence()
    projection = _FakeProjection(fail=True, error_type=runner.ProjectionError)
    rc, terminal = _run_target_main(
        runner,
        admitted=fixture(),
        evidence=evidence,
        projection=projection,
        serial="registered-serial",
        binding_key="k" * 32,
    )
    assert rc == 2
    assert terminal is not None
    assert len(evidence.terminals) == 1
    assert terminal["state"] == "REFUSED"
    assert terminal["mutation_performed"] is False
    assert terminal["postcondition_verified"] is False
    assert terminal["recovery_required"] is False
    assert terminal["facts"]["public_projection"] == {"available": False}
    assert len(projection.status_calls) == 2


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"RELEASE_HANDOFF_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
