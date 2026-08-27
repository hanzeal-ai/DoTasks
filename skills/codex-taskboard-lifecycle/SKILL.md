---
name: codex-taskboard-lifecycle
description: Run only an explicitly invoked Codex Taskboard development, rework, bugfix, code_review, or acceptance lifecycle prompt containing both a TASK ID and RUN ID.
---

# Codex Taskboard Lifecycle

## Activation boundary

- Proceed only when the prompt explicitly invokes `$codex-taskboard-lifecycle` and identifies both a `TASK-*` task ID and `RUN-*` run ID.
- Treat Taskboard MCP state and the prompt's saved location analysis as authoritative for this lifecycle run.
- Do not create unrelated tasks, broaden confirmed scope, or perform manual board-management work.
- If the required IDs or lifecycle context are missing, stop and report the invalid Taskboard prompt.

## Execution and rework

1. Read the prompt's `RUN_CONTEXT_JSON` as the authoritative immutable run snapshot. Do not search the tool registry or refetch task details. If the snapshot or prompt IDs are missing, stop and report an invalid Taskboard prompt.
2. Start with `target_snippet` when present, then use `located_targets`, `implementation_steps`, and `acceptance_commands`. Each current `acceptance_commands` entry groups one check with its `criteria`: run that check once and retain criterion-level evidence for every nested item. For a legacy flat snapshot, treat its top-level `criterion` as a single nested item and still run identical commands only once. The snippet is a bounded starting point, not permission to skip needed context: read the complete target file or direct dependencies whenever correctness requires it.
3. Verify the snippet hash against the current target before relying on it. If the target moved or the saved lock is stale, stop for a new bounded location analysis instead of searching tool registries or scanning the whole project.
4. Execution tasks already enter `implementing` when claimed. Implement only the confirmed scope and run relevant verification.
5. Before delivery, preflight every `changed_locations` entry against `RUN_CONTEXT_JSON.located_targets`: every file must be locked and every submitted symbol must be an exact saved symbol for that file. Match actual changed declarations to saved target names; do not infer symbols from diff-hunk context or a neighboring declaration. For a newly added declaration, use its declared name as saved in the lock. Then call `submit_task_delivery` with exact changed files and symbols, criterion-level evidence, a compact delivery summary, and verification results. When `RUN_CONTEXT_JSON.batch.appended_count` is positive, the delivery is atomic across every item in `RUN_CONTEXT_JSON.tasks`: use the task-prefixed criterion text exactly and pass the current `batch.revision` as `batch_revision`. A `continue_development` response means the batch changed concurrently; incorporate the new task and resubmit the new revision. Stop for a new bounded location analysis only when legitimate work actually falls outside the saved target lock.
6. Put decisions, verification, and remaining risk in the compact `submit_task_delivery` summary; do not make a second summary tool call.

Delivery contract validation errors are recoverable and leave the run active. When `submit_task_delivery` reports submitted and allowed locations or symbols, correct the arguments and retry the same run; never call `report_run_blocked` for a correctable delivery payload mismatch.

If investigation reveals a major scope decision, call `report_run_blocked` with `waiting_confirmation`. If execution cannot continue after recoverable validation issues have been corrected, call it with `blocked` and a concrete reason.

## Code Review

1. For `code_review`, read `implementation`, `review_checks`, and `delivery.diff` from `RUN_CONTEXT_JSON`. Do not rediscover the Taskboard database or edit code. When `batch` is present, review the batch atomically and preserve every `[TASK-ID]` prefix in `passed_items` and `failed_criteria`. Call `review_code` with standards findings and verdict.
2. A failed `review_code` returns to `rework` and the original development conversation. A pass moves the task to `acceptance`.

## Functional acceptance

1. For `acceptance`, read only `acceptance`, `acceptance_criteria`, and `delivery` from `RUN_CONTEXT_JSON`. When `batch` is present, preserve every `[TASK-ID]` prefix and decide the sealed batch atomically.
2. Run required automated checks through `run_acceptance_checks` when present. The service may reuse a passed result only for the same delivery and workspace fingerprint. Then call `accept_task` with exact `passed_criteria` and `failed_criteria` coverage.
3. A failed acceptance creates a linked `type=bug` task for normal tasks. The Bug reuses the original development thread and goes through `bugfix -> code_review -> acceptance`; a Bug failure reworks that Bug instead of creating another Bug. When the Bug passes, the original task is awakened in `acceptance`.

A failed review moves to `rework`; it is not an execution failure. An interrupted review remains in `review`. Rework resumes the implementation conversation, while interrupted review resumes its independent review conversation.

## Lifecycle safety

- Treat `paused` and a disabled dispatcher as persisted operator stops. Never bypass them.
- Only `submit_task_delivery` may move implementation into `code_review`, only `review_code` may pass code review, and only `accept_task` may move a task to `done`.
- Keep acceptance evidence granular. Do not describe warnings, blocked checks, or unrun browser checks as passing.
