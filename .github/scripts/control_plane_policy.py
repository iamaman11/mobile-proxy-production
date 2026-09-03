#!/usr/bin/env python3
"""Hosted static/pure policy checks for the private production control plane.

This module deliberately keeps policy semantics in testable Python while GitHub
Actions workflows remain thin transport wrappers. It never contacts a device,
reads production secrets, or performs mutations.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPTS = ROOT / ".github" / "scripts"


class PolicyFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyFailure(message)


def workflow(name: str) -> str:
    path = WORKFLOWS / name
    require(path.is_file(), f"workflow missing: {name}")
    return path.read_text(encoding="utf-8")


def require_tokens(text: str, tokens: Iterable[str], label: str) -> None:
    missing = [token for token in tokens if token not in text]
    require(not missing, f"{label} missing: {', '.join(missing)}")


def token_position(text: str, token: str, *, start: int = 0) -> int:
    position = text.find(token, start)
    require(position >= 0, f"required token not found: {token}")
    return position


def require_order(text: str, tokens: Iterable[str], label: str) -> None:
    cursor = -1
    for token in tokens:
        position = token_position(text, token, start=cursor + 1)
        require(position > cursor, f"{label} order differs at: {token}")
        cursor = position


def _step_block_by_id(text: str, step_id: str) -> str:
    lines = text.splitlines()
    id_index = next(
        (index for index, line in enumerate(lines) if line.strip() == f"id: {step_id}"),
        None,
    )
    require(id_index is not None, f"step id missing: {step_id}")
    assert id_index is not None
    start = id_index
    while start >= 0 and not lines[start].lstrip().startswith("- name:"):
        start -= 1
    require(start >= 0, f"step name boundary missing for id: {step_id}")
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines):
        stripped = lines[end].lstrip()
        current_indent = len(lines[end]) - len(stripped)
        if current_indent == indent and stripped.startswith("- name:"):
            break
        end += 1
    return "\n".join(lines[start:end])


def _step_block_by_name(text: str, name_fragment: str) -> str:
    lines = text.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.lstrip().startswith("- name:") and name_fragment in line
        ),
        None,
    )
    require(start is not None, f"step missing: {name_fragment}")
    assert start is not None
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines):
        stripped = lines[end].lstrip()
        current_indent = len(lines[end]) - len(stripped)
        if current_indent == indent and stripped.startswith("- name:"):
            break
        end += 1
    return "\n".join(lines[start:end])


def _artifact_identity(block: str, artifact_fragment: str, path_fragment: str) -> tuple[str, str]:
    names = [
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith("name: ") and artifact_fragment in line
    ]
    paths = [
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith("path: ") and path_fragment in line
    ]
    require(len(names) == 1 and len(paths) == 1, "artifact retry identity is ambiguous")
    return names[0], paths[0]


def _assert_three_upload_retries(
    text: str,
    *,
    artifact_fragment: str,
    path_fragment: str,
    label: str,
) -> None:
    blocks = [_step_block_by_id(text, f"evidence-upload-{attempt}") for attempt in (1, 2, 3)]
    identities = []
    for block in blocks:
        require(block.count("uses: actions/upload-artifact@v4") == 1, f"{label}: upload adapter differs")
        require("continue-on-error: true" in block, f"{label}: upload retry must expose transport failure")
        identities.append(_artifact_identity(block, artifact_fragment, path_fragment))
    require(len({name for name, _ in identities}) == 1, f"{label}: artifact name changes across retries")
    require(len({path for _, path in identities}) == 1, f"{label}: artifact path changes across retries")


def _load_module(name: str, path: Path):
    require(path.is_file(), f"Python module missing: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, f"unable to load Python module: {path}")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def policy_phone_preflight() -> None:
    text = workflow("phone-preflight.yml")
    required = (
        "group: production-phone-read-only-preflight",
        "cancel-in-progress: false",
        "runs-on: [self-hosted, Linux, X64, android-production]",
        "ANDROID_PRODUCTION_SERIAL:",
        "secrets.ANDROID_PRODUCTION_SERIAL",
        "ANDROID_TARGET_BINDING_KEY:",
        "secrets.ANDROID_TARGET_BINDING_KEY",
        "id: causal-context",
        "message = b'android-production\\0' + serial.encode('utf-8')",
        "'tb-hmac-sha256:'",
        "--target-binding-id",
        "--session-id",
        "--observation-ref",
        "id: causal-fact-validation",
        "{'scope': 'target/android-production', 'identity': os.environ['TARGET_BINDING_ID']}",
        "{'scope': 'observer/phone-access', 'identity': 'android.phone-access-observer.v2'}",
        "{'scope': 'session/android-production', 'identity': os.environ['SESSION_ID']}",
        "'predicate': 'registered_phone_access_proven'",
        "'authority': 'CONTROL'",
        "'persisted': False",
        "causal_fact_validated=true",
        "id: evidence-upload",
        "id: raw-evidence-state",
        "id: promoted-fact-upload",
        "id: evidence-state",
        "promoted_fact['persisted'] = True",
        "comparison['persisted'] = False",
        "if comparison != raw_fact:",
        "'evidence_type': 'PROMOTED_OBSERVED_FACT'",
        "'promotion_changes_only_persistence': True",
        "phone-access-promoted-fact.json",
    )
    require_tokens(text, required, "phone preflight causal persistence")
    require("production-phone-global-mutation" not in text, "phone preflight must stay outside mutation lock")
    require("--transaction-id" not in text, "reusable phone preflight must not bind destructive transaction")
    require("source/canonical" not in text and "'scope': 'source/" not in text, "Git source must not be a phone-access physical dependency")

    invocation = 'python3 "${RUNNER_TEMP}/phone-preflight-source/run_private_phone_preflight.py"'
    require(text.count(invocation) == 1, "phone preflight producer must execute exactly once")
    for argument in ("--target-binding-id", "--session-id", "--observation-ref"):
        require(text.count(argument) == 1, f"phone preflight producer argument count differs: {argument}")

    require_order(
        text,
        (
            "id: causal-context",
            invocation,
            "id: causal-fact-validation",
            "id: evidence-upload",
            "id: raw-evidence-state",
            "promoted_fact['persisted'] = True",
            "id: promoted-fact-upload",
            "id: evidence-state",
        ),
        "phone preflight causal persistence",
    )

    raw_block = _step_block_by_id(text, "evidence-upload")
    promoted_block = _step_block_by_id(text, "promoted-fact-upload")
    require(raw_block.count("uses: actions/upload-artifact@v4") == 1, "phone preflight raw evidence persistence differs")
    require(promoted_block.count("uses: actions/upload-artifact@v4") == 1, "phone preflight promoted persistence differs")
    require("continue-on-error: true" in raw_block, "phone preflight raw persistence must be classifiable")
    require("continue-on-error: true" in promoted_block, "phone preflight promoted persistence must be classifiable")

    mutators = (
        "android-signing-migration.yml",
        "phone-filesystem-certification.yml",
        "phone-filesystem-quarantine-cleanup.yml",
        "phone-runtime-recovery.yml",
        "phone-runtime-binary-repair.yml",
        "runtime-reconstruction-execution.yml",
    )
    exact_promoted_phone_fact = re.compile(
        r"['\"]evidence_type['\"]\s*:\s*['\"]PROMOTED_OBSERVED_FACT['\"]"
    )
    for name in mutators:
        mutator = workflow(name)
        require("phone-access-observed-fact-" not in mutator, f"{name}: reusable preflight artifact may not replace mutation-boundary proof")
        require("registered_phone_access_proven" not in mutator, f"{name}: reusable phone-access predicate may not replace SAME_TRANSACTION proof")
        require(exact_promoted_phone_fact.search(mutator) is None, f"{name}: reusable phone preflight envelope may not be consumed as mutation-boundary proof")

    def classify(probe_accepted: bool, raw_persisted: bool, promoted_persisted: bool) -> str:
        if probe_accepted and raw_persisted and promoted_persisted:
            return "PHONE_ACCESS_PROVEN"
        if probe_accepted:
            return "PHONE_ACCESS_OBSERVED_UNPERSISTED"
        return "PHONE_ACCESS_UNOBSERVED"

    cases = (
        ((True, True, True), "PHONE_ACCESS_PROVEN"),
        ((True, True, False), "PHONE_ACCESS_OBSERVED_UNPERSISTED"),
        ((True, False, False), "PHONE_ACCESS_OBSERVED_UNPERSISTED"),
        ((False, True, True), "PHONE_ACCESS_UNOBSERVED"),
    )
    for inputs, expected in cases:
        require(classify(*inputs) == expected, f"phone preflight truth table differs: {inputs}")


def policy_quarantine_source_transport() -> None:
    text = workflow("phone-filesystem-quarantine-observation.yml")
    require_tokens(
        text,
        (
            "runs-on: [self-hosted, Linux, X64, android-production]",
            "id: source-download-1",
            "id: source-download-2",
            "id: source-download-3",
            "id: source-transport",
            "id: pre-generation",
            "id: observe-physical",
            "FILESYSTEM_QUARANTINE_UNOBSERVED",
            "FILESYSTEM_QUARANTINE_OBSERVED_UNPERSISTED",
            "FILESYSTEM_QUARANTINE_CLEANUP_ADMISSIBLE",
        ),
        "quarantine source transport",
    )
    require("production-phone-global-mutation" not in text, "read-only quarantine observation must stay outside mutation lock")

    names = []
    paths = []
    for attempt in (1, 2, 3):
        block = _step_block_by_id(text, f"source-download-{attempt}")
        require(block.count("uses: actions/download-artifact@v4") == 1, f"source download attempt {attempt} adapter differs")
        require("continue-on-error: true" in block, f"source download attempt {attempt} must expose failure")
        name, path = _artifact_identity(
            block,
            "phone-filesystem-quarantine-observe-source-",
            f"phone-filesystem-quarantine-observe-source-attempt-{attempt}",
        )
        require("env.CANONICAL_SHA" in name and "github.run_id" in name, "source artifact must bind canonical SHA and exact run")
        names.append(name)
        paths.append(path)
    require(len(set(names)) == 1, "source retries must request one exact artifact")
    require(len(set(paths)) == 3, "source retries must use isolated temporary paths")
    require(text.count("run: sleep 2") == 1 and text.count("run: sleep 5") == 1, "source retry backoff differs")

    second = _step_block_by_id(text, "source-download-2")
    third = _step_block_by_id(text, "source-download-3")
    require("steps.source-download-1.outcome == 'failure'" in second, "source retry 2 must require attempt 1 failure")
    require(
        "steps.source-download-1.outcome == 'failure'" in third
        and "steps.source-download-2.outcome == 'failure'" in third,
        "source retry 3 must require both prior failures",
    )

    promote = _step_block_by_id(text, "source-transport")
    require("continue-on-error: true" not in promote and "if: always()" not in promote, "source transport promotion must fail closed")
    require_tokens(
        promote,
        (
            "SOURCE_DOWNLOAD_1_OUTCOME",
            "SOURCE_DOWNLOAD_2_OUTCOME",
            "SOURCE_DOWNLOAD_3_OUTCOME",
            "exit 1",
            'mv "${selected}" "${destination}"',
        ),
        "source transport promotion",
    )

    transport_position = token_position(text, "id: source-transport")
    verify_position = token_position(text, "manifest = json.loads((root / 'source-manifest.json')", start=transport_position)
    pre_generation_position = token_position(text, "id: pre-generation", start=verify_position)
    operation_position = token_position(text, "id: observe-physical", start=pre_generation_position)
    require(transport_position < verify_position < pre_generation_position < operation_position, "source transport/generation/device order differs")
    require(text.count("'--mode', 'observe'") == 1, "quarantine observation mode must execute exactly once")
    require(text.count("subprocess.run(command, check=True)") == 1, "quarantine observation subprocess must execute exactly once")

    def select_attempt(outcomes: tuple[str, str, str]) -> int | None:
        for index, outcome in enumerate(outcomes, start=1):
            if outcome == "success":
                return index
        return None

    for outcomes, expected in (
        (("success", "skipped", "skipped"), 1),
        (("failure", "success", "skipped"), 2),
        (("failure", "failure", "success"), 3),
        (("failure", "failure", "failure"), None),
    ):
        require(select_attempt(outcomes) == expected, f"source transport truth table differs: {outcomes}")


def policy_quarantine_recovery() -> None:
    observe = workflow("phone-filesystem-quarantine-observation.yml")
    cleanup = workflow("phone-filesystem-quarantine-cleanup.yml")

    require_tokens(
        observe,
        (
            "github.event.comment.user.login == github.repository_owner",
            "startsWith(github.event.comment.body, '/phone-filesystem-quarantine-observe ')",
            "'run_android_filesystem_quarantine_recovery.py'",
            "'run_android_filesystem_certification.py'",
            "'run_private_phone_preflight.py'",
            "'transaction_ids': json.loads(os.environ['TRANSACTION_IDS_JSON'])",
            "'--mode', 'observe'",
            "'android.filesystem-quarantine-observation.v1'",
            "'raw_directory_contents_recorded'",
            "'raw_command_output_recorded'",
            "'raw_device_identifier_recorded'",
            "'phone_mutation_performed'",
            "id: raw-evidence-state",
            "id: post-generation",
            "id: generation-stability",
            "id: promote-facts",
            "id: promoted-facts-upload",
            "FILESYSTEM_QUARANTINE_GENERATION_UNKNOWN",
            "FILESYSTEM_QUARANTINE_GENERATION_INVALID",
            "FILESYSTEM_QUARANTINE_STALE_DURING_OBSERVATION",
        ),
        "quarantine observation",
    )
    require("production-phone-global-mutation" not in observe, "read-only quarantine observation must stay outside global mutation lock")
    require(observe.count("'--mode', 'observe'") == 1, "quarantine observation must execute once")
    require(observe.count("subprocess.run(command, check=True)") == 1, "quarantine observation subprocess must execute once")
    _assert_three_upload_retries(
        observe,
        artifact_fragment="phone-filesystem-quarantine-observation-",
        path_fragment="phone-filesystem-quarantine-observation.json",
        label="quarantine observation",
    )
    require(observe.count("overwrite: true") == 2, "quarantine observation retry overwrite count differs")
    require(observe.count("run: sleep 3") == 1 and observe.count("run: sleep 6") == 1, "quarantine observation evidence backoff differs")

    require_tokens(
        cleanup,
        (
            "github.event.comment.user.login == github.repository_owner",
            "startsWith(github.event.comment.body, '/phone-filesystem-quarantine-clean ')",
            "observation_run_id = int(tokens[2])",
            "observation.get('name') != 'Production Android filesystem quarantine observation'",
            "artifact.get('expired') is not False",
            "raw_pattern = re.compile(",
            "promoted_pattern = re.compile(",
            "observation_source_sha",
            "admission-observation.json",
            "admission-promoted-facts.json",
            "'evidence_type') != 'PROMOTED_OBSERVED_FACT_SET'",
            "id: current-generation",
            "id: causal-admission",
            "control.classify_observed_fact(",
            "group: production-phone-global-mutation",
            "cancel-in-progress: false",
            "queue: max",
            "'--mode', 'cleanup'",
            "UNKNOWN_EXECUTION_OUTCOME",
        ),
        "quarantine cleanup",
    )
    require("observation.get('head_sha')" not in cleanup, "cleanup admission must not depend on observation Git/private SHA")
    require(cleanup.count("group: production-phone-global-mutation") == 1, "cleanup must have one global mutation lock")
    require(cleanup.count("'--mode', 'cleanup'") == 1, "quarantine cleanup must execute once")
    require(cleanup.count("subprocess.run(command, check=True)") == 1, "quarantine cleanup subprocess must execute once")
    _assert_three_upload_retries(
        cleanup,
        artifact_fragment="phone-filesystem-quarantine-cleanup-",
        path_fragment="phone-filesystem-quarantine-cleanup.json",
        label="quarantine cleanup",
    )
    require(cleanup.count("overwrite: true") == 2, "quarantine cleanup retry overwrite count differs")
    require(cleanup.count("run: sleep 3") == 1 and cleanup.count("run: sleep 6") == 1, "quarantine cleanup evidence backoff differs")

    def persistence_accepted(result_ready: bool, outcomes: tuple[str, str, str]) -> bool:
        return result_ready and any(outcome == "success" for outcome in outcomes)

    for inputs, expected in (
        ((True, ("success", "skipped", "skipped")), True),
        ((True, ("failure", "success", "skipped")), True),
        ((True, ("failure", "failure", "success")), True),
        ((True, ("failure", "failure", "failure")), False),
        ((False, ("success", "skipped", "skipped")), False),
    ):
        require(persistence_accepted(*inputs) is expected, f"quarantine persistence truth table differs: {inputs}")

    def classify_cleanup(validated: bool, persisted: bool, state: str, accepted: bool, verified: bool) -> str:
        if not validated:
            return "FILESYSTEM_QUARANTINE_CLEANUP_UNOBSERVED"
        if not persisted:
            return "FILESYSTEM_QUARANTINE_CLEANUP_OBSERVED_UNPERSISTED"
        if state == "CLEANED" and accepted and verified:
            return "FILESYSTEM_QUARANTINE_CLEANUP_PROVEN"
        if state == "ALREADY_CLEAN" and accepted and verified:
            return "FILESYSTEM_QUARANTINE_ALREADY_CLEAN"
        if state == "REFUSED" and not accepted:
            return "FILESYSTEM_QUARANTINE_CLEANUP_REFUSED"
        if state == "QUARANTINED" and not accepted:
            return "FILESYSTEM_QUARANTINE_CLEANUP_QUARANTINED"
        return "FILESYSTEM_QUARANTINE_CLEANUP_INVALID"

    for inputs, expected in (
        ((False, False, "UNOBSERVED", False, False), "FILESYSTEM_QUARANTINE_CLEANUP_UNOBSERVED"),
        ((True, False, "CLEANED", True, True), "FILESYSTEM_QUARANTINE_CLEANUP_OBSERVED_UNPERSISTED"),
        ((True, True, "CLEANED", True, True), "FILESYSTEM_QUARANTINE_CLEANUP_PROVEN"),
        ((True, True, "ALREADY_CLEAN", True, True), "FILESYSTEM_QUARANTINE_ALREADY_CLEAN"),
        ((True, True, "REFUSED", False, False), "FILESYSTEM_QUARANTINE_CLEANUP_REFUSED"),
        ((True, True, "QUARANTINED", False, False), "FILESYSTEM_QUARANTINE_CLEANUP_QUARANTINED"),
    ):
        require(classify_cleanup(*inputs) == expected, f"quarantine cleanup truth table differs: {inputs}")


def policy_dispatch_intent() -> None:
    secret_wiring = "ANDROID_TARGET_BINDING_KEY: ${{ secrets.ANDROID_TARGET_BINDING_KEY }}"
    contracts = {
        "phone-filesystem-certification.yml": {
            "operation": "android.filesystem-certification.v1",
            "transaction_source": "f\"fs-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}\"",
            "adapter": "run_android_filesystem_certification.py",
            "adapter_step": "Run bounded filesystem mutation transaction",
            "admission_anchor": "Verify source bundle before device mutation",
            "unknown_classifier": "markerPersisted && state === 'UNOBSERVED'",
        },
        "phone-filesystem-quarantine-cleanup.yml": {
            "operation": "android.filesystem-quarantine-cleanup.v1",
            "transaction_source": "f\"fs-quarantine-clean-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}\"",
            "adapter": "run_android_filesystem_quarantine_recovery.py",
            "adapter_step": "Run exact bounded quarantine cleanup",
            "admission_anchor": "id: causal-admission",
            "unknown_classifier": "markerPersisted && !validated",
        },
    }

    for name, contract in contracts.items():
        text = workflow(name)
        require_tokens(
            text,
            (
                "group: production-phone-global-mutation",
                "cancel-in-progress: false",
                "queue: max",
                secret_wiring,
                "Build bounded CONTROL mutation dispatch intent",
                "Persist CONTROL mutation dispatch intent before adapter invocation",
                "'evidence_type': 'MUTATION_DISPATCH_INTENT'",
                "'authority': 'CONTROL'",
                "'lifecycle': 'CURRENT'",
                f"'operation_id': '{contract['operation']}'",
                "'target': 'android-production'",
                "'affected_domain_generations': {'domain/filesystem': operation_transaction_id}",
                "'blind_retry_allowed': False",
                "'raw_device_identifier_recorded': False",
                "hmac.new(binding_key.encode('utf-8'), message, hashlib.sha256).hexdigest()",
                "if binding_key == serial:",
                "if-no-files-found: error",
                "retention-days: 90",
                str(contract["adapter_step"]),
                str(contract["adapter"]),
                "classification = 'UNKNOWN_EXECUTION_OUTCOME'",
                str(contract["unknown_classifier"]),
                str(contract["transaction_source"]),
                str(contract["admission_anchor"]),
            ),
            f"{name} dispatch intent",
        )
        require(text.count(secret_wiring) == 1, f"{name}: target binding key must enter one serialized job")
        require(text.count("Build bounded CONTROL mutation dispatch intent") == 1, f"{name}: dispatch intent build count differs")
        require(text.count("Persist CONTROL mutation dispatch intent before adapter invocation") == 1, f"{name}: dispatch intent persistence count differs")
        require(text.count("'affected_domain_generations': {'domain/filesystem': operation_transaction_id}") == 1, f"{name}: filesystem generation transition count differs")
        require(text.count(str(contract["adapter"])) >= 2, f"{name}: canonical adapter must be bundled and invoked")

        admission = token_position(text, str(contract["admission_anchor"]))
        build = token_position(text, "Build bounded CONTROL mutation dispatch intent", start=admission)
        persist = token_position(text, "Persist CONTROL mutation dispatch intent before adapter invocation", start=build)
        adapter = token_position(text, str(contract["adapter_step"]), start=persist)
        require(admission < build < persist < adapter, f"{name}: admission -> intent -> persistence -> adapter order differs")

        persist_section = text[persist:adapter]
        require("continue-on-error: true" not in persist_section and "if: always()" not in persist_section, f"{name}: dispatch intent persistence must fail closed")
        require("uses: actions/upload-artifact@v4" in persist_section, f"{name}: dispatch intent must persist durably")

        adapter_block = _step_block_by_name(text, str(contract["adapter_step"]))
        require(adapter_block.count(str(contract["adapter"])) == 1, f"{name}: destructive adapter must execute exactly once")
        for retry_token in ("attempt 2", "attempt 3", "Back off before", "sleep 3", "sleep 6"):
            require(retry_token not in adapter_block, f"{name}: destructive adapter block must not retry")

        require("target_binding_id" in text and "tb-hmac-sha256:" in text, f"{name}: opaque target binding missing")
        require("markerPersisted ? process.env.OPERATION_TRANSACTION_ID : 'unadvanced'" in text, f"{name}: report must expose persisted filesystem generation")
        require("blind_retry_allowed" in text and "false" in text, f"{name}: blind retry prohibition missing")

    def classify(marker_persisted: bool, state: str, validated: bool = True, accepted: bool = False, recovered: bool = False) -> str:
        if accepted:
            return "ACCEPTED"
        if recovered:
            return "RECOVERED"
        if marker_persisted and (state == "UNOBSERVED" or not validated):
            return "UNKNOWN_EXECUTION_OUTCOME"
        if not validated:
            return "UNOBSERVED"
        return state

    for inputs, expected in (
        ((True, "UNOBSERVED", True, False, False), "UNKNOWN_EXECUTION_OUTCOME"),
        ((True, "UNOBSERVED", False, False, False), "UNKNOWN_EXECUTION_OUTCOME"),
        ((False, "UNOBSERVED", False, False, False), "UNOBSERVED"),
        ((True, "REFUSED", True, False, False), "REFUSED"),
        ((True, "ACCEPTED", True, True, False), "ACCEPTED"),
        ((True, "RECOVERED", True, False, True), "RECOVERED"),
    ):
        require(classify(*inputs) == expected, f"dispatch ambiguity truth table differs: {inputs}")


def _trusted_comment(body: str, login: str = "github-actions[bot]") -> dict[str, Any]:
    return {"body": body, "user": {"login": login}}


def policy_filesystem_causal_integration(canonical_root: Path) -> None:
    adapter = _load_module("filesystem_generation_inventory_policy", SCRIPTS / "filesystem_generation_inventory.py")
    canonical_scripts = canonical_root / "scripts"
    generation = _load_module("physical_domain_generation_policy", canonical_scripts / "physical_domain_generation.py")
    control = _load_module("control_state_machine_policy", canonical_scripts / "control_state_machine.py")
    recovery = _load_module(
        "run_android_filesystem_quarantine_recovery_policy",
        canonical_scripts / "run_android_filesystem_quarantine_recovery.py",
    )

    sha_a = "1" * 40
    sha_b = "2" * 40
    target_binding = "tb-hmac-sha256:" + "a" * 64

    def journal(run_id: int, attempt: int = 1, *, login: str = "github-actions[bot]", extra: dict[str, Any] | None = None):
        transaction_id = f"fs-{run_id}-{attempt}"
        payload: dict[str, Any] = {
            "format_version": 1,
            "journal_type": "FILESYSTEM_MUTATION_INTENT",
            "evidence_type": "MUTATION_DISPATCH_INTENT",
            "authority": "CONTROL",
            "lifecycle": "CURRENT",
            "private_repository": "iamaman11/mobile-proxy-production",
            "private_sha": sha_a,
            "canonical_repository": "iamaman11/mobile-proxy",
            "canonical_sha": sha_a,
            "canonical_quality_run_id": 1000 + run_id,
            "workflow_run_id": run_id,
            "workflow_run_attempt": attempt,
            "operation_id": "android.filesystem-certification.v1",
            "operation_transaction_id": transaction_id,
            "target": "android-production",
            "target_binding_id": target_binding,
            "affected_domain_generations": {"domain/filesystem": transaction_id},
            "dispatch_intent_artifact_persisted": True,
            "journaled_before_adapter_invocation": True,
            "dispatch_may_reach_target": True,
            "blind_retry_allowed": False,
            "raw_device_identifier_recorded": False,
        }
        if extra:
            payload.update(extra)
        body = (
            "## CONTROL FILESYSTEM MUTATION INTENT\n\n```json\n"
            + json.dumps(payload, separators=(",", ":"), sort_keys=True)
            + "\n```"
        )
        return _trusted_comment(body, login)

    def terminal_result(run_id: int, attempt: int = 1, *, login: str = "github-actions[bot]"):
        transaction_id = f"fs-{run_id}-{attempt}"
        lines = [
            "## CONTROL ANDROID FILESYSTEM MUTATION RESULT",
            "- classification: **FILESYSTEM_MUTATION_PROVEN**",
            f"- canonical_sha: `{sha_a}`",
            f"- quality_run_id: `{1000 + run_id}`",
            f"- workflow_run_id: `{run_id}`",
            f"- operation_transaction_id: `{transaction_id}`",
            "- dispatch_intent_persisted: `true`",
            f"- domain/filesystem_generation: `{transaction_id}`",
            "- blind_retry_allowed: `false`",
            "- transaction_state: `ACCEPTED`",
        ]
        return _trusted_comment("\n".join(lines), login)

    empty_inventory = adapter.build_inventory([])
    empty_resolution = generation.resolve_inventory(empty_inventory)
    require(empty_resolution.state == generation.RESOLVED, "empty trusted inventory must resolve")
    require(empty_resolution.generation == generation.FILESYSTEM_BOOTSTRAP_GENERATION, "empty inventory must use stable bootstrap generation")
    require(empty_resolution.source == generation.BOOTSTRAP, "bootstrap source classification differs")

    completed_inventory = adapter.build_inventory([journal(10), terminal_result(10)])
    completed_resolution = generation.resolve_inventory(completed_inventory)
    require(completed_resolution.state == generation.RESOLVED, "completed mutation intent must resolve")
    require(completed_resolution.generation == "fs-10-1", "completed mutation must own filesystem generation")

    latest_inventory = adapter.build_inventory(
        [journal(10), terminal_result(10), journal(11), terminal_result(11)]
    )
    latest_resolution = generation.resolve_inventory(latest_inventory)
    require(latest_resolution.generation == "fs-11-1", "latest completed intent must own filesystem generation")

    unresolved_inventory = adapter.build_inventory([journal(12)])
    unresolved_resolution = generation.resolve_inventory(unresolved_inventory)
    require(
        unresolved_resolution.state == generation.UNKNOWN_EXECUTION_OUTCOME,
        "any unresolved persisted intent must block causal reuse",
    )
    require("fs-12-1" in unresolved_resolution.unresolved_transaction_ids, "unresolved transaction identity missing")

    untrusted_inventory = adapter.build_inventory([journal(13, login="iamaman11")])
    require(untrusted_inventory["intents"] == [], "untrusted author must not create causal generation evidence")

    try:
        adapter.build_inventory([terminal_result(14)])
    except adapter.EvidenceAdapterError:
        pass
    else:
        raise PolicyFailure("trusted Stage-B result without exact journal pair must fail closed")

    try:
        adapter.build_inventory([journal(15, extra={"unexpected": True})])
    except adapter.EvidenceAdapterError:
        pass
    else:
        raise PolicyFailure("trusted journal schema drift must fail closed")

    legacy_body = "\n".join(
        [
            "## CONTROL ANDROID FILESYSTEM MUTATION RESULT",
            "- classification: **FILESYSTEM_MUTATION_PROVEN**",
            f"- canonical_sha: `{sha_a}`",
            "- quality_run_id: `1`",
            "- workflow_run_id: `16`",
            "- transaction_state: `ACCEPTED`",
        ]
    )
    legacy_inventory = adapter.build_inventory([_trusted_comment(legacy_body)])
    require(legacy_inventory["intents"] == [], "legacy pre-Stage-B result must not be retrofitted into causal intent")

    report = {
        "observation_complete": True,
        "cleanup_admissible": True,
        "transactions": [
            {
                "transaction_id": "fs-11-1",
                "scratch": {"node_state": "DIRECTORY"},
                "managed_root": {"node_state": "ABSENT"},
            }
        ],
    }
    raw_facts = recovery.build_quarantine_fact_envelopes(
        sha_a,
        ["fs-11-1"],
        report,
        target_binding_id=target_binding,
        filesystem_generation="fs-11-1",
        observation_ref="filesystem-quarantine-observation:100:1",
    )
    require(len(raw_facts) == 2, "complete quarantine observation must emit exactly two facts")
    expected_dependencies = [
        {"scope": "target/android-production", "identity": target_binding},
        {"scope": "observer/filesystem-quarantine", "identity": "android.filesystem-quarantine-observer.v2"},
        {"scope": "domain/filesystem", "identity": "fs-11-1"},
        {"scope": "transaction/fs-11-1", "identity": "fs-11-1"},
    ]
    for fact in raw_facts:
        require(fact["authority"] == "CONTROL", "quarantine raw fact authority differs")
        require(fact["persisted"] is False, "canonical observer must emit unpersisted facts")
        require(fact["dependencies"] == expected_dependencies, "quarantine fact causal dependencies differ")
        require(not any(item["scope"].startswith("source/") for item in fact["dependencies"]), "Git source must not be a physical quarantine dependency")

    promoted_facts = []
    for raw_fact in raw_facts:
        promoted = copy.deepcopy(raw_fact)
        promoted["persisted"] = True
        comparison = copy.deepcopy(promoted)
        comparison["persisted"] = False
        require(comparison == raw_fact, "promotion must change only persistence")
        promoted_facts.append(promoted)

    def observed_fact(payload: dict[str, Any]):
        dependencies = tuple(
            control.FactDependency(item["scope"], item["identity"])
            for item in payload["dependencies"]
        )
        return control.ObservedFact(
            subject=payload["subject"],
            predicate=payload["predicate"],
            value=payload["value"],
            target=payload["target"],
            observation_ref=payload["observation_ref"],
            source_ref=payload["source_ref"],
            dependencies=dependencies,
            authority=payload["authority"],
            persisted=payload["persisted"],
        )

    stable_context = {
        "target/android-production": target_binding,
        "observer/filesystem-quarantine": "android.filesystem-quarantine-observer.v2",
        "domain/filesystem": "fs-11-1",
        "transaction/fs-11-1": "fs-11-1",
        "source/canonical": sha_b,
    }
    required_scopes = (
        "target/android-production",
        "observer/filesystem-quarantine",
        "domain/filesystem",
        "transaction/fs-11-1",
    )
    for payload in promoted_facts:
        validity = control.classify_observed_fact(
            observed_fact(payload),
            stable_context,
            required_scopes=required_scopes,
        )
        require(validity.state == control.FACT_VALID, "unrelated Git/source change must not stale physical quarantine fact")

    changed_context = dict(stable_context)
    changed_context["domain/filesystem"] = "fs-12-1"
    for payload in promoted_facts:
        validity = control.classify_observed_fact(
            observed_fact(payload),
            changed_context,
            required_scopes=required_scopes,
        )
        require(validity.state != control.FACT_VALID, "filesystem mutation generation change must invalidate prior quarantine fact")

    observe = workflow("phone-filesystem-quarantine-observation.yml")
    cleanup = workflow("phone-filesystem-quarantine-cleanup.yml")
    require_tokens(
        observe,
        (
            "'physical_domain_generation.py'",
            "filesystem_generation_inventory.py",
            "id: pre-history",
            "id: pre-generation",
            "Require resolved filesystem generation before device access",
            "id: causal-context",
            "id: observe-physical",
            "id: raw-evidence-state",
            "id: post-history",
            "id: post-generation",
            "id: generation-stability",
            "PRE_STATE: ${{ steps.pre-generation.outputs.state }}",
            "POST_STATE: ${{ steps.post-generation.outputs.state }}",
            '[ "${PRE_GENERATION}" = "${POST_GENERATION}" ]',
            "id: promote-facts",
            "comparison['persisted'] = False",
            "if comparison != raw_fact:",
            "id: promoted-facts-upload",
            "phone-filesystem-quarantine-observed-facts-",
        ),
        "Stage C.0b.3 observation integration",
    )
    require(observe.count("'--mode', 'observe'") == 1, "Stage C.0b.3 observer producer must execute exactly once")
    require(observe.count("subprocess.run(command, check=True)") == 1, "Stage C.0b.3 observer subprocess must execute exactly once")
    require_order(
        observe,
        (
            "id: pre-history",
            "id: pre-generation",
            "Require resolved filesystem generation before device access",
            "id: causal-context",
            "id: observe-physical",
            "id: raw-evidence-state",
            "id: post-history",
            "id: post-generation",
            "id: generation-stability",
            "id: promote-facts",
            "id: promoted-facts-upload",
        ),
        "Stage C.0b.3 pre/post generation and promotion",
    )
    require("source/canonical" not in _step_block_by_id(observe, "causal-context"), "quarantine causal context must not add Git source dependency")

    require_tokens(
        cleanup,
        (
            "phone-filesystem-quarantine-observed-facts-",
            "admission-promoted-facts.json",
            "id: current-history",
            "id: current-generation",
            "id: causal-admission",
            "control.classify_observed_fact(",
            "'domain/filesystem': os.environ['CURRENT_FILESYSTEM_GENERATION']",
            "id: dispatch-intent",
        ),
        "Stage C.0b.3 cleanup causal admission",
    )
    require("observation.get('head_sha')" not in cleanup, "unrelated private Git revision must not invalidate causal quarantine facts")
    require_order(
        cleanup,
        (
            "id: current-history",
            "id: current-generation",
            "id: causal-admission",
            "id: dispatch-intent",
            "Persist CONTROL mutation dispatch intent before adapter invocation",
            "Run exact bounded quarantine cleanup",
        ),
        "Stage C.0b.3 cleanup admission/dispatch",
    )

    def promotion_allowed(raw_persisted: bool, pre_state: str, pre_generation: str, post_state: str, post_generation: str) -> bool:
        return (
            raw_persisted
            and pre_state == "RESOLVED"
            and post_state == "RESOLVED"
            and bool(pre_generation)
            and pre_generation == post_generation
        )

    require(promotion_allowed(True, "RESOLVED", "fs-11-1", "RESOLVED", "fs-11-1"), "stable pre/post generation must allow promotion")
    require(not promotion_allowed(True, "RESOLVED", "fs-11-1", "RESOLVED", "fs-12-1"), "mutation during observation must block promotion")
    require(not promotion_allowed(True, "RESOLVED", "fs-11-1", "UNKNOWN_EXECUTION_OUTCOME", "fs-12-1"), "unresolved post-observation intent must block promotion")
    require(not promotion_allowed(False, "RESOLVED", "fs-11-1", "RESOLVED", "fs-11-1"), "raw persistence failure must block promotion")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--policy",
        required=True,
        choices=(
            "phone-preflight",
            "quarantine-source-transport",
            "quarantine-recovery",
            "filesystem-dispatch-intent",
            "filesystem-causal-integration",
        ),
    )
    parser.add_argument("--canonical-root", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.policy == "phone-preflight":
            policy_phone_preflight()
        elif args.policy == "quarantine-source-transport":
            policy_quarantine_source_transport()
        elif args.policy == "quarantine-recovery":
            policy_quarantine_recovery()
        elif args.policy == "filesystem-dispatch-intent":
            policy_dispatch_intent()
        elif args.policy == "filesystem-causal-integration":
            require(args.canonical_root is not None, "--canonical-root is required for causal integration policy")
            policy_filesystem_causal_integration(args.canonical_root.resolve())
        else:  # pragma: no cover
            raise PolicyFailure(f"unsupported policy: {args.policy}")
    except (PolicyFailure, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        print(f"control-plane policy failed: {error}", file=sys.stderr)
        return 1
    print(f"control_plane_policy={args.policy}:accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
