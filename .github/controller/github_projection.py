from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping

PUBLIC_REPOSITORY = "iamaman11/mobile-proxy"
API = f"https://api.github.com/repos/{PUBLIC_REPOSITORY}"


class ProjectionError(RuntimeError):
    pass


class PublicDeploymentProjection:
    """Minimal public deployment-history writer.

    The token used here must have only the permission required to create/update
    Deployments in the public product repository. Canonical execution evidence is
    deliberately not copied into the public deployment payload.
    """

    def __init__(self, token: str) -> None:
        if not token:
            raise ProjectionError("PUBLIC_DEPLOYMENTS_TOKEN is unavailable")
        self.token = token

    def _request(self, path: str, *, method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = urllib.request.Request(
            API + path,
            data=json.dumps(payload).encode("utf-8"),
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
                value = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ProjectionError("public GitHub Deployment projection failed") from exc
        if not isinstance(value, Mapping):
            raise ProjectionError("public GitHub Deployment response is invalid")
        return value

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
                    "schema": "mobile-proxy-deployment-projection.v1",
                    "product_release": release_tag,
                    "release_id": release_id,
                },
            },
        )
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
        status_id = value.get("id")
        if not isinstance(status_id, int) or status_id <= 0:
            raise ProjectionError("public GitHub Deployment status id is invalid")
        return status_id
