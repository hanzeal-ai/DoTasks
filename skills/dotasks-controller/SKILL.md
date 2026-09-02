---
name: dotasks-controller
description: Consume durable DoTasks scheduler wakeups from the native Codex app after intake, lifecycle callbacks, manual recovery, or the single global Controller heartbeat.
---

# DoTasks Native Controller

## Activation modes

- `kickoff`: use only when an explicitly invoked `$dotasks` intake has just created an entity with `auto_dispatch=true`. Fill every configured `development` slot once and create or resume every returned native worker.
- `handoff`: use only after an explicitly invoked `$dotasks-lifecycle` worker successfully completes requirement decomposition, delivery, or Code Review. Sweep every stage lane once, start newly eligible native workers, then stop.
- `manual`: use when the user explicitly invokes `$dotasks-controller`. Recover pending native creation, inspect previously bound workers once, sweep every stage lane once, then stop.
- `heartbeat`: use only from the single user-authorized global DoTasks Controller heartbeat. It consumes `scheduler_state.pending`; never create one heartbeat per task.
- Never activate from an ordinary development request or a merely visible `ready` task. A heartbeat with no durable scheduler wakeup exits immediately.
- This skill schedules native Codex App tasks; it never implements or reviews the dispatched work itself.
- Use native Codex App task tools to create, resume, and inspect workers. Do not use Codex CLI execution, UI injection, or private database writes.
- Use the stable MCP base worker ID `codex-native-controller`. Pass an explicit `stage`; the service expands the development lane into the configured parallel slots and keeps Code Review as one lane.

## One event-driven sweep

1. Call `claim_schedule_cycle` with `worker_id=codex-native-controller` and a 7200-second lease. Use `force=true` for `kickoff`, `handoff`, and `manual`; use `force=false` for `heartbeat`.
2. Process the returned `code_review.dispatches` first, then `development.dispatches`. The service atomically applies the Review lane, development capacity, Rework priority, dependency, conflict, target-lock, and Worktree gates.
3. A dispatch may be persisted `bound` or `pending_thread`, newly `claimed`, or absent. Treat `dispatch_title`, `dispatch_prompt`, `project_path`, `execution_environment`, `base_revision`, `base_ref`, `run_id`, `status`, `resume_thread_id`, `client_thread_id`, and `thread_id` as authoritative.
4. Resolve `pending_thread` in **Resolve asynchronous creation**. Start or resume every newly `claimed` dispatch through **Resume or create the native worker**. In `manual` mode only, add a dispatch that was already `bound` when claimed to the recovery snapshot; never add a worker newly bound by this sweep.
5. Process every dispatch independently so one creation failure does not prevent the remaining eligible tasks from appearing in the Codex App sidebar. Pass the returned `dispatch_attempt_id` to `mark_dispatch_pending` and `bind_native_dispatch`.
6. After every dispatch is bound, durably `pending_thread`, or failed with a concrete reason, call `complete_schedule_cycle` with the returned `cycle_generation`. If it remains pending, repeat at most two more cycles to consume state changes caused by binding; otherwise stop. Do not wait for newly created workers.

## Resume or create the native worker

1. The persisted `dispatch_prompt` starts with an explicit `[$dotasks:dotasks-lifecycle](...)` attachment pointing at the current plugin cache. Preserve that attachment exactly whenever creating or resuming a worker; a plain `$dotasks-lifecycle` token alone does not load the skill or its MCP tools.
2. When `resume_thread_id` is present, call `send_message_to_thread` for that exact native Codex App task using the full persisted `dispatch_prompt`, including its lifecycle attachment.
3. If continuation succeeds, call `bind_native_dispatch` with that same real thread ID and the claimed `dispatch_attempt_id`. This is the normal retry path for development, rework, bugfix, and review.
4. If continuation fails because the task is unavailable or inaccessible, record the concrete error and create one replacement native task. Pass the error as `resume_fallback_reason` when binding. Do not create a replacement merely because the old task is idle or completed; completed native tasks can receive follow-ups.
5. For a new worker, call `list_projects` and match `project_path` exactly. If `execution_environment=worktree`, require non-empty `base_revision` and `base_ref`, then call `create_thread` for that project with a worktree environment whose `startingState` is `{type: "branch", branchName: base_ref}`. `base_ref` is the existing immutable task branch pinned to `base_revision`; never use `working-tree`, never substitute the moving integration branch, and never add `onMissing`. Otherwise use the local environment. The saved project path is still the authorization boundary. If there is no exact saved project or the required baseline is missing, call `report_dispatch_failed` and stop that dispatch; never run repository work projectless.
6. Call `create_thread` with the exact persisted `dispatch_title` and the full persisted `dispatch_prompt`. This prompt is the native task's initial user message and starts execution directly; do not follow a successful new creation with `send_message_to_thread`. Do not override the user's configured model or reasoning effort.
7. If creation returns a real `threadId`, immediately call `bind_native_dispatch` with its `hostId`, project ID, and `dispatch_attempt_id`. Only this successful binding moves a development task from `claimed` to `implementing` and makes its Run `running`.
8. If creation returns only `clientThreadId`, call `mark_dispatch_pending` with `dispatch_attempt_id`; never bind that client ID as a thread ID. Continue immediately with **Resolve asynchronous creation** in the current Agent turn.

## Resolve asynchronous creation

1. Renew the dispatch lease to 7200 seconds before resolving.
2. Use `list_threads` to locate the most recent native task whose title exactly matches `dispatch_title` and whose project matches `project_path`. Prefer a candidate not already bound to another dispatch; titles are intentionally human-readable and may repeat across retries. Bind only when its real thread ID and host are available.
3. If setup is still in progress, use bounded waits and repeat the exact-title lookup while the current Agent turn remains active. Do not create a duplicate.
4. If preparation definitively fails, call `report_dispatch_failed` with the concrete setup error.
5. If the current turn must end before setup resolves, preserve `pending_thread` and report its run ID and title. The next explicit DoTasks intake, lifecycle handoff, manual Controller invocation, or user-authorized global heartbeat recovers it; never schedule a per-task timer as a fallback.

## Recover pre-existing bound native workers

1. Use this recovery only in `manual` mode for dispatches that were already `bound` when the sweep began. Call `wait_threads` with an immediate snapshot (`timeoutMs: 0`); never wait for an active worker to finish and never monitor a worker created or resumed by the current sweep.
2. When a native task completes, immediately call `get_dispatch_status` for its run ID. If the dispatch is already `completed` or `failed`, the lifecycle callback won the race and no recovery is needed.
3. If the native task completed but the dispatch is still `bound`, the worker ended without its required lifecycle callback. Call `report_dispatch_failed` with the concrete reason `原生 Codex 任务已结束但未提交生命周期回调`; this atomically interrupts the unsubmitted Run. Development/rework is automatically recoverable twice before moving to manual handling; Code Review uses its independent bounded interruption limit.
4. After reporting a missing callback, finish the current cycle normally. If `complete_schedule_cycle` reports another pending generation, claim one more schedule cycle and resume or create the returned retry using the normal rules above. Do not repeat this recovery path again within the same sweep; the service's review interruption limit remains authoritative.
5. If `wait_threads` reports that a task needs user attention, leave the dispatch bound and report the exact approval or input request. Do not convert an approval/input wait into a failed Run.
6. If the snapshot reports that the worker is still active, leave the dispatch bound and stop. Its lifecycle callback, or a later explicit manual sweep after an abnormal exit, owns the next state change.

## Safety and idempotency

- Bind only real native Codex App `threadId` values. Never bind `clientThreadId`.
- Never start a second native task for a `claimed`, `pending_thread`, or `bound` dispatch unless the recorded resume failed and the replacement reason is persisted.
- Development/rework/bugfix retry prefers the canonical development thread. Code Review retry prefers its prior independent review thread.
- Do not mark development, Code Review, or decomposition complete. Only the lifecycle worker's MCP callback advances those stages.
- A native task's completed turn is not evidence that the lifecycle callback succeeded. Always re-read the persisted dispatch before deciding whether recovery is required.
- Do not create per-task automations, Codex CLI workers, UI automation, or private Codex database writes. At most one explicitly user-authorized global Controller heartbeat may consume durable scheduler wakeups.
