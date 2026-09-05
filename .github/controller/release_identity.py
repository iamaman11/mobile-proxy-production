from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Mapping

_SHA = re.compile(r"[0-9a-f]{40}")
_TYPED_DIGEST = re.compile(r"b3:[0-9a-f]{64}")
_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
_PHONE_RUNTIME_PATH = "phone-production-runtime/components.json"


class ReleaseIdentityError(ValueError):
    pass


@dataclass(frozen=True)
class ProductRelease:
    tag: str
    release_id: int
    source_sha: str
    artifact_name: str
    artifact_digest: str
    manifest_digest: str
    provenance_digest: str
    immutable: bool
    phone_runtime_artifact_name: str | None = None
    phone_runtime_artifact_digest: str | None = None
    phone_runtime_inventory_path: str | None = None
    phone_runtime_inventory_digest: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def validate_product_release(value: Mapping[str, object], *, target: str) -> ProductRelease:
    tag = str(value.get("tag", ""))
    source_sha = str(value.get("source_sha", ""))
    artifact_name = str(value.get("artifact_name", ""))
    artifact_digest = str(value.get("artifact_digest", ""))
    manifest_digest = str(value.get("manifest_digest", ""))
    provenance_digest = str(value.get("provenance_digest", ""))
    phone_runtime_artifact_name = _optional_text(value.get("phone_runtime_artifact_name"))
    phone_runtime_artifact_digest = _optional_text(value.get("phone_runtime_artifact_digest"))
    phone_runtime_inventory_path = _optional_text(value.get("phone_runtime_inventory_path"))
    phone_runtime_inventory_digest = _optional_text(value.get("phone_runtime_inventory_digest"))
    try:
        release_id = int(value.get("release_id", 0))
    except (TypeError, ValueError) as exc:
        raise ReleaseIdentityError("release id is invalid") from exc
    if _TAG.fullmatch(tag) is None or release_id <= 0 or _SHA.fullmatch(source_sha) is None:
        raise ReleaseIdentityError("release tag/id/source SHA is invalid")
    if any(
        _TYPED_DIGEST.fullmatch(item) is None
        for item in (artifact_digest, manifest_digest, provenance_digest)
    ):
        raise ReleaseIdentityError("release typed content digest is invalid")
    if value.get("immutable") is not True:
        raise ReleaseIdentityError("deployment requires a GitHub immutable Release")
    expected_suffix = ".apk" if target == "phone-production" else ".tar.gz"
    if not artifact_name.startswith("mobile-proxy-") or not artifact_name.endswith(expected_suffix):
        raise ReleaseIdentityError("release artifact type does not match target")
    if tag not in artifact_name:
        raise ReleaseIdentityError("release artifact name does not bind exact tag")

    if target == "phone-production":
        expected_runtime_name = f"mobile-proxy-phone-production-runtime-{tag}.tar.gz"
        if phone_runtime_artifact_name != expected_runtime_name:
            raise ReleaseIdentityError("phone runtime artifact name does not bind exact tag")
        if (
            phone_runtime_artifact_digest is None
            or _TYPED_DIGEST.fullmatch(phone_runtime_artifact_digest) is None
        ):
            raise ReleaseIdentityError("phone runtime artifact digest is invalid")
        if phone_runtime_inventory_path != _PHONE_RUNTIME_PATH:
            raise ReleaseIdentityError("phone runtime component inventory path differs")
        if (
            phone_runtime_inventory_digest is None
            or _TYPED_DIGEST.fullmatch(phone_runtime_inventory_digest) is None
        ):
            raise ReleaseIdentityError("phone runtime component inventory digest is invalid")
    elif any(
        item is not None
        for item in (
            phone_runtime_artifact_name,
            phone_runtime_artifact_digest,
            phone_runtime_inventory_path,
            phone_runtime_inventory_digest,
        )
    ):
        raise ReleaseIdentityError("phone runtime identity leaked into a non-phone target")

    return ProductRelease(
        tag=tag,
        release_id=release_id,
        source_sha=source_sha,
        artifact_name=artifact_name,
        artifact_digest=artifact_digest,
        manifest_digest=manifest_digest,
        provenance_digest=provenance_digest,
        immutable=True,
        phone_runtime_artifact_name=phone_runtime_artifact_name,
        phone_runtime_artifact_digest=phone_runtime_artifact_digest,
        phone_runtime_inventory_path=phone_runtime_inventory_path,
        phone_runtime_inventory_digest=phone_runtime_inventory_digest,
    )
