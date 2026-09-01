# Taskboard Management and Review

Read this reference only for explicit dispatch, review, rework, traceability, relation, or board-management requests. DoTasks-generated lifecycle conversations use `$dotasks-lifecycle` instead.

## Dispatch and execution

- Do not implement a queued task in the intake or dispatcher conversation.
- `$dotasks-controller` calls `claim_next_dispatch` with independent `code_review` and `development` stages. Each lane has one active dispatch, different lanes may run concurrently, and all claims still preserve dependency, project-serialization, and target-lock gates. Requirement decomposition shares the development lane.
- A new or resumed native worker task must use the persisted `$dotasks-lifecycle` prompt and be bound with `bind_native_dispatch` only after a real thread ID exists. Never bind the controller conversation or a `clientThreadId`.
- Execution starts from saved targets, location evidence, acceptance plan and `RUN_CONTEXT_JSON`. Current code is authoritative; expand location only when a saved target is missing or contradicted.
- Only `submit_task_delivery` moves implementation into code review. It requires exact in-lock changed locations and criterion-level evidence. Store a compact conversation summary after delivery.
- A material new product decision moves the task to `waiting_confirmation`; an external blocker is reported concretely.

## Review and rework

- Prepare review location from actual changed symbols and their direct callers/tests. Reuse the ordered location strategy from the main skill: CodeGraph, then GitNexus, then bounded direct source matching. A missing graph index does not block review location.
- Code Review reads the implementation and review contracts plus actual diff, runs the saved automated acceptance plan with `run_acceptance_checks`, and does not edit. Only `review_code` may pass this combined stage.
- `review_code` must partition the exact combined set of review checks and acceptance criteria. Never describe warnings, blocked checks, or unrun checks as passing.
- Failed Code Review returns to rework; passing completes the task. Interrupted review remains review; it is not an execution failure. Genuinely non-code tasks skip this stage and complete from fully passing delivery evidence.
- Repeated equivalent failures and interruptions respect persisted backoff and operator pause state. Never bypass a paused task or disabled dispatcher.

## Traceability and relations

- Use `get_task_details` for source, execution, rework and review conversation links. Store bounded summaries rather than copied transcripts.
- Do not reopen completed work for a later change. Confirm a new task, inspect active conflicts, and use the precise `changed_from`, `defect_of`, dependency or continuation relation only after consequential confirmation.

## Board management

- `open_taskboard` returns the local URL and startup command. The default URL is `http://127.0.0.1:8765`.
- Resuming the dispatcher does not resume paused tasks automatically. Reserve task status `failed` for execution failure, not review rejection or interruption.
