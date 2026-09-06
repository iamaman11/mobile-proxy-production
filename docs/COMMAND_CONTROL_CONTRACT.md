# Production Command / Control Contract

## Authority and ownership

`iamaman11/mobile-proxy-production` owns the Deployment Controller. Issue #1 is the sole production command ingress. PRODUCT Issue #179 is the development/migration execution cursor; accepted production request identity remains cursor-free.

The Controller source/policy repository is public. Confidentiality attaches to secrets, target bindings, raw target identifiers, credentials, sensitive rendered configuration and unsafe raw runtime/device logs, not to repository visibility. Durable public evidence must remain bounded and non-sensitive.

A comment is admissible only when repository, Issue, owner, exact syntax, current controller revision, command contract, target contract and handler contract all agree. Any ambiguity fails closed before execution.

## Declarative control plane

The core classifier is `.github/scripts/issue_command_router.py`.

Command contracts live in `.github/production/command-control-registry.json`. Target contracts live in `.github/production/targets.json`.

Each command contract declares:

- exact anchored syntax and typed generic argument schema;
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

The `deployment` handler is intentionally narrower than the generic extension surface. Its registry argument schema remains the established `target+release` deployment contract, while the exact route grammar may additionally distinguish the explicit `retry-deploy` form. The dedicated deployment parser then validates the lineage token as an exact `req-sha256:<64 hex>` semantic request id and incorporates it into the deployment request contract. The lineage token cannot become a workflow/ref/path/JSON/shell input and is revalidated again against trusted durable evidence before Release/public Deployment/target access.

## One ingress, bounded handler classes

`.github/workflows/production-control-router.yml` is the only `issue_comment` production ingress.

It contains three bounded handler classes:

1. `dispatch_workflow` — generic adapter for registry-allowlisted **read-only hosted** `workflow_dispatch` operations. The adapter re-loads and validates the registry, requires current Controller `main` to equal the comment-event controller SHA, derives the workflow/ref only from the trusted registry and dispatches through GitHub-native API. It cannot dispatch a destructive route.
2. `deployment` — the Deployment Controller v2 path. It preserves `production-deployment-request.v2`, durable ACK/admission/intent/terminal evidence, target serialization, recovery and independent postcondition semantics. It accepts ordinary deploy plus the explicitly bounded pre-mutation REFUSED retry grammar described below through the same destructive route and execution engine.
3. `workflow_call` — explicitly bounded reusable control workflows whose privilege/idempotency contract cannot be represented as generic read-only dispatch. Current use is the accepted Android Build Tools runner bootstrap.

Adding another hosted read-only observer/diagnostic/verify route does **not** require modifying the core ingress: add the registry contract, target contract if needed, target workflow and tests. New privileged/mutating handler classes are never inferred automatically; they require an explicit controller design and policy change.

## Active routes

Only these routes are enabled:

1. `/observe-public-deployment-projection`
   - hosted read-only observation;
   - registry maps it to `public-deployment-projection-observer.yml`, `ref=main`;
   - only original ingress run attempt may dispatch, preventing rerun-created duplicate observation.

2. `/verify-release phone-production <vX.Y.Z>`
   - hosted read-only immutable Product Release admission proof;
   - registry maps it to `product-release-admission-proof.yml`, `ref=main`;
   - it has no phone access and cannot perform deployment mutation.

3. Deployment operation `deploy-product-release`, with two exact command forms through the same destructive route:

   ```text
   /deploy <target> <vX.Y.Z>
   /retry-deploy phone-production <vX.Y.Z> <prior-semantic-request-id>
   ```

   Ordinary `/deploy`:
   - preserves the existing `production-deployment-request.v2` semantic identity;
   - routes only to `release-deployment.yml`;
   - `phone-production` is the active physical adapter;
   - `vm-production` remains fail-closed/deferred until its adapter is separately accepted.

   Explicit `/retry-deploy`:
   - is admitted only for `phone-production`;
   - creates a distinct semantic request by including the exact prior semantic request id as immutable lineage;
   - requires trusted durable history to contain exactly one valid matching prior `REFUSED` terminal with `mutation_performed=false`, `recovery_required=false`, and no durable mutation intent;
   - requires exact prior target and Product Release match;
   - rejects `UNKNOWN`, `RECOVERED`, `QUARANTINED`, mutation-bearing/intent-bearing, mismatched, malformed, non-terminal or ambiguous history before target access;
   - never uses GitHub run/comment identity as semantic lineage and is never automatic;
   - uses the same `release-deployment.yml`, target-global serialization and destructive dispatch boundary as ordinary deploy.

4. `/runner-android-build-tools-bootstrap <exact-product-main-sha>`
   - routes only to the existing bounded runner-tooling reusable workflow;
   - runner-tooling mutation is separate from phone mutation and retains its exact-package idempotency contract.

No second destructive deployment route is activated by the retry form. It is a bounded generation of the existing deployment operation and does not weaken the no-blind-retry boundary.

## Typed arguments and extension surface

Supported generic registry argument types are intentionally bounded: target, semantic version, Git SHA, bounded identifier and explicit enum. The regex named captures must exactly equal the declared generic argument schema. Unknown captures, unnamed captures, unknown fields and unknown types are rejected.

The dedicated deployment handler has one explicit special-case grammar boundary: retry lineage is syntax-bounded in the active deployment route and then parsed/validated by `deployment_command_router.py` and `deployment_request.py` as a semantic request id. It is not exposed to generic `dispatch_workflow` argument mapping. This exception exists only to preserve the ordinary v2 deployment argument/identity contract while giving a proven pre-mutation `REFUSED` a distinct, auditable request generation.

The framework recognizes operation classes such as `OBSERVE`, `DIAGNOSTIC`, `VERIFY`, `BUILD`, `RELEASE_VERIFY`, `DEPLOY`, `ROLLBACK`, `RECOVER`, `RECONCILE`, `STATUS` and bounded runner tooling. Names such as `/diagnose`, `/rollback`, `/recover`, `/reconcile` and `/status` are not enabled merely because the class exists.

Generic `dispatch_workflow` routes are permanently constrained to read-only/non-destructive operation contracts. A new physical/destructive operation cannot be smuggled through the generic dispatcher.

## Product and controller identity

Product and controller identity are independent:

- `product_release = vX.Y.Z` identifies an immutable public Product Release;
- `controller_revision = exact Controller Git SHA` identifies the executing Deployment Controller revision;
- `target` identifies the serving/physical target.

`/deploy phone-production v0.1.7` never means “deploy current HEAD”. It resolves the immutable `v0.1.7` Product Release under the existing Release resolver contract.

## Semantic request identity and replay

For ordinary deployment, semantic identity remains the existing `req-sha256:` digest over normalized deployment schema, operation, target and Product Release tag. Comment ID, workflow run ID and run attempt are provenance/execution identities and do not create a new ordinary semantic deployment request.

Repeated equivalent ordinary deployment comments therefore do not imply another destructive operation. The durable Issue #1 evidence ledger reconciles reusable admission, intent, terminal and duplicate state for the semantic request.

For an explicit pre-mutation REFUSED retry, the new semantic payload additionally contains `retry_of_request_id=<exact-prior-semantic-request-id>`. The resulting request id is distinct from the original and from a retry referencing any other prior request, while duplicate comments for the same retry lineage remain the same semantic request. The prior run id, comment id and workflow attempt are never retry lineage.

Retry lineage is not permission by itself. Before any Release/public Deployment/target access, admission requires exactly one trusted matching prior canonical `REFUSED` terminal, `mutation_performed=false`, `recovery_required=false`, exact target/release match, and zero durable mutation intents for that prior request. Any ambiguity fails closed.

Generic read-only dispatch may define stricter transport idempotency. The current observer is `single-run-attempt`: rerunning the ingress workflow is rejected before dispatch.

Runner-tooling keeps its own exact-package no-op/create semantics and fails closed if an existing persisted package differs.

## Mutation lifecycle and evidence

A green workflow is not deployment success. Canonical deployment evidence retains the equivalent lifecycle:

`REQUEST -> ACK -> ADMISSION -> INTENT -> DISPATCH -> TERMINAL -> independent postcondition`

`ADMISSION` alone grants no mutation authority. Durable mutation intent must precede destructive dispatch. Canonical terminal state plus independent target postcondition determines acceptance. Public GitHub Deployment status is only a projection of canonical Controller truth.

## UNKNOWN and recovery

`UNKNOWN != FAILED`.

If destructive dispatch may have occurred but the outcome is not proven, blind retry is forbidden and success must not be synthesized. Only bounded read-only recovery/reconciliation may restore truth before another destructive operation becomes admissible.

The explicit `/retry-deploy` path does not apply to `UNKNOWN`, any mutation-bearing request, or any request with durable mutation intent. It exists only for a separately proven pre-mutation `REFUSED` where the destructive boundary was never reached.

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

The current `/retry-deploy` form is not a generic extension mechanism: it is a bounded special case of the existing `deployment` handler, introduced only for the demonstrated pre-mutation `REFUSED` failure class and guarded by durable lineage evidence.

## Permanent fitness requirements

CI must fail if:

- more than one Issue #1 `issue_comment` production ingress exists;
- an unknown/incomplete command or target contract is introduced;
- generic regex captures and typed argument schema drift;
- retry-deploy can accept malformed lineage, VM target, missing lineage or an ineligible prior terminal/history;
- a route accepts user-supplied workflow/ref/path/JSON/shell values;
- generic workflow dispatch is not read-only/non-destructive or targets a non-hosted/physical workflow;
- a read-only route claims physical mutation domains;
- a destructive route lacks semantic idempotency, serialization, terminal/postcondition evidence or UNKNOWN/no-blind-retry recovery;
- ordinary deployment semantic identity or canonical state-machine invariants weaken;
- the ingress gains direct self-hosted/ADB/phone/provider/secret capability;
- VM ceases to be deferred without an accepted adapter;
- the single destructive phone dispatch callsite or phone target-global serialization invariant weakens.
