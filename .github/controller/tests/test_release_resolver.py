#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
import json
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import release_resolver as resolver
from release_resolver import ReleaseAdmissionError, resolve_payload

TAG = "v0.1.6"
SOURCE = "a" * 40
APK = f"mobile-proxy-android-{TAG}.apk"
LINUX = f"mobile-proxy-linux-x86_64-{TAG}.tar.gz"
PHONE = f"mobile-proxy-phone-production-runtime-{TAG}.tar.gz"
DOMAIN = "mobile-proxy/product-release-asset/v2"
APK_DIGEST = "b3:" + "1" * 64
LINUX_DIGEST = "b3:" + "2" * 64
PHONE_DIGEST = "b3:" + "5" * 64
MANIFEST_DIGEST = "b3:" + "3" * 64
PROVENANCE_DIGEST = "b3:" + "4" * 64
INVENTORY_DIGEST = "b3:" + "6" * 64
COMPONENT_DIGEST = "b3:" + "7" * 64
SING_BOX_DIGEST = "b3:" + "8" * 64


def asset(name: str, ident: int, transport_digest: str) -> dict[str, object]:
    return {
        "name": name,
        "id": ident,
        "digest": "sha256:" + transport_digest,
        "state": "uploaded",
        "browser_download_url": f"https://github.com/iamaman11/mobile-proxy/releases/download/{TAG}/{name}",
    }


def product_record(name: str, digest: str, **extra: object) -> dict[str, object]:
    return {
        "name": name,
        "content_digest": digest,
        "content_digest_algorithm": "blake3-256",
        "content_digest_domain": DOMAIN,
        **extra,
    }


def phone_record(digest: str = PHONE_DIGEST) -> dict[str, object]:
    return product_record(
        PHONE,
        digest,
        kind="phone-production-runtime-tar",
        target="phone-production",
        runtime_abi={
            "os": "android",
            "arch": "arm",
            "rust_target": "armv7-linux-androideabi",
            "elf_machine": 40,
        },
        component_inventory={
            "path": "phone-production-runtime/components.json",
            "content_digest": INVENTORY_DIGEST,
            "content_digest_algorithm": "blake3-256",
            "content_digest_domain": DOMAIN,
        },
        components=[
            {
                "name": "runtime-supervisor",
                "archive_path": "bin/runtime-supervisor",
                "kind": "native-executable",
                "executable": True,
                "content_digest": COMPONENT_DIGEST,
                "content_digest_algorithm": "blake3-256",
                "content_digest_domain": DOMAIN,
            }
        ],
        third_party_runtime=[
            {
                "name": "sing-box",
                "version": "1.12.0",
                "lock_target": "android-arm",
                "archive_size": 12345,
                "archive_upstream_sha256": "9" * 64,
                "archive_content_digest": SING_BOX_DIGEST,
                "archive_content_digest_algorithm": "blake3-256",
                "archive_content_digest_domain": "mobile-proxy/upstream-sing-box-archive/v1",
            }
        ],
    )


def fixture() -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    release = {
        "id": 100,
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "immutable": True,
        "assets": [
            asset(APK, 1, "a" * 64),
            asset(LINUX, 2, "b" * 64),
            asset(PHONE, 6, "f" * 64),
            asset("release-manifest.json", 3, "c" * 64),
            asset("provenance.json", 4, "d" * 64),
            asset("artifact-digests.json", 5, "e" * 64),
        ],
    }
    manifest = {
        "format_version": 2,
        "git_sha": SOURCE,
        "release_tag": TAG,
        "artifacts": [
            product_record(LINUX, LINUX_DIGEST, kind="linux-x86_64-tar"),
            product_record(
                APK,
                APK_DIGEST,
                kind="android-apk",
                package_name="com.example.mobileproxy",
                version_name="0.1.6",
                version_code=16,
            ),
            phone_record(),
        ],
    }
    provenance = {
        "format_version": 2,
        "git_sha": SOURCE,
        "release_tag": TAG,
        "artifacts": [
            product_record(LINUX, LINUX_DIGEST),
            product_record(APK, APK_DIGEST),
            product_record(PHONE, PHONE_DIGEST),
        ],
    }
    digest_set = {
        "format_version": 1,
        "algorithm": "blake3-256",
        "digest_domain": DOMAIN,
        "assets": [
            {"name": APK, "digest": APK_DIGEST},
            {"name": LINUX, "digest": LINUX_DIGEST},
            {"name": PHONE, "digest": PHONE_DIGEST},
            {"name": "release-manifest.json", "digest": MANIFEST_DIGEST},
            {"name": "provenance.json", "digest": PROVENANCE_DIGEST},
        ],
    }
    return release, manifest, provenance, digest_set


def admitted(target: str = "phone-production"):
    release, manifest, provenance, digest_set = fixture()
    return resolve_payload(
        tag=TAG,
        target=target,
        release=release,
        source_sha=SOURCE,
        manifest=manifest,
        provenance=provenance,
        digest_set=digest_set,
    )


def expect_refused(mutator, target: str = "phone-production") -> None:
    release, manifest, provenance, digest_set = fixture()
    mutator(release, manifest, provenance, digest_set)
    try:
        resolve_payload(
            tag=TAG,
            target=target,
            release=release,
            source_sha=SOURCE,
            manifest=manifest,
            provenance=provenance,
            digest_set=digest_set,
        )
    except ReleaseAdmissionError:
        return
    raise AssertionError("invalid Release unexpectedly admitted")


def test_valid_six_asset_phone_release_binds_apk_and_runtime_identity() -> None:
    value = admitted()
    assert value.identity.tag == TAG
    assert value.identity.artifact_name == APK
    assert value.identity.artifact_digest == APK_DIGEST
    assert value.identity.phone_runtime_artifact_name == PHONE
    assert value.identity.phone_runtime_artifact_digest == PHONE_DIGEST
    assert value.identity.phone_runtime_inventory_path == "phone-production-runtime/components.json"
    assert value.identity.phone_runtime_inventory_digest == INVENTORY_DIGEST
    assert value.phone_runtime_asset_id == 6
    assert value.phone_runtime_transport_sha256 == "f" * 64
    assert value.phone_runtime_download_url.endswith("/" + PHONE)
    assert value.android_package == "com.example.mobileproxy"
    assert value.android_version_name == "0.1.6"
    assert value.android_version_code == 16


def test_missing_phone_runtime_asset_refused() -> None:
    expect_refused(
        lambda release, *_: release.__setitem__(
            "assets", [item for item in release["assets"] if item["name"] != PHONE]
        )
    )


def test_five_asset_legacy_release_refused_for_future_phone_deployment() -> None:
    def mutate(release, manifest, provenance, digests):
        release["assets"] = [item for item in release["assets"] if item["name"] != PHONE]
        manifest["artifacts"] = [item for item in manifest["artifacts"] if item["name"] != PHONE]
        provenance["artifacts"] = [item for item in provenance["artifacts"] if item["name"] != PHONE]
        digests["assets"] = [item for item in digests["assets"] if item["name"] != PHONE]

    expect_refused(mutate)


def test_phone_runtime_digest_mismatch_refused() -> None:
    expect_refused(
        lambda _r, manifest, _p, _d: manifest["artifacts"][2].__setitem__(
            "content_digest", "b3:" + "0" * 64
        )
    )


def test_phone_runtime_inventory_digest_must_be_typed() -> None:
    expect_refused(
        lambda _r, manifest, _p, _d: manifest["artifacts"][2]["component_inventory"].__setitem__(
            "content_digest", "not-a-typed-digest"
        )
    )


def test_phone_runtime_inventory_path_is_exact() -> None:
    expect_refused(
        lambda _r, manifest, _p, _d: manifest["artifacts"][2]["component_inventory"].__setitem__(
            "path", "other/components.json"
        )
    )


def test_phone_runtime_component_set_cannot_disappear() -> None:
    expect_refused(
        lambda _r, manifest, _p, _d: manifest["artifacts"][2].__setitem__("components", [])
    )


def test_phone_runtime_cannot_substitute_vm_artifact() -> None:
    def mutate(_release, manifest, _provenance, _digests):
        record = manifest["artifacts"][2]
        record["name"] = LINUX

    expect_refused(mutate)


def test_phone_runtime_requires_android_arm_sing_box_identity() -> None:
    expect_refused(
        lambda _r, manifest, _p, _d: manifest["artifacts"][2]["third_party_runtime"][0].__setitem__(
            "lock_target", "linux-amd64-glibc"
        )
    )


def test_vm_selection_remains_linux_and_does_not_carry_phone_identity() -> None:
    value = admitted("vm-production")
    assert value.identity.artifact_name == LINUX
    assert value.identity.artifact_digest == LINUX_DIGEST
    assert value.identity.phone_runtime_artifact_name is None
    assert value.identity.phone_runtime_artifact_digest is None
    assert value.phone_runtime_asset_id is None
    assert value.phone_runtime_download_url is None


def test_vm_only_payload_digest_change_does_not_substitute_phone_runtime() -> None:
    release, manifest, provenance, digest_set = fixture()
    changed_linux = "b3:" + "a" * 64
    manifest["artifacts"][0]["content_digest"] = changed_linux
    provenance["artifacts"][0]["content_digest"] = changed_linux
    digest_set["assets"][1]["digest"] = changed_linux
    phone = resolve_payload(
        tag=TAG,
        target="phone-production",
        release=release,
        source_sha=SOURCE,
        manifest=manifest,
        provenance=provenance,
        digest_set=digest_set,
    )
    assert phone.identity.artifact_digest == APK_DIGEST
    assert phone.identity.phone_runtime_artifact_digest == PHONE_DIGEST


def test_mutable_release_refused() -> None:
    expect_refused(lambda release, *_: release.__setitem__("immutable", False))


def test_extra_asset_refused() -> None:
    expect_refused(lambda release, *_: release["assets"].append(asset("unexpected.txt", 9, "f" * 64)))


def test_wrong_package_refused() -> None:
    expect_refused(
        lambda _r, manifest, _p, _d: manifest["artifacts"][1].__setitem__(
            "package_name", "other.package"
        )
    )


def test_wrong_version_refused() -> None:
    expect_refused(
        lambda _r, manifest, _p, _d: manifest["artifacts"][1].__setitem__(
            "version_name", "0.1.7"
        )
    )


def test_legacy_manifest_v1_refused() -> None:
    expect_refused(lambda _r, manifest, _p, _d: manifest.__setitem__("format_version", 1))


def test_contract_download_recovers_from_transient_transport_with_fixed_bound() -> None:
    payload = b'{"format_version":2}'
    item = asset("release-manifest.json", 3, hashlib.sha256(payload).hexdigest())
    responses: list[object] = [
        urllib.error.URLError("temporary transport"),
        urllib.error.HTTPError(item["browser_download_url"], 504, "gateway timeout", None, None),
        io.BytesIO(payload),
    ]
    calls = 0
    original = resolver.urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):
        nonlocal calls
        assert timeout == 30
        calls += 1
        value = responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    resolver.urllib.request.urlopen = fake_urlopen
    try:
        assert resolver._download(item) == payload
        assert calls == 3
    finally:
        resolver.urllib.request.urlopen = original


def test_contract_download_transport_exhaustion_is_exactly_three_attempts() -> None:
    payload = b"expected"
    item = asset("release-manifest.json", 3, hashlib.sha256(payload).hexdigest())
    calls = 0
    original = resolver.urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):
        nonlocal calls
        assert timeout == 30
        calls += 1
        raise urllib.error.URLError("temporary transport")

    resolver.urllib.request.urlopen = fake_urlopen
    try:
        try:
            resolver._download(item)
        except ReleaseAdmissionError as exc:
            assert str(exc) == "release contract asset is unavailable"
        else:
            raise AssertionError("contract download exhaustion unexpectedly succeeded")
        assert calls == 3
    finally:
        resolver.urllib.request.urlopen = original


def test_contract_download_integrity_mismatch_is_not_retried() -> None:
    item = asset("release-manifest.json", 3, hashlib.sha256(b"expected").hexdigest())
    calls = 0
    original = resolver.urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):
        nonlocal calls
        assert timeout == 30
        calls += 1
        return io.BytesIO(b"different")

    resolver.urllib.request.urlopen = fake_urlopen
    try:
        try:
            resolver._download(item)
        except ReleaseAdmissionError as exc:
            assert str(exc) == "release contract asset transport digest differs"
        else:
            raise AssertionError("contract digest mismatch unexpectedly succeeded")
        assert calls == 1
    finally:
        resolver.urllib.request.urlopen = original


def test_release_metadata_recovers_from_transient_server_failure() -> None:
    url = "https://api.github.com/repos/iamaman11/mobile-proxy/releases/tags/v0.1.7"
    responses: list[object] = [
        urllib.error.HTTPError(url, 504, "gateway timeout", None, None),
        urllib.error.URLError("temporary transport"),
        io.BytesIO(json.dumps({"id": 1}).encode("utf-8")),
    ]
    calls = 0
    original = resolver.urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):
        nonlocal calls
        assert timeout == 30
        calls += 1
        value = responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    resolver.urllib.request.urlopen = fake_urlopen
    try:
        assert resolver._request_json(url) == {"id": 1}
        assert calls == 3
    finally:
        resolver.urllib.request.urlopen = original


def test_release_metadata_non_transient_http_failure_is_not_retried() -> None:
    url = "https://api.github.com/repos/iamaman11/mobile-proxy/releases/tags/v0.1.7"
    calls = 0
    original = resolver.urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):
        nonlocal calls
        assert timeout == 30
        calls += 1
        raise urllib.error.HTTPError(url, 404, "not found", None, None)

    resolver.urllib.request.urlopen = fake_urlopen
    try:
        try:
            resolver._request_json(url)
        except ReleaseAdmissionError as exc:
            assert str(exc) == "public Release metadata is unavailable"
        else:
            raise AssertionError("non-transient metadata HTTP failure unexpectedly succeeded")
        assert calls == 1
    finally:
        resolver.urllib.request.urlopen = original


def test_release_metadata_invalid_json_is_not_retried() -> None:
    url = "https://api.github.com/repos/iamaman11/mobile-proxy/releases/tags/v0.1.7"
    calls = 0
    original = resolver.urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):
        nonlocal calls
        assert timeout == 30
        calls += 1
        return io.BytesIO(b"not-json")

    resolver.urllib.request.urlopen = fake_urlopen
    try:
        try:
            resolver._request_json(url)
        except ReleaseAdmissionError as exc:
            assert str(exc) == "public Release metadata is unavailable"
        else:
            raise AssertionError("invalid metadata JSON unexpectedly succeeded")
        assert calls == 1
    finally:
        resolver.urllib.request.urlopen = original


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"RELEASE_RESOLVER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
