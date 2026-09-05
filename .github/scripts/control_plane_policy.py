#!/usr/bin/env python3
"""Hosted static/pure policy checks for the private production control plane.

This module deliberately keeps policy semantics in testable Python while GitHub
Actions workflows remain thin transport wrappers. It never contacts a device,
reads production secrets, or performs mutations.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


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
            "inputs.source_actor == github.repository_owner",
            "startsWith(inputs.command, '/phone-filesystem-quarantine-observe ')",
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
            "inputs.source_actor == github.repository_owner",
            "startsWith(inputs.command, '/phone-filesystem-quarantine-clean ')",
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
        ),
    )
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
        else:  # pragma: no cover
            raise PolicyFailure(f"unsupported policy: {args.policy}")
    except (PolicyFailure, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        print(f"control-plane policy failed: {error}", file=sys.stderr)
        return 1
    print(f"control_plane_policy={args.policy}:accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
