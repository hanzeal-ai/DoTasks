---
name: dotasks-lifecycle
description: Run only an explicitly invoked DoTasks requirement decomposition, development, rework, bugfix, or combined code_review lifecycle prompt containing its persisted entity and run IDs.
---

# DoTasks Lifecycle

## Activation boundary

- Proceed only when the prompt explicitly invokes `$dotasks-lifecycle` and identifies either both a `TASK-*` task ID and `RUN-*` run ID, or both a `REQ-*` requirement ID and `RDRUN-*` decomposition run ID.
- Treat Taskboard MCP state and the prompt's saved location analysis as authoritative for this lifecycle run.
- Do not create unrelated tasks, broaden confirmed scope, or perform manual board-management work.
- If the required IDs or lifecycle context are missing, stop and report the invalid Taskboard prompt.

## Requirement decomposition

1. For a `REQ-*` / `RDRUN-*` prompt, call `get_requirement` and treat the returned requirement, existing child tasks, relations, and decomposition run as authoritative.
2. Do not edit code, perform implementation, or create another requirement. Decompose only the claimed requirement into independently deliverable ready tasks with explicit dependency keys and complete location/implementation/review/acceptance contracts.
3. Call `submit_requirement_decomposition` exactly once with the claimed IDs and stable child keys. If decomposition cannot be completed, call `report_requirement_decomposition_failed` with the concrete reason.
4. After a successful callback, perform the **Event-driven native handoff** below. A failed decomposition callback stops without another claim.

## Execution and rework

1. Read the prompt's `RUN_CONTEXT_JSON` as the authoritative immutable run snapshot. Do not search the tool registry or refetch task details. If the snapshot or prompt IDs are missing, stop and report an invalid Taskboard prompt.
2. Use `targets[].file`, `targets[].mode` and `targets[].tasks[]` as the complete execution plan. Run each grouped item in `verify` once and retain criterion-level evidence for every nested criterion. Do not redo code location or search for additional implementation scope; read only the named targets and the direct dependencies strictly required for correctness.
3. `mode=create` means the target is intentionally absent and must not be reported as blocked merely because the file does not exist. If a `modify` or `delete` target moved, or legitimate work falls outside the saved plan, return a precise project `failure_location`/scope gap for bounded self-healing instead of scanning the whole project.
4. A claimed execution task enters `implementing` only after its real native Codex App `threadId` is bound. Confirm the run is bound, renew its lease, then implement only the confirmed scope and run relevant verification.
5. Before delivery, preflight every `changed_locations` entry against `RUN_CONTEXT_JSON.targets`: every file must be locked and every submitted symbol for a symbol-locked target must be exact. Match actual changed declarations to saved target names; do not infer symbols from diff-hunk context or a neighboring declaration. Then call `submit_task_delivery` with exact changed files and symbols, criterion-level evidence, a compact delivery summary, and verification results. When `RUN_CONTEXT_JSON.execution_environment=worktree`, also pass the absolute current working directory as `workspace_path`; never substitute the saved project checkout path. When `RUN_CONTEXT_JSON.batch.appended_count` is positive, the delivery is atomic across every item in `RUN_CONTEXT_JSON.tasks`: use the task-prefixed criterion text exactly and pass the current `batch.revision` as `batch_revision`. A `continue_development` response means the batch changed concurrently; incorporate the new task and resubmit the new revision.
6. Put decisions, verification, and remaining risk in the compact `submit_task_delivery` summary; do not make a second summary tool call. After a successful terminal delivery response, perform the **Event-driven native handoff** below. Do not hand off when the response says `continue_development`.

Delivery contract validation errors are recoverable and leave the run active. When `submit_task_delivery` reports submitted and allowed locations or symbols, correct the arguments and retry the same run; never call `report_run_blocked` for a correctable delivery payload mismatch.

If investigation reveals a major scope decision, call `report_run_blocked` with `waiting_confirmation`. If execution cannot continue after recoverable validation issues have been corrected, call it with `blocked` and a concrete reason.

## Code Review

1. For `code_review`, use only the minimal task goal/constraints, `review_checks`, and the trusted Git scope in `diff_scope`. Do not rediscover the DoTasks database, inspect implementation plans or delivery summaries, or edit code. When `diff_scope.artifact_path` is present, verify its SHA-256 and review that persisted patch; otherwise derive the diff from the saved base revision and changed files. Judge correctness, security, permissions, regressions and unrelated changes from that exact diff.
2. Run required automated items through `run_acceptance_checks`; these checks are service-owned and are not a second source for the code-review verdict. The service may reuse a passed result only for the same delivery and workspace fingerprint, perform one bounded dependency/runtime repair, and rerun the original command. Project-source or test changes must go through controlled rework. When `batch` is present, review it atomically and preserve every `[TASK-ID]` prefix from the callback prompt.
3. Classify missing tests, test scripts, or project configuration as `project`; classify missing dependencies, modules, or tool runtime as `environment`; otherwise use `implementation`. For a project/environment failure, include `failure_category` and exact `failure_locations` in `review_code` so the service can add only those test/config files to a bounded self-healing rework. Never skip, weaken, or delete a check to manufacture a pass.
4. Call `review_code` with `passed_items` and `failed_criteria` that exactly partition the combined review checks and acceptance criteria. A recoverable failure returns to bounded self-healing `rework`; a pass completes the task. After either successful callback, perform the **Event-driven native handoff** below.

A failed review moves to `rework`; it is not an execution failure. An interrupted Code Review remains in `code_review`. Rework resumes the implementation conversation, while interrupted review resumes its independent review conversation.

## Event-driven native handoff

This handoff replaces recurring Controller heartbeats. It runs only inside the currently active native Codex Agent turn after an intake or lifecycle callback made queue state newly eligible.

1. Read the sibling `../dotasks-controller/SKILL.md` and follow its `handoff` mode with base worker ID `codex-native-controller`.
2. Sweep the `code_review` and `development` lanes once. Start or resume every newly claimed dispatch with the Codex App's native task tools and bind only a real `threadId`.
3. Do not monitor the spawned workers and do not create, update, or depend on a recurring automation. Each spawned lifecycle worker repeats this handoff only after its own successful stage callback.
4. If a lane has no claimable work, leave it idle. If an asynchronous native task is still preparing, resolve it in the current turn; if it cannot be resolved, preserve `pending_thread` and report that the next explicit DoTasks invocation must recover it.
5. Renew the current dispatch lease to 7200 seconds at lifecycle start and before or after long-running commands. Lease renewal is part of the active worker turn, not a scheduled trigger.

## Lifecycle safety

- Treat `paused` and a disabled dispatcher as persisted operator stops. Never bypass them.
- Never create a scheduled task, recurring heartbeat, or hidden App Server dispatcher to advance the queue.
- Only `submit_task_delivery` may move implementation into `code_review`, and only `review_code` may pass the combined review and move a code task to `done`. A non-code task may complete directly from a fully passed delivery.
- Keep acceptance evidence granular. Do not describe warnings, blocked checks, or unrun browser checks as passing.
