#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from deployment_request import validate_deployment_request  # noqa: E402
from evidence_store import IssueEvidenceStore  # noqa: E402
from github_projection import PublicDeploymentProjection  # noqa: E402
from projection_reconciler import reconcile_projection  # noqa: E402

_EXECUTION = re.compile(r"gh-run:[1-9][0-9]*:[1-9][0-9]*")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--target-job-result", required=True)
    args = parser.parse_args()

    request = json.loads(args.request_json)
    validate_deployment_request(request)
    if request["target"] != "phone-production":
        raise SystemExit("projection finalizer received a non-phone target")
    if _EXECUTION.fullmatch(args.execution_id) is None:
        raise SystemExit("projection finalizer execution id is invalid")

    evidence = IssueEvidenceStore(os.environ.get("GITHUB_TOKEN", ""))
    projection = PublicDeploymentProjection(os.environ.get("PUBLIC_DEPLOYMENTS_TOKEN", ""))
    decision = reconcile_projection(
        evidence=evidence,
        projection=projection,
        semantic_request_id=str(request["request_id"]),
        execution_id=args.execution_id,
        target_job_result=args.target_job_result,
    )
    print(
        "PUBLIC_DEPLOYMENT_PROJECTION_RECONCILED "
        f"deployment_id={decision.deployment_id} state={decision.state} "
        f"canonical_terminal={str(decision.canonical_terminal).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
