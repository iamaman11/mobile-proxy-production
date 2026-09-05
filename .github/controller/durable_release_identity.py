from __future__ import annotations

import re
from typing import Mapping

from release_identity import ProductRelease

PHONE_RUNTIME_IDENTITY_FIELDS = (
    "phone_runtime_artifact_name",
    "phone_runtime_artifact_digest",
    "phone_runtime_inventory_path",
    "phone_runtime_inventory_digest",
)
_PHONE_TARGET = "phone-production"
_PHONE_RUNTIME_PATH = "phone-production-runtime/components.json"
_TYPED_DIGEST = re.compile(r"b3:[0-9a-f]{64}")


def _runtime_values(identity: ProductRelease) -> dict[str, str]:
    values = {
        "phone_runtime_artifact_name": identity.phone_runtime_artifact_name,
        "phone_runtime_artifact_digest": identity.phone_runtime_artifact_digest,
        "phone_runtime_inventory_path": identity.phone_runtime_inventory_path,
        "phone_runtime_inventory_digest": identity.phone_runtime_inventory_digest,
    }
    if any(value is None for value in values.values()):
        raise ValueError("phone Product Release runtime identity is incomplete")
    return {key: str(value) for key, value in values.items()}


def durable_release_identity(identity: ProductRelease, *, target: str) -> dict[str, object]:
    result: dict[str, object] = {
        "target": target,
        "product_release": identity.tag,
        "release_id": identity.release_id,
        "release_source_sha": identity.source_sha,
        "artifact_digest": identity.artifact_digest,
    }
    if target == _PHONE_TARGET:
        result.update(_runtime_values(identity))
    return result


def payload_matches_release_identity(
    payload: Mapping[str, object],
    identity: ProductRelease,
    *,
    target: str,
) -> bool:
    expected = durable_release_identity(identity, target=target)
    if any(payload.get(key) != value for key, value in expected.items()):
        return False
    if target != _PHONE_TARGET and any(payload.get(key) is not None for key in PHONE_RUNTIME_IDENTITY_FIELDS):
        return False
    return True


def validate_optional_runtime_identity(payload: Mapping[str, object], *, target: str) -> bool:
    values = tuple(payload.get(field) for field in PHONE_RUNTIME_IDENTITY_FIELDS)
    if all(value is None for value in values):
        return False
    if target != _PHONE_TARGET or any(value is None for value in values):
        raise ValueError("durable phone runtime identity is incomplete or leaked to another target")

    tag = str(payload.get("product_release", ""))
    expected_name = f"mobile-proxy-phone-production-runtime-{tag}.tar.gz"
    if payload.get("phone_runtime_artifact_name") != expected_name:
        raise ValueError("durable phone runtime artifact name differs")
    if payload.get("phone_runtime_inventory_path") != _PHONE_RUNTIME_PATH:
        raise ValueError("durable phone runtime inventory path differs")
    if _TYPED_DIGEST.fullmatch(str(payload.get("phone_runtime_artifact_digest", ""))) is None:
        raise ValueError("durable phone runtime artifact digest is invalid")
    if _TYPED_DIGEST.fullmatch(str(payload.get("phone_runtime_inventory_digest", ""))) is None:
        raise ValueError("durable phone runtime inventory digest is invalid")
    return True
