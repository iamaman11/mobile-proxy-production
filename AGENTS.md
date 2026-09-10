# Deployment Controller local agent overlay

Global project governance is not duplicated in this repository.

Canonical cross-project sources live in PRODUCT (`iamaman11/mobile-proxy`):

- dynamic stage/operations authority — Issue #179;
- working method/checkpoint/local-agent/diagnostic-order rules — `STAGE_WORKFLOW.md`;
- cross-plane ownership — `docs/operations/project-authority.md` plus v2 authority/topology/Product Release contracts.

This file contains only Controller-local rules. If it conflicts with PRODUCT #179 or PRODUCT `STAGE_WORKFLOW.md`, PRODUCT authority wins.

## Context recovery

After context loss use:

`PRODUCT AGENTS.md -> PRODUCT STAGE_WORKFLOW.md -> last authoritative PRODUCT #179 checkpoint -> current subordinate Stage Issue -> only stage-relevant references`.

Keep context bounded:

- PRODUCT #179: metadata/body + last owner-authored checkpoint comment only; never full history for normal recovery;
- current Stage Issue: compact current body + only newest comments needed to resume;
- Controller #1: exact causal command/ACK/intent/terminal/recovery comment IDs only when runtime evidence is needed;
- Controller #97: optional reusable rooted-phone diagnostic/probe reference, on demand only.

Do not reconstruct current work from old issue bodies, chat history, GitHub Deployment projection, stale SHAs, A-H/Item15-23 history or static `CURRENT` markers.

### Current command-surface source

For enabled Controller Issue #1 command ingress, treat `.github/production/command-control-registry.json` together with the protected router implementation/policy as the current machine-readable source. Do not infer that a command is current, enabled, safe or authorized from workflow filenames, old Issue prose, historical route inventories, or an older registry snapshot.

In particular, `.github/production/command-routes.json` is **not sufficient evidence of the current Issue #1 surface**. Do not use it as a reasoning shortcut. A route is current only when the protected current router/registry admits it under current PRODUCT authority.

## Controller ownership

This repository owns:

- deployment command ingress;
- admission and semantic request identity;
- target-global serialization;
- target observation/adapters;
- durable mutation intent;
- at most one destructive dispatch per intent;
- independent postcondition observation;
- recovery/quarantine and canonical runtime evidence.

It does **not** own PRODUCT source/build/signing/tag/Product Release authority.

Normal kernel:

```text
exact immutable Product Release + exact admitted Controller revision
  -> semantic request
  -> admission
  -> target lock
  -> observe
  -> durable intent if mutation is required
  -> apply at most once
  -> independent observe/postcondition
  -> canonical terminal
```

Ambiguous post-dispatch outcome is `UNKNOWN` and permits read-only reconciliation only. No blind destructive retry. Workflow success and GitHub Deployment projection are not independent target truth.

## Controller evidence routing

Each evidence class has one owner:

- **current Stage Issue** — current-stage conclusions, classifications, blockers, PR links and acceptance evidence;
- **Controller #1** — machine command/ACK/intent/terminal/recovery ledger only;
- **Controller #97** — reusable long-form rooted-phone probe/transport diagnostic mechanics only;
- **PRODUCT #179** — authority/stage boundaries only.

Do not duplicate one-off evidence across #97 and the Stage Issue. When reusable diagnostic detail belongs in #97, the Stage Issue stores only the concise conclusion/classification and exact #97 evidence link.

If the Stage Issue body contains superseded plans or stale command names, repair the body rather than relying on later comments to cancel large obsolete sections indefinitely.

## Phone/local-agent boundary

Physical phone state is observed, never guessed. Follow PRODUCT `STAGE_WORKFLOW.md`'s observation-before-hypothesis gate.

For phone Stage work, determine the owning evidence domain before selecting a command:

- runner/ADB/root readiness -> owning transport observer/preflight;
- exact installed Product Release identity -> exact Release observer;
- current runtime process/health/readiness -> operational observer;
- bounded resource behavior -> resource observer only after its current operational prerequisites are known;
- lifecycle recovery/restart/reboot/mismatch -> fixed governed lifecycle route only when the lifecycle action itself is the authorized experiment or repair.

A specialized resource/lifecycle workflow is not a substitute for unknown current operational state. Source inspection may explain an observed result, but it cannot establish that `runtime-supervisor`, `host-daemon`, `sing-box`, a listener or a health endpoint is currently present.

If the required fact cannot be obtained reliably, or validation inherently requires device UI/local-workstation/physical interaction, follow PRODUCT `STAGE_WORKFLOW.md`: request the narrow exact local-agent observation/interaction and classify the result as `controller_capability_gap`, `human_only_physical_observation`, or `one_off_observation`.

When that handoff is performed through the project chat bridge, the instruction must be a standalone assistant message as defined by PRODUCT `STAGE_WORKFLOW.md`; do not embed it inside discussion or a status update.

The local agent is never deployment mutation authority. Raw/manual destructive ADB must not bypass immutable Release identity, durable intent, target serialization, exactly-once dispatch, postcondition or UNKNOWN reconciliation unless a newer PRODUCT #179 checkpoint explicitly defines another test boundary.

A repeatable safely machine-observable local-agent dependency is a Controller capability-gap candidate, not automatic framework permission. Prefer the smallest demonstrated target-adapter observation improvement.

### ADB and rooted-shell transport ownership

Do not treat host transport lifecycle, read-only phone observation and phone mutation as one `ADB` category.

- The local ADB daemon is **ephemeral host tooling**. Runner cleanup may remove it between jobs. An accepted `adb start-server` used only to establish the local transport daemon is host-side readiness, not a phone mutation and not evidence that the registered target is healthy.
- A Controller/local diagnostic must not infer `missing`, `offline`, `unauthorized` or any stronger phone state merely because the ADB daemon was absent. Establish the allowed host transport first, then classify only the registered target through the owning adapter.
- When a local-agent task requires ADB-backed read-only evidence, the task must state whether host-side ADB server establishment is allowed. Never request ADB-backed evidence while simultaneously forbidding every possible daemon start unless a running daemon is an explicitly proven prerequisite.
- Rooted runtime mechanics are not free-form shell access. Before constructing or interpreting a rooted local-agent probe, inspect current `.github/controller/phone_target.py` and Controller #97. Current protected code is authoritative over historical diagnostic prose.
- The current canonical rooted-script transport is the fixed non-PTY argv `adb -s <registered-serial> shell -T su 0`, with the static POSIX script supplied through stdin. Do not invent `su -c`, `sh -c`, `su 0 sh -s`, alternate quoting, PTY, or another root-shell protocol for a Controller-owned observation.
- A failed root capability/marker proves only that the rooted transport contract was not established for that attempt. It does not prove BusyBox absence, runtime failure, phone corruption or mutation need. Preserve those downstream facts as unknown until the owning transport issue is classified.

The implementation should make transport prerequisites explicit rather than rely on accidental call order. If one target adapter silently depends on another adapter having started ADB earlier, treat that as a demonstrated Controller capability/ownership smell and close the smallest Stage-relevant gap instead of encoding the ordering only in operator memory.

## Controller-local engineering discipline

- Work on a topic/stage branch.
- Finished meaningful code/docs + direct tests -> commit; significant non-code result -> current Stage Issue.
- Routine PR/CI repair, known-state fixes, read-only observation, protected merge/post-merge checks and local-agent evidence are not stage stop points.
- Add capability only for demonstrated current need; prefer thin adapters over new frameworks.
- Stage 5 owns phone-baseline simplification; Stage 6 is the first normal point for extracting shared phone/VM abstractions from two real implementations.
- VM/provider mutation remains fail-closed until PRODUCT #179 opens Stage 6.

For checkpoint cadence, continuous stage completion, observation-first diagnostic ordering and detailed capability-gap handling, use PRODUCT `STAGE_WORKFLOW.md` as the single source of truth.
