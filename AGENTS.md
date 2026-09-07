# Deployment Controller agent contract

Before any repository or production-state change, read the newest authoritative checkpoint in `iamaman11/mobile-proxy` Issue #179 and revalidate relevant PRODUCT/Controller mains, protected checks and immutable Product Release identity. PRODUCT #179 is the only dynamic development/operations cursor.

Canonical working method: `iamaman11/mobile-proxy/STAGE_WORKFLOW.md`.

## Context recovery and budget

Use exactly:

`PRODUCT AGENTS.md -> PRODUCT STAGE_WORKFLOW.md -> newest PRODUCT #179 checkpoint -> current subordinate Stage Issue -> only stage-relevant permanent references`.

Do not reconstruct current work from historical Issues/plans, chat memory, stale SHAs, hand-written `CURRENT` markers or GitHub Deployment projection.

Do not load long Issue histories by default:

- PRODUCT #179: metadata/body + **last owner-authored authoritative checkpoint comment only**; walk backward minimally only if needed;
- current Stage Issue: body + only newest stage-relevant comments needed to resume;
- Controller #1: exact causal command/intent/terminal comment IDs only; never full ledger for general context;
- Controller #97: optional reusable rooted-phone observation/transport diagnostic reference only when the current stage/probe needs it.

## Stage workflow

One stage has one subordinate Stage Issue in its owning repository. Implementation progress lives in the stage branch/PR; significant non-code decisions/evidence live in the Stage Issue; #179 carries only authority/stage boundaries.

A checkpoint opening a stage authorizes the whole stage within its mission/scope/hard boundaries/exit criteria. `NEXT ALLOWED ITEM` is a starting action, not a stop point.

Routine commits, PR/CI repair, deterministic in-stage fixes, read-only observations, bounded evidence collection, local-agent requests/results, protected merge and post-merge checks are not checkpoint reasons. Continue until real stage exit.

Finished functional slice + direct tests -> commit. Significant non-code finding/blocker/evidence -> Stage Issue. At exit: final Stage Issue summary -> close completed -> one PRODUCT #179 checkpoint opening the next stage.

## Phone facts and local-agent evidence

Physical phone state must be observed, never guessed.

Prefer Controller observer/target-adapter paths. If an exact fact cannot be obtained reliably, or validation inherently requires device UI/local-workstation/physical interaction, ask the local agent for the narrow exact observation/interaction and specify evidence to return. Never ask the agent to improvise or “try things”.

Classify every local-agent result exactly once as:

- `controller_capability_gap` — repeatable/decision-critical observation Controller should reasonably expose;
- `human_only_physical_observation` — inherently device-UI/physical/modem/operator interaction;
- `one_off_observation` — bounded evidence without demonstrated reusable Controller need.

For a capability gap, record the exact missing fact, why Controller could not provide it, which decision/exit criterion depends on it and whether recurrence/ambiguity justifies automation. Implement only the smallest current-stage observation capability when demonstrated, stage-relevant, materially reduces guessing/UNKNOWN/manual dependence and is simpler than repeated local assistance.

Evidence routing:

- stage-level conclusion/classification/blocker -> current Stage Issue;
- reusable long-form rooted-phone probe/transport diagnostic -> optional Controller #97, linked by exact comment from the Stage Issue;
- one-off stage evidence -> Stage Issue only;
- machine command/intent/terminal -> Controller #1 only;
- authority/stage boundary -> PRODUCT #179 only.

If a Controller-owned decision/postcondition/recovery repeatedly depends on a safely machine-observable local-agent fact, treat it as a Controller design smell and close the smallest necessary observation gap before the earliest dependent stage exits.

Local-agent assistance is never deployment mutation authority. It must not bypass immutable Release identity, durable intent, target-global serialization, exactly-once destructive dispatch, postcondition or UNKNOWN reconciliation. Raw/manual destructive ADB remains forbidden as a Controller shortcut unless a newer owner checkpoint explicitly defines another physical-test boundary.

## Controller boundary and kernel

This repository owns deployment ingress, admission, target serialization/observation/adapters, durable mutation intent, exactly-once destructive dispatch, postconditions, recovery/quarantine and canonical runtime execution evidence. It must not become PRODUCT source/build/tag/Release authority.

Normal flow:

```text
exact immutable Product Release
  -> semantic request
  -> admission
  -> target lock
  -> observe
  -> durable intent if mutation is required
  -> apply at most once
  -> independent observe/postcondition
  -> canonical terminal
```

`UNKNOWN` permits read-only reconciliation, never blind destructive retry. GitHub Deployment status is projection only.

## Architecture discipline

Architecture work is stage-mapped, not a parallel roadmap. Add Controller capability only for a demonstrated current need. Prefer a thin target-adapter observation/operation over a new framework. Stage 6 is the first normal point for shared phone/VM target abstractions from demonstrated duplication; do not pre-generalize during phone stages.