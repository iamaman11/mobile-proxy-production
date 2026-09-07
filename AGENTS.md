# Deployment Controller local agent overlay

Global project governance is not duplicated in this repository.

Canonical cross-project sources live in PRODUCT (`iamaman11/mobile-proxy`):

- dynamic stage/operations authority — Issue #179;
- working method/checkpoint/local-agent rules — `STAGE_WORKFLOW.md`;
- cross-plane ownership — `docs/operations/project-authority.md` plus v2 authority/topology/Product Release contracts.

This file contains only Controller-local rules. If it conflicts with PRODUCT #179 or PRODUCT `STAGE_WORKFLOW.md`, PRODUCT authority wins.

## Context recovery

After context loss use:

`PRODUCT AGENTS.md -> PRODUCT STAGE_WORKFLOW.md -> last authoritative PRODUCT #179 checkpoint -> current subordinate Stage Issue -> only stage-relevant references`.

Keep context bounded:

- PRODUCT #179: metadata/body + last owner-authored checkpoint comment only; never full history for normal recovery;
- current Stage Issue: body + only newest comments needed to resume;
- Controller #1: exact causal command/ACK/intent/terminal/recovery comment IDs only when runtime evidence is needed;
- Controller #97: optional reusable rooted-phone diagnostic/probe reference, on demand only.

Do not reconstruct current work from old issue bodies, chat history, GitHub Deployment projection, stale SHAs, A-H/Item15-23 history or static `CURRENT` markers.

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

## Phone/local-agent boundary

Physical phone state is observed, never guessed. Prefer Controller observer/target-adapter evidence.

If the required fact cannot be obtained reliably, or validation inherently requires device UI/local-workstation/physical interaction, follow PRODUCT `STAGE_WORKFLOW.md`: request the narrow exact local-agent observation/interaction and classify the result as `controller_capability_gap`, `human_only_physical_observation`, or `one_off_observation`.

The local agent is never deployment mutation authority. Raw/manual destructive ADB must not bypass immutable Release identity, durable intent, target serialization, exactly-once dispatch, postcondition or UNKNOWN reconciliation unless a newer PRODUCT #179 checkpoint explicitly defines another test boundary.

A repeatable safely machine-observable local-agent dependency is a Controller capability-gap candidate, not automatic framework permission. Prefer the smallest demonstrated target-adapter observation improvement.

## Controller-local engineering discipline

- Work on a topic/stage branch.
- Finished meaningful code/docs + direct tests -> commit; significant non-code result -> current Stage Issue.
- Routine PR/CI repair, known-state fixes, read-only observation, protected merge/post-merge checks and local-agent evidence are not stage stop points.
- Add capability only for demonstrated current need; prefer thin adapters over new frameworks.
- Stage 5 owns phone-baseline simplification; Stage 6 is the first normal point for extracting shared phone/VM abstractions from two real implementations.
- VM/provider mutation remains fail-closed until PRODUCT #179 opens Stage 6.

For checkpoint cadence, continuous stage completion and detailed capability-gap handling, use PRODUCT `STAGE_WORKFLOW.md` as the single source of truth.
