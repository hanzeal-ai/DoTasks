# Taskboard Management and Review

Read this reference only for explicit dispatch, review, rework, traceability, relation, or board-management requests. DoTasks-generated lifecycle conversations use `$dotasks-lifecycle` instead.

## Dispatch and execution

- Do not implement a queued task in the intake or dispatcher conversation.
- The Local DoTasks Agent calls `claim_schedule_cycle` as the only automatic scheduling entry. One event-driven cycle fills the independent `code_review` and `development` lanes: Code Review has one slot and development uses the configured parallel slot count. All claims preserve dependency, project-exclusive, target-lock and isolated-Worktree gates. Requirement decomposition shares a development slot.
- A new or resumed Codex CLI/App Server worker must use the persisted `$dotasks-lifecycle` prompt and be bound with `bind_native_dispatch` only after a real CLI thread ID exists.
- Execution starts from saved targets, location evidence, acceptance plan and `RUN_CONTEXT_JSON`. Current code is authoritative; expand location only when a saved target is missing or contradicted.
- Only `submit_task_delivery` moves implementation into code review. It requires exact in-lock changed locations and criterion-level evidence. Store a compact conversation summary after delivery.
- A material new product decision moves the task to `waiting_confirmation`; an external blocker is reported concretely.

## Review and rework

- Prepare review location from actual changed symbols and their direct callers/tests. Reuse the ordered location strategy from the main skill: CodeGraph, then GitNexus, then bounded direct source matching. A missing graph index does not block review location.
- Code Review obtains the complete change only with existing Git commands against the saved workspace, including Git handling for untracked additions. It never reads a patch artifact, assembles file contents, or implements its own diff. It then uses all relevant already-available project tools and may inspect only direct code dependencies needed to judge code quality, security vulnerabilities, and high cohesion/low coupling; it never installs tools, writes ad-hoc scanners, inspects task goals or acceptance criteria, runs the acceptance plan, or edits code. Only concrete blocking quality findings may fail, and only `review_code` may pass this stage.
- `review_code` must partition the exact set of Review quality checks. Existing human acceptance remains responsible for task completion.
- Failed Code Review returns to rework; passing completes the task. After three completed implementation-quality rework rounds, another equivalent failure moves to `waiting_confirmation`. Interrupted review remains review; it is not an execution failure. Genuinely non-code tasks skip this stage and complete from fully passing delivery evidence.
- Repeated equivalent failures and interruptions respect persisted backoff and operator pause state. Never bypass a paused task or disabled dispatcher.

## Traceability and relations

- Use `get_task_details` for source, execution, rework and review conversation links. Store bounded summaries rather than copied transcripts.
- Do not reopen completed work for a later change. Confirm a new task, inspect active conflicts, and use the precise `changed_from`, `defect_of`, dependency or continuation relation only after consequential confirmation.

## Board management

- `open_taskboard` returns the local URL and startup command. The default URL is `http://127.0.0.1:8765`.
- Pausing the dispatcher only disables new claims. It never changes task status and never interrupts an active native worker.
- For an explicit DoTasks request to resume scheduling, call `set_dispatcher_enabled` with `enabled=true`. The cloud command's WSS notification wakes the Local Agent, which immediately sweeps Code Review and every development slot.
- Resuming the dispatcher does not resume individually paused tasks automatically. Reserve task status `failed` for execution failure, not review rejection or interruption.
