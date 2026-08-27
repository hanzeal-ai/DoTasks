# Taskboard Management and Review

Read this reference only for explicit dispatch, review, rework, traceability, relation, or board-management requests. Taskboard-generated lifecycle conversations use `$codex-taskboard-lifecycle` instead.

## Dispatch and execution

- Do not implement a queued task in the intake or dispatcher conversation.
- `dispatch_next_task` claims only a ready or rework task whose dependencies are complete and whose located targets do not conflict with active work. Never bypass project serialization or target locks.
- A new execution or rework conversation must use the returned `$codex-taskboard-lifecycle` prompt and be bound with the real thread ID. Never bind the dispatcher conversation.
- Execution starts from saved targets, location evidence, acceptance plan and `RUN_CONTEXT_JSON`. Current code is authoritative; expand location only when a saved target is missing or contradicted.
- Only `submit_task_delivery` moves implementation into code review. It requires exact in-lock changed locations and criterion-level evidence. Store a compact conversation summary after delivery.
- A material new product decision moves the task to `waiting_confirmation`; an external blocker is reported concretely.

## Review and rework

- Prepare review location from actual changed symbols and their direct callers/tests. Reuse the ordered location strategy from the main skill: CodeGraph, then GitNexus, then bounded direct source matching. A missing graph index does not block review location.
- Code review reads the implementation and review contracts plus actual diff; it does not edit. Only `review_code` may pass this stage.
- Functional acceptance uses saved criterion evidence and `run_acceptance_checks`; only `accept_task` may finish the task. Never describe blocked or unrun checks as passing.
- Failed code review returns to rework. Failed acceptance creates the linked Bug flow defined by the service. Interrupted review remains review; it is not an execution failure.
- Repeated equivalent failures and interruptions respect persisted backoff and operator pause state. Never bypass a paused task or disabled dispatcher.

## Traceability and relations

- Use `get_task_details` for source, execution, rework and review conversation links. Store bounded summaries rather than copied transcripts.
- Do not reopen completed work for a later change. Confirm a new task, inspect active conflicts, and use the precise `changed_from`, `defect_of`, dependency or continuation relation only after consequential confirmation.

## Board management

- `open_taskboard` returns the local URL and startup command. The default URL is `http://127.0.0.1:8765`.
- Resuming the dispatcher does not resume paused tasks automatically. Reserve task status `failed` for execution failure, not review rejection or interruption.
