from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

SCHEMA = "production-deployment-request.v2"
_REQUEST_RE = re.compile(r"req-sha256:[0-9a-f]{64}")
_TAG_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
_TARGETS = frozenset({"phone-production", "vm-production"})


class DeploymentRequestError(ValueError):
    pass


def _canonical_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RequestProvenance:
    repository: str
    issue_number: int
    comment_id: int
    actor: str
    event_name: str = "issue_comment"

    def validate(self) -> None:
        if self.repository != "iamaman11/mobile-proxy-production":
            raise DeploymentRequestError("deployment command repository differs")
        if self.issue_number != 1:
            raise DeploymentRequestError("deployment commands are accepted only from private Issue #1")
        if self.comment_id <= 0:
            raise DeploymentRequestError("source comment id is invalid")
        if not self.actor:
            raise DeploymentRequestError("source actor is missing")
        if self.event_name != "issue_comment":
            raise DeploymentRequestError("unsupported deployment source event")


@dataclass(frozen=True)
class DeploymentRequest:
    schema: str
    request_id: str
    operation: str
    target: str
    product_release_tag: str
    mutating: bool
    provenance: RequestProvenance

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "operation": self.operation,
            "target": self.target,
            "product_release_tag": self.product_release_tag,
        }

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build_deployment_request(
    *, target: str,
    product_release_tag: str,
    provenance: RequestProvenance,
) -> DeploymentRequest:
    provenance.validate()
    target = target.strip()
    tag = product_release_tag.strip()
    if target not in _TARGETS:
        raise DeploymentRequestError("unsupported deployment target")
    if _TAG_RE.fullmatch(tag) is None:
        raise DeploymentRequestError("release tag must be exact semantic version vX.Y.Z")
    semantic = {
        "schema": SCHEMA,
        "operation": "deploy-product-release",
        "target": target,
        "product_release_tag": tag,
    }
    request_id = "req-sha256:" + _canonical_digest(semantic)
    request = DeploymentRequest(
        schema=SCHEMA,
        request_id=request_id,
        operation="deploy-product-release",
        target=target,
        product_release_tag=tag,
        mutating=True,
        provenance=provenance,
    )
    validate_deployment_request(request.to_dict())
    return request


def validate_deployment_request(value: Mapping[str, object]) -> None:
    if value.get("schema") != SCHEMA:
        raise DeploymentRequestError("deployment request schema differs")
    if value.get("operation") != "deploy-product-release":
        raise DeploymentRequestError("deployment operation differs")
    target = str(value.get("target", ""))
    tag = str(value.get("product_release_tag", ""))
    if target not in _TARGETS or _TAG_RE.fullmatch(tag) is None:
        raise DeploymentRequestError("deployment target/release identity is invalid")
    if value.get("mutating") is not True:
        raise DeploymentRequestError("deployment request must be mutating")
    request_id = str(value.get("request_id", ""))
    if _REQUEST_RE.fullmatch(request_id) is None:
        raise DeploymentRequestError("semantic request id is invalid")
    semantic = {
        "schema": SCHEMA,
        "operation": "deploy-product-release",
        "target": target,
        "product_release_tag": tag,
    }
    if request_id != "req-sha256:" + _canonical_digest(semantic):
        raise DeploymentRequestError("semantic request id does not recompute")
    provenance = value.get("provenance")
    if not isinstance(provenance, Mapping):
        raise DeploymentRequestError("deployment request provenance is missing")
    RequestProvenance(
        repository=str(provenance.get("repository", "")),
        issue_number=int(provenance.get("issue_number", 0)),
        comment_id=int(provenance.get("comment_id", 0)),
        actor=str(provenance.get("actor", "")),
        event_name=str(provenance.get("event_name", "")),
    ).validate()
