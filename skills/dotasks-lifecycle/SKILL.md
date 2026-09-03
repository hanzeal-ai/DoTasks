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
- The launch prompt includes `DoTasks lifecycle CLI fallback: <absolute-path>`. If a required `dotasks` MCP tool is absent from the available tool registry or the MCP server fails before accepting the call, invoke the same tool once through that CLI by piping its JSON arguments to `<absolute-path> --call-tool <tool-name>`. This is the same local DoTasks service and remains the authoritative callback path. Do not use the CLI to bypass a validation or business error returned by an available MCP tool.

## Requirement decomposition

1. For a `REQ-*` / `RDRUN-*` prompt, call `get_requirement` and treat the returned requirement, existing child tasks, relations, and decomposition run as authoritative.
2. Do not edit code, perform implementation, or create another requirement. For every planned child task, call `prepare_task_location`, execute exactly one returned bounded route, and persist the connected evidence with `report_location_status`. Retain each prepared `analysis_id` and its exact location evidence.
3. Do not call `complete_location_analysis` during requirement decomposition: `submit_requirement_decomposition` atomically completes and consumes every prepared analysis while creating its child task. Decompose only the claimed requirement into independently deliverable ready tasks with explicit dependency keys, location evidence, and verifiable acceptance criteria. Code-changing children require exact targets and code-review gates; read-only children may omit targets, implementation steps, review checks, dependency declarations, and non-applicable acceptance file/symbol fields. Call `submit_requirement_decomposition` exactly once with the claimed IDs, stable child keys, and their prepared location analyses. If decomposition cannot be completed, call `report_requirement_decomposition_failed` with the concrete reason.
4. After a successful callback, perform the **Event-driven native handoff** below. A failed decomposition callback stops without another claim.

## Execution and rework

For `RUN_CONTEXT_JSON.execution_environment=projectless`, do not inspect a repository,
run code-location steps, or create/modify files. Complete the natural-language task in
the current Codex conversation, then call `submit_task_delivery` with
`changed_locations=[]`, a compact result summary, and passing evidence for the saved
criterion. The remaining file-target instructions in this section apply only to
project-backed runs.

For a project-backed run with `RUN_CONTEXT_JSON.delivery.requires_changes=false`,
inspect only what the task and optional targets require, do not create, modify, or
delete files, and submit `changed_locations=[]` with passing criterion-level evidence.
Empty targets, implementation steps, and review checks are valid for this read-only
task. The modification-lock instructions below apply only when
`delivery.requires_changes=true`.

1. Start directly from the natural-language task brief at the top of the prompt. Treat that brief as authoritative for the goal, scope, exclusions, and acceptance criteria. Use `RUN_CONTEXT_JSON` only for the immutable modification targets, verification commands, execution environment, and optional batch input; do not search the tool registry or refetch task details. If the snapshot or prompt IDs are missing, stop and report an invalid Taskboard prompt.
2. Implement only the behavior described by the natural-language task brief. Treat `targets[].file`, `targets[].mode` and `targets[].symbols` only as immutable modification locks, never as another requirement source. Run each grouped item in `verify` once and retain criterion-level evidence for every nested criterion. Do not redo code location or search for additional implementation scope; read only the named targets and the direct dependencies strictly required for correctness.
3. `mode=create` means the target is intentionally absent and must not be reported as blocked merely because the file does not exist. If a `modify` or `delete` target moved, or legitimate work falls outside the saved plan, return a precise project `failure_location`/scope gap for bounded self-healing instead of scanning the whole project.
4. A claimed execution task enters `implementing` only after its real native Codex App `threadId` is bound. Confirm the run is bound, renew its lease, then implement only the confirmed scope and run relevant verification.
5. Before delivery, preflight every `changed_locations` entry against `RUN_CONTEXT_JSON.targets`: every file must be locked and every submitted symbol for a symbol-locked target must be exact. Match actual changed declarations to saved target names; do not infer symbols from diff-hunk context or a neighboring declaration. Then call `submit_task_delivery` with exact changed files and symbols, criterion-level evidence, a compact delivery summary, and verification results. When `RUN_CONTEXT_JSON.execution_environment=worktree`, also pass the absolute current working directory as `workspace_path`; never substitute the saved project checkout path. When `RUN_CONTEXT_JSON.batch.appended_count` is positive, the delivery is atomic across every item in `RUN_CONTEXT_JSON.tasks`: use the task-prefixed criterion text exactly and pass the current `batch.revision` as `batch_revision`. A `continue_development` response means the batch changed concurrently; incorporate the new task and resubmit the new revision.
6. Put decisions, verification, and remaining risk in the compact `submit_task_delivery` summary; do not make a second summary tool call. After a successful terminal delivery response, perform the **Event-driven native handoff** below. Do not hand off when the response says `continue_development`.

Delivery contract validation errors are recoverable and leave the run active. When `submit_task_delivery` reports submitted and allowed locations or symbols, correct the arguments and retry the same run; never call `report_run_blocked` for a correctable delivery payload mismatch.

If investigation reveals a major scope decision, call `report_run_blocked` with `waiting_confirmation`. If execution cannot continue after recoverable validation issues have been corrected, call it with `blocked` and a concrete reason. For a known project or environment problem that has one or more exact safe repair files, also pass `failure_category` and `failure_locations`; the service may schedule one bounded repair. Do not provide repair fields for user decisions, approvals, external access, or an uncertain location.

After any successful `report_run_blocked` callback, finish the current worker. The Local Agent observes worker completion and consumes the durable scheduler wakeup.

## Code Review

1. For `code_review`, obtain the complete diff only with existing Git commands against `diff_scope.workspace_path`: use `git -C <workspace_path> status --short`, `git diff --no-ext-diff --no-textconv <base_revision> -- <changed_files>`, `git ls-files --others --exclude-standard`, and `git diff --no-index --no-ext-diff --no-textconv -- /dev/null <file>` for untracked additions. Exit code 1 from the last command means differences were found and is not a failure. Do not read a persisted patch, concatenate file contents, implement a diff, ask development to provide one, or edit code.
2. After obtaining the Git diff, use every relevant tool already available in the environment or configured by the project, such as source navigation, lint, type checking, static analysis, security scanners, and focused tests. You may read the changed symbols' direct dependencies, callers, and relevant project configuration when needed to judge local correctness, security, cohesion, or coupling. Do not install tools, write temporary scripts or custom scanners, or modify source, configuration, or dependencies.
3. Judge only code quality, security vulnerabilities, and high-cohesion/low-coupling design. Do not rediscover the DoTasks database, inspect the task goal, acceptance criteria, acceptance plan, implementation plan or delivery summary, run product-level acceptance, or treat missing product behavior as a Review failure. A concrete defect, regression risk, vulnerability, material maintainability problem, mixed responsibility, or unreasonable dependency visible from the change is still a quality failure. Style-only preferences and non-blocking suggestions must not enter `failed_criteria`.
4. Call `review_code` with `passed_items` and `failed_criteria` that exactly partition the Review quality checks. Every failure reason must identify the exact file and symbol or line, trigger, impact, and smallest repair direction. Do not fail merely because task-goal completion cannot be established. A quality failure returns to bounded `rework`; a pass completes the DoTasks code workflow. After either successful callback, perform the **Event-driven native handoff** below.

A failed review moves to `rework`; it is not an execution failure. After three completed implementation-quality rework rounds, another equivalent failure moves the task to `waiting_confirmation` instead of looping automatically. An interrupted Code Review remains in `code_review`. Rework resumes the implementation conversation, while interrupted review resumes its independent review conversation.

## Event-driven native handoff

Every lifecycle state transition writes a durable scheduler wakeup. Submit the required callback exactly once and then finish this CLI/App Server turn. The parent Local Agent monitors `turn/completed`, verifies that the dispatch reached a terminal callback state, releases capacity, and immediately consumes the next pending scheduler generation. Do not call `claim_schedule_cycle`, create another Codex task, or create a scheduled automation from the lifecycle worker. Renew the current dispatch lease before long-running commands; lease renewal is active-run maintenance, not a scheduling trigger.

## Lifecycle safety

- Treat `paused` and a disabled dispatcher as persisted operator stops. Never bypass them.
- Never create scheduled work. The Local Agent is a persistent event consumer and starts Codex CLI/App Server only when WSS or a completed worker supplies a real wakeup.
- Only `submit_task_delivery` may move implementation into `code_review`, and only `review_code` may pass the combined review and move a code task to `done`. A non-code task may complete directly from a fully passed delivery.
- Keep acceptance evidence granular. Do not describe warnings, blocked checks, or unrun browser checks as passing.
