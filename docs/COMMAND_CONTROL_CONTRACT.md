# Production Command / Control Contract

## Authority and ownership

`iamaman11/mobile-proxy-production` owns the Deployment Controller. Private Issue #1 is the sole production command ingress. Public Issue #179 remains the migration/execution authority cursor during development and hardening.

The command ingress is not an arbitrary workflow runner. A comment is admissible only when the repository, Issue, owner, exact syntax, current controller revision, command contract, target contract and static handler mapping all agree. Any ambiguity fails closed before dispatch.

## Declarative control plane

The core classifier is `.github/scripts/issue_command_router.py`.

Command contracts live in `.github/production/command-control-registry.json`. Target contracts live in `.github/production/targets.json`.

A command contract declares at minimum:

- exact anchored syntax;
- operation and operation class;
- static handler, workflow and ref policy;
- read-only/destructive classification;
- allowed targets and physical domains;
- phone/VM/release capability requirements;
- concurrency domain;
- semantic identity and idempotency policy;
- evidence policy;
- recovery policy.

The classifier validates the registries before it accepts any command. Workflow names and refs are never supplied by Issue text and are never interpolated into dispatch endpoints.

## Active routes

Only these routes are currently enabled:

1. `/observe-public-deployment-projection`
   - hosted read-only observation;
   - literal `public-deployment-projection-observer.yml` dispatch on `main`;
   - original router run attempt only, so rerunning the ingress cannot issue a second observer dispatch.

2. `/deploy <target> <vX.Y.Z>`
   - preserves the existing `production-deployment-request.v2` request builder and semantic identity;
   - routes only to the existing Deployment State Machine in `release-deployment.yml`;
   - `phone-production` is the accepted physical adapter;
   - `vm-production` remains fail-closed until its adapter is explicitly accepted.

3. `/runner-android-build-tools-bootstrap <exact-public-main-sha>`
   - routes only to the existing bounded runner-tooling workflow;
   - it is runner-tooling mutation, not phone mutation, and retains its own idempotent exact-package contract.

No additional destructive command is activated by the registry framework.

## Future command namespace

The framework can represent operation classes such as `OBSERVE`, `DIAGNOSTIC`, `VERIFY`, `BUILD`, `RELEASE_VERIFY`, `DEPLOY`, `ROLLBACK`, `RECOVER`, `RECONCILE`, `STATUS` and bounded runner tooling.

Names such as `/diagnose`, `/verify`, `/rollback`, `/recover`, `/reconcile` and `/status` are not automatically enabled. Each future route requires an explicit registry contract, a static handler/workflow mapping and permanent tests before activation. A registry entry alone must never grant a new physical capability accidentally.

## Product and controller identity

Product identity and controller identity are independent:

- `product_release = vX.Y.Z` identifies an immutable public Product Release;
- `controller_revision = exact private Git SHA` identifies the Deployment Controller revision executing the operation;
- `target` identifies the physical serving target.

A deployment command never means “build and deploy current HEAD”. It resolves the immutable Product Release contract for the requested version.

## Semantic request identity and duplicate handling

For deployment, the semantic request identity remains the existing `req-sha256:` digest over the normalized deployment schema, operation, target and Product Release tag. Comment ID, workflow run ID and run attempt are provenance/execution identities, not a new semantic request.

Therefore repeated comments for the same deployment request do not imply a new destructive operation. The durable Issue #1 evidence ledger reconciles reusable admission, intent, terminal and duplicate state for the semantic request.

Read-only observer routing uses a stricter transport rule: only the original ingress run attempt may dispatch the observer. A workflow rerun is refused before dispatch.

Runner-tooling operations retain their operation-specific idempotency contract and must fail closed if existing persistent tooling differs from the pinned package.

## Mutation lifecycle and evidence

A destructive deployment is not successful because a workflow is green. Canonical execution evidence must preserve the existing lifecycle, including the equivalent of:

`REQUEST -> ACK -> ADMISSION -> INTENT -> DISPATCH -> TERMINAL -> independent postcondition`

`ACK` identifies the controller execution. `ADMISSION` does not itself grant mutation authority. Mutation requires durable pre-dispatch intent. Canonical terminal state plus independent target postcondition determines acceptance. Public GitHub Deployment status is only a projection of canonical private truth.

## UNKNOWN and recovery

`UNKNOWN` is distinct from `FAILED`.

If destructive dispatch may have occurred and the outcome is not proven, the controller must not perform a blind retry and must not synthesize success. Only bounded read-only recovery/reconciliation may re-establish truth before another destructive action can become admissible.

`RECOVERED` is not the same as `ACCEPTED`. `UNKNOWN`, `RECOVERED` and quarantine states must remain fail-closed in public success projection.

Any future destructive command contract must explicitly carry semantic idempotency, serialization, terminal/postcondition evidence and an `UNKNOWN` / no-blind-retry recovery policy. The registry validator rejects incomplete destructive contracts.

## Target serialization

Target mutation is serialized by target/domain contract, not by comment or workflow-run identity. `phone-production` retains the existing global mutation serialization in the Deployment State Machine. Future targets must define their own serialization domain before physical mutation is enabled.

Read-only observation may run outside the physical mutation lock only when its operation contract explicitly declares no physical mutation domain.

## Permission and secret boundary

The sole Issue ingress receives only the permissions needed by each job. It has no direct phone, ADB, provider or secret-bearing execution step.

Higher privileges and target secrets remain confined to the accepted child workflows and GitHub Environments. Commands never contain PATs, credentials, secret values, arbitrary URLs, filesystem paths, JSON payloads, shell fragments, workflow names or Git refs.

The observer dispatch job receives only `contents: read` and `actions: write`. Deployment ACK receives the Issue write permission it needs. Physical deployment secrets remain inside the existing deployment child workflow/environment boundary.

## Current historical public projection observation

The bounded Stage 2AD observation returned `exact_match_count=2`. That is ambiguous under the accepted classification and is not a reusable deployment projection and not evidence of deployment success. The command-control framework does not modify or repair those historical public Deployments.

## Adding a future command

A future command is complete only when all of the following are true:

1. Add one explicit command contract to `command-control-registry.json`.
2. If target-bound, use an existing accepted target contract or add/accept a target contract first.
3. Add one static handler/workflow mapping to the sole `production-control-router.yml`; never dynamically dispatch a registry-provided workflow/ref.
4. Add exact parser/negative tests, including malformed input and capability-escalation cases.
5. For mutation, prove semantic identity, idempotency, serialization, durable intent, terminal/postcondition evidence and UNKNOWN/no-blind-retry recovery.
6. Keep secrets/physical adapters in the child workflow/environment, not the ingress.
7. Require hosted policy CI on the exact PR head and exact merged `main` revision.
8. Update the authoritative development checkpoint before executing any newly enabled production operation when the migration cursor requires it.

If any required metadata or evidence is absent, the route must remain disabled or the classifier/policy must reject it.

## Permanent fitness requirements

CI must fail if:

- more than one private `issue_comment` production ingress exists;
- an unknown/incomplete command or target contract is introduced;
- a route accepts a user-supplied workflow or ref;
- a read-only route claims physical mutation domains;
- a destructive route lacks semantic idempotency, serialization, terminal/postcondition evidence or UNKNOWN/no-blind-retry recovery;
- existing deployment semantic identity or canonical state-machine invariants are weakened;
- the ingress acquires direct self-hosted/ADB/phone/provider/secret capability;
- the single destructive phone dispatch callsite or target-global serialization invariant is weakened.
