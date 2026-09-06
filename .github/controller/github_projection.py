from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode

PUBLIC_REPOSITORY = "iamaman11/mobile-proxy"
API = f"https://api.github.com/repos/{PUBLIC_REPOSITORY}"
_PAGE_SIZE = 100
_MAX_PAGES = 100
_READ_TRANSPORT_ATTEMPTS = 3
_STATUS_STATES = frozenset({"error", "failure", "inactive", "in_progress", "pending", "queued", "success"})


class ProjectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublicDeploymentMatch:
    deployment_id: int
    source_sha: str
    ref: str
    environment: str
    payload: Mapping[str, Any]
    latest_state: str | None


class PublicDeploymentProjection:
    """Minimal public deployment-history reader/writer.

    The token used here must have only the Deployments permission needed to
    read/create/update projection records in the public product repository.
    Canonical execution evidence is deliberately not copied into the public
    deployment payload.
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
        read_only = method.upper() == "GET" and payload is None
        attempts = _READ_TRANSPORT_ATTEMPTS if read_only else 1
        for attempt in range(attempts):
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
                transient = exc.code == 429 or 500 <= exc.code < 600
                if read_only and transient and attempt + 1 < attempts:
                    continue
                raise ProjectionError("public GitHub Deployment projection failed") from exc
            except (OSError, urllib.error.URLError) as exc:
                if read_only and attempt + 1 < attempts:
                    continue
                raise ProjectionError("public GitHub Deployment projection failed") from exc
            except json.JSONDecodeError as exc:
                raise ProjectionError("public GitHub Deployment projection failed") from exc
        raise AssertionError("bounded public Deployment read retry loop exhausted unexpectedly")

    @staticmethod
    def _payload_mapping(value: object) -> Mapping[str, Any] | None:
        if isinstance(value, Mapping):
            return value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return None
            if isinstance(decoded, Mapping):
                return decoded
        return None

    def _latest_status(self, deployment_id: int) -> str | None:
        latest_id = -1
        latest_state: str | None = None
        for page in range(1, _MAX_PAGES + 1):
            query = urlencode({"per_page": _PAGE_SIZE, "page": page})
            value = self._request(
                f"/deployments/{deployment_id}/statuses?{query}",
                method="GET",
            )
            if not isinstance(value, list):
                raise ProjectionError("public GitHub Deployment statuses response is invalid")
            for item in value:
                if not isinstance(item, Mapping):
                    raise ProjectionError("public GitHub Deployment status item is invalid")
                status_id = item.get("id")
                state = item.get("state")
                if not isinstance(status_id, int) or status_id <= 0:
                    raise ProjectionError("public GitHub Deployment status id is invalid")
                if not isinstance(state, str) or state not in _STATUS_STATES:
                    raise ProjectionError("public GitHub Deployment status state is invalid")
                if status_id > latest_id:
                    latest_id = status_id
                    latest_state = state
            if len(value) < _PAGE_SIZE:
                return latest_state
        raise ProjectionError("public GitHub Deployment status pagination bound exceeded")

    def find_exact(
        self,
        *,
        source_sha: str,
        environment: str,
        release_tag: str,
        release_id: int,
    ) -> tuple[PublicDeploymentMatch, ...]:
        matches: list[tuple[int, str, str, str, Mapping[str, Any]]] = []
        for page in range(1, _MAX_PAGES + 1):
            query = urlencode(
                {
                    "sha": source_sha,
                    "environment": environment,
                    "per_page": _PAGE_SIZE,
                    "page": page,
                }
            )
            value = self._request(f"/deployments?{query}", method="GET")
            if not isinstance(value, list):
                raise ProjectionError("public GitHub Deployments response is invalid")
            for item in value:
                if not isinstance(item, Mapping):
                    raise ProjectionError("public GitHub Deployment item is invalid")
                payload = self._payload_mapping(item.get("payload"))
                exact = (
                    item.get("sha") == source_sha
                    and item.get("ref") == source_sha
                    and item.get("environment") == environment
                    and payload is not None
                    and payload.get("schema") == "mobile-proxy-deployment-projection.v1"
                    and payload.get("product_release") == release_tag
                    and payload.get("release_id") == release_id
                )
                if not exact:
                    continue
                deployment_id = item.get("id")
                if not isinstance(deployment_id, int) or deployment_id <= 0:
                    raise ProjectionError("public GitHub Deployment id is invalid")
                assert payload is not None
                matches.append((deployment_id, source_sha, source_sha, environment, payload))
                if len(matches) > 1:
                    return tuple(
                        PublicDeploymentMatch(
                            deployment_id=match[0],
                            source_sha=match[1],
                            ref=match[2],
                            environment=match[3],
                            payload=match[4],
                            latest_state=None,
                        )
                        for match in matches
                    )
            if len(value) < _PAGE_SIZE:
                break
        else:
            raise ProjectionError("public GitHub Deployment pagination bound exceeded")

        return tuple(
            PublicDeploymentMatch(
                deployment_id=match[0],
                source_sha=match[1],
                ref=match[2],
                environment=match[3],
                payload=match[4],
                latest_state=self._latest_status(match[0]),
            )
            for match in matches
        )

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
            raise ProjectionError("public GitHub Deployment response is invalid")
        status_id = value.get("id")
        if not isinstance(status_id, int) or status_id <= 0:
            raise ProjectionError("public GitHub Deployment status id is invalid")
        return status_id
