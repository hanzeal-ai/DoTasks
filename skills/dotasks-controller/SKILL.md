---
name: dotasks-controller
description: Diagnose or manually re-enable the event-driven DoTasks Local Agent dispatcher when explicitly invoked; automatic execution belongs to the Local Agent and Codex CLI/App Server.
---

# DoTasks Controller Diagnostics

## Activation boundary

- Use only when the user explicitly invokes `$dotasks-controller` to inspect or recover dispatch.
- Automatic scheduling does not run in this Codex App task. The cloud writes a durable command and sends a WSS notification; the Local DoTasks Agent forwards the command, consumes `scheduler_state.pending`, then creates or resumes a Codex CLI/App Server thread.
- Never create a heartbeat, recurring automation, per-task timer, Desktop task, or UI-injection workflow.
- Never call `claim_schedule_cycle` from this diagnostic task: doing so would take the lease away from the Local Agent without providing its CLI worker.

## Manual recovery

1. Read the dispatcher and task state through the DoTasks MCP tools or board API.
2. If scheduling is paused and the user requested recovery, call `set_dispatcher_enabled` with `enabled=true`. This persists a scheduler wakeup.
3. Report whether the Local Agent is connected. An enabled queue with no new CLI/App Server thread means the Agent process, its WSS connection, Codex authentication, project mapping, or Codex executable must be repaired; do not describe the task as executing merely because it is claimed.
4. Leave claimed/bound lifecycle work to the Local Agent. It binds the actual CLI thread ID, monitors the turn, verifies the lifecycle callback, and wakes the next scheduling cycle when capacity becomes available.

## Safety

- Do not mutate private Codex storage or bind invented thread IDs.
- Do not mark decomposition, development, or Code Review complete. Only their lifecycle callbacks advance them.
- Pausing disables new claims but does not interrupt a running CLI worker.
