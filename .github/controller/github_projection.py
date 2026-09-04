from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

PUBLIC_REPOSITORY = "iamaman11/mobile-proxy"
API = f"https://api.github.com/repos/{PUBLIC_REPOSITORY}"
_PROJECTION_SCHEMA = "mobile-proxy-deployment-projection.v1"
_NONTERMINAL_STATES = frozenset({"queued", "pending", "in_progress"})
_TERMINAL_STATES = frozenset({"success", "failure", "error", "inactive"})


class ProjectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectionMatch:
    deployment_id: int
    current_state: str | None


class PublicDeploymentProjection:
    """Minimal public deployment-history projection.

    The token used here must have only the permission required to read/create/update
    Deployments in the public product repository. Canonical execution evidence is
    deliberately not copied into the public deployment payload.
    """

    def __init__(self, token: str) -> None:
        if not token:
            raise ProjectionError("PUBLIC_DEPLOYMENTS_TOKEN is unavailable")
        self.token = token

    def _request(
        self,
        path: str,
        *,
        method: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        request = urllib.request.Request(
            API + path,
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "mobile-proxy-production-deployment-projection-v2",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ProjectionError(f"public GitHub Deployment request rejected with HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ProjectionError("public GitHub Deployment projection failed") from exc

    @staticmethod
    def _matches_release(
        value: Mapping[str, Any],
        *,
        source_sha: str,
        environment: str,
        release_tag: str,
        release_id: int,
    ) -> bool:
        payload = value.get("payload")
        return (
            value.get("sha") == source_sha
            and value.get("environment") == environment
            and isinstance(payload, Mapping)
            and payload.get("schema") == _PROJECTION_SCHEMA
            and payload.get("product_release") == release_tag
            and payload.get("release_id") == release_id
        )

    def _current_state(self, deployment_id: int) -> str | None:
        value = self._request(
            f"/deployments/{deployment_id}/statuses?per_page=100",
            method="GET",
        )
        if not isinstance(value, list):
            raise ProjectionError("public Deployment status inventory is invalid")
        if not value:
            return None

        states: list[tuple[str, int, str]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise ProjectionError("public Deployment status entry is invalid")
            state = item.get("state")
            created_at = item.get("created_at")
            status_id = item.get("id")
            if state not in _NONTERMINAL_STATES | _TERMINAL_STATES:
                raise ProjectionError("public Deployment status state is unsupported")
            if not isinstance(created_at, str) or not created_at:
                raise ProjectionError("public Deployment status timestamp is invalid")
            if not isinstance(status_id, int) or status_id <= 0:
                raise ProjectionError("public Deployment status id is invalid")
            states.append((created_at, status_id, str(state)))
        return max(states)[2]

    def find_exact(
        self,
        *,
        source_sha: str,
        environment: str,
        release_tag: str,
        release_id: int,
    ) -> ProjectionMatch | None:
        matches: list[int] = []
        for page in range(1, 11):
            query = urllib.parse.urlencode(
                {
                    "ref": source_sha,
                    "environment": environment,
                    "per_page": 100,
                    "page": page,
                }
            )
            value = self._request(f"/deployments?{query}", method="GET")
            if not isinstance(value, list):
                raise ProjectionError("public Deployment inventory is invalid")
            for item in value:
                if not isinstance(item, Mapping):
                    raise ProjectionError("public Deployment inventory entry is invalid")
                if not self._matches_release(
                    item,
                    source_sha=source_sha,
                    environment=environment,
                    release_tag=release_tag,
                    release_id=release_id,
                ):
                    continue
                deployment_id = item.get("id")
                if not isinstance(deployment_id, int) or deployment_id <= 0:
                    raise ProjectionError("matching public Deployment id is invalid")
                matches.append(deployment_id)
            if len(value) < 100:
                break
        else:
            raise ProjectionError("public Deployment inventory exceeds bounded reconciliation scan")

        unique_matches = sorted(set(matches))
        if len(unique_matches) > 1:
            raise ProjectionError("multiple exact public Deployments exist for one semantic Release target")
        if not unique_matches:
            return None
        deployment_id = unique_matches[0]
        return ProjectionMatch(deployment_id, self._current_state(deployment_id))

    def create(self, *, source_sha: str, environment: str, release_tag: str, release_id: int) -> int:
        value = self._request(
            "/deployments",
            method="POST",
            payload={
                "ref": source_sha,
                "environment": environment,
                "auto_merge": False,
                "required_contexts": [],
                "description": f"Deploy {release_tag} to {environment}",
                "payload": {
                    "schema": _PROJECTION_SCHEMA,
                    "product_release": release_tag,
                    "release_id": release_id,
                },
            },
        )
        if not isinstance(value, Mapping):
            raise ProjectionError("public GitHub Deployment response is invalid")
        deployment_id = value.get("id")
        if not isinstance(deployment_id, int) or deployment_id <= 0:
            raise ProjectionError("public GitHub Deployment id is invalid")
        return deployment_id

    def status(self, *, deployment_id: int, state: str, description: str) -> int:
        if state not in {"queued", "in_progress", "success", "failure", "error"}:
            raise ProjectionError("unsupported public Deployment status")
        value = self._request(
            f"/deployments/{deployment_id}/statuses",
            method="POST",
            payload={
                "state": state,
                "description": description[:140],
                "auto_inactive": False,
            },
        )
        if not isinstance(value, Mapping):
            raise ProjectionError("public GitHub Deployment status response is invalid")
        status_id = value.get("id")
        if not isinstance(status_id, int) or status_id <= 0:
            raise ProjectionError("public GitHub Deployment status id is invalid")
        return status_id

    def reconcile_or_create(
        self,
        *,
        source_sha: str,
        environment: str,
        release_tag: str,
        release_id: int,
    ) -> tuple[int, bool]:
        """Return (deployment_id, reused) for an ACK-only admission.

        Private INTENT/TERMINAL evidence remains authoritative. This method is
        called only when both are absent. A matching public terminal can never be
        promoted to canonical authority and therefore fails closed.
        """
        existing = self.find_exact(
            source_sha=source_sha,
            environment=environment,
            release_tag=release_tag,
            release_id=release_id,
        )
        if existing is not None:
            if existing.current_state in _TERMINAL_STATES:
                raise ProjectionError(
                    "public Deployment terminal exists without private canonical terminal"
                )
            if existing.current_state is not None and existing.current_state not in _NONTERMINAL_STATES:
                raise ProjectionError("matching public Deployment state cannot be reconciled")
            if existing.current_state is None:
                self.status(
                    deployment_id=existing.deployment_id,
                    state="queued",
                    description=f"{release_tag} reconciled by production controller",
                )
            return existing.deployment_id, True

        deployment_id = self.create(
            source_sha=source_sha,
            environment=environment,
            release_tag=release_tag,
            release_id=release_id,
        )
        self.status(
            deployment_id=deployment_id,
            state="queued",
            description=f"{release_tag} admitted by production controller",
        )
        return deployment_id, False
