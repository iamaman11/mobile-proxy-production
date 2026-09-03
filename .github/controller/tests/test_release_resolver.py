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
APK_DIGEST = "1" * 64
MANIFEST_DIGEST = "2" * 64
PROVENANCE_DIGEST = "3" * 64
SUMS_DIGEST = "4" * 64


def asset(name: str, ident: int, digest: str) -> dict[str, object]:
    return {
        "name": name,
        "id": ident,
        "digest": "sha256:" + digest,
        "state": "uploaded",
        "browser_download_url": f"https://github.com/iamaman11/mobile-proxy/releases/download/{TAG}/{name}",
    }


def fixture() -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, str]]:
    release = {
        "id": 100,
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "immutable": True,
        "assets": [
            asset(APK, 1, APK_DIGEST),
            asset("release-manifest.json", 2, MANIFEST_DIGEST),
            asset("provenance.json", 3, PROVENANCE_DIGEST),
            asset("SHA256SUMS", 4, SUMS_DIGEST),
        ],
    }
    manifest = {
        "format_version": 2,
        "git_sha": SOURCE,
        "release_tag": TAG,
        "artifacts": [{
            "name": APK,
            "sha256": APK_DIGEST,
            "kind": "android-apk",
            "package_name": "com.example.mobileproxy",
            "version_name": "0.1.4",
            "version_code": 14,
        }],
    }
    provenance = {
        "format_version": 2,
        "git_sha": SOURCE,
        "release_tag": TAG,
        "artifacts": [{"name": APK, "sha256": APK_DIGEST}],
    }
    checksums = {
        APK: APK_DIGEST,
        "release-manifest.json": MANIFEST_DIGEST,
        "provenance.json": PROVENANCE_DIGEST,
    }
    return release, manifest, provenance, checksums


def admitted():
    release, manifest, provenance, checksums = fixture()
    return resolve_payload(
        tag=TAG,
        target="phone-production",
        release=release,
        source_sha=SOURCE,
        manifest=manifest,
        provenance=provenance,
        checksums=checksums,
    )


def expect_refused(mutator) -> None:
    release, manifest, provenance, checksums = fixture()
    mutator(release, manifest, provenance, checksums)
    try:
        resolve_payload(
            tag=TAG,
            target="phone-production",
            release=release,
            source_sha=SOURCE,
            manifest=manifest,
            provenance=provenance,
            checksums=checksums,
        )
    except ReleaseAdmissionError:
        return
    raise AssertionError("invalid Release unexpectedly admitted")


def test_valid_v2_immutable_release() -> None:
    value = admitted()
    assert value.identity.tag == TAG
    assert value.identity.artifact_digest == APK_DIGEST
    assert value.identity.immutable is True
    assert value.android_package == "com.example.mobileproxy"
    assert value.android_version_name == "0.1.4"
    assert value.android_version_code == 14
    assert value.immutability_control == "github-immutable-release"


def test_mutable_release_refused() -> None:
    expect_refused(lambda release, *_: release.__setitem__("immutable", False))


def test_missing_apk_refused() -> None:
    expect_refused(lambda release, *_: release.__setitem__("assets", release["assets"][1:]))


def test_manifest_digest_mismatch_refused() -> None:
    expect_refused(lambda _r, manifest, _p, _s: manifest["artifacts"][0].__setitem__("sha256", "9" * 64))


def test_provenance_digest_mismatch_refused() -> None:
    expect_refused(lambda _r, _m, provenance, _s: provenance["artifacts"][0].__setitem__("sha256", "8" * 64))


def test_checksums_mismatch_refused() -> None:
    expect_refused(lambda _r, _m, _p, sums: sums.__setitem__(APK, "7" * 64))


def test_wrong_package_refused() -> None:
    expect_refused(lambda _r, manifest, _p, _s: manifest["artifacts"][0].__setitem__("package_name", "other.package"))


def test_wrong_version_refused() -> None:
    expect_refused(lambda _r, manifest, _p, _s: manifest["artifacts"][0].__setitem__("version_name", "0.1.5"))


def test_legacy_manifest_v1_refused() -> None:
    expect_refused(lambda _r, manifest, _p, _s: manifest.__setitem__("format_version", 1))


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in sorted(tests, key=lambda fn: fn.__name__):
        test()
    print(f"RELEASE_RESOLVER_TESTS_OK count={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
