# ADR-0001: Product Release / Deployment Controller Boundary

- Status: Accepted
- Date: 2026-09-04
- Owners: `iamaman11/mobile-proxy`, `iamaman11/mobile-proxy-production`

## Context

The earlier implementation placed the physical deployment State Machine and Universal Transaction Kernel in the public product repository and treated the private repository as a thin runner shim. That coupled production execution authority to public source SHA/cursor checkpoints, made ordinary product commits capable of invalidating physical execution assumptions, and forced normal production classification to be reconstructed from development Issue comments and multiple workflow logs.

The transactional properties developed in that period are still valuable. The ownership boundary was the mistake, not the guarantees.

## Decision

### Product repository

`iamaman11/mobile-proxy` owns the product and its immutable release output:

- Android/VM product source;
- product runtime behavior;
- product tests and Quality;
- build and release packaging;
- annotated version tags;
- GitHub Releases;
- target artifacts, release manifest, checksums and provenance;
- product documentation.

An ordinary product commit/PR ends at product Quality/merge. It does not contact a phone/VM, dispatch a deployment controller, or invalidate target state merely because the source SHA changed.

### Deployment controller repository

`iamaman11/mobile-proxy-production` owns production control:

- Issue #1 command ingress;
- deployment State Machine and Transaction Kernel;
- controller operation contracts;
- target observers/adapters;
- target mutation locks;
- target binding/secrets and deployment policy;
- exactly-once dispatch boundary;
- durable mutation intent and terminal evidence;
- UNKNOWN recovery/quarantine;
- duplicate semantic-request handling;
- automatic classification and next-operation result.

The canonical deployment transition model is:

```text
REQUEST → AUTHORIZE → OBSERVE → INTENT → DISPATCH → VERIFY
        → ACCEPTED / REFUSED / UNKNOWN / RECOVERED / QUARANTINED
```

### Release identity is deployment input

A controller request selects one explicit Release tag, never `latest` and never mutable `main`.

```text
product_release:
  tag
  release_id
  source_sha
  artifact_name
  artifact_digest
  manifest_digest
  provenance_digest

controller_revision:
  private_repo_sha
```

The controller resolves an annotated tag to an exact source commit and verifies the GitHub Release plus target artifact/digest/manifest/provenance. If GitHub Release immutability is enabled, that is the preferred immutability control. Otherwise admission requires the equivalent fail-closed identity chain defined by the controller and repository tag/release policy.

### GitHub Deployments are a projection

The private canonical terminal is execution truth. The controller may create/update GitHub Deployments in the public product repository as a safe status/history projection for stable environments:

- `phone-production`
- `vm-production`

Projection rules:

```text
ACCEPTED    → success
REFUSED     → failure
UNKNOWN     → error
QUARANTINED → failure
RECOVERED   → error for the original deployment
```

A public Deployment must never report success before the corresponding private canonical terminal is durably `ACCEPTED` with independent postcondition verification.

### Tracker and historical evidence

Public Issue #179 remains a development/migration/audit tracker. It is not a runtime cursor. A v2 semantic production request cannot depend on an Issue #179 comment ID.

Historical Issue #1 records and v1 UNKNOWN/recovery evidence remain immutable historical evidence. They are not rewritten to fit the new schema.

## Required invariants

The migration preserves and moves the useful transactional guarantees into the private controller:

1. deterministic pure reducer;
2. durable intent before target mutation;
3. exactly one destructive dispatch attempt;
4. no blind retry if dispatch may have reached the target;
5. causal/fresh observations scoped to the affected target/domain;
6. independent postcondition verification;
7. UNKNOWN can continue only through read-only recovery observation;
8. `RECOVERED != ACCEPTED`;
9. quarantine for contradictory/unsafe state;
10. duplicate semantic request cannot create a second mutation;
11. global mutation serialization per target;
12. public Deployment success requires canonical private `ACCEPTED`.

## Consequences

- Product and controller versions evolve independently.
- A new product commit does not stale an installed Release.
- Production controller code no longer checks out arbitrary public `main` as its kernel.
- Release build/signing belongs to the product release boundary; target/deployment credentials belong to the private controller boundary.
- Android is the first accepted target adapter. A VM adapter is added only after the phone path is proved and only as the smallest abstraction needed to reuse the same controller guarantees.
- Legacy controller-v1 workflows/scripts may remain in Git history or explicitly historical compatibility paths, but they must not be reachable from the active v2 command surface or be normative architecture.
