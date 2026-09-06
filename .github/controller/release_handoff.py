from __future__ import annotations

import re
from typing import Mapping

from release_identity import ReleaseIdentityError, validate_product_release
from release_resolver import AdmittedRelease, PUBLIC_REPOSITORY, ReleaseAdmissionError

_RAW_SHA256 = re.compile(r"[0-9a-f]{64}")
_PHONE_TARGET = "phone-production"
_IMMUTABILITY_CONTROL = "github-immutable-release"

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


def _strict_positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReleaseAdmissionError(f"{label} is invalid")
    return value


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseAdmissionError(f"{label} is invalid")
    return value


def _strict_sha256(value: object, label: str) -> str:
    text = _strict_text(value, label)
    if _RAW_SHA256.fullmatch(text) is None:
        raise ReleaseAdmissionError(f"{label} is invalid")
    return text


def _download_url(*, tag: str, name: str) -> str:
    return f"https://github.com/{PUBLIC_REPOSITORY}/releases/download/{tag}/{name}"


def parse_admitted_release(value: object, *, tag: str, target: str) -> AdmittedRelease:
    """Validate the exact hosted Release-admission envelope without network access."""
    if target != _PHONE_TARGET:
        raise ReleaseAdmissionError("admitted Release handoff target is unsupported")
    if not isinstance(value, Mapping) or set(value) != _ADMITTED_FIELDS:
        raise ReleaseAdmissionError("admitted Release handoff field set differs")

    raw_identity = value.get("identity")
    if not isinstance(raw_identity, Mapping) or set(raw_identity) != _IDENTITY_FIELDS:
        raise ReleaseAdmissionError("admitted Release identity field set differs")
    for field in (
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
    ):
        _strict_text(raw_identity.get(field), f"admitted Release identity {field}")
    _strict_positive(raw_identity.get("release_id"), "admitted Release identity release id")
    if raw_identity.get("immutable") is not True:
        raise ReleaseAdmissionError("admitted Release identity is not immutable")

    try:
        identity = validate_product_release(raw_identity, target=target)
    except ReleaseIdentityError as exc:
        raise ReleaseAdmissionError(str(exc)) from exc
    if identity.tag != tag:
        raise ReleaseAdmissionError("admitted Release tag differs from deployment request")

    apk_name = f"mobile-proxy-android-{tag}.apk"
    runtime_name = f"mobile-proxy-phone-production-runtime-{tag}.tar.gz"
    if identity.artifact_name != apk_name or identity.phone_runtime_artifact_name != runtime_name:
        raise ReleaseAdmissionError("admitted Release target artifact identity differs")

    artifact_asset_id = _strict_positive(value.get("artifact_asset_id"), "artifact asset id")
    manifest_asset_id = _strict_positive(value.get("manifest_asset_id"), "manifest asset id")
    provenance_asset_id = _strict_positive(value.get("provenance_asset_id"), "provenance asset id")
    digests_asset_id = _strict_positive(value.get("digests_asset_id"), "artifact-digests asset id")
    phone_runtime_asset_id = _strict_positive(value.get("phone_runtime_asset_id"), "phone runtime asset id")
    if len({artifact_asset_id, manifest_asset_id, provenance_asset_id, digests_asset_id, phone_runtime_asset_id}) != 5:
        raise ReleaseAdmissionError("admitted Release asset ids are not distinct")

    artifact_download_url = _strict_text(value.get("artifact_download_url"), "artifact download URL")
    phone_runtime_download_url = _strict_text(value.get("phone_runtime_download_url"), "phone runtime download URL")
    if artifact_download_url != _download_url(tag=tag, name=apk_name):
        raise ReleaseAdmissionError("admitted Android Release asset URL differs")
    if phone_runtime_download_url != _download_url(tag=tag, name=runtime_name):
        raise ReleaseAdmissionError("admitted rooted-phone runtime Release asset URL differs")

    artifact_transport_sha256 = _strict_sha256(
        value.get("artifact_transport_sha256"), "artifact transport sha256"
    )
    phone_runtime_transport_sha256 = _strict_sha256(
        value.get("phone_runtime_transport_sha256"), "phone runtime transport sha256"
    )
    if value.get("immutability_control") != _IMMUTABILITY_CONTROL:
        raise ReleaseAdmissionError("admitted Release immutability control differs")

    android_package = _strict_text(value.get("android_package"), "Android package")
    android_version_name = _strict_text(value.get("android_version_name"), "Android version name")
    android_version_code = _strict_positive(value.get("android_version_code"), "Android version code")
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
        immutability_control=_IMMUTABILITY_CONTROL,
        android_package=android_package,
        android_version_name=android_version_name,
        android_version_code=android_version_code,
        phone_runtime_asset_id=phone_runtime_asset_id,
        phone_runtime_download_url=phone_runtime_download_url,
        phone_runtime_transport_sha256=phone_runtime_transport_sha256,
    )
    if admitted.to_dict() != dict(value):
        raise ReleaseAdmissionError("admitted Release handoff does not round-trip exactly")
    return admitted
