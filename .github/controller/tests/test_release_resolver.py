#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from release_resolver import ReleaseAdmissionError, resolve_payload

TAG = "v0.1.4"
SOURCE = "a" * 40
APK = f"mobile-proxy-android-{TAG}.apk"
LINUX = f"mobile-proxy-linux-x86_64-{TAG}.tar.gz"
DOMAIN = "mobile-proxy/product-release-asset/v2"
APK_DIGEST = "b3:" + "1" * 64
LINUX_DIGEST = "b3:" + "2" * 64
MANIFEST_DIGEST = "b3:" + "3" * 64
PROVENANCE_DIGEST = "b3:" + "4" * 64


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
                version_name="0.1.4",
                version_code=14,
            ),
        ],
    }
    provenance = {
        "format_version": 2,
        "git_sha": SOURCE,
        "release_tag": TAG,
        "artifacts": [
            product_record(LINUX, LINUX_DIGEST),
            product_record(APK, APK_DIGEST),
        ],
    }
    digest_set = {
        "format_version": 1,
        "algorithm": "blake3-256",
        "digest_domain": DOMAIN,
        "assets": [
            {"name": APK, "digest": APK_DIGEST},
            {"name": LINUX, "digest": LINUX_DIGEST},
            {"name": "release-manifest.json", "digest": MANIFEST_DIGEST},
            {"name": "provenance.json", "digest": PROVENANCE_DIGEST},
        ],
    }
    return release, manifest, provenance, digest_set


def admitted():
    release, manifest, provenance, digest_set = fixture()
    return resolve_payload(
        tag=TAG,
        target="phone-production",
        release=release,
        source_sha=SOURCE,
        manifest=manifest,
        provenance=provenance,
        digest_set=digest_set,
    )


def expect_refused(mutator) -> None:
    release, manifest, provenance, digest_set = fixture()
    mutator(release, manifest, provenance, digest_set)
    try:
        resolve_payload(
            tag=TAG,
            target="phone-production",
            release=release,
            source_sha=SOURCE,
            manifest=manifest,
            provenance=provenance,
            digest_set=digest_set,
        )
    except ReleaseAdmissionError:
        return
    raise AssertionError("invalid Release unexpectedly admitted")


def test_valid_v2_immutable_release() -> None:
    value = admitted()
    assert value.identity.tag == TAG
    assert value.identity.artifact_digest == APK_DIGEST
    assert value.identity.manifest_digest == MANIFEST_DIGEST
    assert value.identity.provenance_digest == PROVENANCE_DIGEST
    assert value.identity.immutable is True
    assert value.artifact_transport_sha256 == "a" * 64
    assert value.android_package == "com.example.mobileproxy"
    assert value.android_version_name == "0.1.4"
    assert value.android_version_code == 14
    assert value.immutability_control == "github-immutable-release"


def test_mutable_release_refused() -> None:
    expect_refused(lambda release, *_: release.__setitem__("immutable", False))


def test_missing_apk_refused() -> None:
    expect_refused(lambda release, *_: release.__setitem__("assets", release["assets"][1:]))


def test_extra_asset_refused() -> None:
    expect_refused(lambda release, *_: release["assets"].append(asset("unexpected.txt", 9, "f" * 64)))


def test_manifest_digest_mismatch_refused() -> None:
    expect_refused(
        lambda _r, manifest, _p, _d: manifest["artifacts"][1].__setitem__(
            "content_digest", "b3:" + "9" * 64
        )
    )


def test_provenance_digest_mismatch_refused() -> None:
    expect_refused(
        lambda _r, _m, provenance, _d: provenance["artifacts"][1].__setitem__(
            "content_digest", "b3:" + "8" * 64
        )
    )


def test_digest_set_mismatch_refused() -> None:
    expect_refused(
        lambda _r, _m, _p, digests: digests["assets"][0].__setitem__(
            "digest", "b3:" + "7" * 64
        )
    )


def test_wrong_digest_domain_refused() -> None:
    expect_refused(lambda _r, _m, _p, digests: digests.__setitem__("digest_domain", "wrong-domain"))


def test_wrong_package_refused() -> None:
    expect_refused(
        lambda _r, manifest, _p, _d: manifest["artifacts"][1].__setitem__(
            "package_name", "other.package"
        )
    )


def test_wrong_version_refused() -> None:
    expect_refused(
        lambda _r, manifest, _p, _d: manifest["artifacts"][1].__setitem__(
            "version_name", "0.1.5"
        )
    )


def test_legacy_manifest_v1_refused() -> None:
    expect_refused(lambda _r, manifest, _p, _d: manifest.__setitem__("format_version", 1))


def test_obsolete_sha256sums_contract_refused() -> None:
    def mutate(release, _manifest, _provenance, digest_set):
        release["assets"][-1] = asset("SHA256SUMS", 5, "e" * 64)
        digest_set["algorithm"] = "sha256"
        digest_set["digest_domain"] = ""
    expect_refused(mutate)


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"RELEASE_RESOLVER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
