from __future__ import annotations

import re
from typing import Mapping

from release_identity import ReleaseIdentityError, validate_product_release
from release_resolver import AdmittedRelease, PUBLIC_REPOSITORY, ReleaseAdmissionError

_PHONE_TARGET = "phone-production"
_RAW_SHA256 = re.compile(r"[0-9a-f]{64}")

_ADMITTED_FIELDS = frozenset(
    {
        "identity",
        "artifact_asset_id",
        "artifact_download_url",
        "artifact_transport_sha256",
        "manifest_asset_id",
        "provenance_asset_id",
        "digests_asset_id",
        "immutability_control",
        "android_package",
        "android_version_name",
        "android_version_code",
        "phone_runtime_asset_id",
        "phone_runtime_download_url",
        "phone_runtime_transport_sha256",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "tag",
        "release_id",
        "source_sha",
        "artifact_name",
        "artifact_digest",
        "manifest_digest",
        "provenance_digest",
        "immutable",
        "phone_runtime_artifact_name",
        "phone_runtime_artifact_digest",
        "phone_runtime_inventory_path",
        "phone_runtime_inventory_digest",
    }
)
_IDENTITY_TEXT_FIELDS = (
    "tag",
    "source_sha",
    "artifact_name",
    "artifact_digest",
    "manifest_digest",
    "provenance_digest",
    "phone_runtime_artifact_name",
    "phone_runtime_artifact_digest",
    "phone_runtime_inventory_path",
    "phone_runtime_inventory_digest",
)


def _exact_fields(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise ReleaseAdmissionError(f"{label} field set differs")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseAdmissionError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReleaseAdmissionError(f"{label} is invalid")
    return value


def _transport_sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if _RAW_SHA256.fullmatch(text) is None:
        raise ReleaseAdmissionError(f"{label} is invalid")
    return text


def _exact_release_url(value: object, *, tag: str, asset_name: str, label: str) -> str:
    url = _text(value, label)
    expected = f"https://github.com/{PUBLIC_REPOSITORY}/releases/download/{tag}/{asset_name}"
    if url != expected:
        raise ReleaseAdmissionError(f"{label} differs")
    return url


def parse_admitted_release(
    value: Mapping[str, object], *, tag: str, target: str,
) -> AdmittedRelease:
    """Reconstruct one hosted-admitted immutable Release without network access."""
    if target != _PHONE_TARGET:
        raise ReleaseAdmissionError("local admitted Release target is unsupported")
    if not isinstance(value, Mapping):
        raise ReleaseAdmissionError("admitted Release envelope is not an object")
    _exact_fields(value, _ADMITTED_FIELDS, "admitted Release envelope")

    identity_value = value.get("identity")
    if not isinstance(identity_value, Mapping):
        raise ReleaseAdmissionError("admitted Release identity is not an object")
    _exact_fields(identity_value, _IDENTITY_FIELDS, "admitted Release identity")
    for field in _IDENTITY_TEXT_FIELDS:
        _text(identity_value.get(field), f"admitted Release identity {field}")
    _positive_int(identity_value.get("release_id"), "admitted Release identity release_id")
    if identity_value.get("immutable") is not True:
        raise ReleaseAdmissionError("admitted Release identity is not immutable")

    try:
        identity = validate_product_release(identity_value, target=target)
    except ReleaseIdentityError as exc:
        raise ReleaseAdmissionError(str(exc)) from exc

    if identity.tag != tag:
        raise ReleaseAdmissionError("admitted Release tag differs from request")
    expected_apk = f"mobile-proxy-android-{tag}.apk"
    if identity.artifact_name != expected_apk:
        raise ReleaseAdmissionError("admitted Android artifact does not bind exact target/tag")
    expected_runtime = f"mobile-proxy-phone-production-runtime-{tag}.tar.gz"
    if identity.phone_runtime_artifact_name != expected_runtime:
        raise ReleaseAdmissionError("admitted rooted-phone runtime does not bind exact target/tag")

    artifact_asset_id = _positive_int(value.get("artifact_asset_id"), "artifact asset id")
    manifest_asset_id = _positive_int(value.get("manifest_asset_id"), "manifest asset id")
    provenance_asset_id = _positive_int(value.get("provenance_asset_id"), "provenance asset id")
    digests_asset_id = _positive_int(value.get("digests_asset_id"), "artifact-digests asset id")
    phone_runtime_asset_id = _positive_int(value.get("phone_runtime_asset_id"), "phone runtime asset id")
    if len({artifact_asset_id, manifest_asset_id, provenance_asset_id, digests_asset_id, phone_runtime_asset_id}) != 5:
        raise ReleaseAdmissionError("admitted Release asset ids are not unique")

    artifact_download_url = _exact_release_url(
        value.get("artifact_download_url"), tag=tag, asset_name=expected_apk,
        label="Android Release download URL",
    )
    phone_runtime_download_url = _exact_release_url(
        value.get("phone_runtime_download_url"), tag=tag, asset_name=expected_runtime,
        label="rooted-phone Release download URL",
    )
    artifact_transport_sha256 = _transport_sha256(
        value.get("artifact_transport_sha256"), "Android Release transport SHA-256",
    )
    phone_runtime_transport_sha256 = _transport_sha256(
        value.get("phone_runtime_transport_sha256"), "rooted-phone Release transport SHA-256",
    )

    if value.get("immutability_control") != "github-immutable-release":
        raise ReleaseAdmissionError("admitted Release immutability control differs")
    android_package = _text(value.get("android_package"), "Android package")
    android_version_name = _text(value.get("android_version_name"), "Android version name")
    android_version_code = _positive_int(value.get("android_version_code"), "Android version code")
    if android_package != "com.example.mobileproxy" or android_version_name != tag.removeprefix("v"):
        raise ReleaseAdmissionError("admitted Android package/version differs")

    admitted = AdmittedRelease(
        identity=identity,
        artifact_asset_id=artifact_asset_id,
        artifact_download_url=artifact_download_url,
        artifact_transport_sha256=artifact_transport_sha256,
        manifest_asset_id=manifest_asset_id,
        provenance_asset_id=provenance_asset_id,
        digests_asset_id=digests_asset_id,
        immutability_control="github-immutable-release",
        android_package=android_package,
        android_version_name=android_version_name,
        android_version_code=android_version_code,
        phone_runtime_asset_id=phone_runtime_asset_id,
        phone_runtime_download_url=phone_runtime_download_url,
        phone_runtime_transport_sha256=phone_runtime_transport_sha256,
    )
    if admitted.to_dict() != dict(value):
        raise ReleaseAdmissionError("admitted Release envelope normalization differs")
    return admitted
