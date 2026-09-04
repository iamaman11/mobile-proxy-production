#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Sequence

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from github_projection import PublicDeploymentMatch, PublicDeploymentProjection  # noqa: E402

SOURCE_SHA = "ff882e5ca5116fa90b80f15c6cc019f88e68ccfa"
ENVIRONMENT = "phone-production"
PRODUCT_RELEASE = "v0.1.4"
RELEASE_ID = 382429107
SCHEMA = "public-deployment-projection-observation.v1"


def build_observation(matches: Sequence[PublicDeploymentMatch]) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": SCHEMA,
        "source_sha": SOURCE_SHA,
        "environment": ENVIRONMENT,
        "product_release": PRODUCT_RELEASE,
        "release_id": RELEASE_ID,
        "exact_match_count": len(matches),
    }
    if len(matches) == 1:
        match = matches[0]
        if match.source_sha != SOURCE_SHA or match.ref != SOURCE_SHA:
            raise RuntimeError("exact public Deployment observer received a different source identity")
        if match.environment != ENVIRONMENT:
            raise RuntimeError("exact public Deployment observer received a different environment")
        if not isinstance(match.deployment_id, int) or match.deployment_id <= 0:
            raise RuntimeError("exact public Deployment observer received an invalid deployment id")
        if not isinstance(match.latest_state, str) or not match.latest_state:
            raise RuntimeError("exact public Deployment observer received an unavailable latest state")
        value["deployment_id"] = match.deployment_id
        value["latest_state"] = match.latest_state
    return value


def observe_projection(projection: PublicDeploymentProjection) -> dict[str, object]:
    matches = projection.find_exact(
        source_sha=SOURCE_SHA,
        environment=ENVIRONMENT,
        release_tag=PRODUCT_RELEASE,
        release_id=RELEASE_ID,
    )
    return build_observation(matches)


def main() -> int:
    projection = PublicDeploymentProjection(os.environ.get("PUBLIC_DEPLOYMENTS_TOKEN", ""))
    value = observe_projection(projection)
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
