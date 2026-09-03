#!/usr/bin/env python3
"""Private execution-plane seam for canonical android.apk-install.v1 transactions.

This module owns private GitHub evidence/persistence and transport integration only.
Canonical reducers, transaction ordering, APK dispatch, and postcondition semantics
remain in the exact digest-verified public source bundle.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Protocol
import urllib.error
import urllib.parse
import urllib.request

PRIVATE_REPOSITORY = "iamaman11/mobile-proxy-production"
CANONICAL_REPOSITORY = "iamaman11/mobile-proxy"
ISSUE_NUMBER = 1
TARGET = "android-production"
OPERATION_ID = "android.apk-install.v1"
GLOBAL_MUTATION_GROUP = "production-phone-global-mutation"
PHONE_OBSERVER = "android.phone-access-observer.v2"
TRUSTED_EVIDENCE_ACTOR = "github-actions[bot]"

BOUNDARY_HEADING = "## CONTROL APK SAME_TRANSACTION BOUNDARY"
INTENT_HEADING = "## CONTROL APK MUTATION INTENT"
TERMINAL_HEADING = "## CONTROL APK TRANSACTION TERMINAL"

_SHA = re.compile(r"^[0-9a-f]{40}$")
_TYPED_ARTIFACT = re.compile(r"^b3:[0-9a-f]{64}$")
_TRANSACTION = re.compile(r"^apk-install-[1-9][0-9]{0,19}$")
_TARGET_BINDING = re.compile(r"^tb-hmac-sha256:[0-9a-f]{64}$")
_OPAQUE_REF = re.compile(r"^[!-~]{1,240}$")

_REQUIRED_CANONICAL = (
    "control_state_machine.py",
    "operation_state_machine.py",
    "transaction_runner.py",
    "operations/install_apk.py",
    "run_private_phone_preflight.py",
)


class IntegrationFailure(RuntimeError):
    """Fail-closed private integration error."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise IntegrationFailure(message)


def _non_empty_ref(value: Any, label: str) -> str:
    require(isinstance(value, str), f"{label} must be text")
    normalized = value.strip()
    require(_OPAQUE_REF.fullmatch(normalized) is not None, f"{label} is invalid")
    require(not any(character.isspace() for character in normalized), f"{label} contains whitespace")
    return normalized


def stable_transaction_id(owner_command_id: int) -> str:
    """Bind one destructive identity to the immutable owner command, never to rerun attempt."""
    require(isinstance(owner_command_id, int) and not isinstance(owner_command_id, bool), "owner command id must be an integer")
    require(0 < owner_command_id <= 9_999_999_999_999_999_999, "owner command id is out of range")
    value = f"apk-install-{owner_command_id}"
    require(_TRANSACTION.fullmatch(value) is not None, "derived APK transaction id is invalid")
    return value


@dataclass(frozen=True)
class CanonicalBundle:
    root: Path
    scripts_root: Path
    canonical_sha: str
    quality_run_id: int

    @classmethod
    def load(
        cls,
        root: Path,
        *,
        expected_sha: str,
        expected_quality_run_id: int,
    ) -> "CanonicalBundle":
        root = root.resolve()
        require(_SHA.fullmatch(expected_sha) is not None, "expected canonical SHA is invalid")
        require(expected_quality_run_id > 0, "expected Quality run ID is invalid")
        manifest_path = root / "source-manifest.json"
        require(manifest_path.is_file(), "canonical source manifest is missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        require(isinstance(manifest, dict), "canonical source manifest must be an object")
        require(
            set(manifest)
            == {
                "format_version",
                "repository",
                "canonical_sha",
                "quality_run_id",
                "scripts_sha256",
            },
            "canonical source manifest schema differs",
        )
        require(manifest["format_version"] == 1, "canonical source manifest version differs")
        require(manifest["repository"] == CANONICAL_REPOSITORY, "canonical source repository differs")
        require(manifest["canonical_sha"] == expected_sha, "canonical source SHA differs")
        require(manifest["quality_run_id"] == expected_quality_run_id, "canonical Quality authority differs")
        digests = manifest["scripts_sha256"]
        require(isinstance(digests, dict), "canonical source digest map is invalid")
        require(set(digests) == set(_REQUIRED_CANONICAL), "canonical source script set differs")

        scripts_root = root / "scripts"
        require(scripts_root.is_dir(), "canonical scripts root is missing")
        for relative in _REQUIRED_CANONICAL:
            require(
                isinstance(digests[relative], str)
                and re.fullmatch(r"[0-9a-f]{64}", digests[relative]) is not None,
                f"canonical source digest is invalid: {relative}",
            )
            path = scripts_root / relative
            require(path.is_file(), f"canonical source file is missing: {relative}")
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
            require(
                hmac.compare_digest(observed, digests[relative]),
                f"canonical source digest differs: {relative}",
            )
        return cls(root, scripts_root, expected_sha, expected_quality_run_id)

    def load_modules(self) -> "CanonicalModules":
        scripts = str(self.scripts_root)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        importlib.invalidate_caches()
        for name in (
            "operations.install_apk",
            "operations",
            "transaction_runner",
            "operation_state_machine",
            "control_state_machine",
        ):
            sys.modules.pop(name, None)
        control = importlib.import_module("control_state_machine")
        operation = importlib.import_module("operation_state_machine")
        transaction = importlib.import_module("transaction_runner")
        apk = importlib.import_module("operations.install_apk")
        return CanonicalModules(control, operation, transaction, apk)


@dataclass(frozen=True)
class CanonicalModules:
    control: Any
    operation: Any
    transaction: Any
    apk: Any


@dataclass(frozen=True)
class AuthorityEnvelope:
    canonical_sha: str
    quality_run_id: int
    artifact_ref: str
    same_lineage_compatible: bool
    compatibility_ref: str

    @classmethod
    def load(cls, path: Path) -> "AuthorityEnvelope":
        payload = json.loads(path.read_text(encoding="utf-8"))
        require(isinstance(payload, dict), "APK authority envelope must be an object")
        require(
            set(payload)
            == {
                "format_version",
                "canonical_repository",
                "canonical_sha",
                "canonical_quality_run_id",
                "artifact_ref",
                "same_lineage_compatible",
                "compatibility_ref",
            },
            "APK authority envelope schema differs",
        )
        require(payload["format_version"] == 1, "APK authority envelope version differs")
        require(payload["canonical_repository"] == CANONICAL_REPOSITORY, "APK authority canonical repository differs")
        canonical_sha = payload["canonical_sha"]
        require(isinstance(canonical_sha, str) and _SHA.fullmatch(canonical_sha) is not None, "APK authority canonical SHA is invalid")
        quality_run_id = payload["canonical_quality_run_id"]
        require(isinstance(quality_run_id, int) and not isinstance(quality_run_id, bool) and quality_run_id > 0, "APK authority Quality run ID is invalid")
        artifact_ref = payload["artifact_ref"]
        require(isinstance(artifact_ref, str) and _TYPED_ARTIFACT.fullmatch(artifact_ref) is not None, "APK authority artifact ref is invalid")
        compatible = payload["same_lineage_compatible"]
        require(isinstance(compatible, bool), "APK compatibility admission must be boolean")
        compatibility_ref = payload["compatibility_ref"]
        require(isinstance(compatibility_ref, str), "APK compatibility reference must be text")
        compatibility_ref = compatibility_ref.strip()
        if compatible:
            compatibility_ref = _non_empty_ref(compatibility_ref, "APK compatibility reference")
        else:
            require(compatibility_ref == "", "incompatible APK authority must not claim compatibility evidence")
        return cls(
            canonical_sha=canonical_sha,
            quality_run_id=quality_run_id,
            artifact_ref=artifact_ref,
            same_lineage_compatible=compatible,
            compatibility_ref=compatibility_ref,
        )


@dataclass(frozen=True)
class OuterMutationScopeProof:
    group: str
    cancel_in_progress: bool
    queue: str

    @classmethod
    def from_environment(cls) -> "OuterMutationScopeProof":
        cancel = os.environ.get("PRODUCTION_PHONE_MUTATION_CANCEL_IN_PROGRESS", "")
        require(cancel in {"true", "false"}, "outer mutation cancel policy is unavailable")
        return cls(
            group=os.environ.get("PRODUCTION_PHONE_MUTATION_SCOPE", ""),
            cancel_in_progress=cancel == "true",
            queue=os.environ.get("PRODUCTION_PHONE_MUTATION_QUEUE", ""),
        )

    def validate(self) -> None:
        require(self.group == GLOBAL_MUTATION_GROUP, "outer mutation scope is not the global production-phone lock")
        require(self.cancel_in_progress is False, "outer mutation scope must not cancel in-progress mutations")
        require(self.queue == "max", "outer mutation scope must preserve the max queue")


class IssueStore(Protocol):
    def comments(self) -> list[Mapping[str, Any]]: ...
    def create_comment(self, body: str) -> int: ...


@dataclass
class GitHubIssueStore:
    token: str
    repository: str = PRIVATE_REPOSITORY
    issue_number: int = ISSUE_NUMBER

    def _request(self, method: str, url: str, payload: Mapping[str, Any] | None = None) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "mobile-proxy-apk-transaction-control",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = None
        if payload is not None:
            data = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as error:
            raise IntegrationFailure("GitHub evidence store request failed") from error

    def comments(self) -> list[Mapping[str, Any]]:
        require(bool(self.token), "GitHub evidence store token is unavailable")
        owner, repo = self.repository.split("/", 1)
        result: list[Mapping[str, Any]] = []
        page = 1
        while True:
            query = urllib.parse.urlencode({"per_page": 100, "page": page})
            url = f"https://api.github.com/repos/{owner}/{repo}/issues/{self.issue_number}/comments?{query}"
            payload = self._request("GET", url)
            require(isinstance(payload, list), "GitHub evidence comment response is invalid")
            result.extend(item for item in payload if isinstance(item, Mapping))
            if len(payload) < 100:
                break
            page += 1
            require(page <= 100, "GitHub evidence comment pagination is unexpectedly large")
        return result

    def create_comment(self, body: str) -> int:
        require(bool(self.token), "GitHub evidence store token is unavailable")
        owner, repo = self.repository.split("/", 1)
        url = f"https://api.github.com/repos/{owner}/{repo}/issues/{self.issue_number}/comments"
        payload = self._request("POST", url, {"body": body})
        require(isinstance(payload, Mapping), "GitHub evidence comment response is invalid")
        comment_id = payload.get("id")
        require(isinstance(comment_id, int) and comment_id > 0, "GitHub evidence comment id is invalid")
        return comment_id


def _trusted(comment: Mapping[str, Any]) -> bool:
    user = comment.get("user")
    return isinstance(user, Mapping) and user.get("login") == TRUSTED_EVIDENCE_ACTOR


def _record_body(heading: str, payload: Mapping[str, Any]) -> str:
    return (
        heading
        + "\n\n```json\n"
        + json.dumps(dict(payload), separators=(",", ":"), sort_keys=True)
        + "\n```"
    )


def _record_payload(comment: Mapping[str, Any], heading: str) -> dict[str, Any] | None:
    if not _trusted(comment):
        return None
    body = comment.get("body")
    if not isinstance(body, str) or not body.startswith(heading):
        return None
    prefix = heading + "\n\n```json\n"
    suffix = "\n```"
    require(body.startswith(prefix) and body.endswith(suffix), f"trusted {heading} record is malformed")
    raw = body[len(prefix) : -len(suffix)]
    require("\n" not in raw, f"trusted {heading} record must use one canonical JSON line")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise IntegrationFailure(f"trusted {heading} JSON is invalid") from error
    require(isinstance(payload, dict), f"trusted {heading} payload must be an object")
    return payload


class BoundaryObserver(Protocol):
    def observe(self, transaction_id: str, target_binding_id: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class CanonicalPhoneBoundaryObserver:
    bundle: CanonicalBundle
    serial: str

    def observe(self, transaction_id: str, target_binding_id: str) -> Mapping[str, Any]:
        require(_TRANSACTION.fullmatch(transaction_id) is not None, "boundary transaction id is invalid")
        require(_TARGET_BINDING.fullmatch(target_binding_id) is not None, "boundary target binding is invalid")
        session_id = f"apk-boundary-session-{transaction_id}"
        observation_ref = f"apk-boundary-observation-{transaction_id}"
        script = self.bundle.scripts_root / "run_private_phone_preflight.py"
        require(script.is_file(), "canonical phone boundary observer is unavailable")
        with tempfile.TemporaryDirectory(prefix="mobile-proxy-apk-boundary-") as raw:
            output = Path(raw) / "phone-boundary.json"
            env = dict(os.environ)
            env["ANDROID_PRODUCTION_SERIAL"] = self.serial
            command = (
                sys.executable,
                str(script),
                "--canonical-sha",
                self.bundle.canonical_sha,
                "--output",
                str(output),
                "--target-binding-id",
                target_binding_id,
                "--session-id",
                session_id,
                "--observation-ref",
                observation_ref,
                "--transaction-id",
                transaction_id,
            )
            try:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    env=env,
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                raise IntegrationFailure("transaction-bound canonical phone observer failed") from error
            require(output.is_file(), "transaction-bound phone observer emitted no report")
            report = json.loads(output.read_text(encoding="utf-8"))
        require(isinstance(report, dict), "transaction-bound phone report is invalid")
        require(report.get("accepted") is True, "transaction-bound phone observer was not accepted")
        require(report.get("mutation_performed") is False, "transaction-bound phone observer must be read-only")
        facts = report.get("observed_facts")
        require(isinstance(facts, list) and len(facts) == 1 and isinstance(facts[0], Mapping), "transaction-bound phone fact is unavailable or ambiguous")
        return dict(facts[0])


def derive_target_binding(serial: str, binding_key: str) -> str:
    require(bool(serial) and len(serial) <= 128 and not any(ch.isspace() for ch in serial), "registered production target binding is invalid")
    require(len(binding_key) >= 32, "target binding HMAC key is unavailable or too short")
    require(binding_key != serial, "target binding HMAC key must be independent from raw target binding")
    digest = hmac.new(
        binding_key.encode("utf-8"),
        b"android-production\0" + serial.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return "tb-hmac-sha256:" + digest


@dataclass
class GitHubTransactionPorts:
    modules: CanonicalModules
    bundle: CanonicalBundle
    envelope: AuthorityEnvelope
    store: IssueStore
    scope_proof: OuterMutationScopeProof
    boundary_observer: BoundaryObserver
    target_binding_id: str

    def resolve_authority(self, request: object, contract: Any) -> Any:
        artifact_ref = getattr(request, "artifact_ref", "")
        authorized = (
            getattr(contract, "operation_id", "") == OPERATION_ID
            and self.envelope.canonical_sha == self.bundle.canonical_sha
            and self.envelope.quality_run_id == self.bundle.quality_run_id
            and artifact_ref == self.envelope.artifact_ref
            and self.envelope.same_lineage_compatible is True
            and bool(self.envelope.compatibility_ref)
        )
        compatibility_ref = self.envelope.compatibility_ref or "compatibility-not-admitted"
        return self.modules.transaction.AuthorityProof(
            authorized,
            f"apk-authority:{self.bundle.canonical_sha}:{self.bundle.quality_run_id}:{compatibility_ref}",
        )

    @contextmanager
    def acquire_mutation_scope(self, target: str, transaction_id: str):
        require(target == TARGET, "APK transaction target differs")
        require(_TRANSACTION.fullmatch(transaction_id) is not None, "APK transaction id is invalid")
        self.scope_proof.validate()
        yield f"mutation-scope:{GLOBAL_MUTATION_GROUP}:{transaction_id}"

    def _promote_boundary_fact(
        self,
        payload: Mapping[str, Any],
        transaction_id: str,
    ) -> tuple[Any, dict[str, str]]:
        required_keys = {
            "subject",
            "predicate",
            "value",
            "target",
            "observation_ref",
            "source_ref",
            "dependencies",
            "authority",
            "persisted",
        }
        require(set(payload) == required_keys, "transaction-bound phone fact schema differs")
        require(payload["subject"] == "phone", "transaction-bound phone fact subject differs")
        require(payload["predicate"] == "registered_phone_access_proven", "transaction-bound phone fact predicate differs")
        require(payload["value"] is True, "transaction-bound phone fact value differs")
        require(payload["target"] == TARGET, "transaction-bound phone fact target differs")
        require(payload["authority"] == "CONTROL", "transaction-bound phone fact authority differs")
        require(payload["persisted"] is False, "canonical phone observer must emit unpersisted boundary fact")
        require(payload["source_ref"] == self.bundle.canonical_sha, "transaction-bound phone fact source differs")
        dependencies = payload["dependencies"]
        require(isinstance(dependencies, list), "transaction-bound phone dependencies are invalid")
        context: dict[str, str] = {}
        converted = []
        for item in dependencies:
            require(isinstance(item, Mapping), "transaction-bound phone dependency is invalid")
            require(set(item) == {"scope", "identity"}, "transaction-bound phone dependency schema differs")
            scope = _non_empty_ref(item["scope"], "transaction-bound phone dependency scope")
            identity = _non_empty_ref(item["identity"], "transaction-bound phone dependency identity")
            require(scope not in context, "transaction-bound phone dependency is duplicated")
            context[scope] = identity
            converted.append(self.modules.control.FactDependency(scope, identity))
        require(context.get(f"target/{TARGET}") == self.target_binding_id, "transaction-bound target identity differs")
        require(context.get("observer/phone-access") == PHONE_OBSERVER, "transaction-bound phone observer identity differs")
        require(context.get(f"transaction/{transaction_id}") == transaction_id, "transaction-bound phone fact is not from this transaction")
        session_scope = f"session/{TARGET}"
        require(bool(context.get(session_scope)), "transaction-bound phone session identity is missing")

        promoted = dict(payload)
        promoted["persisted"] = True
        record = {
            "format_version": 1,
            "record_type": "APK_SAME_TRANSACTION_BOUNDARY",
            "operation_id": OPERATION_ID,
            "operation_transaction_id": transaction_id,
            "target": TARGET,
            "fact": promoted,
            "promotion_changes_only_persistence": True,
            "raw_device_identifier_recorded": False,
            "phone_mutation_performed": False,
        }
        comment_id = self.store.create_comment(_record_body(BOUNDARY_HEADING, record))
        observation_ref = _non_empty_ref(payload["observation_ref"], "transaction-bound phone observation ref")
        fact = self.modules.control.ObservedFact(
            subject=payload["subject"],
            predicate=payload["predicate"],
            value=payload["value"],
            target=payload["target"],
            observation_ref=observation_ref,
            source_ref=payload["source_ref"],
            dependencies=tuple(converted),
            authority=payload["authority"],
            persisted=True,
        )
        context["evidence/apk-boundary"] = f"issue-comment:{comment_id}"
        return fact, context

    def prove_same_transaction_boundary(self, contract: Any, transaction_id: str) -> Any:
        require(getattr(contract, "operation_id", "") == OPERATION_ID, "boundary operation differs")
        raw_fact = self.boundary_observer.observe(transaction_id, self.target_binding_id)
        fact, context = self._promote_boundary_fact(raw_fact, transaction_id)
        return self.modules.transaction.BoundaryProof(fact, context)

    def persist_mutation_intent(self, intent: Any) -> str:
        transaction_id = getattr(intent, "transaction_id", "")
        require(_TRANSACTION.fullmatch(transaction_id) is not None, "mutation intent transaction id is invalid")
        generations = dict(getattr(intent, "affected_domain_generations", {}))
        require(generations == {"domain/package": transaction_id}, "APK package generation transition differs")
        require(getattr(intent, "operation_id", "") == OPERATION_ID, "mutation intent operation differs")
        require(getattr(intent, "target", "") == TARGET, "mutation intent target differs")
        require(getattr(intent, "dispatch_step_id", "") == "install_apk", "mutation intent dispatch step differs")
        require(getattr(intent, "mutation_subject_ref", "") == self.envelope.artifact_ref, "mutation intent artifact identity differs")
        payload = {
            "format_version": 1,
            "record_type": "APK_MUTATION_INTENT",
            "operation_id": OPERATION_ID,
            "operation_transaction_id": transaction_id,
            "target": TARGET,
            "canonical_sha": self.bundle.canonical_sha,
            "canonical_quality_run_id": self.bundle.quality_run_id,
            "artifact_ref": self.envelope.artifact_ref,
            "compatibility_ref": self.envelope.compatibility_ref,
            "dispatch_step_id": "install_apk",
            "dispatch_status": "DISPATCHED",
            "affected_domain_generations": generations,
            "dispatch_may_reach_target": True,
            "blind_retry_allowed": False,
            "raw_device_identifier_recorded": False,
        }
        comment_id = self.store.create_comment(_record_body(INTENT_HEADING, payload))
        return f"issue-comment:{comment_id}"

    def persist_terminal(self, record: Any) -> str:
        transaction_id = getattr(record, "transaction_id", "")
        require(_TRANSACTION.fullmatch(transaction_id) is not None, "terminal transaction id is invalid")
        evidence = []
        for item in getattr(record, "evidence", ()):
            evidence.append(
                {
                    "step_id": item.step_id,
                    "status": item.status,
                    "transaction_id": item.transaction_id,
                    "source_ref": item.source_ref,
                    "authority": item.authority,
                    "lifecycle": item.lifecycle,
                }
            )
        payload = {
            "format_version": 1,
            "record_type": "APK_TRANSACTION_TERMINAL",
            "operation_id": getattr(record, "operation_id", ""),
            "operation_transaction_id": transaction_id,
            "target": getattr(record, "target", ""),
            "affected_domain_generations": dict(getattr(record, "affected_domain_generations", {})),
            "evidence": evidence,
            "derived": dict(getattr(record, "derived", {})),
            "blind_retry_allowed": False,
            "raw_device_identifier_recorded": False,
        }
        require(payload["operation_id"] == OPERATION_ID, "terminal operation differs")
        require(payload["target"] == TARGET, "terminal target differs")
        comment_id = self.store.create_comment(_record_body(TERMINAL_HEADING, payload))
        return f"issue-comment:{comment_id}"

    def load_existing_evidence(self, transaction_id: str) -> tuple[Any, ...]:
        require(_TRANSACTION.fullmatch(transaction_id) is not None, "existing evidence transaction id is invalid")
        intents: list[dict[str, Any]] = []
        terminals: list[dict[str, Any]] = []
        for comment in self.store.comments():
            intent = _record_payload(comment, INTENT_HEADING)
            if intent is not None and intent.get("operation_transaction_id") == transaction_id:
                intents.append(intent)
            terminal = _record_payload(comment, TERMINAL_HEADING)
            if terminal is not None and terminal.get("operation_transaction_id") == transaction_id:
                terminals.append(terminal)

        require(len(intents) <= 1, "duplicate durable APK mutation intent exists")
        require(len(terminals) <= 1, "duplicate durable APK terminal record exists")

        contract = self.modules.operation.operation_contract(OPERATION_ID)
        if terminals:
            payload = terminals[0]
            require(
                set(payload)
                == {
                    "format_version",
                    "record_type",
                    "operation_id",
                    "operation_transaction_id",
                    "target",
                    "affected_domain_generations",
                    "evidence",
                    "derived",
                    "blind_retry_allowed",
                    "raw_device_identifier_recorded",
                },
                "trusted APK terminal schema differs",
            )
            require(payload["format_version"] == 1, "trusted APK terminal version differs")
            require(payload["record_type"] == "APK_TRANSACTION_TERMINAL", "trusted APK terminal type differs")
            require(payload["operation_id"] == OPERATION_ID and payload["target"] == TARGET, "trusted APK terminal authority differs")
            require(payload["blind_retry_allowed"] is False, "trusted APK terminal permits blind retry")
            require(payload["raw_device_identifier_recorded"] is False, "trusted APK terminal records raw target identity")
            raw_evidence = payload["evidence"]
            require(isinstance(raw_evidence, list) and raw_evidence, "trusted APK terminal evidence is invalid")
            evidence = []
            for item in raw_evidence:
                require(isinstance(item, Mapping), "trusted APK terminal phase is invalid")
                require(
                    set(item)
                    == {
                        "step_id",
                        "status",
                        "transaction_id",
                        "source_ref",
                        "authority",
                        "lifecycle",
                    },
                    "trusted APK terminal phase schema differs",
                )
                require(item["transaction_id"] == transaction_id, "trusted APK terminal phase transaction differs")
                evidence.append(
                    self.modules.operation.PhaseEvidence(
                        item["step_id"],
                        item["status"],
                        item["transaction_id"],
                        item["source_ref"],
                        item["authority"],
                        item["lifecycle"],
                    )
                )
            derived = self.modules.operation.derive_operation_state(
                contract,
                evidence,
                transaction_id=transaction_id,
            )
            require(derived == payload["derived"], "trusted APK terminal derived state differs")
            return tuple(evidence)

        if intents:
            payload = intents[0]
            require(
                set(payload)
                == {
                    "format_version",
                    "record_type",
                    "operation_id",
                    "operation_transaction_id",
                    "target",
                    "canonical_sha",
                    "canonical_quality_run_id",
                    "artifact_ref",
                    "compatibility_ref",
                    "dispatch_step_id",
                    "dispatch_status",
                    "affected_domain_generations",
                    "dispatch_may_reach_target",
                    "blind_retry_allowed",
                    "raw_device_identifier_recorded",
                },
                "trusted APK mutation intent schema differs",
            )
            require(payload["format_version"] == 1, "trusted APK mutation intent version differs")
            require(payload["record_type"] == "APK_MUTATION_INTENT", "trusted APK mutation intent type differs")
            require(payload["operation_id"] == OPERATION_ID and payload["target"] == TARGET, "trusted APK mutation intent authority differs")
            require(payload["dispatch_step_id"] == "install_apk" and payload["dispatch_status"] == "DISPATCHED", "trusted APK mutation dispatch marker differs")
            require(payload["affected_domain_generations"] == {"domain/package": transaction_id}, "trusted APK package generation differs")
            require(payload["dispatch_may_reach_target"] is True, "trusted APK mutation intent cannot reach target")
            require(payload["blind_retry_allowed"] is False, "trusted APK mutation intent permits blind retry")
            require(payload["raw_device_identifier_recorded"] is False, "trusted APK mutation intent records raw target identity")
            ref = "durable-apk-intent:" + transaction_id
            return (
                self.modules.operation.PhaseEvidence("resolve_authority", self.modules.operation.PASSED, transaction_id, ref),
                self.modules.operation.PhaseEvidence("mutation_scope", self.modules.operation.PASSED, transaction_id, ref),
                self.modules.operation.PhaseEvidence("phone_access_boundary", self.modules.operation.PASSED, transaction_id, ref),
                self.modules.operation.PhaseEvidence("mutation_intent", self.modules.operation.PASSED, transaction_id, ref),
                self.modules.operation.PhaseEvidence("install_apk", self.modules.operation.DISPATCHED, transaction_id, ref),
            )
        return ()


def _positive_int(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise IntegrationFailure(f"{label} must be a positive integer") from error
    require(parsed > 0, f"{label} must be a positive integer")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-bundle", type=Path, required=True)
    parser.add_argument("--canonical-sha", required=True)
    parser.add_argument("--quality-run-id", required=True)
    parser.add_argument("--owner-command-id", required=True)
    parser.add_argument("--authority-envelope", type=Path, required=True)
    parser.add_argument("--apk-path", type=Path, required=True)
    parser.add_argument("--typed-digest-tool", type=Path, required=True)
    parser.add_argument("--github-token-env", default="GITHUB_TOKEN")
    parser.add_argument("--serial-env", default="ANDROID_PRODUCTION_SERIAL")
    parser.add_argument("--target-binding-key-env", default="ANDROID_TARGET_BINDING_KEY")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        quality_run_id = _positive_int(args.quality_run_id, "Quality run ID")
        owner_command_id = _positive_int(args.owner_command_id, "owner command id")
        bundle = CanonicalBundle.load(
            args.canonical_bundle,
            expected_sha=args.canonical_sha,
            expected_quality_run_id=quality_run_id,
        )
        modules = bundle.load_modules()
        envelope = AuthorityEnvelope.load(args.authority_envelope)
        require(envelope.canonical_sha == bundle.canonical_sha, "APK authority source differs from canonical bundle")
        require(envelope.quality_run_id == bundle.quality_run_id, "APK authority Quality differs from canonical bundle")
        transaction_id = stable_transaction_id(owner_command_id)

        token = os.environ.get(args.github_token_env, "")
        serial = os.environ.get(args.serial_env, "")
        binding_key = os.environ.get(args.target_binding_key_env, "")
        target_binding_id = derive_target_binding(serial, binding_key)
        scope_proof = OuterMutationScopeProof.from_environment()
        store = GitHubIssueStore(token)
        observer = CanonicalPhoneBoundaryObserver(bundle, serial)
        ports = GitHubTransactionPorts(
            modules=modules,
            bundle=bundle,
            envelope=envelope,
            store=store,
            scope_proof=scope_proof,
            boundary_observer=observer,
            target_binding_id=target_binding_id,
        )

        commands = modules.apk.SubprocessCommandEdge()
        digests = modules.apk.ExternalTypedArtifactDigest(
            args.typed_digest_tool,
            commands,
        )
        executor = modules.apk.CanonicalApkInstallExecutor(
            serial=serial,
            apk_path=args.apk_path,
            admitted_artifact_ref=envelope.artifact_ref,
            commands=commands,
            digests=digests,
        )
        binding = modules.apk.ApkInstallBinding(executor)
        request = modules.apk.ApkInstallRequest(transaction_id, envelope.artifact_ref)
        existing = ports.load_existing_evidence(transaction_id)
        result = modules.transaction.TransactionRunner().run(
            request,
            ports=ports,
            binding=binding,
            existing_evidence=existing,
        )
        state = result.derived.get("state", "UNKNOWN")
        print(
            json.dumps(
                {
                    "operation_id": OPERATION_ID,
                    "operation_transaction_id": transaction_id,
                    "state": state,
                    "terminal_ref": result.terminal_ref,
                    "dispatch_error": result.dispatch_error,
                    "blind_retry_allowed": False,
                    "raw_device_identifier_recorded": False,
                },
                sort_keys=True,
            )
        )
        return 0 if state == "ACCEPTED" and result.dispatch_error is None else 1
    except (
        IntegrationFailure,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as error:
        print(f"APK transaction integration failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
