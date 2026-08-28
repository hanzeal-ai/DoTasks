from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from taskboard.app_server import AppServerError, app_server_initialize_params
from core.dispatcher import TaskDispatcher
from core.service.runs import effective_token_total


class FakeClient:
    def __init__(self):
        self.connected = False
        self.last_error = ""
        self.calls = []
        self.notifications = []
        self.turn_status = "inProgress"
        self.fail_thread_name = False
        self.fail_turn_steer = False
        self.stop_calls = 0

    def start(self):
        self.connected = True

    def stop(self):
        self.stop_calls += 1
        self.connected = False

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/name/set" and self.fail_thread_name:
            raise AppServerError("thread/name/set: failed to update thread metadata")
        if method == "turn/steer" and self.fail_turn_steer:
            raise AppServerError("turn/steer: active turn already completed")
        if method == "thread/start":
            return {"thread": {"id": "thread-123"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-456"}}
        if method == "thread/read":
            return {"thread": {"turns": [{"id": "turn-456", "status": self.turn_status}]}}
        return {}

    def drain_notifications(self):
        result, self.notifications = self.notifications, []
        return result


class FakeService:
    def __init__(self, project):
        self.project = project
        self.claimed = False
        self.bound = None
        self.transitions = []
        self.renewals = []
        self.token_updates = []
        self.token_usage_details = []
        self.interruptions = []
        self.budget_pauses = []
        self.run_status = "running"
        self.auto_accept_result = {"eligible": False, "completed": False}
        self.task = {
            "id": "TASK-0001", "title": "测试调度", "project": project,
            "status": "implementing", "effective_token_used": 0,
        }

    def claim_next_task(self, worker_id, lease_seconds):
        if self.claimed:
            return None
        self.claimed = True
        return {
            "task": dict(self.task),
            "run": {"id": "RUN-0001", "run_type": "execution", "status": "awaiting_thread"},
            "lease_token": "lease-1",
            "dispatch_prompt": "执行测试任务\n\nRUN_CONTEXT_JSON={}；完成后调用 submit_task_delivery。",
        }

    def claim_next_review_task(self, worker_id, lease_seconds):
        return None

    def dispatcher_enabled(self):
        return True

    def get_run(self, run_id):
        return {"id": run_id, "status": self.run_status}

    def renew_run_lease(self, run_id, lease_token, lease_seconds):
        self.renewals.append((run_id, lease_token, lease_seconds))
        return self.get_run(run_id)

    def record_run_token_usage(self, run_id, token_used, usage=None):
        self.token_updates.append((run_id, token_used))
        self.token_usage_details.append(dict(usage or {}))
        self.task["token_used"] = token_used
        self.task["effective_token_used"] = effective_token_total(token_used, usage)
        return self.get_run(run_id)

    def pause_run_for_budget(self, run_id, reason):
        self.budget_pauses.append((run_id, reason))
        self.run_status = "interrupted"
        self.task["status"] = "waiting_confirmation"
        return {"changed": True, "task": dict(self.task)}

    def auto_accept_automated_task(self, task_id, run_id):
        return dict(self.auto_accept_result)

    def interrupt_unsubmitted_run(self, run_id, reason):
        self.interruptions.append((run_id, reason))
        return {"changed": True}

    def bind_conversation(self, task_id, role, thread_id, run_id, title):
        self.bound = (task_id, role, thread_id, run_id, title)
        self.task["status"] = "implementing"

    def get_task(self, task_id):
        return dict(self.task)

    def transition_task(self, task_id, status, reason):
        self.task["status"] = status
        self.transitions.append((task_id, status, reason))


class TaskDispatcherTest(unittest.TestCase):
    def test_app_server_declares_experimental_api_capability(self):
        self.assertTrue(app_server_initialize_params()["capabilities"]["experimentalApi"])

    def test_batch_append_steers_the_exact_active_turn_and_persists_failure(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            event = {
                "id": 7,
                "thread_id": "batch-thread",
                "active_turn_id": "active-turn",
                "input": {"batch_id": "BATCH-0001", "batch_revision": 2},
            }
            service.pending_batch_steers = lambda: [event]
            sent = []
            pending = []
            service.mark_batch_steer_sent = sent.append
            service.mark_batch_steer_pending = lambda event_id, error: pending.append(
                (event_id, error)
            )
            client = FakeClient()
            client.start()
            dispatcher = TaskDispatcher(service, client=client)
            dispatcher._active_clients["batch-thread"] = client

            dispatcher._flush_batch_steers()

            steer = next(params for method, params in client.calls if method == "turn/steer")
            self.assertEqual("active-turn", steer["expectedTurnId"])
            self.assertIn('"batch_revision":2', steer["input"][0]["text"])
            self.assertEqual([7], sent)

            client.fail_turn_steer = True
            dispatcher._flush_batch_steers()
            self.assertEqual(7, pending[-1][0])
            self.assertIn("already completed", pending[-1][1])

    def test_stage_tool_profiles_are_minimal_without_losing_manual_acceptance(self):
        automated = {
            "acceptance_criteria": ["works"],
            "acceptance_plan": [{
                "criterion": "works", "check_type": "automated", "command": "true",
            }],
        }
        manual = {
            "acceptance_criteria": ["works"],
            "acceptance_plan": [{"criterion": "works", "check_type": "manual_runtime"}],
        }

        self.assertEqual("execution", TaskDispatcher._tool_profile_for_run("execution", automated))
        self.assertEqual("code_review", TaskDispatcher._tool_profile_for_run("code_review", automated))
        self.assertEqual("verifier", TaskDispatcher._tool_profile_for_run("code_review", manual))
        self.assertEqual("acceptance", TaskDispatcher._tool_profile_for_run("acceptance", manual))
        self.assertEqual(
            "verifier", TaskDispatcher._tool_profile_for_run("acceptance", manual, "review-thread"),
        )

    def test_dispatch_limit_counts_existing_active_clients(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            dispatcher = TaskDispatcher(service, client=FakeClient())
            active = FakeClient()
            active.start()
            dispatcher._active_clients["existing-thread"] = active
            dispatcher._client_started_at["existing-thread"] = time.monotonic()
            dispatcher._active_runs["existing-thread"] = {
                "run_id": "RUN-existing",
                "lease_token": "lease-existing",
                "turn_id": "TURN-existing",
                "last_renewed": time.monotonic(),
                "last_activity_at": time.monotonic(),
            }

            self.assertEqual(0, dispatcher.dispatch_once(limit=1))
            self.assertFalse(service.claimed)

    def test_dispatch_creates_names_binds_and_starts_thread(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)

            self.assertEqual(1, dispatcher.dispatch_once())
            self.assertEqual(
                ("TASK-0001", "execution", "thread-123", "RUN-0001", "开发"),
                service.bound,
            )
            methods = [method for method, _ in client.calls]
            self.assertEqual(["thread/start", "thread/name/set", "turn/start"], methods)
            turn = next(params for method, params in client.calls if method == "turn/start")
            self.assertIn("RUN_CONTEXT_JSON", turn["input"][0]["text"])
            self.assertNotIn("必须调用 get_task_context", turn["input"][0]["text"])
            self.assertIn("submit_task_delivery", turn["input"][0]["text"])
            self.assertEqual(str(Path(project).resolve()), client.calls[0][1]["cwd"])
            self.assertEqual([str(Path(project).resolve())], client.calls[0][1]["runtimeWorkspaceRoots"])
            self.assertEqual("danger-full-access", client.calls[0][1]["sandbox"])
            self.assertEqual("user", client.calls[0][1]["threadSource"])
            dispatcher.dispatch_once()
            self.assertTrue(client.connected)

    def test_requirement_is_decomposed_before_any_implementation_session(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            plan = [{"key": "one", "title": "子任务", "goal": "完成子任务"}]
            service.submitted_decomposition = None

            def claim_next_task(worker_id, lease_seconds):
                if service.claimed:
                    return None
                service.claimed = True
                return {
                    "kind": "requirement_decomposition",
                    "requirement": {"id": "REQ-0001", "project": project, "decomposition_plan": plan},
                    "run": {"id": "RDRUN-0001", "run_type": "requirement_decomposition", "status": "running"},
                    "lease_token": "requirement-lease",
                }

            service.claim_next_task = claim_next_task
            service.submit_requirement_decomposition = lambda requirement_id, run_id, tasks: setattr(
                service, "submitted_decomposition", (requirement_id, run_id, tasks)
            )
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)

            self.assertEqual(1, dispatcher.dispatch_once(limit=1))
            self.assertEqual(("REQ-0001", "RDRUN-0001", plan), service.submitted_decomposition)
            self.assertEqual([], client.calls)
            self.assertIsNone(service.bound)

    def test_requirement_without_plan_starts_tracked_decomposition_session(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)

            def claim_next_task(worker_id, lease_seconds=1800, **kwargs):
                if kwargs.get("action") == "get_requirement":
                    return {"requirement": {"id": "REQ-0002", "status": "decomposed"}}
                if kwargs.get("action"):
                    return {"status": "ok"}
                if service.claimed:
                    return None
                service.claimed = True
                return {
                    "kind": "requirement_decomposition",
                    "requirement": {
                        "id": "REQ-0002", "title": "完整需求", "project": project,
                        "goal": "完成完整需求", "scope": ["能力一", "能力二"],
                        "acceptance_criteria": ["完整需求可验收"],
                        "decomposition_plan": [],
                    },
                    "run": {"id": "RDRUN-0002", "run_type": "requirement_decomposition", "status": "running"},
                    "lease_token": "requirement-lease",
                }

            service.claim_next_task = claim_next_task
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)

            self.assertEqual(1, dispatcher.dispatch_once(limit=1))
            methods = [method for method, _ in client.calls]
            self.assertEqual(["thread/start", "thread/name/set", "turn/start"], methods)
            prompt = next(params for method, params in client.calls if method == "turn/start")["input"][0]["text"]
            self.assertIn("submit_requirement_decomposition", prompt)
            self.assertIn("RDRUN-0002", prompt)
            self.assertIsNone(service.bound)

    def test_thread_name_failure_does_not_fail_execution(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            client = FakeClient()
            client.fail_thread_name = True
            dispatcher = TaskDispatcher(service, client=client)

            self.assertEqual(1, dispatcher.dispatch_once())
            self.assertEqual("thread-123", service.bound[2])
            self.assertEqual([], service.interruptions)
            self.assertTrue(client.connected)
            self.assertIn("会话已创建，但命名失败", dispatcher.last_error)

    def test_failed_execution_retry_resumes_existing_thread(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            original_claim = service.claim_next_task

            def claim(worker_id, lease_seconds):
                result = original_claim(worker_id, lease_seconds)
                if result:
                    result["resume_thread_id"] = "existing-thread"
                return result

            service.claim_next_task = claim
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)
            self.assertEqual(1, dispatcher.dispatch_once(limit=1))
            methods = [method for method, _ in client.calls]
            self.assertIn("thread/resume", methods)
            self.assertNotIn("thread/start", methods)
            self.assertEqual("existing-thread", service.bound[2])

    def test_active_writer_retry_never_creates_continuation_thread(self):
        class WriterConflictClient(FakeClient):
            def request(self, method, params):
                if method == "thread/resume":
                    self.calls.append((method, params))
                    raise AppServerError("thread/resume: thread existing-thread already has an active writer")
                return super().request(method, params)

        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            original_claim = service.claim_next_task

            def claim(worker_id, lease_seconds):
                result = original_claim(worker_id, lease_seconds)
                if result:
                    result["resume_thread_id"] = "existing-thread"
                return result

            service.claim_next_task = claim
            client = WriterConflictClient()
            dispatcher = TaskDispatcher(service, client=client)

            self.assertEqual(0, dispatcher.dispatch_once(limit=1))

            methods = [method for method, _ in client.calls]
            self.assertIn("thread/resume", methods)
            self.assertNotIn("thread/start", methods)
            self.assertIsNone(service.bound)
            self.assertEqual("RUN-0001", service.interruptions[0][0])
            self.assertIn("未创建新会话", service.interruptions[0][1])

    def test_unexpected_resume_failure_does_not_silently_create_new_thread(self):
        class FailingResumeClient(FakeClient):
            def request(self, method, params):
                if method == "thread/resume":
                    raise AppServerError("thread/resume: corrupt rollout")
                return super().request(method, params)

        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            original_claim = service.claim_next_task

            def claim(worker_id, lease_seconds):
                result = original_claim(worker_id, lease_seconds)
                if result:
                    result["resume_thread_id"] = "existing-thread"
                return result

            service.claim_next_task = claim
            client = FailingResumeClient()
            dispatcher = TaskDispatcher(service, client=client)

            self.assertEqual(0, dispatcher.dispatch_once(limit=1))
            self.assertNotIn("thread/start", [method for method, _ in client.calls])
            self.assertIn("corrupt rollout", service.interruptions[0][1])

    def test_active_run_lease_is_renewed(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)
            dispatcher.dispatch_once()
            dispatcher._active_runs["thread-123"]["last_renewed"] = time.monotonic() - 61
            dispatcher.dispatch_once()
            self.assertEqual([("RUN-0001", "lease-1", 1800)], service.renewals)

    def test_completed_turn_without_submission_is_execution_failure(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)
            dispatcher.dispatch_once()
            client.notifications.append({"method": "turn/completed", "params": {"threadId": "thread-123"}})
            dispatcher.dispatch_once()
            self.assertEqual("RUN-0001", service.interruptions[0][0])
            self.assertIn("没有提交", service.interruptions[0][1])

    def test_submitted_run_keeps_connection_until_final_turn_completes(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)
            dispatcher.dispatch_once()
            service.run_status = "waiting_review"

            dispatcher.dispatch_once()
            self.assertTrue(client.connected)
            self.assertEqual([], service.interruptions)

            client.notifications.append({
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-123",
                    "turn": {"id": "turn-456", "status": "completed"},
                },
            })
            dispatcher.dispatch_once()
            self.assertFalse(client.connected)
            self.assertEqual([], service.interruptions)

    def test_persisted_turn_status_is_not_polled_while_execution_connection_is_active(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)
            dispatcher.dispatch_once()
            client.turn_status = "interrupted"

            dispatcher.dispatch_once()

            self.assertEqual([], service.interruptions)
            self.assertNotIn("thread/read", [method for method, _ in client.calls])
            self.assertTrue(client.connected)

    def test_idle_execution_is_reaped_without_cross_process_status_probe(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)
            dispatcher.dispatch_once()
            dispatcher.turn_idle_timeout_seconds = 1
            dispatcher._active_runs["thread-123"]["last_activity_at"] = 0

            dispatcher.dispatch_once()

            self.assertEqual("RUN-0001", service.interruptions[0][0])
            self.assertIn("按卡死运行回收", service.interruptions[0][1])
            self.assertNotIn("thread/read", [method for method, _ in client.calls])
            self.assertFalse(client.connected)

    def test_nonterminal_notification_refreshes_idle_watchdog(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)
            dispatcher.dispatch_once()
            dispatcher.turn_idle_timeout_seconds = 1
            dispatcher._active_runs["thread-123"]["last_activity_at"] = 0
            client.notifications.append({
                "method": "item/started",
                "params": {"threadId": "thread-123", "turnId": "turn-456"},
            })

            dispatcher.dispatch_once()

            self.assertEqual([], service.interruptions)
            self.assertGreater(dispatcher._active_runs["thread-123"]["last_activity_at"], 0)
            self.assertTrue(client.connected)

    def test_token_notifications_accumulate_current_run_usage(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)
            dispatcher.dispatch_once()
            client.notifications.extend([
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-123", "turnId": "turn-456",
                        "tokenUsage": {
                            "last": {"totalTokens": 1200, "inputTokens": 1000, "cachedInputTokens": 600, "outputTokens": 200, "reasoningOutputTokens": 50},
                            "total": {"totalTokens": 1200, "inputTokens": 1000, "cachedInputTokens": 600, "outputTokens": 200, "reasoningOutputTokens": 50},
                        },
                    },
                },
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-123", "turnId": "turn-456",
                        "tokenUsage": {
                            "last": {"totalTokens": 650, "inputTokens": 500, "cachedInputTokens": 300, "outputTokens": 150, "reasoningOutputTokens": 25},
                            "total": {"totalTokens": 1850, "inputTokens": 1500, "cachedInputTokens": 900, "outputTokens": 350, "reasoningOutputTokens": 75},
                        },
                    },
                },
            ])

            dispatcher.dispatch_once()

            self.assertEqual([("RUN-0001", 1200), ("RUN-0001", 1850)], service.token_updates)
            self.assertEqual(
                {"input_tokens": 1500, "cached_input_tokens": 900, "output_tokens": 350, "reasoning_output_tokens": 75},
                service.token_usage_details[-1],
            )

    def test_token_budget_stops_the_run_without_retrying(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            service.task["token_budget"] = 100
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)
            dispatcher.dispatch_once()
            client.notifications.append({
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-123", "turnId": "turn-456",
                    "tokenUsage": {
                        "last": {"totalTokens": 100},
                        "total": {"totalTokens": 100},
                    },
                },
            })

            dispatcher.dispatch_once()

            self.assertEqual(1, len(service.budget_pauses))
            self.assertIn("100/100", service.budget_pauses[0][1])
            self.assertFalse(client.connected)

    def test_cached_input_uses_effective_budget_instead_of_raw_total(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            service.task["token_budget"] = 500
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)
            dispatcher.dispatch_once()
            client.notifications.append({
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-123", "turnId": "turn-456",
                    "tokenUsage": {
                        "last": {
                            "totalTokens": 1000, "inputTokens": 1000,
                            "cachedInputTokens": 900, "outputTokens": 0,
                        },
                        "total": {
                            "totalTokens": 1000, "inputTokens": 1000,
                            "cachedInputTokens": 900, "outputTokens": 0,
                        },
                    },
                },
            })

            dispatcher.dispatch_once()

            self.assertEqual(190, service.task["effective_token_used"])
            self.assertEqual([], service.budget_pauses)
            self.assertTrue(client.connected)

    def test_unrelated_turn_completion_does_not_interrupt_active_run(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)
            dispatcher.dispatch_once()
            client.notifications.append({
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-123",
                    "turn": {"id": "older-turn", "status": "completed"},
                },
            })

            dispatcher.dispatch_once()

            self.assertEqual([], service.interruptions)
            self.assertTrue(client.connected)

    def test_missing_project_blocks_claim(self):
        service = FakeService("/path/that/does/not/exist")
        dispatcher = TaskDispatcher(service, client=FakeClient())

        self.assertEqual(0, dispatcher.dispatch_once())
        self.assertEqual("blocked", service.task["status"])
        self.assertIn("项目目录不存在", service.transitions[0][2])

    def test_review_is_prioritized_and_creates_review_conversation(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            service.claim_next_review_task = lambda worker_id, lease_seconds: {
                "task": {"id": "TASK-0002", "title": "待验收", "project": project, "status": "review"},
                "run": {"id": "RUN-0002", "run_type": "review"},
                "lease_token": "lease-2",
                "dispatch_prompt": "复核 CodeGraph 后调用 review_task",
            }
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)

            self.assertEqual(1, dispatcher.dispatch_once(limit=1))
            self.assertEqual(
                ("TASK-0002", "review", "thread-123", "RUN-0002", "验收"),
                service.bound,
            )
            turn = next(params for method, params in client.calls if method == "turn/start")
            self.assertIn("review_task", turn["input"][0]["text"])

    def test_v2_stage_dispatch_binds_exact_role_and_lease(self):
        for run_type, claim_method, task_status in (("code_review", "claim_next_code_review_task", "code_review"), ("acceptance", "claim_next_acceptance_task", "acceptance"), ("bugfix", "claim_next_task", "claimed")):
            with self.subTest(run_type=run_type), tempfile.TemporaryDirectory() as project:
                service = FakeService(project)
                lease = f"lease-{run_type}"
                task = {"id": f"TASK-{run_type}", "title": run_type, "project": project, "status": task_status}
                run = {"id": f"RUN-{run_type}", "run_type": run_type, "status": "awaiting_thread"}
                claim = {"task": task, "run": run, "lease_token": lease, "dispatch_prompt": f"$codex-taskboard-lifecycle\n\n{task['id']} {run['id']}"}
                setattr(service, claim_method, lambda worker_id, lease_seconds, claim=claim: claim if not service.claimed else None)
                service.claim_next_review_task = lambda worker_id, lease_seconds: None
                client = FakeClient()
                dispatcher = TaskDispatcher(service, client=client)
                self.assertEqual(1, dispatcher.dispatch_once(limit=1))
                self.assertEqual(run_type, service.bound[1])
                self.assertEqual(lease, dispatcher._active_runs["thread-123"]["lease_token"])
                self.assertIn("$codex-taskboard-lifecycle", next(p for m, p in client.calls if m == "turn/start")["input"][0]["text"])

    def test_automated_acceptance_shortcut_does_not_start_a_model(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            service.auto_accept_result = {"eligible": True, "completed": True}
            claim = {
                "task": {"id": "TASK-auto", "title": "auto", "project": project, "status": "acceptance"},
                "run": {"id": "RUN-auto", "run_type": "acceptance", "status": "awaiting_thread"},
                "lease_token": "lease-auto", "dispatch_prompt": "auto",
            }
            service.claim_next_acceptance_task = lambda worker_id, lease_seconds: claim
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)

            self.assertEqual(1, dispatcher.dispatch_once(limit=1))
            self.assertEqual([], client.calls)
            self.assertFalse(client.connected)
            self.assertIsNone(service.bound)

    def test_stop_interrupts_active_runs_before_terminating_clients(self):
        with tempfile.TemporaryDirectory() as project:
            service = FakeService(project)
            client = FakeClient()
            dispatcher = TaskDispatcher(service, client=client)
            dispatcher.dispatch_once()
            dispatcher.stop()
            self.assertIn(("RUN-0001", "Codex Taskboard 调度器已停止"), service.interruptions)
            self.assertFalse(client.connected)
            self.assertEqual(1, client.stop_calls)

    def test_dispatcher_file_lock_prevents_a_second_instance(self):
        class IdleService:
            def __init__(self, home):
                self.data_home = Path(home)

            def dispatcher_enabled(self):
                return False

            def recover_orphaned_dispatcher_runs(self, worker_id):
                return 0

        with tempfile.TemporaryDirectory() as home:
            first = TaskDispatcher(IdleService(home), interval_seconds=0.01)
            second = TaskDispatcher(IdleService(home), interval_seconds=0.01)
            first.start()
            second.start()
            try:
                self.assertTrue(first.status()["running"])
                self.assertFalse(second.status()["running"])
                self.assertIn("另一个", second.status()["last_error"])
            finally:
                second.stop()
                first.stop()


if __name__ == "__main__":
    unittest.main()
