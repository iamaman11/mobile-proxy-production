#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from android_target import (  # noqa: E402
    AndroidArtifactRefused,
    AndroidObservationUnavailable,
    dispatch_install_once,
    observe,
    verify_artifact,
    verify_local_artifact_bytes,
)
from deployment_request import validate_deployment_request  # noqa: E402
from deployment_state_machine import DeploymentState, recover_unknown, reduce_state  # noqa: E402
from evidence_store import EvidenceError, IssueEvidenceStore  # noqa: E402
from github_projection import ProjectionError, PublicDeploymentProjection  # noqa: E402
from release_resolver import resolve_release  # noqa: E402
from terminal_result import DeploymentTerminal, validate_terminal  # noqa: E402

_SHA = re.compile(r"[0-9a-f]{40}")
_EXECUTION = re.compile(r"gh-run:[1-9][0-9]*:[1-9][0-9]*")


def _next(state: str) -> str:
    return {
        "ACCEPTED": "deploy-product-release",
        "REFUSED": "fix_blocking_predicates_then_reissue",
        "UNKNOWN": "read-only-recovery-observation",
        "RECOVERED": "operator-review-before-new-mutation",
        "QUARANTINED": "read-only-observation-or-approved-recovery",
    }.get(state, "none")


def _download(url: str, destination: Path, expected_transport_sha256: str) -> None:
    if not url.startswith("https://github.com/iamaman11/mobile-proxy/releases/download/"):
        raise AndroidArtifactRefused("Android Release artifact URL differs")
    if re.fullmatch(r"[0-9a-f]{64}", expected_transport_sha256) is None:
        raise AndroidArtifactRefused("Android Release transport digest is invalid")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "mobile-proxy-production-controller-v2"},
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 250 * 1024 * 1024:
                    raise AndroidArtifactRefused("Android Release artifact exceeds size bound")
                digest.update(chunk)
                handle.write(chunk)
    except (OSError, urllib.error.URLError) as exc:
        raise AndroidArtifactRefused("Android Release artifact download failed") from exc
    if total <= 0 or digest.hexdigest() != expected_transport_sha256:
        raise AndroidArtifactRefused("Android Release artifact transport digest differs after download")


def _materialize_verified_release_apk(admitted: object, apk: Path, facts: dict[str, object]) -> None:
    _download(
        admitted.artifact_download_url,
        apk,
        admitted.artifact_transport_sha256,
    )
    artifact_fact = verify_artifact(
        apk=apk,
        expected_sha256=admitted.artifact_transport_sha256,
        expected_version_name=str(admitted.android_version_name),
        expected_version_code=int(admitted.android_version_code or 0),
    )
    artifact_fact["product_content_digest"] = admitted.identity.artifact_digest
    facts["artifact_verification"] = artifact_fact


def _base_state_authorized() -> DeploymentState:
    state = reduce_state(DeploymentState(), "request_received")
    return reduce_state(state, "authorized")


def _unknown_from_existing_intent() -> DeploymentState:
    return DeploymentState(
        state="UNKNOWN",
        current_step="DISPATCH",
        intent_persisted=True,
        dispatch_attempted=True,
        mutation_performed=True,
        postcondition_verified=False,
        recovery_required=True,
        recovery_state=None,
        blocking_predicates=("durable_intent_exists_without_terminal",),
    )


def _terminal_payload(
    *,
    request: dict[str, object],
    execution_id: str,
    controller_revision: str,
    admitted: object,
    deployment_id: int,
    state: DeploymentState,
    facts: dict[str, object],
    evidence_refs: list[str],
) -> dict[str, object]:
    identity = admitted.identity
    terminal = DeploymentTerminal(
        operation="deploy-product-release",
        semantic_request_id=str(request["request_id"]),
        execution_id=execution_id,
        controller_revision=controller_revision,
        target=str(request["target"]),
        product_release=identity.tag,
        release_id=identity.release_id,
        release_source_sha=identity.source_sha,
        artifact_digest=identity.artifact_digest,
        deployment_id=deployment_id,
        state=state.state,
        current_step=state.current_step,
        facts=facts,
        blocking_predicates=state.blocking_predicates,
        mutation_performed=state.mutation_performed,
        postcondition_verified=state.postcondition_verified,
        recovery_required=state.recovery_required,
        recovery_state=state.recovery_state,
        next_allowed_operation=_next(state.state),
        evidence_refs=tuple(evidence_refs),
    ).to_dict()
    validate_terminal(terminal)
    return terminal


def _project_terminal(
    projection: PublicDeploymentProjection,
    deployment_id: int,
    terminal: dict[str, object],
) -> None:
    projection.status(
        deployment_id=deployment_id,
        state=str(terminal["deployment_projection"]),
        description=f"canonical controller terminal: {terminal['state']}",
    )


def _persist_and_write(
    *,
    evidence: IssueEvidenceStore,
    projection: PublicDeploymentProjection,
    output: Path,
    deployment_id: int,
    terminal: dict[str, object],
) -> None:
    evidence.persist_terminal(terminal)
    _project_terminal(projection, deployment_id, terminal)
    output.write_text(json.dumps(terminal, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--admitted-release-json", required=True)
    parser.add_argument("--deployment-id", type=int, required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--recovery-only", choices=("true", "false"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    request = json.loads(args.request_json)
    validate_deployment_request(request)
    if request["target"] != "phone-production":
        raise SystemExit("Android adapter received a non-phone target")
    if _SHA.fullmatch(args.controller_revision) is None or _EXECUTION.fullmatch(args.execution_id) is None:
        raise SystemExit("execution provenance is invalid")
    if os.environ.get("GITHUB_SHA") != args.controller_revision:
        raise SystemExit("runtime controller revision differs")
    if args.deployment_id <= 0:
        raise SystemExit("public Deployment id is invalid")

    expected_admitted = json.loads(args.admitted_release_json)
    admitted = resolve_release(
        tag=str(request["product_release_tag"]),
        target="phone-production",
    )
    if admitted.to_dict() != expected_admitted:
        raise SystemExit("Release identity changed between hosted admission and target execution")

    serial = os.environ.get("ANDROID_PRODUCTION_SERIAL", "")
    binding_key = os.environ.get("ANDROID_TARGET_BINDING_KEY", "")
    if not serial or not binding_key:
        raise SystemExit("registered Android production target binding is unavailable")

    evidence = IssueEvidenceStore(os.environ.get("GITHUB_TOKEN", ""))
    projection = PublicDeploymentProjection(os.environ.get("PUBLIC_DEPLOYMENTS_TOKEN", ""))
    existing_intent, existing_terminal = evidence.request_history(str(request["request_id"]))
    if existing_terminal is not None:
        args.output.write_text(
            json.dumps(existing_terminal.payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0

    projection.status(
        deployment_id=args.deployment_id,
        state="in_progress",
        description=f"{admitted.identity.tag} target execution started",
    )

    source_ref = f"issue-comment:{request['provenance']['comment_id']}"
    evidence_refs = [source_ref]
    facts: dict[str, object] = {
        "release_admission": {
            "release_id": admitted.identity.release_id,
            "source_sha": admitted.identity.source_sha,
            "artifact_name": admitted.identity.artifact_name,
            "artifact_digest": admitted.identity.artifact_digest,
            "artifact_transport_sha256": admitted.artifact_transport_sha256,
            "manifest_digest": admitted.identity.manifest_digest,
            "provenance_digest": admitted.identity.provenance_digest,
            "immutability_control": admitted.immutability_control,
        }
    }

    if args.recovery_only == "true":
        if existing_intent is None:
            raise SystemExit("recovery-only execution lacks durable mutation intent")
        evidence_refs.append(existing_intent.ref)
        state = _unknown_from_existing_intent()
        with tempfile.TemporaryDirectory(prefix="mobile-proxy-release-recovery-") as td:
            apk = Path(td) / admitted.identity.artifact_name
            try:
                _materialize_verified_release_apk(admitted, apk, facts)
            except AndroidArtifactRefused as exc:
                facts["artifact_verification"] = {"available": False, "exact_release_artifact": False}
                state = recover_unknown(state, "recovery_unavailable", reason=str(exc))
            else:
                try:
                    recovered = observe(
                        serial=serial,
                        binding_key=binding_key,
                        expected_version_name=str(admitted.android_version_name),
                        expected_version_code=int(admitted.android_version_code or 0),
                        expected_artifact_sha256=admitted.artifact_transport_sha256,
                    )
                    facts["recovery_observation"] = recovered.to_dict()
                    state = recover_unknown(
                        state,
                        "recovery_observed_desired" if recovered.desired else "recovery_observed_other",
                    )
                except AndroidObservationUnavailable as exc:
                    facts["recovery_observation"] = {"available": False}
                    state = recover_unknown(state, "recovery_unavailable", reason=str(exc))
        terminal = _terminal_payload(
            request=request,
            execution_id=args.execution_id,
            controller_revision=args.controller_revision,
            admitted=admitted,
            deployment_id=args.deployment_id,
            state=state,
            facts=facts,
            evidence_refs=evidence_refs,
        )
        record = evidence.persist_terminal(terminal)
        facts["canonical_terminal_ref"] = record.ref
        try:
            _project_terminal(projection, args.deployment_id, terminal)
        except ProjectionError:
            pass
        args.output.write_text(json.dumps(terminal, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0 if state.state in {"RECOVERED", "ACCEPTED"} else 2

    if existing_intent is not None:
        raise SystemExit("new execution unexpectedly found a durable mutation intent")

    with tempfile.TemporaryDirectory(prefix="mobile-proxy-release-") as td:
        apk = Path(td) / admitted.identity.artifact_name
        try:
            _materialize_verified_release_apk(admitted, apk, facts)
        except AndroidArtifactRefused as exc:
            state = reduce_state(_base_state_authorized(), "observation_refused", reason=str(exc))
            terminal = _terminal_payload(
                request=request,
                execution_id=args.execution_id,
                controller_revision=args.controller_revision,
                admitted=admitted,
                deployment_id=args.deployment_id,
                state=state,
                facts=facts,
                evidence_refs=evidence_refs,
            )
            _persist_and_write(
                evidence=evidence,
                projection=projection,
                output=args.output,
                deployment_id=args.deployment_id,
                terminal=terminal,
            )
            return 2

        state = _base_state_authorized()
        try:
            pre = observe(
                serial=serial,
                binding_key=binding_key,
                expected_version_name=str(admitted.android_version_name),
                expected_version_code=int(admitted.android_version_code or 0),
                expected_artifact_sha256=admitted.artifact_transport_sha256,
            )
            facts["preflight_observation"] = pre.to_dict()
        except AndroidObservationUnavailable as exc:
            state = reduce_state(state, "observation_refused", reason=str(exc))
            terminal = _terminal_payload(
                request=request,
                execution_id=args.execution_id,
                controller_revision=args.controller_revision,
                admitted=admitted,
                deployment_id=args.deployment_id,
                state=state,
                facts=facts | {"preflight_observation": {"available": False}},
                evidence_refs=evidence_refs,
            )
            _persist_and_write(
                evidence=evidence,
                projection=projection,
                output=args.output,
                deployment_id=args.deployment_id,
                terminal=terminal,
            )
            return 2

        if pre.desired:
            state = reduce_state(state, "already_desired")
            terminal = _terminal_payload(
                request=request,
                execution_id=args.execution_id,
                controller_revision=args.controller_revision,
                admitted=admitted,
                deployment_id=args.deployment_id,
                state=state,
                facts=facts,
                evidence_refs=evidence_refs,
            )
            _persist_and_write(
                evidence=evidence,
                projection=projection,
                output=args.output,
                deployment_id=args.deployment_id,
                terminal=terminal,
            )
            return 0

        state = reduce_state(state, "observed")
        intent_payload = {
            "schema": "production-deployment-intent.v2",
            "semantic_request_id": request["request_id"],
            "execution_id": args.execution_id,
            "controller_revision": args.controller_revision,
            "target": "phone-production",
            "target_binding_id": pre.target_binding_id,
            "product_release": admitted.identity.tag,
            "release_id": admitted.identity.release_id,
            "release_source_sha": admitted.identity.source_sha,
            "artifact_digest": admitted.identity.artifact_digest,
            "deployment_id": args.deployment_id,
            "dispatch_operation": "adb-install-replace",
            "dispatch_may_reach_target": True,
            "blind_retry_allowed": False,
            "mutation_performed": False,
        }
        try:
            intent = evidence.persist_intent(intent_payload)
        except EvidenceError as exc:
            state = reduce_state(state, "intent_persistence_failed", reason=str(exc))
            terminal = _terminal_payload(
                request=request,
                execution_id=args.execution_id,
                controller_revision=args.controller_revision,
                admitted=admitted,
                deployment_id=args.deployment_id,
                state=state,
                facts=facts,
                evidence_refs=evidence_refs,
            )
            _persist_and_write(
                evidence=evidence,
                projection=projection,
                output=args.output,
                deployment_id=args.deployment_id,
                terminal=terminal,
            )
            return 2
        evidence_refs.append(intent.ref)
        state = reduce_state(state, "intent_persisted")

        try:
            exact_sha256 = verify_local_artifact_bytes(
                apk=apk,
                expected_sha256=admitted.artifact_transport_sha256,
            )
            facts["pre_dispatch_artifact_reverification"] = {
                "exact_release_artifact": True,
                "sha256": exact_sha256,
            }
        except AndroidArtifactRefused as exc:
            facts["pre_dispatch_artifact_reverification"] = {
                "exact_release_artifact": False,
            }
            facts["dispatch"] = {
                "attempted_exactly_once": False,
                "confirmed": False,
                "outcome_unknown": False,
                "error_class": "LOCAL_ARTIFACT_IDENTITY_REFUSED",
            }
            state = reduce_state(state, "dispatch_refused", reason=str(exc))
            terminal = _terminal_payload(
                request=request,
                execution_id=args.execution_id,
                controller_revision=args.controller_revision,
                admitted=admitted,
                deployment_id=args.deployment_id,
                state=state,
                facts=facts,
                evidence_refs=evidence_refs,
            )
            _persist_and_write(
                evidence=evidence,
                projection=projection,
                output=args.output,
                deployment_id=args.deployment_id,
                terminal=terminal,
            )
            return 2

        dispatch = dispatch_install_once(serial=serial, apk=apk)
        facts["dispatch"] = {
            "attempted_exactly_once": True,
            "confirmed": dispatch.confirmed,
            "outcome_unknown": dispatch.outcome_unknown,
            "error_class": dispatch.error_class,
        }
        if dispatch.outcome_unknown:
            state = reduce_state(state, "dispatch_outcome_unknown", reason=dispatch.error_class)
        else:
            state = reduce_state(state, "dispatch_confirmed")
            try:
                post = observe(
                    serial=serial,
                    binding_key=binding_key,
                    expected_version_name=str(admitted.android_version_name),
                    expected_version_code=int(admitted.android_version_code or 0),
                    expected_artifact_sha256=admitted.artifact_transport_sha256,
                )
                facts["postcondition_observation"] = post.to_dict()
                state = reduce_state(state, "verify_match" if post.desired else "verify_mismatch")
            except AndroidObservationUnavailable as exc:
                facts["postcondition_observation"] = {"available": False}
                state = reduce_state(state, "verify_unavailable", reason=str(exc))

        if state.state == "UNKNOWN":
            try:
                recovery = observe(
                    serial=serial,
                    binding_key=binding_key,
                    expected_version_name=str(admitted.android_version_name),
                    expected_version_code=int(admitted.android_version_code or 0),
                    expected_artifact_sha256=admitted.artifact_transport_sha256,
                )
                facts["recovery_observation"] = recovery.to_dict()
                state = recover_unknown(
                    state,
                    "recovery_observed_desired" if recovery.desired else "recovery_observed_other",
                )
            except AndroidObservationUnavailable as exc:
                facts["recovery_observation"] = {"available": False}
                state = recover_unknown(state, "recovery_unavailable", reason=str(exc))

        terminal = _terminal_payload(
            request=request,
            execution_id=args.execution_id,
            controller_revision=args.controller_revision,
            admitted=admitted,
            deployment_id=args.deployment_id,
            state=state,
            facts=facts,
            evidence_refs=evidence_refs,
        )
        record = evidence.persist_terminal(terminal)
        facts["canonical_terminal_ref"] = record.ref
        try:
            _project_terminal(projection, args.deployment_id, terminal)
        except ProjectionError:
            pass
        args.output.write_text(json.dumps(terminal, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0 if state.state == "ACCEPTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
