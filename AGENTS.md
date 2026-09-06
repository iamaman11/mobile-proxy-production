# Deployment Controller agent contract

Before any repository or production-state change, read the newest authoritative checkpoint in `iamaman11/mobile-proxy` Issue #179 and revalidate PRODUCT/Controller mains plus relevant protected checks. PRODUCT #179 is the only development/operations stage cursor; this repository's issues are subordinate unless #179 explicitly says otherwise.

The canonical project workflow is `iamaman11/mobile-proxy/STAGE_WORKFLOW.md`. If any local text conflicts with the newest #179 checkpoint, #179 wins.

## Stage workflow

**Analyze only enough to act. Save every meaningful result durably. One stage has one subordinate Stage Issue in its owning repository; implementation progress lives in the stage branch/PR, working decisions/evidence live in the Stage Issue, and #179 carries only authority/stage boundaries. Continue the stage until its real exit criteria are satisfied.**

For a Controller-owned stage:

1. Create exactly one subordinate Stage Issue here with mission, scope, hard boundaries, exit criteria and PR links. It is not authority.
2. After the first completed code/docs slice, create the stage branch and open the stage PR. Keep later slices and bounded CI fixes in that PR.
3. Finished functional slice + direct tests -> commit immediately.
4. Important decision/finding/blocker/evidence with no ready code -> comment in the Stage Issue with enough detail to resume without repeating analysis.
5. Routine implementation/CI fixes -> commit, not Issue commentary. Comment only for architecture/scope/authority changes or significant non-code evidence.
6. PR-ready, individual commits and ordinary CI iterations are not stop points. Continue through protected merge/post-merge acceptance when included in the stage.
7. At stage exit: final Stage Issue summary -> close Stage Issue -> one #179 checkpoint opening the next stage.

No more than one completed meaningful slice may remain only local/chat. Before switching context or ending a work session, commit finished code/docs or record the significant non-code result in the Stage Issue.

## Controller boundary

This repository owns deployment ingress, admission, durable mutation intent, target serialization/adapters, exactly-once destructive dispatch, postconditions, recovery/quarantine and canonical deployment evidence. It must not become PRODUCT source/build/tag/Release authority. No blind destructive retry after an ambiguous physical outcome.