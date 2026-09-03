from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from release_identity import ProductRelease, ReleaseIdentityError, validate_product_release

PUBLIC_REPOSITORY = "iamaman11/mobile-proxy"
API = f"https://api.github.com/repos/{PUBLIC_REPOSITORY}"
_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"sha256:([0-9a-f]{64})")


class ReleaseAdmissionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdmittedRelease:
    identity: ProductRelease
    artifact_asset_id: int
    artifact_download_url: str
    manifest_asset_id: int
    provenance_asset_id: int
    checksums_asset_id: int
    immutability_control: str
    android_package: str | None = None
    android_version_name: str | None = None
    android_version_code: int | None = None

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["identity"] = self.identity.to_dict()
        return result


def _request_json(url: str) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "mobile-proxy-production-release-resolver-v2",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ReleaseAdmissionError("public Release metadata is unavailable") from exc
    if not isinstance(value, Mapping):
        raise ReleaseAdmissionError("public Release metadata is invalid")
    return value


def _download(asset: Mapping[str, Any]) -> bytes:
    url = str(asset.get("browser_download_url", ""))
    if not url.startswith(f"https://github.com/{PUBLIC_REPOSITORY}/releases/download/"):
        raise ReleaseAdmissionError("release asset download URL differs")
    request = urllib.request.Request(url, headers={"User-Agent": "mobile-proxy-production-release-resolver-v2"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(2_000_001)
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseAdmissionError("release contract asset is unavailable") from exc
    if len(data) > 2_000_000:
        raise ReleaseAdmissionError("release contract asset exceeds bounded size")
    expected = _asset_digest(asset)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ReleaseAdmissionError("release contract asset digest differs from GitHub asset metadata")
    return data


def _asset_digest(asset: Mapping[str, Any]) -> str:
    match = _SHA256.fullmatch(str(asset.get("digest", "")))
    if match is None:
        raise ReleaseAdmissionError("GitHub Release asset lacks sha256 digest")
    if asset.get("state") != "uploaded":
        raise ReleaseAdmissionError("GitHub Release asset is not fully uploaded")
    return match.group(1)


def _positive(value: object, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ReleaseAdmissionError(f"{label} is invalid") from exc
    if parsed <= 0:
        raise ReleaseAdmissionError(f"{label} is invalid")
    return parsed


def _assets(release: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = release.get("assets")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ReleaseAdmissionError("release assets are invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ReleaseAdmissionError("release asset entry is invalid")
        name = str(item.get("name", ""))
        if not name or name in result:
            raise ReleaseAdmissionError("release asset names are empty or duplicate")
        result[name] = item
    return result


def _annotated_tag_source(tag: str) -> str:
    encoded = urllib.parse.quote(tag, safe="")
    ref = _request_json(f"{API}/git/ref/tags/{encoded}")
    obj = ref.get("object")
    if not isinstance(obj, Mapping) or obj.get("type") != "tag":
        raise ReleaseAdmissionError("deployment Release must use an annotated Git tag")
    tag_object_sha = str(obj.get("sha", ""))
    if _SHA.fullmatch(tag_object_sha) is None:
        raise ReleaseAdmissionError("annotated tag object SHA is invalid")
    tag_object = _request_json(f"{API}/git/tags/{tag_object_sha}")
    if tag_object.get("tag") != tag:
        raise ReleaseAdmissionError("annotated tag object name differs")
    target = tag_object.get("object")
    if not isinstance(target, Mapping) or target.get("type") != "commit":
        raise ReleaseAdmissionError("annotated release tag does not resolve directly to a commit")
    source_sha = str(target.get("sha", ""))
    if _SHA.fullmatch(source_sha) is None:
        raise ReleaseAdmissionError("annotated release source SHA is invalid")
    return source_sha


def _json_asset(asset: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(_download(asset).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseAdmissionError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise ReleaseAdmissionError(f"{label} is not a JSON object")
    return value


def _checksum_map(asset: Mapping[str, Any]) -> dict[str, str]:
    try:
        text = _download(asset).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseAdmissionError("SHA256SUMS is not UTF-8") from exc
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2 or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None:
            raise ReleaseAdmissionError("SHA256SUMS contains an invalid record")
        name = parts[1].removeprefix("*")
        if not name or "/" in name or name in result:
            raise ReleaseAdmissionError("SHA256SUMS contains an unsafe or duplicate artifact")
        result[name] = parts[0]
    if not result:
        raise ReleaseAdmissionError("SHA256SUMS is empty")
    return result


def _matching_artifact(records: object, name: str, digest: str, *, label: str) -> Mapping[str, Any]:
    if not isinstance(records, list):
        raise ReleaseAdmissionError(f"{label} artifact set is invalid")
    matches = [item for item in records if isinstance(item, Mapping) and item.get("name") == name]
    if len(matches) != 1:
        raise ReleaseAdmissionError(f"{label} does not identify exact deployment artifact")
    item = matches[0]
    if item.get("sha256") != digest:
        raise ReleaseAdmissionError(f"{label} deployment artifact digest differs")
    return item


def resolve_payload(
    *,
    tag: str,
    target: str,
    release: Mapping[str, Any],
    source_sha: str,
    manifest: Mapping[str, Any],
    provenance: Mapping[str, Any],
    checksums: Mapping[str, str],
) -> AdmittedRelease:
    if _TAG.fullmatch(tag) is None:
        raise ReleaseAdmissionError("release tag is invalid")
    if release.get("tag_name") != tag or release.get("draft") is True or release.get("prerelease") is True:
        raise ReleaseAdmissionError("release tag/state is not deployable")
    if _SHA.fullmatch(source_sha) is None:
        raise ReleaseAdmissionError("release source SHA is invalid")
    if manifest.get("format_version") != 2 or provenance.get("format_version") != 2:
        raise ReleaseAdmissionError("deployment requires release manifest/provenance format v2")
    for contract, label in ((manifest, "release manifest"), (provenance, "release provenance")):
        if contract.get("release_tag") != tag or contract.get("git_sha") != source_sha:
            raise ReleaseAdmissionError(f"{label} tag/source identity differs")

    assets = _assets(release)
    artifact_name = (
        f"mobile-proxy-android-{tag}.apk"
        if target == "phone-production"
        else f"mobile-proxy-linux-x86_64-{tag}.tar.gz"
    )
    required = (artifact_name, "release-manifest.json", "provenance.json", "SHA256SUMS")
    missing = [name for name in required if name not in assets]
    if missing:
        raise ReleaseAdmissionError("release is missing required deployment assets: " + ", ".join(missing))

    artifact_digest = _asset_digest(assets[artifact_name])
    if checksums.get(artifact_name) != artifact_digest:
        raise ReleaseAdmissionError("SHA256SUMS does not bind exact deployment artifact")
    manifest_artifact = _matching_artifact(
        manifest.get("artifacts"), artifact_name, artifact_digest, label="release manifest"
    )
    _matching_artifact(
        provenance.get("artifacts"), artifact_name, artifact_digest, label="release provenance"
    )

    manifest_digest = _asset_digest(assets["release-manifest.json"])
    provenance_digest = _asset_digest(assets["provenance.json"])
    if checksums.get("release-manifest.json") != manifest_digest:
        raise ReleaseAdmissionError("SHA256SUMS does not bind release manifest")
    if checksums.get("provenance.json") != provenance_digest:
        raise ReleaseAdmissionError("SHA256SUMS does not bind release provenance")

    identity_raw = {
        "tag": tag,
        "release_id": _positive(release.get("id"), "release id"),
        "source_sha": source_sha,
        "artifact_name": artifact_name,
        "artifact_digest": artifact_digest,
        "manifest_digest": manifest_digest,
        "provenance_digest": provenance_digest,
        "immutable": release.get("immutable") is True,
    }
    try:
        identity = validate_product_release(identity_raw, target=target)
    except ReleaseIdentityError as exc:
        raise ReleaseAdmissionError(str(exc)) from exc

    android_package = None
    android_version_name = None
    android_version_code = None
    if target == "phone-production":
        if manifest_artifact.get("kind") != "android-apk":
            raise ReleaseAdmissionError("Android release artifact kind differs")
        android_package = str(manifest_artifact.get("package_name", ""))
        android_version_name = str(manifest_artifact.get("version_name", ""))
        android_version_code = _positive(manifest_artifact.get("version_code"), "Android version code")
        if android_package != "com.example.mobileproxy" or android_version_name != tag.removeprefix("v"):
            raise ReleaseAdmissionError("Android package/version does not match deployment Release")

    immutable = release.get("immutable") is True
    control = "github-immutable-release" if immutable else "annotated-tag+github-asset-digest+manifest+provenance"
    return AdmittedRelease(
        identity=identity,
        artifact_asset_id=_positive(assets[artifact_name].get("id"), "artifact asset id"),
        artifact_download_url=str(assets[artifact_name].get("browser_download_url", "")),
        manifest_asset_id=_positive(assets["release-manifest.json"].get("id"), "manifest asset id"),
        provenance_asset_id=_positive(assets["provenance.json"].get("id"), "provenance asset id"),
        checksums_asset_id=_positive(assets["SHA256SUMS"].get("id"), "checksums asset id"),
        immutability_control=control,
        android_package=android_package,
        android_version_name=android_version_name,
        android_version_code=android_version_code,
    )


def resolve_release(*, tag: str, target: str) -> AdmittedRelease:
    if _TAG.fullmatch(tag) is None:
        raise ReleaseAdmissionError("release tag is invalid")
    encoded = urllib.parse.quote(tag, safe="")
    release = _request_json(f"{API}/releases/tags/{encoded}")
    source_sha = _annotated_tag_source(tag)
    assets = _assets(release)
    for name in ("release-manifest.json", "provenance.json", "SHA256SUMS"):
        if name not in assets:
            raise ReleaseAdmissionError(f"release contract asset is missing: {name}")
    manifest = _json_asset(assets["release-manifest.json"], "release manifest")
    provenance = _json_asset(assets["provenance.json"], "release provenance")
    checksums = _checksum_map(assets["SHA256SUMS"])
    return resolve_payload(
        tag=tag,
        target=target,
        release=release,
        source_sha=source_sha,
        manifest=manifest,
        provenance=provenance,
        checksums=checksums,
    )
