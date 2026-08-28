---
name: codex-taskboard
description: Manually invoke Codex Taskboard to clarify, create, dispatch, relate, review, and inspect tracked tasks. Use only when the user explicitly invokes $codex-taskboard, selects the Codex Taskboard plugin, or explicitly asks to use, open, or manage Codex Taskboard. Do not use for ordinary software or product change requests, PRDs, blockers, acceptance discussions, or Taskboard-generated lifecycle prompts.
---

# Codex Taskboard

## Activation boundary

- Treat Taskboard intake as opt-in. Do not call `codex-taskboard` MCP tools or create Taskboard records for ordinary development requests.
- Activate only through an explicit `$codex-taskboard` invocation, plugin selection, or a user request that explicitly names Codex Taskboard and asks to use, open, or manage it.
- Do not infer activation from the current repository, task-like wording, or general requests such as “清空任务”, “增加需求”, “实现这个功能”, or “开始验收”. Ask which task system the user means when the target is ambiguous.
- Do not use this manual skill in Taskboard-generated execution, rework, or review conversations. Those prompts must explicitly invoke `$codex-taskboard-lifecycle`.
- After activation, use the `codex-taskboard` MCP tools as the task system of record for that task only.

## Fast new-task intake

Do not create a task from vague brainstorming or an unconfirmed request.

Classify the consolidated input before location work and include the structured
`intake_kind` in the final call:

- Use `intake_kind=requirement` when the input describes one product outcome but
  still contains multiple independently deliverable changes, ordering choices,
  or dependency decisions that must be decomposed before implementation. Record
  the complete original text and requirement-level outcomes; do not disguise it
  as one oversized task or create implementation sessions during intake.
- Use `intake_kind=task` when one worker can implement and verify the whole input
  as one delivery with a single acceptance result. Frontend/backend/test files do
  not by themselves make it a requirement.
- When uncertain, ask only whether the separately valuable deliverables may ship
  independently. Classification is about delivery boundaries, not request length.
- A requirement returns `intake_kind=requirement`, `requirement_id=REQ-...`, and
  `requirement_status=ready`. A direct task returns `intake_kind=task`,
  `task_id=TASK-...`, and `task_status=ready`; keep that distinction in the reply.

### 60-second creation budget

- For a complete request, target a ready requirement or task within 60 seconds and reserve the last 5 seconds for creation. This intake turn records planning state; it does not implement the product change.
- Do not load implementation-domain skills, full project development manuals, architecture guides, or broad repository context during intake. The independent execution conversation owns those instructions before editing.
- Use at most one capability probe, one bounded location command, and two Taskboard execution cells: `detect_task_change` + `prepare_task_location`, then one `finalize_task_intake` call. Avoid model-visible round trips between deterministic calls.
- Build each `{file, symbols, reason}` target, `{file, symbol, action}` ordered step, review check and criterion-level acceptance item exactly once in the final intake bundle. The service derives duplicate criteria, contracts and scheduling relations; do not send those duplicates or repair schema errors interactively.
- If the first graph query is irrelevant, empty, or misses exact UI text, immediately use one bounded `source_match` command. Do not run a second semantic graph query, CLI help, broad documentation reads, or a second impact system during intake.
- Only a strong active-task match may pause for confirmation. Completed, cancelled, different-project, generic-module-only, and weak lexical candidates are informational and must resolve to `independent` without another user turn.
- If 55 seconds elapse before creation, stop further discovery and finish from the best bounded connected evidence already collected. Never weaken required target, implementation, review, or acceptance contracts.

1. Consolidate title, absolute project path, goal, scope, exclusions, acceptance criteria, modules and priority. Inspect current code for discoverable details. Ask one grouped set of questions only when unresolved answers materially change behavior or acceptance. A complete explicit request needs no extra confirmation. Set `intake_kind` using the rules above.
   - Decide whether the request is one independently deliverable task or several tasks before location analysis. Split only when each part has its own deliverable and acceptance result; do not split merely by frontend, backend, test, or file layer.
   - When splitting is necessary, state the task boundaries and ordering once, then run the complete intake flow for each task. Record `split_from` plus `depends_on` or `continues_from` where applicable. Never weaken location, implementation, review, or acceptance contracts for child tasks.
2. For `intake_kind=requirement`, call `finalize_task_intake` now with the original requirement and requirement-level fields; defer code location and task dependency construction to the tracked decomposition run. For `intake_kind=task`, resolve the location route once; do not repeatedly search the tool registry or require a graph index:
   - Probe CodeGraph first. Prefer `codegraph_explore`; otherwise use `codegraph explore --path <project> --max-files 8 <bounded query>` only when the CLI exists and `codegraph status <project>` reports a current index.
   - If CodeGraph is unusable, probe GitNexus. Prefer its query/context MCP tools; otherwise use bounded `gitnexus query` or `gitnexus context` only when the CLI exists and the target repository is indexed. Scope multi-repository queries explicitly.
   - If neither graph route is usable, continue with bounded direct source matching. Start from requirement terms, UI labels, routes, configuration keys, API names and likely module names; use `rg --files`, `rg -n` or equivalent read-only searches, then inspect the matching definitions, direct consumers and related tests.
   - Do not install tools, initialize indexes or re-index repositories without user authorization. Missing CodeGraph and GitNexus are normal fallback conditions, not blockers.
3. When programmatic tool calling is available, compose sequential Taskboard calls inside one execution cell instead of returning to the model between deterministic steps:
   - Call `detect_task_change` with the consolidated request and `source_thread_id` when available.
   - If it returns `requires_confirmation`, follow **Revise or split an active task**.
   - Otherwise call `prepare_task_location` and retain its bounded query plan.
4. Execute the bounded plan through the selected route. Use one graph query only; if it does not locate the requirement, switch immediately to one bounded direct match. For direct matching, search only the likely source/config/test roots and narrow from textual matches to definitions and consumers. Keep source evidence, relationships or direct references, related tests, exact files and stable symbols.
5. Map every acceptance criterion to a target file/symbol and concrete method, command or UI check. Build `targets` as `{file, symbols}` objects, `ordered_steps` as `{file, symbol, action}` objects, and `review_checks` as non-empty strings; never use compact `file::symbol` strings. Keep each fact in one field only.
6. For a direct task, call `finalize_task_intake` once with `intake_kind=task`, the prepared analysis ID, confirmed task metadata, selected CodeGraph/GitNexus/`source_match` evidence, targets, ordered steps, review checks and acceptance plan. The service reuses the history already collected by `prepare_task_location`, reports location status, classifies dependencies, completes the analysis, derives duplicate contracts and criteria, and creates the task. If it returns `requires_confirmation`, resolve only that strong relation choice and retry the same bundle with explicit `dependency_analysis`. A ready task still requires location evidence, dependency, target-lock, implementation and review contracts; the single call must not weaken validation.
7. Pass `source_thread_id` to creation when available, then return the task ID and `ready` state. Background execution is reported as started only after its real thread is bound.

For workflow v2, dependency analysis is exactly `{"decision":"independent"}` or `{"decision":"depends_on"|"continues_from","target_task_id":"TASK-..."}`. `depends_on` waits in a new development thread; `continues_from` and `defect_of` resume the referenced development thread.

Every connected evidence object must include the bounded `query`, non-empty `files`, and useful `symbols`. For CodeGraph MCP use `tool: codegraph_explore`. For the CodeGraph CLI use:

```json
{
  "tool": "codegraph_cli_explore",
  "command": ["codegraph", "explore", "--path", "/absolute/project", "--max-files", "8", "bounded query"],
  "exit_code": 0,
  "query": "bounded query",
  "files": ["relative/source/file"],
  "symbols": ["LocatedSymbol"]
}
```

For GitNexus MCP, normalize the evidence tool to `gitnexus_query` or `gitnexus_context` and include the absolute `project_path`. For its CLI use `tool: gitnexus_cli_query`, the absolute `project_path`, executed `command`, `exit_code: 0`, query, files and symbols.

When both graphs are unavailable, report direct matching as:

```json
{
  "tool": "source_match",
  "project_path": "/absolute/project",
  "query": "bounded requirement terms",
  "commands": [["rg", "-n", "bounded pattern", "src", "tests"]],
  "files": ["relative/source/file"],
  "symbols": ["LocatedSymbol"]
}
```

Never label a failed, unbounded, wrong-project or empty result as connected. Direct matching must identify the best-supported implementation location; do not ask the user to create a graph index merely to confirm a task.

## Revise or split an active task

When `detect_task_change` finds an active candidate, never merge silently and never ask the user to confirm with a plain-text reply.

1. Call `prepare_task_change_location` for the candidate. Use the same ordered CodeGraph, GitNexus or direct-match route, report the selected evidence, and call `complete_location_analysis` with the complete proposed scope, criteria, targets, implementation contract, and review contract.
2. Call `prepare_task_change_confirmation` with the complete `proposed_task`, the user's new request as `request_text`, the candidate evidence, and current `source_thread_id` when available.
3. Prefer the host's interactive choice tool when it is available. Present exactly two choices: `修订 TASK-...` (recommended) and `创建新任务`. After selection, call `resolve_task_change_confirmation` with `revise` or `create_new`.
4. If the host has no interactive choice tool, call `open_taskboard`. The pending Taskboard dialog is the confirmation surface and applies the selected action directly. Do not fall back to numbered text, yes/no prose, or asking the user to type a decision. Do not create or revise anything before the interactive choice is resolved.
5. A `revise` decision preserves the task ID and development thread, stores a revision snapshot, increments `context_version`, interrupts stale active runs, and queues a new rework run when implementation has started. Do not manually emulate these state changes with `transition_task`.
6. A `create_new` decision creates a separate task related to the candidate. Use it for independently deliverable scope even when the request arrived in the same conversation.

Current-thread continuity is strong matching evidence, not authorization to merge. Specific business-module and meaningful wording overlap are supporting evidence; generic layers such as `web`, `frontend`, `backend`, and `api` are not. If detection returns no active candidate, continue the normal creation flow without an extra confirmation. Completed or cancelled tasks are ignored unless the user explicitly names one as the relation target.

## Other Taskboard operations

For explicit dispatch, review, rework, traceability, relation or board-management requests, read [references/management-and-review.md](references/management-and-review.md). Do not load that reference during ordinary new-task intake.
