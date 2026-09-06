#!/usr/bin/env python3
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from release_handoff import parse_admitted_release
from release_resolver import ReleaseAdmissionError

TAG = "v0.1.7"
APK = f"mobile-proxy-android-{TAG}.apk"
RUNTIME = f"mobile-proxy-phone-production-runtime-{TAG}.tar.gz"
PREFIX = f"https://github.com/iamaman11/mobile-proxy/releases/download/{TAG}/"


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


def expect_refused(mutator) -> None:
    value = fixture()
    mutator(value)
    try:
        parse_admitted_release(value, tag=TAG, target="phone-production")
    except ReleaseAdmissionError:
        return
    raise AssertionError("invalid admitted Release handoff unexpectedly accepted")


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


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"RELEASE_HANDOFF_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
