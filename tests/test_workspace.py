from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.service import TaskboardService
from taskboard.app_server import AppServerError
from taskboard.workspace import CodexWorkspaceService, thread_group_name, thread_stage_name


class FakeCodexClient:
    def __init__(self, threads=None):
        self.threads = list(threads or [])
        self.calls = []
        self.stopped = False
        self.read_status = {"type": "notLoaded"}

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/list":
            threads = self.threads
            if params.get("cwd"):
                threads = [thread for thread in threads if thread.get("cwd") == params["cwd"]]
            return {"data": threads}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "status": self.read_status, "turns": []}}
        if method == "thread/start":
            return {"thread": {"id": "thread-new", "cwd": params["cwd"], "status": {"type": "idle"}}}
        if method == "turn/start":
            return {"turn": {"id": "turn-new", "status": "inProgress"}}
        return {}

    def stop(self):
        self.stopped = True


class FailingCodexClient(FakeCodexClient):
    def __init__(self, message):
        super().__init__()
        self.message = message

    def request(self, method, params):
        self.calls.append((method, params))
        raise AppServerError(self.message)


class FakeHelper:
    def __init__(self, project=None):
        self.project = project
        self.calls = 0

    def authorize_project(self):
        self.calls += 1
        return self.project


class CodexWorkspaceServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project_a = (Path(self.temp.name) / "project-a").resolve()
        self.project_b = (Path(self.temp.name) / "project-b").resolve()
        self.project_a.mkdir()
        self.project_b.mkdir()
        self.service = TaskboardService(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_projects_merge_manual_folders_and_codex_thread_workspaces(self):
        self.service.add_workspace_project(str(self.project_a))
        client = FakeCodexClient([
            {"id": "thread-1", "cwd": str(self.project_b), "updatedAt": 30},
            {"id": "thread-2", "cwd": str(self.project_b), "updatedAt": 20},
        ])
        workspace = CodexWorkspaceService(self.service, client=client)

        projects = workspace.list_projects()

        by_path = {item["path"]: item for item in projects}
        self.assertEqual(["manual"], by_path[str(self.project_a)]["sources"])
        self.assertEqual(2, by_path[str(self.project_b)]["thread_count"])
        self.assertIn("codex", by_path[str(self.project_b)]["sources"])

    def test_manual_project_is_validated_and_persisted(self):
        self.service.add_workspace_project(str(self.project_a))
        reloaded = TaskboardService(self.temp.name)
        self.assertEqual([str(self.project_a.resolve())], reloaded.workspace_projects())
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.service.add_workspace_project(str(Path(self.temp.name) / "missing"))

    def test_archived_project_stays_hidden_from_all_discovery_sources(self):
        self.service.add_workspace_project(str(self.project_a))
        with self.service.db.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(id, title, project) VALUES('TASK-0001', '归档测试', ?)",
                (str(self.project_a),),
            )
        client = FakeCodexClient([
            {"id": "thread-a", "cwd": str(self.project_a), "updatedAt": 30},
        ])
        workspace = CodexWorkspaceService(self.service, client=client)

        first = workspace.archive_project(str(self.project_a))
        second = workspace.archive_project(str(self.project_a))

        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertNotIn(str(self.project_a), {item["path"] for item in workspace.list_projects()})
        archived = workspace.list_projects(archived=True)
        self.assertEqual([str(self.project_a)], [item["path"] for item in archived])
        self.assertEqual({"manual", "taskboard", "codex", "archived"}, set(archived[0]["sources"]))
        reloaded = TaskboardService(self.temp.name)
        self.assertEqual([str(self.project_a)], reloaded.archived_workspace_projects())

    def test_missing_archived_project_can_be_restored_without_data_loss(self):
        self.service.add_workspace_project(str(self.project_b))
        self.project_b.rmdir()
        workspace = CodexWorkspaceService(self.service, client=FakeCodexClient())

        workspace.archive_project(str(self.project_b))
        archived = workspace.list_projects(archived=True)
        self.assertFalse(archived[0]["exists"])
        self.assertEqual({"path": str(self.project_b), "archived": False, "changed": True}, workspace.restore_project(str(self.project_b)))
        self.assertIn(str(self.project_b), {item["path"] for item in workspace.list_projects()})
        self.assertFalse(workspace.restore_project(str(self.project_b))["changed"])

    def test_archive_requires_a_normalized_absolute_path(self):
        workspace = CodexWorkspaceService(self.service, client=FakeCodexClient())
        with self.assertRaisesRegex(ValueError, "normalized absolute"):
            workspace.archive_project("relative/project")
        with self.assertRaisesRegex(ValueError, "normalized absolute"):
            workspace.archive_project(f"{self.project_a}/../project-a")

    def test_helper_folder_picker_registers_selected_project(self):
        helper = FakeHelper(str(self.project_a))
        workspace = CodexWorkspaceService(
            self.service, client=FakeCodexClient(), helper=helper,
        )

        project = workspace.pick_project()

        self.assertEqual(str(self.project_a), project["path"])
        self.assertEqual(1, helper.calls)

    def test_helper_folder_picker_allows_cancel(self):
        helper = FakeHelper()
        workspace = CodexWorkspaceService(
            self.service, client=FakeCodexClient(), helper=helper,
        )

        self.assertIsNone(workspace.pick_project())
        self.assertEqual(1, helper.calls)

    def test_thread_listing_is_filtered_by_project(self):
        client = FakeCodexClient([
            {"id": "thread-a", "cwd": str(self.project_a)},
            {"id": "thread-b", "cwd": str(self.project_b)},
        ])
        workspace = CodexWorkspaceService(self.service, client=client)

        threads = workspace.list_threads(str(self.project_a))

        self.assertEqual(["thread-a"], [thread["id"] for thread in threads])
        self.assertEqual(str(self.project_a.resolve()), client.calls[0][1]["cwd"])
        self.assertEqual(["appServer", "vscode"], client.calls[0][1]["sourceKinds"])

    def test_workspace_replaces_a_stale_app_server_client_once(self):
        stale = FailingCodexClient(
            "Codex app-server initialize failed after 2 attempts: connection closed"
        )
        recovered = FakeCodexClient([
            {"id": "thread-a", "cwd": str(self.project_a)},
        ])
        workspace = CodexWorkspaceService(self.service, client=stale)

        with patch.object(workspace, "_new_client", return_value=recovered) as new_client:
            threads = workspace.list_threads(str(self.project_a))

        self.assertEqual(["thread-a"], [thread["id"] for thread in threads])
        self.assertTrue(stale.stopped)
        new_client.assert_called_once_with()

    def test_workspace_does_not_retry_protocol_errors(self):
        rejected = FailingCodexClient("thread/list: invalid params")
        workspace = CodexWorkspaceService(self.service, client=rejected)

        with patch.object(workspace, "_new_client") as new_client:
            with self.assertRaisesRegex(AppServerError, "invalid params"):
                workspace.list_threads(str(self.project_a))

        self.assertFalse(rejected.stopped)
        new_client.assert_not_called()

    def test_task_threads_receive_development_and_review_folder_names(self):
        self.assertEqual(
            "TASK-001",
            thread_group_name(self.project_a, "TASK-0001", "execution"),
        )
        self.assertEqual(
            "TASK-012",
            thread_group_name(self.project_a, "TASK-0012", "rework"),
        )
        self.assertEqual(
            "TASK-012",
            thread_group_name(self.project_a, "TASK-0012", "review"),
        )
        self.assertEqual("开发", thread_stage_name("execution"))
        self.assertEqual("Review", thread_stage_name("code_review"))
        self.assertEqual("验收", thread_stage_name("acceptance"))

    def test_thread_listing_is_enriched_with_task_folder_metadata(self):
        with self.service.db.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(id, title, project) VALUES('TASK-0001', '分组测试', ?)",
                (str(self.project_a),),
            )
            connection.execute(
                """INSERT INTO task_conversations(task_id, role, thread_id, title)
                   VALUES('TASK-0001', 'source', 'thread-a', '来源')""",
            )
        client = FakeCodexClient([{"id": "thread-a", "cwd": str(self.project_a)}])
        workspace = CodexWorkspaceService(self.service, client=client)

        thread = workspace.list_threads(str(self.project_a))[0]

        self.assertEqual("TASK-0001", thread["taskId"])
        self.assertEqual("source", thread["conversationRole"])
        self.assertEqual("TASK-001", thread["groupName"])
        self.assertEqual("需求", thread["displayName"])

    def test_task_stage_threads_share_one_task_group_with_short_names(self):
        roles = (("thread-dev", "execution"), ("thread-review", "code_review"), ("thread-accept", "acceptance"))
        with self.service.db.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(id, title, project) VALUES('TASK-0001', '分组测试', ?)",
                (str(self.project_a),),
            )
            connection.executemany(
                "INSERT INTO task_conversations(task_id, role, thread_id, title) VALUES('TASK-0001', ?, ?, '历史长标题')",
                [(role, thread_id) for thread_id, role in roles],
            )
        client = FakeCodexClient([
            {"id": thread_id, "cwd": str(self.project_a), "name": "任务 TASK-0001：历史长标题"}
            for thread_id, _ in roles
        ])
        workspace = CodexWorkspaceService(self.service, client=client)

        threads = workspace.list_threads(str(self.project_a))

        self.assertEqual({"TASK-001"}, {thread["groupName"] for thread in threads})
        self.assertEqual({"开发", "Review", "验收"}, {thread["displayName"] for thread in threads})
        self.assertEqual("Review", workspace.read_thread("thread-review")["displayName"])

    def test_start_turn_refuses_an_active_thread(self):
        client = FakeCodexClient()
        client.read_status = {"type": "active"}
        workspace = CodexWorkspaceService(self.service, client=client)

        with self.assertRaisesRegex(ValueError, "仍在运行"):
            workspace.start_turn("thread-a", "继续")

        self.assertEqual(["thread/read"], [method for method, _ in client.calls])

    def test_new_workspace_thread_is_scoped_to_the_project(self):
        client = FakeCodexClient()
        workspace = CodexWorkspaceService(self.service, client=client)

        thread = workspace.create_thread(str(self.project_a), "新会话")

        self.assertEqual("thread-new", thread["id"])
        start = next(params for method, params in client.calls if method == "thread/start")
        self.assertEqual(str(self.project_a), start["cwd"])
        self.assertEqual("workspace-write", start["sandbox"])
        self.assertEqual("never", start["approvalPolicy"])

    def test_start_turn_resumes_idle_thread_before_sending(self):
        client = FakeCodexClient()
        workspace = CodexWorkspaceService(self.service, client=client)

        turn = workspace.start_turn("thread-a", "继续")

        self.assertEqual("turn-new", turn["id"])
        self.assertEqual(
            ["thread/read", "thread/resume", "turn/start"],
            [method for method, _ in client.calls],
        )


if __name__ == "__main__":
    unittest.main()
