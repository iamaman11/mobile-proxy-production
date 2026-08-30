# mobile-proxy-production

Private execution satellite for the canonical project repository:

- canonical repository: `iamaman11/mobile-proxy`
- canonical GitOps tracker: `iamaman11/mobile-proxy#90`

## Authority rule

This repository is **not** a source of project truth. It must not define an independent roadmap,
architecture, product behavior, release identity, provider desired state, ownership policy, or
production acceptance policy. If anything here conflicts with `iamaman11/mobile-proxy`, execution
must fail closed and the conflict must be reconciled in the canonical repository first.

The private repository exists only because the physical Android runner must not be attached to the
public source repository.

## Allowed contents

Only execution-boundary material belongs here:

- a thin GitHub Actions caller/shim required to reach the private `android-production` runner;
- private runner wiring that contains no credential values in Git;
- bounded non-secret execution evidence that cannot safely be public, with a safe summary or
  reference returned to the canonical repository;
- the reserved production-control Issue used as a command/audit transport.

Phone deployment logic, release contracts, manifests, desired state, documentation and acceptance
rules remain canonical in `iamaman11/mobile-proxy`. The preferred implementation is for this
private caller to invoke canonical public logic pinned to an immutable ref, or to execute a verified
immutable release artifact produced there.

## Forbidden

- application/source-code copies;
- independent manifests, architecture decisions or roadmap documents;
- Vultr credentials or Vultr lifecycle execution;
- secrets, private keys, tokens or raw credential-bearing logs in Git/Issues/artifacts;
- manual production SSH/ADB/provider CLI as the normal control path;
- deployment from a mutable branch or an unverified artifact.

## Current state

A read-only phone preflight caller is available through reserved Issue #1 using
`/phone-preflight <40-hex-canonical-sha>`. The command gate requires that SHA to equal the current
canonical public `main` before the private `android-production` runner can start. The runner then
executes the canonical preflight script fetched by that immutable SHA and may emit only bounded
non-secret evidence.

The caller requires the private `ANDROID_PRODUCTION_SERIAL` binding and performs no install,
restart, configuration change, deployment, rollback, or other phone mutation. No production
mutation workflow is enabled. Vultr credentials and lifecycle remain forbidden in this repository.
