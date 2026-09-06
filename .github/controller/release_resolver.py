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
_RAW_SHA256 = re.compile(r"[0-9a-f]{64}")
_TYPED_DIGEST = re.compile(r"b3:[0-9a-f]{64}")
_DIGEST_ALGORITHM = "blake3-256"
_DIGEST_DOMAIN = "mobile-proxy/product-release-asset/v2"
_PHONE_TARGET = "phone-production"
_PHONE_RUNTIME_KIND = "phone-production-runtime-tar"
_PHONE_RUNTIME_INVENTORY_PATH = "phone-production-runtime/components.json"
_PHONE_RUNTIME_ABI = {
    "os": "android",
    "arch": "arm",
    "rust_target": "armv7-linux-androideabi",
    "elf_machine": 40,
}
_SING_BOX_ARCHIVE_DOMAIN = "mobile-proxy/upstream-sing-box-archive/v1"
_READ_TRANSPORT_ATTEMPTS = 3


class ReleaseAdmissionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdmittedRelease:
    identity: ProductRelease
    artifact_asset_id: int
    artifact_download_url: str
    artifact_transport_sha256: str
    manifest_asset_id: int
    provenance_asset_id: int
    digests_asset_id: int
    immutability_control: str
    android_package: str | None = None
    android_version_name: str | None = None
    android_version_code: int | None = None
    phone_runtime_asset_id: int | None = None
    phone_runtime_download_url: str | None = None
    phone_runtime_transport_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["identity"] = self.identity.to_dict()
        return result


def _transient_http_error(exc: urllib.error.HTTPError) -> bool:
    return exc.code == 429 or 500 <= exc.code < 600


def _request_json(url: str) -> Mapping[str, Any]:
    for attempt in range(_READ_TRANSPORT_ATTEMPTS):
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
        except urllib.error.HTTPError as exc:
            if _transient_http_error(exc) and attempt + 1 < _READ_TRANSPORT_ATTEMPTS:
                continue
            raise ReleaseAdmissionError("public Release metadata is unavailable") from exc
        except (OSError, urllib.error.URLError) as exc:
            if attempt + 1 < _READ_TRANSPORT_ATTEMPTS:
                continue
            raise ReleaseAdmissionError("public Release metadata is unavailable") from exc
        except json.JSONDecodeError as exc:
            raise ReleaseAdmissionError("public Release metadata is unavailable") from exc
        if not isinstance(value, Mapping):
            raise ReleaseAdmissionError("public Release metadata is invalid")
        return value
    raise AssertionError("bounded public Release metadata retry loop exhausted unexpectedly")


def _asset_transport_sha256(asset: Mapping[str, Any]) -> str:
    match = _SHA256.fullmatch(str(asset.get("digest", "")))
    if match is None:
        raise ReleaseAdmissionError("GitHub Release asset lacks sha256 transport digest")
    if asset.get("state") != "uploaded":
        raise ReleaseAdmissionError("GitHub Release asset is not fully uploaded")
    return match.group(1)


def _download(asset: Mapping[str, Any]) -> bytes:
    url = str(asset.get("browser_download_url", ""))
    if not url.startswith(f"https://github.com/{PUBLIC_REPOSITORY}/releases/download/"):
        raise ReleaseAdmissionError("release asset download URL differs")
    for attempt in range(_READ_TRANSPORT_ATTEMPTS):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "mobile-proxy-production-release-resolver-v2"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            if _transient_http_error(exc) and attempt + 1 < _READ_TRANSPORT_ATTEMPTS:
                continue
            raise ReleaseAdmissionError("release contract asset is unavailable") from exc
        except (OSError, urllib.error.URLError) as exc:
            if attempt + 1 < _READ_TRANSPORT_ATTEMPTS:
                continue
            raise ReleaseAdmissionError("release contract asset is unavailable") from exc
        if len(data) > 2_000_000:
            raise ReleaseAdmissionError("release contract asset exceeds bounded size")
        expected = _asset_transport_sha256(asset)
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise ReleaseAdmissionError("release contract asset transport digest differs")
        return data
    raise AssertionError("bounded Release contract asset retry loop exhausted unexpectedly")


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


def _typed_digest_map(value: Mapping[str, Any], *, expected_names: set[str]) -> dict[str, str]:
    if (
        value.get("format_version") != 1
        or value.get("algorithm") != _DIGEST_ALGORITHM
        or value.get("digest_domain") != _DIGEST_DOMAIN
    ):
        raise ReleaseAdmissionError("artifact-digests contract metadata differs")
    raw = value.get("assets")
    if not isinstance(raw, list):
        raise ReleaseAdmissionError("artifact-digests asset set is invalid")
    result: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ReleaseAdmissionError("artifact-digests entry is invalid")
        name = str(item.get("name", ""))
        digest = str(item.get("digest", ""))
        if not name or "/" in name or name in result or _TYPED_DIGEST.fullmatch(digest) is None:
            raise ReleaseAdmissionError("artifact-digests contains an unsafe, duplicate, or invalid record")
        result[name] = digest
    if set(result) != expected_names:
        raise ReleaseAdmissionError("artifact-digests does not bind the exact Product Release v2 asset set")
    return result


def _artifact_records(
    records: object,
    *,
    label: str,
    expected_names: set[str],
    digests: Mapping[str, str],
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(records, list):
        raise ReleaseAdmissionError(f"{label} artifact set is invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for item in records:
        if not isinstance(item, Mapping):
            raise ReleaseAdmissionError(f"{label} artifact record is invalid")
        name = str(item.get("name", ""))
        if not name or name in result:
            raise ReleaseAdmissionError(f"{label} artifact names are empty or duplicate")
        if (
            item.get("content_digest_algorithm") != _DIGEST_ALGORITHM
            or item.get("content_digest_domain") != _DIGEST_DOMAIN
        ):
            raise ReleaseAdmissionError(f"{label} artifact digest metadata differs")
        digest = str(item.get("content_digest", ""))
        if _TYPED_DIGEST.fullmatch(digest) is None or digests.get(name) != digest:
            raise ReleaseAdmissionError(f"{label} Product content digest differs")
        result[name] = item
    if set(result) != expected_names:
        raise ReleaseAdmissionError(f"{label} does not identify the exact Product artifact set")
    return result


def _safe_component_records(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ReleaseAdmissionError("phone runtime component set is empty or invalid")
    names: set[str] = set()
    paths: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ReleaseAdmissionError("phone runtime component record is invalid")
        name = str(item.get("name", ""))
        path = str(item.get("archive_path", ""))
        kind = str(item.get("kind", ""))
        digest = str(item.get("content_digest", ""))
        executable = item.get("executable")
        if (
            not name
            or not path
            or not kind
            or name in names
            or path in paths
            or path.startswith("/")
            or ".." in path.split("/")
            or item.get("content_digest_algorithm") != _DIGEST_ALGORITHM
            or item.get("content_digest_domain") != _DIGEST_DOMAIN
            or _TYPED_DIGEST.fullmatch(digest) is None
            or not isinstance(executable, bool)
        ):
            raise ReleaseAdmissionError("phone runtime component identity is invalid or ambiguous")
        names.add(name)
        paths.add(path)
    return tuple(sorted(names))


def _validate_sing_box_identity(value: object) -> None:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], Mapping):
        raise ReleaseAdmissionError("phone runtime third-party identity set differs")
    item = value[0]
    if (
        item.get("name") != "sing-box"
        or not isinstance(item.get("version"), str)
        or not str(item.get("version"))
        or item.get("lock_target") != "android-arm"
        or not isinstance(item.get("archive_size"), int)
        or isinstance(item.get("archive_size"), bool)
        or int(item.get("archive_size", 0)) <= 0
        or _RAW_SHA256.fullmatch(str(item.get("archive_upstream_sha256", ""))) is None
        or _TYPED_DIGEST.fullmatch(str(item.get("archive_content_digest", ""))) is None
        or item.get("archive_content_digest_algorithm") != _DIGEST_ALGORITHM
        or item.get("archive_content_digest_domain") != _SING_BOX_ARCHIVE_DOMAIN
    ):
        raise ReleaseAdmissionError("phone runtime sing-box identity differs")


def _phone_runtime_identity(record: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    if (
        record.get("kind") != _PHONE_RUNTIME_KIND
        or record.get("target") != _PHONE_TARGET
        or record.get("runtime_abi") != _PHONE_RUNTIME_ABI
    ):
        raise ReleaseAdmissionError("phone runtime target/kind/ABI identity differs")
    inventory = record.get("component_inventory")
    if not isinstance(inventory, Mapping):
        raise ReleaseAdmissionError("phone runtime component inventory identity is missing")
    inventory_path = str(inventory.get("path", ""))
    inventory_digest = str(inventory.get("content_digest", ""))
    if (
        inventory_path != _PHONE_RUNTIME_INVENTORY_PATH
        or inventory.get("content_digest_algorithm") != _DIGEST_ALGORITHM
        or inventory.get("content_digest_domain") != _DIGEST_DOMAIN
        or _TYPED_DIGEST.fullmatch(inventory_digest) is None
    ):
        raise ReleaseAdmissionError("phone runtime component inventory identity differs")
    components = _safe_component_records(record.get("components"))
    _validate_sing_box_identity(record.get("third_party_runtime"))
    return inventory_path, inventory_digest, components


def resolve_payload(
    *,
    tag: str,
    target: str,
    release: Mapping[str, Any],
    source_sha: str,
    manifest: Mapping[str, Any],
    provenance: Mapping[str, Any],
    digest_set: Mapping[str, Any],
) -> AdmittedRelease:
    if _TAG.fullmatch(tag) is None:
        raise ReleaseAdmissionError("release tag is invalid")
    if (
        release.get("tag_name") != tag
        or release.get("draft") is True
        or release.get("prerelease") is True
        or release.get("immutable") is not True
    ):
        raise ReleaseAdmissionError("release tag/state is not immutable and deployable")
    if _SHA.fullmatch(source_sha) is None:
        raise ReleaseAdmissionError("release source SHA is invalid")
    if manifest.get("format_version") != 2 or provenance.get("format_version") != 2:
        raise ReleaseAdmissionError("deployment requires release manifest/provenance format v2")
    for contract, label in ((manifest, "release manifest"), (provenance, "release provenance")):
        if contract.get("release_tag") != tag or contract.get("git_sha") != source_sha:
            raise ReleaseAdmissionError(f"{label} tag/source identity differs")

    assets = _assets(release)
    linux_name = f"mobile-proxy-linux-x86_64-{tag}.tar.gz"
    apk_name = f"mobile-proxy-android-{tag}.apk"
    phone_name = f"mobile-proxy-phone-production-runtime-{tag}.tar.gz"
    expected_release_assets = {
        linux_name,
        apk_name,
        phone_name,
        "release-manifest.json",
        "provenance.json",
        "artifact-digests.json",
    }
    if set(assets) != expected_release_assets:
        raise ReleaseAdmissionError("Release does not contain the exact six Product Release v2 assets")
    for asset in assets.values():
        _asset_transport_sha256(asset)

    digest_covered_names = {
        linux_name,
        apk_name,
        phone_name,
        "release-manifest.json",
        "provenance.json",
    }
    digests = _typed_digest_map(digest_set, expected_names=digest_covered_names)
    product_names = {linux_name, apk_name, phone_name}
    manifest_records = _artifact_records(
        manifest.get("artifacts"),
        label="release manifest",
        expected_names=product_names,
        digests=digests,
    )
    _artifact_records(
        provenance.get("artifacts"),
        label="release provenance",
        expected_names=product_names,
        digests=digests,
    )

    phone_record = manifest_records[phone_name]
    inventory_path, inventory_digest, _component_names = _phone_runtime_identity(phone_record)

    artifact_name = apk_name if target == _PHONE_TARGET else linux_name
    artifact_digest = digests[artifact_name]
    identity_raw: dict[str, object] = {
        "tag": tag,
        "release_id": _positive(release.get("id"), "release id"),
        "source_sha": source_sha,
        "artifact_name": artifact_name,
        "artifact_digest": artifact_digest,
        "manifest_digest": digests["release-manifest.json"],
        "provenance_digest": digests["provenance.json"],
        "immutable": True,
    }
    if target == _PHONE_TARGET:
        identity_raw.update(
            {
                "phone_runtime_artifact_name": phone_name,
                "phone_runtime_artifact_digest": digests[phone_name],
                "phone_runtime_inventory_path": inventory_path,
                "phone_runtime_inventory_digest": inventory_digest,
            }
        )
    try:
        identity = validate_product_release(identity_raw, target=target)
    except ReleaseIdentityError as exc:
        raise ReleaseAdmissionError(str(exc)) from exc

    android_package = None
    android_version_name = None
    android_version_code = None
    phone_runtime_asset_id = None
    phone_runtime_download_url = None
    phone_runtime_transport_sha256 = None
    if target == _PHONE_TARGET:
        manifest_artifact = manifest_records[apk_name]
        if manifest_artifact.get("kind") != "android-apk":
            raise ReleaseAdmissionError("Android release artifact kind differs")
        android_package = str(manifest_artifact.get("package_name", ""))
        android_version_name = str(manifest_artifact.get("version_name", ""))
        android_version_code = _positive(manifest_artifact.get("version_code"), "Android version code")
        if android_package != "com.example.mobileproxy" or android_version_name != tag.removeprefix("v"):
            raise ReleaseAdmissionError("Android package/version does not match deployment Release")
        phone_runtime_asset_id = _positive(assets[phone_name].get("id"), "phone runtime asset id")
        phone_runtime_download_url = str(assets[phone_name].get("browser_download_url", ""))
        phone_runtime_transport_sha256 = _asset_transport_sha256(assets[phone_name])

    return AdmittedRelease(
        identity=identity,
        artifact_asset_id=_positive(assets[artifact_name].get("id"), "artifact asset id"),
        artifact_download_url=str(assets[artifact_name].get("browser_download_url", "")),
        artifact_transport_sha256=_asset_transport_sha256(assets[artifact_name]),
        manifest_asset_id=_positive(assets["release-manifest.json"].get("id"), "manifest asset id"),
        provenance_asset_id=_positive(assets["provenance.json"].get("id"), "provenance asset id"),
        digests_asset_id=_positive(assets["artifact-digests.json"].get("id"), "artifact-digests asset id"),
        immutability_control="github-immutable-release",
        android_package=android_package,
        android_version_name=android_version_name,
        android_version_code=android_version_code,
        phone_runtime_asset_id=phone_runtime_asset_id,
        phone_runtime_download_url=phone_runtime_download_url,
        phone_runtime_transport_sha256=phone_runtime_transport_sha256,
    )


def resolve_release(*, tag: str, target: str) -> AdmittedRelease:
    if _TAG.fullmatch(tag) is None:
        raise ReleaseAdmissionError("release tag is invalid")
    encoded = urllib.parse.quote(tag, safe="")
    release = _request_json(f"{API}/releases/tags/{encoded}")
    source_sha = _annotated_tag_source(tag)
    assets = _assets(release)
    for name in ("release-manifest.json", "provenance.json", "artifact-digests.json"):
        if name not in assets:
            raise ReleaseAdmissionError(f"release contract asset is missing: {name}")
    manifest = _json_asset(assets["release-manifest.json"], "release manifest")
    provenance = _json_asset(assets["provenance.json"], "release provenance")
    digest_set = _json_asset(assets["artifact-digests.json"], "artifact-digests")
    return resolve_payload(
        tag=tag,
        target=target,
        release=release,
        source_sha=source_sha,
        manifest=manifest,
        provenance=provenance,
        digest_set=digest_set,
    )
