---
name: dotasks
description: Manually invoke DoTasks to clarify, create, relate, and inspect tracked tasks. Use only when the user explicitly invokes $dotasks, selects the DoTasks plugin, or explicitly asks to use, open, or manage DoTasks. Do not use for ordinary software or product change requests, PRDs, blockers, acceptance discussions, native Controller work, or DoTasks-generated lifecycle prompts.
---

# DoTasks

## Activation boundary

- Treat DoTasks intake as opt-in. Do not call DoTasks MCP tools or create DoTasks records for ordinary development requests.
- Activate only through an explicit `$dotasks` invocation, plugin selection, or a user request that explicitly names DoTasks and asks to use, open, or manage it.
- Do not infer activation from the current repository, task-like wording, or general requests such as “清空任务”, “增加需求”, “实现这个功能”, or “开始验收”. Ask which task system the user means when the target is ambiguous.
- Do not use this manual skill in DoTasks-generated execution, rework, or review conversations. Those workers receive a self-contained stage prompt and call the allowed DoTasks MCP completion tool directly.
- After activation, use the DoTasks MCP tools as the task system of record for that task only.
- A successful `finalize_task_intake` for `auto_dispatch=true` with
  `controller_kickoff_required=true` is
  not the end of the turn. Treat its returned `controller_kickoff` as the next
  mandatory action before writing the user-facing response, even if earlier
  discovery or creation output was truncated.

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
- Preserve every user-provided screenshot. Call `upload_visual_artifact` once per image and pass the returned `artifact_id`, filename, and purpose in `visual_references` when finalizing either a requirement or a direct task. Never rely on the original conversation attachment being inherited by a new execution conversation.
- Do not load implementation-domain skills, full project development manuals, architecture guides, or broad repository context during intake. The independent execution conversation owns those instructions before editing.
- Use at most one capability probe, one bounded location command, and two Taskboard execution cells: `detect_task_change` + `prepare_task_location`, then one `finalize_task_intake` call. Avoid model-visible round trips between deterministic calls.
- For code-changing tasks, build each execution target exactly once as `{file, mode, symbols, tasks:[{symbol?, action, expected?}]}`. Use `mode=create` for a file that is intentionally absent, `config` for whole-file configuration, and `modify` or `delete` only with an exact existing symbol unless the target is a non-symbol configuration format. Read-only tasks may omit targets or use `mode=inspect` without implementation tasks.
- If the first graph query is irrelevant, empty, or misses exact UI text, immediately use one bounded `source_match` command. Do not run a second semantic graph query, CLI help, broad documentation reads, or a second impact system during intake.
- Only a strong active-task match may pause for confirmation. Completed and cancelled same-project candidates become `history_tasks`; different-project, generic-module-only, and weak lexical candidates are informational and must not block creation.
- If 55 seconds elapse before creation, stop further discovery and finish from the best bounded connected evidence already collected. Never omit the task nature, connected location evidence, or verifiable acceptance plan; require target and implementation contracts only for code-changing work.

### Decide quality gates during analysis

Every direct task and every decomposed child task must include:

```json
{
  "quality_gates": {
    "code_review": {"required": true, "reason": "task-specific reason"}
  }
}
```

Decide whether the code-quality Review stage is required from the confirmed scope and located targets:

- Require Code Review for every code task: executable source, tests, scripts, configuration with runtime effect, schemas/migrations, build/release files, dependencies, or shared contracts. The reviewer judges only code quality, security vulnerabilities, and high cohesion/low coupling from the trusted Git diff. Existing human acceptance owns task-goal completion.
- Set `code_review.required=false` only for a genuinely non-code task such as documentation, research, copy/content, planning, or metadata with no runtime/build/deployment effect. Such a task completes directly after development evidence shows every acceptance criterion passed.
- If classification is uncertain or any located target can affect runtime behavior, treat it as a code task and require Code Review. Never skip it merely to save time.
- Always keep non-empty acceptance criteria, criterion-level `acceptance_plan`, and development `acceptance_evidence`. Omit `review_checks` to use the service defaults for code quality, security vulnerabilities, and high cohesion/low coupling. Task goals and acceptance criteria never become Review checks.

The resulting routes are: code task → code-quality Review → done; non-code task → done immediately after a valid development delivery. Human acceptance remains external to this status flow.

1. Consolidate title, absolute project path, goal, scope, exclusions, acceptance criteria, modules and priority. Inspect current code for discoverable details. Ask one grouped set of questions only when unresolved answers materially change behavior or acceptance. A complete explicit request needs no extra confirmation. Set `intake_kind` using the rules above.
   - Decide whether the request is one independently deliverable task or several tasks before location analysis. Split only when each part has its own deliverable and acceptance result; do not split merely by frontend, backend, test, or file layer.
   - When splitting is necessary, state the task boundaries and ordering once, then run the complete intake flow for each task. Record actual dependencies where applicable. Every child keeps its own location evidence, explicit code-review decision, and acceptance plan; only code-changing children require implementation targets and review checks.
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
5. Give every acceptance criterion a concrete method or UI/command check and expected result. For code-changing work, also map it to a target file and, when applicable, a symbol; build `targets` as `{file, mode, symbols, tasks}` objects and give every target task an action. For read-only work, targets, target tasks, review checks, dependency metadata, and acceptance file/symbol fields may be omitted or empty. Decide `quality_gates.code_review` for every task and give a task-specific reason. Omit `review_checks` to use defaults for code tasks; keep them empty for read-only tasks.
6. For a direct task, call `finalize_task_intake` once with `intake_kind=task`, the prepared analysis ID, confirmed title/goal/project, selected CodeGraph/GitNexus/`source_match` evidence, explicit quality gate, and acceptance plan. Include exact targets and implementation tasks only when the task changes code; include optional scope, exclusions, review checks, and dependencies only when they apply. At finalization the service performs one project-scoped history lookup and creates the task. If it returns `requires_confirmation`, resolve only that strong active relation choice and retry the same bundle with explicit `dependency_analysis`.
7. Pass `source_thread_id` to creation when available. Follow the returned
   `controller_kickoff_required` field rather than reconstructing the decision
   from earlier context. When true, the durable scheduler wakeup is consumed by
   the Local DoTasks Agent; report the entity as queued for event-driven dispatch.

### Automatic dispatch kickoff

An intake that creates an auto-dispatched `ready` entity writes `scheduler_state.pending` in the same transaction. In the three-layer deployment, the cloud returns the command result and emits a WSS notification; the Local Agent then claims one scheduler cycle and starts or resumes every eligible Codex CLI/App Server worker. Do not call `claim_schedule_cycle`, `create_thread`, or `send_message_to_thread` from the intake task. Never create a scheduled automation. Execution has started only after the Local Agent persists the real CLI thread ID through `bind_native_dispatch`; before that, report the entity as queued.

For workflow v2, scheduling metadata is a DoTasks-only object. Use `depends_tasks` for every direct prerequisite, `conflicts_tasks` for overlapping active work without an artifact dependency, `history_tasks` for the ordered same-project lineage and `history_edges` to preserve branches. Use `continues_from_task_id` only when the development thread should resume. `depends_tasks` must all be done before dispatch; history never blocks scheduling. None of these fields enter development or Code Review model context.

When the bounded location query proves a scheduling relation, also send `relation_evidence` with the related task ID, relation type, confidence, evidence kind, concrete reason, source, and exact files or symbols. Only `high` confidence evidence changes scheduling automatically: `artifact_dependency` may create `depends_on`, `thread_continuation` may create `continues_from`, and `target_overlap` may create `conflicts_with`. A call edge, similar module, historical co-change, or lexical match alone is informational; record it as `medium` or `low` and do not block or resume work from it. Relations must stay inside the same normalized project.

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

1. Call `prepare_task_change_location` for the candidate. Use the same ordered CodeGraph, GitNexus or direct-match route, report the selected evidence, re-evaluate the quality gate for the complete revised scope, and call `complete_location_analysis` with location evidence, acceptance criteria, and a review contract containing `quality_gates`. Include targets and an implementation contract only when the revised task changes code.
2. Call `prepare_task_change_confirmation` with the complete `proposed_task`, the user's new request as `request_text`, the candidate evidence, and current `source_thread_id` when available.
3. Prefer the host's interactive choice tool when it is available. Present exactly two choices: `修订 TASK-...` (recommended) and `创建新任务`. After selection, call `resolve_task_change_confirmation` with `revise` or `create_new`.
4. If the host has no interactive choice tool, call `open_taskboard`. The pending Taskboard dialog is the confirmation surface and applies the selected action directly. Do not fall back to numbered text, yes/no prose, or asking the user to type a decision. Do not create or revise anything before the interactive choice is resolved.
5. A `revise` decision preserves the task ID and development thread, stores a revision snapshot, increments `context_version`, interrupts stale active runs, and queues a new rework run when implementation has started. Do not manually emulate these state changes with `transition_task`.
6. A `create_new` decision creates a separate task related to the candidate. Use it for independently deliverable scope even when the request arrived in the same conversation.

Current-thread continuity is strong matching evidence, not authorization to merge. Specific business-module and meaningful wording overlap are supporting evidence; generic layers such as `web`, `frontend`, `backend`, and `api` are not. If detection returns no active candidate, continue the normal creation flow without an extra confirmation. Completed or cancelled tasks are ignored unless the user explicitly names one as the relation target.

## Other Taskboard operations

For explicit dispatch, review, rework, traceability, relation or board-management requests, read [references/management-and-review.md](references/management-and-review.md). Do not load that reference during ordinary new-task intake.
