from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import apk_transaction_control as CONTROL


CANONICAL_SHA = "dba341bed382a8cbfbdb66e45a14ec37a4257741"
QUALITY_RUN_ID = 33765720022
REF = "b3:" + "a" * 64
TARGET_BINDING = "tb-hmac-sha256:" + "c" * 64
COMPATIBILITY_REF = "same-lineage-proof:unit-test"
OWNER_COMMAND_ID = 5527343094


class FakeStore:
    def __init__(self, *, fail_heading: str | None = None) -> None:
        self.items: list[dict[str, object]] = []
        self.next_id = 100
        self.fail_heading = fail_heading

    def comments(self):
        return list(self.items)

    def create_comment(self, body: str) -> int:
        if self.fail_heading and body.startswith(self.fail_heading):
            raise CONTROL.IntegrationFailure("simulated persistence failure")
        comment_id = self.next_id
        self.next_id += 1
        self.items.append(
            {
                "id": comment_id,
                "body": body,
                "user": {"login": CONTROL.TRUSTED_EVIDENCE_ACTOR},
            }
        )
        return comment_id


class FakeBoundaryObserver:
    def __init__(self, source_ref: str) -> None:
        self.source_ref = source_ref
        self.calls = 0

    def observe(self, transaction_id: str, target_binding_id: str):
        self.calls += 1
        return {
            "subject": "phone",
            "predicate": "registered_phone_access_proven",
            "value": True,
            "target": CONTROL.TARGET,
            "observation_ref": f"boundary-observation-{transaction_id}",
            "source_ref": self.source_ref,
            "dependencies": [
                {"scope": f"target/{CONTROL.TARGET}", "identity": target_binding_id},
                {"scope": "observer/phone-access", "identity": CONTROL.PHONE_OBSERVER},
                {"scope": f"session/{CONTROL.TARGET}", "identity": f"session-{transaction_id}"},
                {"scope": f"transaction/{transaction_id}", "identity": transaction_id},
            ],
            "authority": "CONTROL",
            "persisted": False,
        }


class FakeExecutor:
    def __init__(self, transaction_module, *, fail_dispatch: bool = False) -> None:
        self.transaction = transaction_module
        self.fail_dispatch = fail_dispatch
        self.dispatches = 0
        self.postconditions = 0

    def dispatch_once(self, request):
        self.dispatches += 1
        if self.fail_dispatch:
            raise RuntimeError("simulated ambiguous dispatch")
        return self.transaction.DispatchReceipt(
            f"fake-dispatch:{request.transaction_id}:{request.artifact_ref}"
        )

    def verify_postcondition(self, request):
        self.postconditions += 1
        return self.transaction.PostconditionProof(
            True,
            f"fake-postcondition:{request.transaction_id}:{request.artifact_ref}",
        )


class ApkTransactionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        canonical_root = os.environ.get("CANONICAL_ROOT", "").strip()
        if not canonical_root:
            raise unittest.SkipTest("CANONICAL_ROOT is required")
        cls.canonical_root = Path(canonical_root).resolve()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="apk-transaction-integration-test-")
        self.addCleanup(self.temp.cleanup)
        self.bundle = self._bundle(Path(self.temp.name) / "bundle")
        self.modules = self.bundle.load_modules()

    def _bundle(self, root: Path) -> CONTROL.CanonicalBundle:
        scripts_root = root / "scripts"
        scripts_root.mkdir(parents=True)
        digests = {}
        for relative in CONTROL._REQUIRED_CANONICAL:
            source = self.canonical_root / "scripts" / relative
            target = scripts_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            digests[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest = {
            "format_version": 1,
            "repository": CONTROL.CANONICAL_REPOSITORY,
            "canonical_sha": CANONICAL_SHA,
            "quality_run_id": QUALITY_RUN_ID,
            "scripts_sha256": digests,
        }
        (root / "source-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return CONTROL.CanonicalBundle.load(
            root,
            expected_sha=CANONICAL_SHA,
            expected_quality_run_id=QUALITY_RUN_ID,
        )

    def _envelope(self, compatible: bool = True) -> CONTROL.AuthorityEnvelope:
        return CONTROL.AuthorityEnvelope(
            canonical_sha=CANONICAL_SHA,
            quality_run_id=QUALITY_RUN_ID,
            artifact_ref=REF,
            same_lineage_compatible=compatible,
            compatibility_ref=COMPATIBILITY_REF if compatible else "",
        )

    def _ports(
        self,
        *,
        store: FakeStore | None = None,
        observer: FakeBoundaryObserver | None = None,
        compatible: bool = True,
        scope: CONTROL.OuterMutationScopeProof | None = None,
    ):
        store = store or FakeStore()
        observer = observer or FakeBoundaryObserver(CANONICAL_SHA)
        ports = CONTROL.GitHubTransactionPorts(
            modules=self.modules,
            bundle=self.bundle,
            envelope=self._envelope(compatible),
            store=store,
            scope_proof=scope
            or CONTROL.OuterMutationScopeProof(
                CONTROL.GLOBAL_MUTATION_GROUP,
                False,
                "max",
            ),
            boundary_observer=observer,
            target_binding_id=TARGET_BINDING,
        )
        return ports, store, observer

    def _run(self, ports, executor, owner_command_id: int = OWNER_COMMAND_ID):
        transaction_id = CONTROL.stable_transaction_id(owner_command_id)
        request = self.modules.apk.ApkInstallRequest(transaction_id, REF)
        binding = self.modules.apk.ApkInstallBinding(executor)
        existing = ports.load_existing_evidence(transaction_id)
        return self.modules.transaction.TransactionRunner().run(
            request,
            ports=ports,
            binding=binding,
            existing_evidence=existing,
        )

    def test_transaction_identity_depends_only_on_immutable_owner_command(self) -> None:
        first = CONTROL.stable_transaction_id(123456789)
        second = CONTROL.stable_transaction_id(123456789)
        self.assertEqual(first, "apk-install-123456789")
        self.assertEqual(first, second)
        source = (SCRIPT_DIR / "apk_transaction_control.py").read_text(encoding="utf-8")
        self.assertNotIn("GITHUB_RUN_ATTEMPT", source)

    def test_incompatible_artifact_fails_before_boundary_or_dispatch(self) -> None:
        ports, store, observer = self._ports(compatible=False)
        executor = FakeExecutor(self.modules.transaction)
        result = self._run(ports, executor)
        self.assertEqual(result.derived["state"], "REFUSED")
        self.assertEqual(observer.calls, 0)
        self.assertEqual(executor.dispatches, 0)
        self.assertEqual(executor.postconditions, 0)
        self.assertTrue(any(str(item["body"]).startswith(CONTROL.TERMINAL_HEADING) for item in store.items))
        self.assertFalse(any(str(item["body"]).startswith(CONTROL.INTENT_HEADING) for item in store.items))

    def test_success_uses_canonical_kernel_and_persists_boundary_intent_terminal(self) -> None:
        ports, store, observer = self._ports()
        executor = FakeExecutor(self.modules.transaction)
        result = self._run(ports, executor)
        self.assertEqual(result.derived["state"], "ACCEPTED")
        self.assertEqual(observer.calls, 1)
        self.assertEqual(executor.dispatches, 1)
        self.assertEqual(executor.postconditions, 1)
        headings = [str(item["body"]).splitlines()[0] for item in store.items]
        self.assertEqual(
            headings,
            [CONTROL.BOUNDARY_HEADING, CONTROL.INTENT_HEADING, CONTROL.TERMINAL_HEADING],
        )
        existing = ports.load_existing_evidence(CONTROL.stable_transaction_id(OWNER_COMMAND_ID))
        self.assertTrue(existing)
        with self.assertRaises(self.modules.transaction.TransactionRefusal):
            self._run(ports, executor)
        self.assertEqual(observer.calls, 1)
        self.assertEqual(executor.dispatches, 1)

    def test_ambiguous_dispatch_is_durable_unknown_and_rerun_is_blind_retry_forbidden(self) -> None:
        ports, store, observer = self._ports()
        executor = FakeExecutor(self.modules.transaction, fail_dispatch=True)
        result = self._run(ports, executor)
        self.assertEqual(result.derived["state"], "UNKNOWN_EXECUTION_OUTCOME")
        self.assertIsNone(result.terminal_ref)
        self.assertIsNotNone(result.dispatch_error)
        self.assertEqual(observer.calls, 1)
        self.assertEqual(executor.dispatches, 1)
        self.assertTrue(any(str(item["body"]).startswith(CONTROL.INTENT_HEADING) for item in store.items))
        self.assertFalse(any(str(item["body"]).startswith(CONTROL.TERMINAL_HEADING) for item in store.items))
        existing = ports.load_existing_evidence(CONTROL.stable_transaction_id(OWNER_COMMAND_ID))
        with self.assertRaises(self.modules.transaction.BlindRetryForbidden):
            self.modules.transaction.TransactionRunner().run(
                self.modules.apk.ApkInstallRequest(CONTROL.stable_transaction_id(OWNER_COMMAND_ID), REF),
                ports=ports,
                binding=self.modules.apk.ApkInstallBinding(executor),
                existing_evidence=existing,
            )
        self.assertEqual(observer.calls, 1)
        self.assertEqual(executor.dispatches, 1)

    def test_terminal_persistence_loss_also_blocks_rerun_without_second_dispatch(self) -> None:
        store = FakeStore(fail_heading=CONTROL.TERMINAL_HEADING)
        ports, _, observer = self._ports(store=store)
        executor = FakeExecutor(self.modules.transaction)
        with self.assertRaises(CONTROL.IntegrationFailure):
            self._run(ports, executor)
        self.assertEqual(observer.calls, 1)
        self.assertEqual(executor.dispatches, 1)
        self.assertEqual(executor.postconditions, 1)
        self.assertTrue(any(str(item["body"]).startswith(CONTROL.INTENT_HEADING) for item in store.items))
        store.fail_heading = None
        existing = ports.load_existing_evidence(CONTROL.stable_transaction_id(OWNER_COMMAND_ID))
        with self.assertRaises(self.modules.transaction.BlindRetryForbidden):
            self.modules.transaction.TransactionRunner().run(
                self.modules.apk.ApkInstallRequest(CONTROL.stable_transaction_id(OWNER_COMMAND_ID), REF),
                ports=ports,
                binding=self.modules.apk.ApkInstallBinding(executor),
                existing_evidence=existing,
            )
        self.assertEqual(observer.calls, 1)
        self.assertEqual(executor.dispatches, 1)

    def test_boundary_persistence_failure_prevents_intent_and_dispatch(self) -> None:
        store = FakeStore(fail_heading=CONTROL.BOUNDARY_HEADING)
        ports, _, observer = self._ports(store=store)
        executor = FakeExecutor(self.modules.transaction)
        with self.assertRaises(CONTROL.IntegrationFailure):
            self._run(ports, executor)
        self.assertEqual(observer.calls, 1)
        self.assertEqual(executor.dispatches, 0)
        self.assertFalse(any(str(item["body"]).startswith(CONTROL.INTENT_HEADING) for item in store.items))

    def test_outer_scope_must_be_exact_global_non_cancelling_queue(self) -> None:
        ports, _, observer = self._ports(
            scope=CONTROL.OuterMutationScopeProof("wrong-group", False, "max")
        )
        executor = FakeExecutor(self.modules.transaction)
        with self.assertRaises(CONTROL.IntegrationFailure):
            self._run(ports, executor)
        self.assertEqual(observer.calls, 0)
        self.assertEqual(executor.dispatches, 0)

    def test_untrusted_issue_comment_cannot_create_prior_dispatch_state(self) -> None:
        tx = CONTROL.stable_transaction_id(OWNER_COMMAND_ID)
        store = FakeStore()
        payload = {
            "format_version": 1,
            "record_type": "APK_MUTATION_INTENT",
            "operation_id": CONTROL.OPERATION_ID,
            "operation_transaction_id": tx,
            "target": CONTROL.TARGET,
            "canonical_sha": CANONICAL_SHA,
            "canonical_quality_run_id": QUALITY_RUN_ID,
            "artifact_ref": REF,
            "compatibility_ref": COMPATIBILITY_REF,
            "dispatch_step_id": "install_apk",
            "dispatch_status": "DISPATCHED",
            "affected_domain_generations": {"domain/package": tx},
            "dispatch_may_reach_target": True,
            "blind_retry_allowed": False,
            "raw_device_identifier_recorded": False,
        }
        store.items.append(
            {
                "id": 1,
                "body": CONTROL._record_body(CONTROL.INTENT_HEADING, payload),
                "user": {"login": "iamaman11"},
            }
        )
        ports, _, _ = self._ports(store=store)
        self.assertEqual(ports.load_existing_evidence(tx), ())


if __name__ == "__main__":
    unittest.main()
