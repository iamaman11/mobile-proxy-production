from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Mapping, Sequence

from deployment_state_machine import TERMINALS, deployment_projection

SCHEMA = "production-deployment-terminal.v2"
_SHA = re.compile(r"[0-9a-f]{40}")
_TYPED_DIGEST = re.compile(r"b3:[0-9a-f]{64}")
_REQUEST = re.compile(r"req-sha256:[0-9a-f]{64}")
_EXECUTION = re.compile(r"gh-run:[1-9][0-9]*:[1-9][0-9]*")


class TerminalContractError(ValueError):
    pass


@dataclass(frozen=True)
class DeploymentTerminal:
    operation: str
    semantic_request_id: str
    execution_id: str
    controller_revision: str
    target: str
    product_release: str
    release_id: int | None
    release_source_sha: str | None
    artifact_digest: str | None
    deployment_id: int | None
    state: str
    current_step: str
    facts: Mapping[str, object] = field(default_factory=dict)
    blocking_predicates: Sequence[str] = field(default_factory=tuple)
    mutation_performed: bool = False
    postcondition_verified: bool = False
    recovery_required: bool = False
    recovery_state: str | None = None
    next_allowed_operation: str = "none"
    evidence_refs: Sequence[str] = field(default_factory=tuple)
    schema: str = SCHEMA

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["blocking_predicates"] = list(self.blocking_predicates)
        value["evidence_refs"] = list(self.evidence_refs)
        value["deployment_projection"] = deployment_projection(self.state)
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def validate_terminal(value: Mapping[str, object]) -> None:
    if value.get("schema") != SCHEMA:
        raise TerminalContractError("terminal schema differs")
    if value.get("operation") != "deploy-product-release":
        raise TerminalContractError("terminal operation differs")
    if _REQUEST.fullmatch(str(value.get("semantic_request_id", ""))) is None:
        raise TerminalContractError("terminal semantic request id invalid")
    if _EXECUTION.fullmatch(str(value.get("execution_id", ""))) is None:
        raise TerminalContractError("terminal execution id invalid")
    if _SHA.fullmatch(str(value.get("controller_revision", ""))) is None:
        raise TerminalContractError("controller revision invalid")
    state = str(value.get("state", ""))
    if state not in TERMINALS:
        raise TerminalContractError("terminal state is not terminal")

    release_id = value.get("release_id")
    source_sha = value.get("release_source_sha")
    artifact_digest = value.get("artifact_digest")
    release_resolved = (
        isinstance(release_id, int)
        and release_id > 0
        and _SHA.fullmatch(str(source_sha or "")) is not None
        and _TYPED_DIGEST.fullmatch(str(artifact_digest or "")) is not None
    )
    if state != "REFUSED" and not release_resolved:
        raise TerminalContractError("post-admission terminal requires exact Release identity")
    if state == "REFUSED":
        partial = [release_id is not None, source_sha is not None, artifact_digest is not None]
        if any(partial) and not all(partial):
            raise TerminalContractError("REFUSED terminal has a partial Release identity")
        if all(partial) and not release_resolved:
            raise TerminalContractError("REFUSED terminal Release identity is invalid")

    expected_projection = deployment_projection(state)
    if value.get("deployment_projection", expected_projection) != expected_projection:
        raise TerminalContractError("public Deployment projection contradicts canonical terminal")
    if state == "ACCEPTED" and value.get("postcondition_verified") is not True:
        raise TerminalContractError("ACCEPTED requires independent postcondition verification")
    if state == "RECOVERED" and expected_projection == "success":
        raise TerminalContractError("RECOVERED must never project original deployment success")
