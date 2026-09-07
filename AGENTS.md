# Deployment Controller agent contract

Before any repository or production-state change, read the newest authoritative checkpoint in `iamaman11/mobile-proxy` Issue #179 and revalidate PRODUCT/Controller mains plus relevant protected checks. PRODUCT #179 is the only dynamic development/operations stage cursor; this repository's issues are subordinate unless #179 explicitly says otherwise.

The canonical working method is `iamaman11/mobile-proxy/STAGE_WORKFLOW.md`. If local text conflicts with the newest #179 checkpoint, #179 wins.

## Context recovery and budget

After context loss use only:

`PRODUCT AGENTS.md -> PRODUCT STAGE_WORKFLOW.md -> newest PRODUCT #179 checkpoint -> current subordinate Stage Issue -> only stage-relevant permanent standards/contracts`.

Keep the recovery bounded:

- **PRODUCT #179:** read metadata/body and the last comment only; require an owner-authored authoritative checkpoint. Walk backward minimally only if needed. Never load the full historical comment stream for normal recovery.
- **Current Stage Issue:** read its body and only the newest stage-relevant comments needed to resume.
- **Controller #1:** machine command/intent/terminal ledger only. Read exact causal comment IDs referenced by current stage/Controller evidence; never load the full ledger for general context.
- **Controller #97:** optional reusable rooted-phone observation/diagnostic reference. Open only when the current stage needs a reusable probe/transport fact; it is not current phone state or a second stage journal.
- Static roadmaps, acceptance catalogs, backlogs and historical issues are on-demand references, not extra mandatory recovery hops.

Do not reconstruct current work from old Issue bodies, historical Item15-23/Item19-20 or A-H plans, chat memory, GitHub Deployment projection, stale SHAs or hand-written `CURRENT` markers.

## Stage workflow

**Analyze only enough to act. Save every meaningful result durably. One stage has one subordinate Stage Issue in its owning repository; implementation progress lives in the stage branch/PR, working decisions/evidence live in the Stage Issue, and #179 carries only authority/stage boundaries. A checkpoint that opens a stage authorizes the whole stage within its scope and hard boundaries: continue until its real exit criteria are satisfied.**

For a Controller-owned stage:

1. Create exactly one subordinate Stage Issue here with mission, scope, hard boundaries, exit criteria and PR links. It is not authority.
2. After the first completed code/docs slice, create the stage branch and stage PR. Keep later slices and bounded CI fixes in that PR.
3. Finished functional slice + direct tests -> commit immediately.
4. Important decision/finding/blocker/evidence with no ready code -> record it in the Stage Issue with enough detail to resume without repeating analysis.
5. Routine implementation/CI fixes -> commit, not Issue commentary.
6. PR-ready, commits, red/green CI, deterministic known-state repair, read-only observations, ordinary evidence collection, protected merge/post-merge checks and local-agent evidence requests/results are not stop points inside the stage.
7. At stage exit: final Stage Issue summary -> close completed -> one PRODUCT #179 checkpoint opening the next stage.

`NEXT ALLOWED ITEM` is a starting action, not a one-step permission token. Do not manufacture intermediate #179 checkpoints merely to restate routine progress.

## Phone interaction and evidence routing

Physical phone state must be observed, never guessed from chat history, workflow color, elapsed time, timeout wording or expected architecture.

1. Prefer Controller observer/target-adapter paths.
2. If an exact fact cannot be obtained reliably, or validation inherently requires device UI/local-workstation/physical interaction, ask the local agent for the **narrow exact observation/interaction** and specify the evidence to return.
3. Do not ask the local agent to improvise or “try things”.
4. If a phone-dependent conclusion is not proven, keep it unknown/unproven and request the missing evidence.
5. Local-agent assistance is not a checkpoint and is never deployment mutation authority.

Every local-agent result is classified exactly once as:

- `controller_capability_gap` — repeatable/decision-critical machine-observable fact Controller should reasonably expose;
- `human_only_physical_observation` — inherently device-UI/physical/modem/operator interaction;
- `one_off_observation` — bounded evidence without demonstrated reusable Controller need.

For `controller_capability_gap`, record the exact missing fact, why Controller could not provide it, which current-stage decision/exit criterion depends on it, and whether recurrence/ambiguity justifies the smallest observation improvement. A gap does **not** automatically authorize framework code.

Evidence owners:

- **current Stage Issue** — stage-level conclusion, classification, blocker and acceptance relevance;
- **Controller #97** — only reusable long-form rooted-phone probe/transport diagnostics; link the exact #97 evidence comment from the Stage Issue instead of duplicating it;
- **Controller #1** — machine command/ACK/intent/terminal/recovery ledger records only;
- **PRODUCT #179** — authority/stage boundaries only.

One-off stage evidence stays in the Stage Issue. If a Controller-owned authoritative decision/postcondition/recovery repeatedly depends on a safely machine-observable local-agent fact, treat that dependency as a Controller design smell and close the smallest necessary observation gap before the earliest dependent stage exits.

Raw/manual destructive ADB must never bypass immutable Release identity, durable mutation intent, target-global serialization, exactly-once destructive dispatch, postcondition or UNKNOWN reconciliation unless a newer owner checkpoint explicitly defines another physical-test boundary.

## Controller boundary and safety kernel

This repository owns deployment ingress, admission, durable mutation intent, target serialization/adapters, exactly-once destructive dispatch, postconditions, recovery/quarantine and canonical deployment evidence. It must not become PRODUCT source/build/tag/Release authority.

Normal flow remains:

```text
exact Product Release
  -> semantic request
  -> admission
  -> target lock
  -> observe
  -> durable intent if mutation is required
  -> apply at most once
  -> independent observe/postcondition
  -> canonical terminal
```

`UNKNOWN` permits read-only reconciliation, never blind destructive retry. GitHub Deployment is projection, not canonical runtime truth. Workflow success is not an independent phone postcondition.

## Architecture discipline

Architecture work is stage-mapped, not a parallel roadmap. Add Controller capability only for a demonstrated current need. Prefer a thin target-adapter observation/operation over a new framework. Stage 5 owns phone-baseline simplification. Stage 6 is the first normal point for shared phone/VM target abstractions from demonstrated duplication; do not pre-generalize during phone stages.

No more than one completed meaningful slice may remain only local/chat. Commit finished code/docs or record significant non-code evidence in the current Stage Issue before switching context.