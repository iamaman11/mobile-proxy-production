from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Mapping

_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")


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

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def validate_product_release(value: Mapping[str, object], *, target: str) -> ProductRelease:
    tag = str(value.get("tag", ""))
    source_sha = str(value.get("source_sha", ""))
    artifact_name = str(value.get("artifact_name", ""))
    artifact_digest = str(value.get("artifact_digest", "")).removeprefix("sha256:")
    manifest_digest = str(value.get("manifest_digest", "")).removeprefix("sha256:")
    provenance_digest = str(value.get("provenance_digest", "")).removeprefix("sha256:")
    try:
        release_id = int(value.get("release_id", 0))
    except (TypeError, ValueError) as exc:
        raise ReleaseIdentityError("release id is invalid") from exc
    if _TAG.fullmatch(tag) is None or release_id <= 0 or _SHA.fullmatch(source_sha) is None:
        raise ReleaseIdentityError("release tag/id/source SHA is invalid")
    if any(_SHA256.fullmatch(item) is None for item in (artifact_digest, manifest_digest, provenance_digest)):
        raise ReleaseIdentityError("release digest is invalid")
    if value.get("immutable") is not True:
        raise ReleaseIdentityError("deployment requires a GitHub immutable Release")
    expected_suffix = ".apk" if target == "phone-production" else ".tar.gz"
    if not artifact_name.startswith("mobile-proxy-") or not artifact_name.endswith(expected_suffix):
        raise ReleaseIdentityError("release artifact type does not match target")
    if tag not in artifact_name:
        raise ReleaseIdentityError("release artifact name does not bind exact tag")
    return ProductRelease(
        tag=tag,
        release_id=release_id,
        source_sha=source_sha,
        artifact_name=artifact_name,
        artifact_digest=artifact_digest,
        manifest_digest=manifest_digest,
        provenance_digest=provenance_digest,
        immutable=True,
    )
