---
name: dotasks-controller
description: Perform one event-driven DoTasks queue handoff from the native Codex app when explicitly invoked, when an authorized DoTasks intake requests immediate kickoff, or after a DoTasks lifecycle callback makes more work eligible. Never run from a recurring heartbeat.
---

# DoTasks Native Controller

## Activation modes

- `kickoff`: use only when an explicitly invoked `$dotasks` intake has just created an entity with `auto_dispatch=true`. Claim the `development` lane once and create or resume its native worker.
- `handoff`: use only after an explicitly invoked `$dotasks-lifecycle` worker successfully completes requirement decomposition, delivery, or Code Review. Sweep every stage lane once, start newly eligible native workers, then stop.
- `manual`: use when the user explicitly invokes `$dotasks-controller`. Recover pending native creation and sweep every stage lane once, then stop.
- Never activate from a timer, scheduled task, recurring heartbeat, ordinary development request, or a merely visible `ready` task.
- This skill schedules native Codex App tasks; it never implements or reviews the dispatched work itself.
- Use native Codex App task tools to create, resume, and inspect workers. Do not use Codex CLI execution, UI injection, or private database writes.
- Use the stable MCP base worker ID `codex-native-controller`. Pass an explicit `stage`; the service expands the development lane into the configured parallel slots and keeps Code Review as one lane.

## One event-driven sweep

1. In `kickoff` mode, call `claim_dispatch_batch` once with `worker_id=codex-native-controller`, `stage=development`, and a 7200-second lease.
2. In `handoff` or `manual` mode, call `claim_dispatch_batch` once for each lane in this order: `code_review`, `development`. Use the same base worker ID and a 7200-second lease. Process every item in each returned `dispatches` array as one bounded scheduling sweep.
3. A dispatch may be persisted `bound` or `pending_thread`, newly `claimed`, or absent. Treat `dispatch_title`, `dispatch_prompt`, `project_path`, `execution_environment`, `base_revision`, `base_ref`, `run_id`, `status`, `resume_thread_id`, `client_thread_id`, and `thread_id` as authoritative.
4. Resolve `pending_thread` in **Resolve asynchronous creation**. Start or resume every newly `claimed` dispatch through **Resume or create the native worker**. Add every `bound` dispatch with a real thread ID to the current sweep's reconciliation set.
5. After both lanes have been processed, follow **Reconcile bound native workers** for that set. Stop after every returned dispatch is terminal, durably `pending_thread`, waiting for user attention, failed with a concrete reason, or absent. Never create a recurring automation.

## Resume or create the native worker

1. When `resume_thread_id` is present, call `send_message_to_thread` for that exact native Codex App task using the persisted `dispatch_prompt`.
2. If continuation succeeds, call `bind_native_dispatch` with that same real thread ID. This is the normal retry path for development, rework, bugfix, and review.
3. If continuation fails because the task is unavailable or inaccessible, record the concrete error and create one replacement native task. Pass the error as `resume_fallback_reason` when binding. Do not create a replacement merely because the old task is idle or completed; completed native tasks can receive follow-ups.
4. For a new worker, call `list_projects` and match `project_path` exactly. If `execution_environment=worktree`, require non-empty `base_revision` and `base_ref`, then call `create_thread` for that project with a worktree environment whose `startingState` is `{type: "branch", branchName: base_ref}`. `base_ref` is the existing immutable task branch pinned to `base_revision`; never use `working-tree`, never substitute the moving integration branch, and never add `onMissing`. Otherwise use the local environment. The saved project path is still the authorization boundary. If there is no exact saved project or the required baseline is missing, call `report_dispatch_failed` and stop that dispatch; never run repository work projectless.
5. Call `create_thread` with the exact persisted `dispatch_title` and `dispatch_prompt`. Do not override the user's configured model or reasoning effort.
6. If creation returns a real `threadId`, immediately call `bind_native_dispatch` with its `hostId` and project ID. Only this successful binding moves a development task from `claimed` to `implementing` and makes its Run `running`.
7. If creation returns only `clientThreadId`, call `mark_dispatch_pending`; never bind that client ID as a thread ID. Continue immediately with **Resolve asynchronous creation** in the current Agent turn.

## Resolve asynchronous creation

1. Renew the dispatch lease to 7200 seconds before resolving.
2. Use `list_threads` to locate the most recent native task whose title exactly matches `dispatch_title` and whose project matches `project_path`. Prefer a candidate not already bound to another dispatch; titles are intentionally human-readable and may repeat across retries. Bind only when its real thread ID and host are available.
3. If setup is still in progress, use bounded waits and repeat the exact-title lookup while the current Agent turn remains active. Do not create a duplicate.
4. If preparation definitively fails, call `report_dispatch_failed` with the concrete setup error.
5. If the current turn must end before setup resolves, preserve `pending_thread` and report its run ID and title. The next explicit DoTasks intake, lifecycle handoff, or manual Controller invocation recovers it; never schedule a timer as a fallback.

## Reconcile bound native workers

1. Use `wait_threads` for the bound thread IDs returned by this sweep. Wait in bounded calls, pass each returned cursor into the next wait, and renew the corresponding dispatch lease while its native task remains active. Commentary-only updates do not complete a worker.
2. When a native task completes, immediately call `get_dispatch_status` for its run ID. If the dispatch is already `completed` or `failed`, the lifecycle callback won the race and no recovery is needed.
3. If the native task completed but the dispatch is still `bound`, the worker ended without its required lifecycle callback. Call `report_dispatch_failed` with the concrete reason `原生 Codex 任务已结束但未提交生命周期回调`; this atomically interrupts the unsubmitted Run and releases the lane for the existing retry policy.
4. After reporting a missing callback, call `claim_next_dispatch` once more for the same stage and resume or create that single retry using the normal rules above. Do not repeat this recovery loop within the same sweep; the service's review interruption limit and retry delay remain authoritative.
5. If `wait_threads` reports that a task needs user attention, leave the dispatch bound and report the exact approval or input request. Do not convert an approval/input wait into a failed Run.
6. If the controller turn is interrupted before a worker reaches a terminal or attention state, leave the dispatch bound. A later explicit intake, lifecycle handoff, or manual sweep will reconcile it before scheduling another worker in that lane.

## Safety and idempotency

- Bind only real native Codex App `threadId` values. Never bind `clientThreadId`.
- Never start a second native task for a `claimed`, `pending_thread`, or `bound` dispatch unless the recorded resume failed and the replacement reason is persisted.
- Development/rework/bugfix retry prefers the canonical development thread. Code Review retry prefers its prior independent review thread.
- Do not mark development, Code Review, or decomposition complete. Only the lifecycle worker's MCP callback advances those stages.
- A native task's completed turn is not evidence that the lifecycle callback succeeded. Always re-read the persisted dispatch before deciding whether recovery is required.
- Do not create or depend on Codex automations, recurring heartbeats, Codex CLI workers, UI automation, or private Codex database writes.
