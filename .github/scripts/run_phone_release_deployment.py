#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping

CONTROLLER = Path(__file__).resolve().parents[1] / "controller"
sys.path.insert(0, str(CONTROLLER))

from android_target import (  # noqa: E402
    AndroidArtifactRefused,
    AndroidObservationUnavailable,
    observe,
    verify_artifact,
    verify_local_artifact_bytes,
)
from deployment_request import validate_deployment_request  # noqa: E402
from deployment_state_machine import DeploymentState, recover_unknown, reduce_state  # noqa: E402
from durable_release_identity import durable_release_identity, payload_matches_release_identity  # noqa: E402
from evidence_store import EvidenceError, IssueEvidenceStore  # noqa: E402
from github_projection import ProjectionError, PublicDeploymentProjection  # noqa: E402
from phone_runtime import PhoneRuntimeRefused, materialize_runtime_bundle  # noqa: E402
from phone_target import PhoneTargetUnavailable, dispatch_release_once, observe_runtime  # noqa: E402
from product_runtime_renderer import (  # noqa: E402
    bind_renderer_inputs,
    render_required_runtime_configs,
    verify_product_source,
    verify_release_component_digests,
)
from release_resolver import resolve_release  # noqa: E402
from terminal_result import DeploymentTerminal, validate_terminal  # noqa: E402

_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EXECUTION = re.compile(r"gh-run:[1-9][0-9]*:[1-9][0-9]*")
_RELEASE_URL_PREFIX = "https://github.com/iamaman11/mobile-proxy/releases/download/"
_MAX_RELEASE_ASSET_BYTES = 250 * 1024 * 1024
_RUNTIME_SECRET_FIELDS = (
    "adminTokenEnv", "deviceTokenEnv", "uiTokenEnv", "relayUserEnv",
    "relayPasswordEnv", "reverseTunnelCertDerB64Env",
)


def _next(state: str) -> str:
    return {
        "ACCEPTED": "deploy-product-release",
        "REFUSED": "fix_blocking_predicates_then_reissue",
        "UNKNOWN": "read-only-recovery-observation",
        "RECOVERED": "operator-review-before-new-mutation",
        "QUARANTINED": "read-only-observation-or-approved-recovery",
    }.get(state, "none")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PhoneRuntimeRefused("local Release asset bytes are unreadable") from exc
    return digest.hexdigest()


def _download_release_asset(*, url: str, destination: Path, expected_transport_sha256: str, label: str) -> None:
    if not url.startswith(_RELEASE_URL_PREFIX):
        raise PhoneRuntimeRefused(f"{label} Release asset URL differs")
    if _SHA256.fullmatch(expected_transport_sha256) is None:
        raise PhoneRuntimeRefused(f"{label} Release transport digest is invalid")
    request = urllib.request.Request(url, headers={"User-Agent": "mobile-proxy-production-controller-v2"})
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_RELEASE_ASSET_BYTES:
                    raise PhoneRuntimeRefused(f"{label} Release asset exceeds size bound")
                digest.update(chunk)
                handle.write(chunk)
    except PhoneRuntimeRefused:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise PhoneRuntimeRefused(f"{label} Release asset download failed") from exc
    if total <= 0 or not hmac.compare_digest(digest.hexdigest(), expected_transport_sha256):
        raise PhoneRuntimeRefused(f"{label} Release transport digest differs after download")


def _materialize_verified_release_apk(admitted: object, apk: Path, facts: dict[str, object]) -> None:
    try:
        _download_release_asset(
            url=admitted.artifact_download_url, destination=apk,
            expected_transport_sha256=admitted.artifact_transport_sha256, label="Android APK",
        )
    except PhoneRuntimeRefused as exc:
        raise AndroidArtifactRefused(str(exc)) from exc
    artifact_fact = verify_artifact(
        apk=apk,
        expected_sha256=admitted.artifact_transport_sha256,
        expected_version_name=str(admitted.android_version_name),
        expected_version_code=int(admitted.android_version_code or 0),
    )
    artifact_fact["product_content_digest"] = admitted.identity.artifact_digest
    facts["artifact_verification"] = artifact_fact


def _read_runtime_manifest(path: Path) -> tuple[str, Mapping[str, object]]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhoneRuntimeRefused("phone production runtime manifest is unavailable") from exc
    if not isinstance(value, Mapping):
        raise PhoneRuntimeRefused("phone production runtime manifest is not an object")
    if value.get("operatorProfile") not in {"a1_by", "default", "mts_by"}:
        raise PhoneRuntimeRefused("phone production runtime operator profile is unsupported")
    tokens = value.get("tokens")
    if not isinstance(tokens, Mapping):
        raise PhoneRuntimeRefused("phone production runtime token mapping is unavailable")
    for field in _RUNTIME_SECRET_FIELDS:
        name = tokens.get(field)
        if not isinstance(name, str) or not name or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise PhoneRuntimeRefused(f"runtime secret environment mapping is invalid: {field}")
    return raw, value


def _runtime_secret_binding_ids(
    manifest: Mapping[str, object], *, environment: Mapping[str, str], binding_key: str,
) -> dict[str, str]:
    tokens = manifest.get("tokens")
    if not isinstance(tokens, Mapping):
        raise PhoneRuntimeRefused("runtime token mapping is unavailable")
    if len(binding_key) < 32:
        raise PhoneRuntimeRefused("runtime credential binding key is unavailable")
    result: dict[str, str] = {}
    for field in _RUNTIME_SECRET_FIELDS:
        env_name = str(tokens[field])
        secret = environment.get(env_name, "")
        if not secret:
            raise PhoneRuntimeRefused(f"required runtime secret is unavailable: {env_name}")
        digest = hmac.new(
            binding_key.encode("utf-8"), env_name.encode("utf-8") + b"\0" + secret.encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        result[env_name] = "hmac-sha256:" + digest
    return dict(sorted(result.items()))


def _materialize_verified_release_runtime(
    admitted: object, *, archive: Path, work_root: Path, product_root: Path,
    runtime_manifest_path: Path, binding_key: str, facts: dict[str, object],
):
    identity = admitted.identity
    runtime_name = identity.phone_runtime_artifact_name
    runtime_digest = identity.phone_runtime_artifact_digest
    inventory_digest = identity.phone_runtime_inventory_digest
    if (
        not isinstance(runtime_name, str) or not runtime_name
        or not isinstance(runtime_digest, str) or not isinstance(inventory_digest, str)
        or not admitted.phone_runtime_download_url or not admitted.phone_runtime_transport_sha256
    ):
        raise PhoneRuntimeRefused("admitted rooted-phone runtime identity is incomplete")
    _download_release_asset(
        url=str(admitted.phone_runtime_download_url), destination=archive,
        expected_transport_sha256=str(admitted.phone_runtime_transport_sha256), label="rooted-phone runtime",
    )
    materialized = materialize_runtime_bundle(
        archive_path=archive, work_root=work_root,
        expected_transport_sha256=str(admitted.phone_runtime_transport_sha256),
    )
    verify_product_source(product_root=product_root, expected_source_sha=identity.source_sha)
    verify_release_component_digests(
        materialized, product_root=product_root, runtime_archive=archive,
        expected_artifact_name=runtime_name, expected_artifact_digest=runtime_digest,
        expected_inventory_digest=inventory_digest,
    )
    bind_renderer_inputs(materialized, product_root=product_root)
    manifest_json, manifest = _read_runtime_manifest(runtime_manifest_path)
    environment = dict(os.environ)
    rendered = render_required_runtime_configs(
        materialized, product_root=product_root, manifest_json=manifest_json,
        release_id=identity.tag, environment=environment,
    )
    secret_bindings = _runtime_secret_binding_ids(
        manifest, environment=environment, binding_key=binding_key,
    )
    facts["runtime_verification"] = {
        "exact_release_runtime": True,
        "artifact_name": runtime_name,
        "product_content_digest": runtime_digest,
        "transport_sha256": materialized.transport_sha256,
        "component_inventory_digest": inventory_digest,
        "component_count": len(materialized.components),
        "required_live_file_count": len(materialized.required_live_release_paths),
        "derived_files_rendered": list(rendered),
        "renderer_source_sha": identity.source_sha,
        "runtime_manifest_sha256": hashlib.sha256(manifest_json.encode("utf-8")).hexdigest(),
        "secret_binding_ids": secret_bindings,
        "secret_values_recorded": False,
        "vm_provider_access_performed": False,
    }
    return materialized


def _base_state_authorized() -> DeploymentState:
    state = reduce_state(DeploymentState(), "request_received")
    return reduce_state(state, "authorized")


def _unknown_from_existing_intent() -> DeploymentState:
    return DeploymentState(
        state="UNKNOWN", current_step="DISPATCH", intent_persisted=True,
        dispatch_attempted=True, mutation_performed=True, postcondition_verified=False,
        recovery_required=True, recovery_state=None,
        blocking_predicates=("durable_intent_exists_without_terminal",),
    )


def _terminal_payload(
    *, request: dict[str, object], execution_id: str, controller_revision: str,
    admitted: object, deployment_id: int, state: DeploymentState,
    facts: dict[str, object], evidence_refs: list[str],
) -> dict[str, object]:
    identity = admitted.identity
    terminal = DeploymentTerminal(
        operation="deploy-product-release",
        semantic_request_id=str(request["request_id"]), execution_id=execution_id,
        controller_revision=controller_revision, target=str(request["target"]),
        product_release=identity.tag, release_id=identity.release_id,
        release_source_sha=identity.source_sha, artifact_digest=identity.artifact_digest,
        deployment_id=deployment_id, state=state.state, current_step=state.current_step,
        facts=facts, blocking_predicates=state.blocking_predicates,
        mutation_performed=state.mutation_performed, postcondition_verified=state.postcondition_verified,
        recovery_required=state.recovery_required, recovery_state=state.recovery_state,
        next_allowed_operation=_next(state.state), evidence_refs=tuple(evidence_refs),
    ).to_dict()
    validate_terminal(terminal)
    return terminal


def _project_terminal(projection: PublicDeploymentProjection, deployment_id: int, terminal: dict[str, object]) -> None:
    projection.status(
        deployment_id=deployment_id, state=str(terminal["deployment_projection"]),
        description=f"canonical controller terminal: {terminal['state']}",
    )


def _persist_and_write(
    *, evidence: IssueEvidenceStore, projection: PublicDeploymentProjection,
    output: Path, deployment_id: int, terminal: dict[str, object],
) -> None:
    evidence.persist_terminal(terminal)
    _project_terminal(projection, deployment_id, terminal)
    output.write_text(json.dumps(terminal, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _observe_composite(*, serial: str, binding_key: str, admitted: object, materialized: object) -> tuple[object, object]:
    apk = observe(
        serial=serial, binding_key=binding_key,
        expected_version_name=str(admitted.android_version_name),
        expected_version_code=int(admitted.android_version_code or 0),
        expected_artifact_sha256=admitted.artifact_transport_sha256,
    )
    runtime = observe_runtime(
        serial=serial, release_root=materialized.release_root,
        release_id=admitted.identity.tag, required_paths=materialized.required_live_release_paths,
    )
    return apk, runtime


def _composite_fact(apk: object, runtime: object) -> dict[str, object]:
    return {"apk": apk.to_dict(), "runtime": runtime.to_dict(), "desired": bool(apk.desired and runtime.desired), "mode": "read_only"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--admitted-release-json", required=True)
    parser.add_argument("--deployment-id", type=int, required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--controller-revision", required=True)
    parser.add_argument("--recovery-only", choices=("true", "false"), required=True)
    parser.add_argument("--product-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
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
    admitted = resolve_release(tag=str(request["product_release_tag"]), target="phone-production")
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
        args.output.write_text(json.dumps(existing_terminal.payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0

    projection.status(
        deployment_id=args.deployment_id, state="in_progress",
        description=f"{admitted.identity.tag} target execution started",
    )
    source_ref = f"issue-comment:{request['provenance']['comment_id']}"
    evidence_refs = [source_ref]
    facts: dict[str, object] = {
        "release_admission": {
            **durable_release_identity(admitted.identity, target="phone-production"),
            "source_sha": admitted.identity.source_sha,
            "artifact_name": admitted.identity.artifact_name,
            "artifact_transport_sha256": admitted.artifact_transport_sha256,
            "phone_runtime_transport_sha256": admitted.phone_runtime_transport_sha256,
            "manifest_digest": admitted.identity.manifest_digest,
            "provenance_digest": admitted.identity.provenance_digest,
            "immutability_control": admitted.immutability_control,
        }
    }

    if args.recovery_only == "true":
        if existing_intent is None:
            raise SystemExit("recovery-only execution lacks durable mutation intent")
        if not payload_matches_release_identity(existing_intent.payload, admitted.identity, target="phone-production"):
            raise SystemExit("recovery mutation intent conflicts with current immutable Release identity")
        admission = evidence.reusable_admission(str(request["request_id"]))
        if admission is None or not payload_matches_release_identity(admission.payload, admitted.identity, target="phone-production"):
            raise SystemExit("recovery lacks matching full durable deployment admission")
        evidence_refs.extend((admission.ref, existing_intent.ref))
        state = _unknown_from_existing_intent()
        with tempfile.TemporaryDirectory(prefix="mobile-proxy-release-recovery-") as td:
            root = Path(td)
            apk = root / admitted.identity.artifact_name
            runtime_archive = root / str(admitted.identity.phone_runtime_artifact_name)
            try:
                _materialize_verified_release_apk(admitted, apk, facts)
                materialized = _materialize_verified_release_runtime(
                    admitted, archive=runtime_archive, work_root=root / "runtime",
                    product_root=args.product_root, runtime_manifest_path=args.runtime_manifest,
                    binding_key=binding_key, facts=facts,
                )
            except (AndroidArtifactRefused, PhoneRuntimeRefused) as exc:
                facts["recovery_materialization"] = {"available": False}
                state = recover_unknown(state, "recovery_unavailable", reason=str(exc))
            else:
                try:
                    apk_observed, runtime_observed = _observe_composite(
                        serial=serial, binding_key=binding_key, admitted=admitted, materialized=materialized,
                    )
                    facts["recovery_observation"] = _composite_fact(apk_observed, runtime_observed)
                    state = recover_unknown(
                        state, "recovery_observed_desired" if apk_observed.desired and runtime_observed.desired else "recovery_observed_other",
                    )
                except (AndroidObservationUnavailable, PhoneTargetUnavailable) as exc:
                    facts["recovery_observation"] = {"available": False, "mode": "read_only"}
                    state = recover_unknown(state, "recovery_unavailable", reason=str(exc))
        terminal = _terminal_payload(
            request=request, execution_id=args.execution_id, controller_revision=args.controller_revision,
            admitted=admitted, deployment_id=args.deployment_id, state=state, facts=facts, evidence_refs=evidence_refs,
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
        root = Path(td)
        apk = root / admitted.identity.artifact_name
        runtime_archive = root / str(admitted.identity.phone_runtime_artifact_name)
        try:
            _materialize_verified_release_apk(admitted, apk, facts)
            materialized = _materialize_verified_release_runtime(
                admitted, archive=runtime_archive, work_root=root / "runtime",
                product_root=args.product_root, runtime_manifest_path=args.runtime_manifest,
                binding_key=binding_key, facts=facts,
            )
        except (AndroidArtifactRefused, PhoneRuntimeRefused) as exc:
            state = reduce_state(_base_state_authorized(), "observation_refused", reason=str(exc))
            terminal = _terminal_payload(
                request=request, execution_id=args.execution_id, controller_revision=args.controller_revision,
                admitted=admitted, deployment_id=args.deployment_id, state=state, facts=facts, evidence_refs=evidence_refs,
            )
            _persist_and_write(evidence=evidence, projection=projection, output=args.output, deployment_id=args.deployment_id, terminal=terminal)
            return 2

        state = _base_state_authorized()
        try:
            apk_pre, runtime_pre = _observe_composite(
                serial=serial, binding_key=binding_key, admitted=admitted, materialized=materialized,
            )
            facts["preflight_observation"] = _composite_fact(apk_pre, runtime_pre)
        except (AndroidObservationUnavailable, PhoneTargetUnavailable) as exc:
            state = reduce_state(state, "observation_refused", reason=str(exc))
            terminal = _terminal_payload(
                request=request, execution_id=args.execution_id, controller_revision=args.controller_revision,
                admitted=admitted, deployment_id=args.deployment_id, state=state,
                facts=facts | {"preflight_observation": {"available": False, "mode": "read_only"}}, evidence_refs=evidence_refs,
            )
            _persist_and_write(evidence=evidence, projection=projection, output=args.output, deployment_id=args.deployment_id, terminal=terminal)
            return 2

        if not runtime_pre.admissible_for_new_dispatch:
            state = reduce_state(state, "observation_refused", reason="rooted runtime target/current state is not safe for new composite dispatch")
            terminal = _terminal_payload(
                request=request, execution_id=args.execution_id, controller_revision=args.controller_revision,
                admitted=admitted, deployment_id=args.deployment_id, state=state, facts=facts, evidence_refs=evidence_refs,
            )
            _persist_and_write(evidence=evidence, projection=projection, output=args.output, deployment_id=args.deployment_id, terminal=terminal)
            return 2

        if apk_pre.desired and runtime_pre.desired:
            state = reduce_state(state, "already_desired")
            terminal = _terminal_payload(
                request=request, execution_id=args.execution_id, controller_revision=args.controller_revision,
                admitted=admitted, deployment_id=args.deployment_id, state=state, facts=facts, evidence_refs=evidence_refs,
            )
            _persist_and_write(evidence=evidence, projection=projection, output=args.output, deployment_id=args.deployment_id, terminal=terminal)
            return 0

        state = reduce_state(state, "observed")
        intent_payload = {
            "schema": "production-deployment-intent.v2",
            "semantic_request_id": request["request_id"], "execution_id": args.execution_id,
            "controller_revision": args.controller_revision,
            **durable_release_identity(admitted.identity, target="phone-production"),
            "target_binding_id": apk_pre.target_binding_id, "deployment_id": args.deployment_id,
            "dispatch_operation": "android-release-composite-v1",
            "dispatch_may_reach_target": True, "blind_retry_allowed": False, "mutation_performed": False,
            "physical_domains": {"apk": not apk_pre.desired, "runtime": not runtime_pre.desired},
        }
        try:
            intent = evidence.persist_intent(intent_payload)
        except EvidenceError as exc:
            state = reduce_state(state, "intent_persistence_failed", reason=str(exc))
            terminal = _terminal_payload(
                request=request, execution_id=args.execution_id, controller_revision=args.controller_revision,
                admitted=admitted, deployment_id=args.deployment_id, state=state, facts=facts, evidence_refs=evidence_refs,
            )
            _persist_and_write(evidence=evidence, projection=projection, output=args.output, deployment_id=args.deployment_id, terminal=terminal)
            return 2
        evidence_refs.append(intent.ref)
        state = reduce_state(state, "intent_persisted")

        try:
            exact_apk_sha256 = verify_local_artifact_bytes(apk=apk, expected_sha256=admitted.artifact_transport_sha256)
            runtime_transport = _sha256_file(runtime_archive)
            if not hmac.compare_digest(runtime_transport, str(admitted.phone_runtime_transport_sha256)):
                raise PhoneRuntimeRefused("local rooted-phone runtime bytes differ from admitted Release")
            facts["pre_dispatch_artifact_reverification"] = {
                "apk_exact_release_artifact": True, "apk_sha256": exact_apk_sha256,
                "runtime_exact_release_artifact": True, "runtime_sha256": runtime_transport,
            }
        except (AndroidArtifactRefused, PhoneRuntimeRefused) as exc:
            facts["pre_dispatch_artifact_reverification"] = {
                "apk_exact_release_artifact": False, "runtime_exact_release_artifact": False,
            }
            facts["dispatch"] = {
                "attempted_exactly_once": False, "confirmed": False, "outcome_unknown": False,
                "error_class": "LOCAL_COMPOSITE_IDENTITY_REFUSED",
            }
            state = reduce_state(state, "dispatch_refused", reason=str(exc))
            terminal = _terminal_payload(
                request=request, execution_id=args.execution_id, controller_revision=args.controller_revision,
                admitted=admitted, deployment_id=args.deployment_id, state=state, facts=facts, evidence_refs=evidence_refs,
            )
            _persist_and_write(evidence=evidence, projection=projection, output=args.output, deployment_id=args.deployment_id, terminal=terminal)
            return 2

        dispatch = dispatch_release_once(
            serial=serial, apk=apk, release_root=materialized.release_root,
            release_id=admitted.identity.tag, required_paths=materialized.required_live_release_paths,
            install_apk=not apk_pre.desired, install_runtime=not runtime_pre.desired,
        )
        facts["dispatch"] = {
            "operation": "android-release-composite-v1", "attempted_exactly_once": True,
            "apk_domain_requested": not apk_pre.desired, "runtime_domain_requested": not runtime_pre.desired,
            "confirmed": dispatch.confirmed, "outcome_unknown": dispatch.outcome_unknown, "error_class": dispatch.error_class,
        }
        if dispatch.outcome_unknown:
            state = reduce_state(state, "dispatch_outcome_unknown", reason=dispatch.error_class)
        else:
            state = reduce_state(state, "dispatch_confirmed")
            try:
                apk_post, runtime_post = _observe_composite(
                    serial=serial, binding_key=binding_key, admitted=admitted, materialized=materialized,
                )
                facts["postcondition_observation"] = _composite_fact(apk_post, runtime_post)
                state = reduce_state(state, "verify_match" if apk_post.desired and runtime_post.desired else "verify_mismatch")
            except (AndroidObservationUnavailable, PhoneTargetUnavailable) as exc:
                facts["postcondition_observation"] = {"available": False, "mode": "read_only"}
                state = reduce_state(state, "verify_unavailable", reason=str(exc))

        if state.state == "UNKNOWN":
            try:
                apk_recovery, runtime_recovery = _observe_composite(
                    serial=serial, binding_key=binding_key, admitted=admitted, materialized=materialized,
                )
                facts["recovery_observation"] = _composite_fact(apk_recovery, runtime_recovery)
                state = recover_unknown(
                    state, "recovery_observed_desired" if apk_recovery.desired and runtime_recovery.desired else "recovery_observed_other",
                )
            except (AndroidObservationUnavailable, PhoneTargetUnavailable) as exc:
                facts["recovery_observation"] = {"available": False, "mode": "read_only"}
                state = recover_unknown(state, "recovery_unavailable", reason=str(exc))

        terminal = _terminal_payload(
            request=request, execution_id=args.execution_id, controller_revision=args.controller_revision,
            admitted=admitted, deployment_id=args.deployment_id, state=state, facts=facts, evidence_refs=evidence_refs,
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
