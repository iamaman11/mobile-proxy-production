# Deployment Controller agent contract

Before any repository or production-state change, read the newest authoritative checkpoint in `iamaman11/mobile-proxy` Issue #179 and revalidate PRODUCT/Controller mains plus relevant protected checks. PRODUCT #179 is the only dynamic development/operations stage cursor; this repository's issues are subordinate unless #179 explicitly says otherwise.

The canonical project workflow is `iamaman11/mobile-proxy/STAGE_WORKFLOW.md`. If any local text conflicts with the newest #179 checkpoint, #179 wins.

## Context recovery

After context loss use exactly:

`PRODUCT AGENTS.md -> PRODUCT STAGE_WORKFLOW.md -> newest PRODUCT #179 checkpoint -> current subordinate Stage Issue -> only stage-relevant permanent standards/contracts`.

Do not reconstruct current work from old Issue bodies, historical Item15-23/Item19-20 or A-H plans, chat memory, GitHub Deployment projection, stale SHAs or hand-written `CURRENT` markers in static docs.

## Stage workflow

**Analyze only enough to act. Save every meaningful result durably. One stage has one subordinate Stage Issue in its owning repository; implementation progress lives in the stage branch/PR, working decisions/evidence live in the Stage Issue, and #179 carries only authority/stage boundaries. A checkpoint that opens a stage authorizes the whole stage within its scope and hard boundaries: continue until its real exit criteria are satisfied.**

For a Controller-owned stage:

1. Create exactly one subordinate Stage Issue here with mission, scope, hard boundaries, exit criteria and PR links. It is not authority.
2. After the first completed code/docs slice, create the stage branch and open the stage PR. Keep later slices and bounded CI fixes in that PR.
3. Finished functional slice + direct tests -> commit immediately.
4. Important decision/finding/blocker/evidence with no ready code -> comment in the Stage Issue with enough detail to resume without repeating analysis.
5. Routine implementation/CI fixes -> commit, not Issue commentary. Comment only for architecture/scope/authority changes or significant non-code evidence.
6. PR-ready, individual commits, red/green CI, deterministic known-state repair, read-only observations, ordinary evidence collection, protected merge/post-merge checks and local-agent evidence requests/results are not stop points when they remain inside the current stage. Continue through stage exit.
7. At stage exit: final Stage Issue summary -> close Stage Issue -> one PRODUCT #179 checkpoint opening the next stage.

`NEXT ALLOWED ITEM` identifies the next starting action, not a one-step permission token. Do not manufacture intermediate #179 cursors merely to restate routine progress. A concrete defect discovered inside the current stage should be repaired and verified inside the same stage when no authority/stage boundary is crossed.

No more than one completed meaningful slice may remain only local/chat. Before switching context or ending a work session, commit finished code/docs or record the significant non-code result in the Stage Issue.

## Phone interaction and local-agent assistance

Physical phone state must be observed, never guessed from chat history, workflow color, elapsed time, timeout wording or expected architecture.

1. Prefer Controller observer/target-adapter paths for phone observation and mutation.
2. If the exact phone fact needed to continue cannot be obtained reliably through available Controller observation, or validation inherently requires physical device UI/local-workstation interaction, explicitly ask the local agent for the narrow exact observation or interaction needed and specify the evidence to return.
3. A local-agent request/result is operational assistance, not a new checkpoint or stage stop. Record significant returned evidence in the current Stage Issue and continue the same stage.
4. Do not ask the local agent to improvise or “try things”.
5. The local agent is not deployment authority. It must not bypass immutable Release identity, durable mutation intent, target-global serialization, exactly-once destructive dispatch, postcondition or UNKNOWN reconciliation. Raw/manual destructive ADB remains forbidden as a Controller shortcut unless a newer owner checkpoint explicitly defines another physical-test boundary.
6. If a phone-dependent conclusion cannot be proven, classify it unknown/unproven and request the missing evidence rather than substitute a hypothesis.

### Mandatory capability-gap classification

Every local-agent result is classified as exactly one:

- `controller_capability_gap` — repeatable or decision-critical observation needed by deployment, recovery or operational validation that should reasonably be available through Controller `observe`/target-adapter semantics;
- `human_only_physical_observation` — inherently device-UI, physical, modem or operator interaction that should remain outside normal Controller automation;
- `one_off_observation` — bounded evidence with no demonstrated reusable Controller requirement.

For `controller_capability_gap`, record the exact missing fact, why Controller could not provide it, which decision/exit criterion depends on it, and whether recurrence/ambiguity justifies automation.

A capability gap does **not** automatically authorize code. Apply the project's complexity/necessity gate. Implement the smallest Controller observation capability inside the current stage when the gap is demonstrated, stage-relevant, reduces guessing/UNKNOWN/manual dependence and is simpler than repeated local assistance. Otherwise record/defer it to the earliest stage it actually blocks.

If a Controller-owned authoritative decision, postcondition or recovery classification repeatedly needs a safely machine-observable local-agent fact, accepting the relevant stage while preserving that dependency requires explicit justification; the default is to close the observation gap first.

## Controller boundary

This repository owns deployment ingress, admission, durable mutation intent, target serialization/adapters, exactly-once destructive dispatch, postconditions, recovery/quarantine and canonical deployment evidence. It must not become PRODUCT source/build/tag/Release authority.

Controller normal flow remains understandable as:

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

`UNKNOWN` permits read-only reconciliation, never blind destructive retry.

## Architecture discipline

Architecture work is stage-mapped, not a parallel roadmap. Add Controller capability only for a demonstrated current need. Prefer a thin target-adapter observation/operation over a new framework. Stage 6 is the first normal point for shared phone/VM target abstractions from demonstrated duplication; do not pre-generalize during phone stages.
