# Production Command / Control Contract

## Authority and ownership

`iamaman11/mobile-proxy-production` owns the Deployment Controller. Private Issue #1 is the sole production command ingress. Public Issue #179 is the development/migration execution cursor; accepted production request identity remains cursor-free.

A comment is admissible only when repository, Issue, owner, exact syntax, current controller revision, command contract, target contract and handler contract all agree. Any ambiguity fails closed before execution.

## Declarative control plane

The core classifier is `.github/scripts/issue_command_router.py`.

Command contracts live in `.github/production/command-control-registry.json`. Target contracts live in `.github/production/targets.json`.

Each command contract declares:

- exact anchored syntax and typed argument schema;
- operation and operation class;
- authority and target-capability policy;
- handler, literal workflow mapping and ref policy;
- read-only/destructive classification;
- allowed targets and physical domains;
- phone/VM/release capability requirements;
- concurrency domain;
- semantic identity and idempotency policy;
- evidence policy;
- recovery policy;
- optional typed `workflow_dispatch` input mapping for generic read-only routes.

The classifier validates the complete registries before admitting any command. Issue text never supplies a workflow name, Git ref, path, URL, JSON payload or shell fragment.

## One ingress, bounded handler classes

`.github/workflows/production-control-router.yml` is the only `issue_comment` production ingress.

It contains three bounded handler classes:

1. `dispatch_workflow` — generic adapter for registry-allowlisted **read-only hosted** `workflow_dispatch` operations. The adapter re-loads and validates the registry, requires current private `main` to equal the comment-event controller SHA, derives the workflow/ref only from the trusted registry and dispatches through GitHub-native API. It cannot dispatch a destructive route.
2. `deployment` — the existing Deployment Controller v2 path. It preserves `production-deployment-request.v2`, durable ACK/admission/intent/terminal evidence, target serialization, recovery and independent postcondition semantics.
3. `workflow_call` — explicitly bounded reusable control workflows whose privilege/idempotency contract cannot be represented as generic read-only dispatch. Current use is the accepted Android Build Tools runner bootstrap.

Adding another hosted read-only observer/diagnostic/verify route does **not** require modifying the core ingress: add the registry contract, target contract if needed, target workflow and tests. New privileged/mutating handler classes are never inferred automatically; they require an explicit controller design and policy change.

## Active routes

Only these routes are enabled:

1. `/observe-public-deployment-projection`
   - hosted read-only observation;
   - registry maps it to `public-deployment-projection-observer.yml`, `ref=main`;
   - only original ingress run attempt may dispatch, preventing rerun-created duplicate observation.

2. `/deploy <target> <vX.Y.Z>`
   - preserves existing `production-deployment-request.v2` semantic identity;
   - routes only to `release-deployment.yml`;
   - `phone-production` is the active physical adapter;
   - `vm-production` remains fail-closed/deferred until its adapter is separately accepted.

3. `/runner-android-build-tools-bootstrap <exact-public-main-sha>`
   - routes only to the existing bounded runner-tooling reusable workflow;
   - runner-tooling mutation is separate from phone mutation and retains its exact-package idempotency contract.

No new destructive command is activated by this framework.

## Typed arguments and extension surface

Supported registry argument types are intentionally bounded: target, semantic version, Git SHA, bounded identifier and explicit enum. The regex named captures must exactly equal the declared argument schema. Unknown captures, unnamed captures, unknown fields and unknown types are rejected.

The framework recognizes operation classes such as `OBSERVE`, `DIAGNOSTIC`, `VERIFY`, `BUILD`, `RELEASE_VERIFY`, `DEPLOY`, `ROLLBACK`, `RECOVER`, `RECONCILE`, `STATUS` and bounded runner tooling. Names such as `/diagnose`, `/verify`, `/rollback`, `/recover`, `/reconcile` and `/status` are not enabled merely because the class exists.

Generic `dispatch_workflow` routes are permanently constrained to read-only/non-destructive operation contracts. A new physical/destructive operation cannot be smuggled through the generic dispatcher.

## Product and controller identity

Product and controller identity are independent:

- `product_release = vX.Y.Z` identifies an immutable public Product Release;
- `controller_revision = exact private Git SHA` identifies the executing Deployment Controller revision;
- `target` identifies the serving/physical target.

`/deploy phone-production v0.1.4` never means “deploy current HEAD”. It resolves the immutable `v0.1.4` Product Release under the existing Release resolver contract.

## Semantic request identity and replay

Deployment semantic identity remains the existing `req-sha256:` digest over normalized deployment schema, operation, target and Product Release tag. Comment ID, workflow run ID and run attempt are provenance/execution identities and do not create a new semantic deployment request.

Repeated equivalent deployment comments therefore do not imply another destructive operation. The durable Issue #1 evidence ledger reconciles reusable admission, intent, terminal and duplicate state for the semantic request.

Generic read-only dispatch may define stricter transport idempotency. The current observer is `single-run-attempt`: rerunning the ingress workflow is rejected before dispatch.

Runner-tooling keeps its own exact-package no-op/create semantics and fails closed if an existing persisted package differs.

## Mutation lifecycle and evidence

A green workflow is not deployment success. Canonical deployment evidence retains the equivalent lifecycle:

`REQUEST -> ACK -> ADMISSION -> INTENT -> DISPATCH -> TERMINAL -> independent postcondition`

`ADMISSION` alone grants no mutation authority. Durable mutation intent must precede destructive dispatch. Canonical terminal state plus independent target postcondition determines acceptance. Public GitHub Deployment status is only a projection of canonical private truth.

## UNKNOWN and recovery

`UNKNOWN != FAILED`.

If destructive dispatch may have occurred but the outcome is not proven, blind retry is forbidden and success must not be synthesized. Only bounded read-only recovery/reconciliation may restore truth before another destructive operation becomes admissible.

`RECOVERED` is not `ACCEPTED`. `UNKNOWN`, `RECOVERED` and quarantine states remain fail-closed for public success projection.

Every destructive command contract must carry semantic idempotency, non-empty serialization, terminal/postcondition evidence and explicit `UNKNOWN` / `no-blind-retry` recovery metadata. Runtime registry validation rejects an incomplete destructive contract before routing.

## Target registry and serialization

Target mutation is serialized by target/domain contract, never by comment/run identity. `phone-production` retains the existing `production-target-phone-production` global mutation serialization. `vm-production` is present only as a deferred target contract and has no accepted physical adapter.

A future target must define type, adapter, allowed operations, physical domains, serialization domain and required postcondition before physical mutation can become active.

## Permissions and secrets

The sole ingress has no direct phone, ADB, provider or secret-bearing physical execution step.

Per-job permissions are minimized:

- route classification: repository contents read;
- generic hosted read-only dispatch: contents read + actions write;
- deployment ACK: contents read + Issue write;
- physical secrets remain in the existing deployment child workflow/GitHub Environment.

Credentials never appear in Issue commands, registry values, evidence payloads or router logs. The generic dispatcher receives only the ephemeral GitHub job token and may call only the same-repository Actions dispatch endpoint selected from a validated registry contract.

## Current historical projection observation

Stage 2AD returned `exact_match_count=2`. That is ambiguous under the accepted classification. It is not a reusable projection candidate and not deployment-success evidence. This framework does not modify or repair those historical public Deployments.

## How to add a command without rebuilding the ingress

For a new hosted read-only operation:

1. Add the target workflow with `workflow_dispatch`, hosted runner and least privileges.
2. Add one command contract to `command-control-registry.json`: exact regex, typed arguments, operation class, literal workflow path, `ref=main`, read-only safety flags, evidence/idempotency/recovery metadata and optional argument-to-workflow-input mapping.
3. If target-bound, use/add a target contract with explicit allowed operation.
4. Add positive and negative tests, including malformed arguments, rerun/replay behavior and capability-escalation attempts.
5. Require exact PR-head hosted CI and exact merged-main push guards.
6. Update the development authority checkpoint before execution when #179 requires it.

The core ingress and generic dispatcher do not change for this class of command.

For a destructive/physical operation, do **not** use `dispatch_workflow`. Define/extend a controller-owned privileged handler only after semantic identity, durable intent, serialization, postcondition and UNKNOWN recovery are specified and policy-tested. The generic adapter is intentionally incapable of becoming that bypass.

## Permanent fitness requirements

CI must fail if:

- more than one private `issue_comment` production ingress exists;
- an unknown/incomplete command or target contract is introduced;
- regex captures and typed argument schema drift;
- a route accepts user-supplied workflow/ref/path/JSON/shell values;
- generic workflow dispatch is not read-only/non-destructive or targets a non-hosted/physical workflow;
- a read-only route claims physical mutation domains;
- a destructive route lacks semantic idempotency, serialization, terminal/postcondition evidence or UNKNOWN/no-blind-retry recovery;
- existing deployment semantic identity or canonical state-machine invariants weaken;
- the ingress gains direct self-hosted/ADB/phone/provider/secret capability;
- VM ceases to be deferred without an accepted adapter;
- the single destructive phone dispatch callsite or phone target-global serialization invariant weakens.
