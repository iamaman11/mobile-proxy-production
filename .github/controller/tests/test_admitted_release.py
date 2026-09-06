#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
GITHUB_DIR = ROOT.parent
sys.path.insert(0, str(ROOT))

from admitted_release import parse_admitted_release
from release_identity import ProductRelease
from release_resolver import AdmittedRelease, ReleaseAdmissionError

TAG = "v0.1.7"
SOURCE = "a" * 40
APK = f"mobile-proxy-android-{TAG}.apk"
RUNTIME = f"mobile-proxy-phone-production-runtime-{TAG}.tar.gz"


def envelope() -> dict[str, object]:
    identity = ProductRelease(
        tag=TAG,
        release_id=100,
        source_sha=SOURCE,
        artifact_name=APK,
        artifact_digest="b3:" + "1" * 64,
        manifest_digest="b3:" + "2" * 64,
        provenance_digest="b3:" + "3" * 64,
        immutable=True,
        phone_runtime_artifact_name=RUNTIME,
        phone_runtime_artifact_digest="b3:" + "4" * 64,
        phone_runtime_inventory_path="phone-production-runtime/components.json",
        phone_runtime_inventory_digest="b3:" + "5" * 64,
    )
    return AdmittedRelease(
        identity=identity,
        artifact_asset_id=11,
        artifact_download_url=f"https://github.com/iamaman11/mobile-proxy/releases/download/{TAG}/{APK}",
        artifact_transport_sha256="6" * 64,
        manifest_asset_id=12,
        provenance_asset_id=13,
        digests_asset_id=14,
        immutability_control="github-immutable-release",
        android_package="com.example.mobileproxy",
        android_version_name="0.1.7",
        android_version_code=17,
        phone_runtime_asset_id=15,
        phone_runtime_download_url=f"https://github.com/iamaman11/mobile-proxy/releases/download/{TAG}/{RUNTIME}",
        phone_runtime_transport_sha256="7" * 64,
    ).to_dict()


def expect_refused(mutator, *, tag: str = TAG, target: str = "phone-production") -> None:
    value = copy.deepcopy(envelope())
    mutator(value)
    try:
        parse_admitted_release(value, tag=tag, target=target)
    except ReleaseAdmissionError:
        return
    raise AssertionError("invalid admitted Release envelope unexpectedly accepted")


def test_hosted_admitted_release_envelope_round_trips_without_network_resolution() -> None:
    value = envelope()
    parsed = parse_admitted_release(value, tag=TAG, target="phone-production")
    assert parsed.to_dict() == value


def test_strict_parser_rejects_missing_extra_and_malformed_identity_fields() -> None:
    expect_refused(lambda value: value.pop("manifest_asset_id"))
    expect_refused(lambda value: value.__setitem__("unexpected", True))
    expect_refused(lambda value: value["identity"].pop("manifest_digest"))
    expect_refused(lambda value: value["identity"].__setitem__("unexpected", True))
    expect_refused(lambda value: value["identity"].__setitem__("release_id", "100"))
    expect_refused(lambda value: value["identity"].__setitem__("immutable", 1))


def test_strict_parser_rejects_wrong_binding_urls_digests_ids_versions_and_runtime_identity() -> None:
    expect_refused(lambda _value: None, tag="v0.1.8")
    expect_refused(lambda value: value["identity"].__setitem__("artifact_name", f"mobile-proxy-linux-x86_64-{TAG}.tar.gz"))
    expect_refused(lambda value: value.__setitem__("artifact_download_url", "https://example.invalid/release.apk"))
    expect_refused(lambda value: value.__setitem__("phone_runtime_download_url", f"https://github.com/iamaman11/mobile-proxy/releases/download/{TAG}/other.tar.gz"))
    expect_refused(lambda value: value.__setitem__("artifact_transport_sha256", "sha256:" + "6" * 64))
    expect_refused(lambda value: value.__setitem__("phone_runtime_transport_sha256", "bad"))
    expect_refused(lambda value: value.__setitem__("artifact_asset_id", 0))
    expect_refused(lambda value: value.__setitem__("manifest_asset_id", True))
    expect_refused(lambda value: value.__setitem__("phone_runtime_asset_id", 11))
    expect_refused(lambda value: value.__setitem__("android_package", "other.package"))
    expect_refused(lambda value: value.__setitem__("android_version_name", "0.1.8"))
    expect_refused(lambda value: value.__setitem__("android_version_code", False))
    expect_refused(lambda value: value["identity"].__setitem__("phone_runtime_artifact_name", "other.tar.gz"))
    expect_refused(lambda value: value["identity"].__setitem__("phone_runtime_inventory_path", "other/components.json"))
    expect_refused(lambda _value: None, target="vm-production")


def _load_target_script():
    path = GITHUB_DIR / "scripts" / "run_phone_release_deployment.py"
    spec = importlib.util.spec_from_file_location("admitted_release_target_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_target_execution_consumes_local_admission_before_any_phone_adapter_boundary() -> None:
    source = (GITHUB_DIR / "scripts" / "run_phone_release_deployment.py").read_text(encoding="utf-8")
    assert "resolve_release(" not in source
    assert "from release_resolver import resolve_release" not in source
    parsed = source.index("admitted = parse_admitted_release(")
    serial = source.index('serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")')
    materialize = source.index("_materialize_verified_release_apk(admitted, apk, facts)")
    observe = source.index("apk_pre, runtime_pre = _observe_composite(")
    dispatch = source.index("dispatch = dispatch_release_once(")
    assert parsed < serial < materialize < observe < dispatch


class _FakeEvidence:
    def __init__(self) -> None:
        self.terminals: list[dict[str, object]] = []

    def persist_terminal(self, payload):
        self.terminals.append(dict(payload))
        return SimpleNamespace(ref="issue-comment:999")


class _FakeProjection:
    def __init__(self) -> None:
        self.status_calls: list[dict[str, object]] = []

    def status(self, **kwargs):
        self.status_calls.append(dict(kwargs))
        return 1


def test_bounded_pre_intent_target_refusal_persists_canonical_refused_terminal() -> None:
    target = _load_target_script()
    admitted = parse_admitted_release(envelope(), tag=TAG, target="phone-production")
    request = {"request_id": "req-sha256:" + "8" * 64, "target": "phone-production"}
    evidence = _FakeEvidence()
    projection = _FakeProjection()
    with tempfile.TemporaryDirectory() as raw:
        output = Path(raw) / "terminal.json"
        terminal = target._persist_pre_intent_refusal(
            request=request,
            execution_id="gh-run:123:1",
            controller_revision="9" * 40,
            admitted=admitted,
            deployment_id=77,
            facts={"release_admission": {"validated_locally": True}},
            evidence_refs=["issue-comment:1"],
            evidence=evidence,
            projection=projection,
            output=output,
            reason="registered Android production target binding is unavailable",
        )
        persisted = json.loads(output.read_text(encoding="utf-8"))
    assert terminal == persisted == evidence.terminals[0]
    assert terminal["schema"] == "production-deployment-terminal.v2"
    assert terminal["state"] == "REFUSED"
    assert terminal["current_step"] == "OBSERVE"
    assert terminal["mutation_performed"] is False
    assert terminal["postcondition_verified"] is False
    assert terminal["recovery_required"] is False
    assert terminal["blocking_predicates"] == ["registered Android production target binding is unavailable"]
    assert projection.status_calls[-1]["state"] == "failure"


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"ADMITTED_RELEASE_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
