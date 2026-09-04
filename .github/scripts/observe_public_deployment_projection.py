#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from github_projection import PublicDeploymentProjection  # noqa: E402

SOURCE_SHA = "ff882e5ca5116fa90b80f15c6cc019f88e68ccfa"
ENVIRONMENT = "phone-production"
PRODUCT_RELEASE = "v0.1.4"
RELEASE_ID = 382429107


def main() -> int:
    projection = PublicDeploymentProjection(os.environ.get("PUBLIC_DEPLOYMENTS_TOKEN", ""))
    matches = projection.find_exact(
        source_sha=SOURCE_SHA,
        environment=ENVIRONMENT,
        release_tag=PRODUCT_RELEASE,
        release_id=RELEASE_ID,
    )

    evidence: dict[str, object] = {"exact_match_count": len(matches)}
    if len(matches) == 1:
        match = matches[0]
        evidence["deployment_id"] = match.deployment_id
        evidence["latest_state"] = match.latest_state

    print("PUBLIC_DEPLOYMENT_PROJECTION_OBSERVATION " + json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
