# mobile-proxy-production

Private production deployment/controller repository for `iamaman11/mobile-proxy`.

## Repository boundary

`iamaman11/mobile-proxy` is the **product repository**. It owns application source, product runtime logic, product Quality, build, Git tags, GitHub Releases, release manifests/provenance and product documentation.

`iamaman11/mobile-proxy-production` is the **deployment controller**. It owns:

- private Issue #1 production command surface;
- deployment State Machine and Transaction Kernel;
- deployment operation contracts;
- observers and Android target adapter (VM adapter only after Android proof);
- target-global mutation serialization;
- private target bindings and deployment secrets;
- durable mutation intent, canonical execution terminal and recovery/quarantine evidence;
- exactly-once physical dispatch semantics;
- automatic result classification;
- safe GitHub Deployment projection into the public product repository.

This repository must not copy or independently develop product source. The product/controller boundary is normative in `docs/adr/0001-product-release-deployment-controller-boundary.md`.

## Production input

Production never deploys "current public main". A deployment selects one exact published product Release:

```text
/deploy phone-production vX.Y.Z
```

The controller resolves and verifies the exact Release tag, source commit, target artifact, SHA-256 digest, release manifest and provenance before target access. `latest` is not deployment authority.

Runtime version identity is deliberately split:

```text
product_release      = tag + release_id + source_sha + artifact_digest + provenance
controller_revision  = exact private repository SHA
```

A new product commit therefore does not make the physical target stale by itself.

## Deployment State Machine

The private controller owns the deployment State Machine:

```text
REQUEST
→ AUTHORIZE
→ OBSERVE
→ INTENT
→ DISPATCH
→ VERIFY
→ ACCEPTED / REFUSED / UNKNOWN / RECOVERED / QUARANTINED
```

Core guarantees:

- pure deterministic reducer;
- durable intent before any destructive dispatch;
- exactly one destructive dispatch attempt per semantic request;
- no blind retry after dispatch may have reached the target;
- duplicate semantic request protection;
- fresh, independent postcondition observation;
- `UNKNOWN` can continue only through read-only recovery observation;
- `RECOVERED != ACCEPTED`;
- quarantine on contradictory postconditions;
- target-global mutation serialization.

## Canonical execution truth

Private durable terminal evidence is the execution truth. Each v2 deployment terminal is machine-readable and records the operation, semantic request ID, execution ID, controller revision, target, product Release identity, public Deployment ID, state/current step, facts, blocking predicates, mutation/postcondition/recovery fields, next allowed operation and evidence references.

Historical v1 Issue #1 terminals, including prior UNKNOWN/recovery evidence, are immutable historical records. The v2 controller uses separate evidence headings/schema and does not reinterpret or rewrite them.

## Public GitHub Deployments

The controller projects only safe status/history to `iamaman11/mobile-proxy` GitHub Deployments. Public Deployment records are **not** the State Machine and are **not** the canonical evidence store.

Stable environments:

- `phone-production`
- `vm-production` (reserved until a VM adapter is accepted after phone proof)

Projection:

- `ACCEPTED` → `success`
- `REFUSED` → `failure`
- `UNKNOWN` → `error`
- `QUARANTINED` → `failure`
- `RECOVERED` → `error` for the original deployment, never success

The projection credential must be separate from product-source credentials and limited to the minimum GitHub Deployments write capability required by the workflow.

## Current command surface

Issue #1 has one production ingress for v2 deployment:

```text
/deploy <target> <vX.Y.Z>
```

For now only `phone-production` can proceed beyond authorization. `vm-production` fails closed before Release projection or target access until the Android path has been proved end-to-end and a small reusable VM adapter is intentionally added.

Normal execution is automatic after one Issue #1 command: immediate ACK → Release admission → public Deployment queued/in-progress → target lock → observation → durable intent → at-most-one dispatch → independent verification → optional read-only UNKNOWN recovery → private canonical terminal → public terminal status.

Development Issue `iamaman11/mobile-proxy#179` is an architecture/migration/audit tracker only. It is not a runtime cursor and is not part of v2 semantic request identity.

## Secrets and privacy

Do not commit tokens, private keys, target serials or raw credential-bearing/device logs. Target identity is represented only by a derived binding identifier in durable evidence. Product release-signing material belongs with the product Release build boundary; target/deployment credentials remain private here.

Manual ADB/SSH/provider CLI is not the normal production control path.
