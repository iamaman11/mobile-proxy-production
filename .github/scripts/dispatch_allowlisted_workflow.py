#!/usr/bin/env python3
"""Dispatch a registry-admitted read-only workflow without accepting workflow/ref from Issue text."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from issue_command_router import RouteRefused, _validate_argument_value, load_contracts

REPOSITORY = "iamaman11/mobile-proxy-production"
_SHA = re.compile(r"[0-9a-f]{40}")


class DispatchRefused(RuntimeError):
    pass


def _parse_arguments(raw: str) -> Mapping[str, str]:
    if len(raw.encode("utf-8")) > 4096:
        raise DispatchRefused("dispatch argument envelope exceeds bound")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispatchRefused("dispatch argument envelope is invalid JSON") from exc
    if not isinstance(value, Mapping) or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise DispatchRefused("dispatch argument envelope must be string-to-string object")
    return dict(value)


def build_dispatch(route_id: str, arguments_json: str) -> tuple[str, str, dict[str, str]]:
    """Resolve only one enabled generic read-only dispatch contract."""
    _, routes, targets = load_contracts()
    matches = [item for item in routes if item.get("enabled") is True and item.get("id") == route_id]
    if len(matches) != 1:
        raise DispatchRefused("dispatch route does not resolve exactly once")
    route = matches[0]
    if route.get("handler") != "dispatch_workflow":
        raise DispatchRefused("route is not generic workflow-dispatch class")
    if route.get("read_only") is not True or route.get("destructive") is not False:
        raise DispatchRefused("generic workflow dispatch must remain read-only")
    if route.get("ref") != "main" or route.get("ref_policy") != "controller-main-exact":
        raise DispatchRefused("generic dispatch ref policy must remain exact controller main")

    arguments = _parse_arguments(arguments_json)
    specs = route.get("arguments")
    if not isinstance(specs, list):
        raise DispatchRefused("route argument schema is unavailable")
    spec_names = {str(item.get("name", "")) for item in specs if isinstance(item, Mapping)}
    if set(arguments) != spec_names:
        raise DispatchRefused("dispatch arguments differ from route schema")
    allowed_targets = list(route.get("allowed_targets", []))
    try:
        for spec in specs:
            if not isinstance(spec, Mapping):
                raise DispatchRefused("route argument spec is invalid")
            name = str(spec["name"])
            _validate_argument_value(spec, arguments[name], allowed_targets=allowed_targets, targets=targets)
    except RouteRefused as exc:
        raise DispatchRefused(str(exc)) from exc

    workflow = str(route.get("workflow", ""))
    workflow_path = Path(workflow)
    if workflow_path.parent.as_posix() != ".github/workflows" or workflow_path.suffix != ".yml":
        raise DispatchRefused("generic dispatch workflow path escaped allowlisted directory")
    if not (Path(__file__).resolve().parents[1] / "workflows" / workflow_path.name).is_file():
        raise DispatchRefused("generic dispatch workflow does not exist")

    mapping = route.get("dispatch_inputs")
    if not isinstance(mapping, Mapping):
        raise DispatchRefused("dispatch input mapping is unavailable")
    inputs: dict[str, str] = {}
    for workflow_input, argument_name in mapping.items():
        if not isinstance(workflow_input, str) or not isinstance(argument_name, str) or argument_name not in arguments:
            raise DispatchRefused("dispatch input mapping is invalid")
        inputs[workflow_input] = arguments[argument_name]
    return workflow_path.name, "main", inputs


def _request(token: str, url: str, *, method: str, payload: Mapping[str, Any] | None = None):
    data = None if payload is None else json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "mobile-proxy-production-command-dispatch",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        return urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as exc:
        raise DispatchRefused(f"GitHub dispatch transport rejected with HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise DispatchRefused("GitHub dispatch transport failed") from exc


def dispatch(*, route_id: str, arguments_json: str, expected_controller_sha: str, token: str) -> None:
    if _SHA.fullmatch(expected_controller_sha) is None:
        raise DispatchRefused("expected controller revision is not a full SHA")
    if not token:
        raise DispatchRefused("GitHub token is unavailable")
    workflow_name, ref, inputs = build_dispatch(route_id, arguments_json)

    main_url = f"https://api.github.com/repos/{REPOSITORY}/commits/main"
    with _request(token, main_url, method="GET") as response:
        try:
            main_value = json.load(response)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DispatchRefused("current main response is invalid") from exc
    current_sha = main_value.get("sha") if isinstance(main_value, Mapping) else None
    if current_sha != expected_controller_sha:
        raise DispatchRefused("controller main drifted before workflow dispatch")

    workflow_id = urllib.parse.quote(workflow_name, safe="")
    dispatch_url = f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/{workflow_id}/dispatches"
    payload: dict[str, Any] = {"ref": ref}
    if inputs:
        payload["inputs"] = inputs
    with _request(token, dispatch_url, method="POST", payload=payload) as response:
        status = getattr(response, "status", None)
        if status != 204:
            raise DispatchRefused("GitHub workflow dispatch was not confirmed")
    print(f"ALLOWLISTED_READ_ONLY_WORKFLOW_DISPATCHED route_id={route_id} workflow={workflow_name} ref={ref}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route-id", required=True)
    parser.add_argument("--arguments-json", required=True)
    parser.add_argument("--expected-controller-sha", required=True)
    args = parser.parse_args(argv)
    try:
        dispatch(
            route_id=args.route_id,
            arguments_json=args.arguments_json,
            expected_controller_sha=args.expected_controller_sha,
            token=os.environ.get("GITHUB_TOKEN", ""),
        )
    except (DispatchRefused, RouteRefused) as exc:
        print(f"ALLOWLISTED_WORKFLOW_DISPATCH_REFUSED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
