import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

import scheduler as scheduler_module
from history import HistoryStore
from scheduler import Scheduler
from webui import create_web_app


class FakeBot:
    async def send_message(self, chat_id, text):
        return SimpleNamespace(edit_text=self._edit_text)

    async def _edit_text(self, text):
        return None


def settings(workdir: str):
    return SimpleNamespace(
        default_workdir=workdir,
        max_sessions=8,
        queue_limit=3,
        allowed_user_ids=frozenset({123}),
        file_threshold=3500,
        status_edit_interval=4,
    )


class SchedulerWebApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old_state = scheduler_module.STATE_FILE
        scheduler_module.STATE_FILE = root / "state.json"
        self.history = HistoryStore(root / "history.sqlite3")
        self.scheduler = Scheduler(FakeBot(), settings(str(root)), self.history)
        self.scheduler.get(123)
        app = create_web_app(self.scheduler, self.history, "test-token")
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        for chat in self.scheduler.chats.values():
            for session in chat.sessions.values():
                if session.worker_task:
                    session.worker_task.cancel()
        await asyncio.sleep(0)
        await self.client.close()
        self.history.close()
        scheduler_module.STATE_FILE = self.old_state
        self.temp.cleanup()

    def auth(self):
        return {"Authorization": "Bearer test-token"}

    async def test_api_requires_token_and_lists_stable_task_ids(self):
        response = await self.client.get("/api/state")
        self.assertEqual(response.status, 401)

        response = await self.client.get("/api/state", headers=self.auth())
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(len(payload["tasks"]), 1)
        self.assertEqual(payload["tasks"][0]["name"], "main")
        self.assertTrue(payload["tasks"][0]["id"])
        self.assertNotIn("contexts", payload["tasks"][0])

        first_id = payload["tasks"][0]["id"]
        scheduler_module.Scheduler(FakeBot(), settings(self.temp.name), self.history)
        reloaded = Scheduler(FakeBot(), settings(self.temp.name), self.history)
        self.assertEqual(reloaded.get(123).cur.key, first_id)

    async def test_create_validate_submit_and_stop_specific_task(self):
        response = await self.client.post(
            "/api/tasks", headers=self.auth(),
            json={"name": "网页任务", "agent": "codex", "workdir": self.temp.name},
        )
        self.assertEqual(response.status, 201)
        task = (await response.json())["task"]

        duplicate = await self.client.post(
            "/api/tasks", headers=self.auth(),
            json={"name": "网页任务", "agent": "codex", "workdir": self.temp.name},
        )
        self.assertEqual(duplicate.status, 409)

        response = await self.client.post(
            f"/api/tasks/{task['id']}/submit", headers=self.auth(), json={"prompt": "测试消息"}
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(self.history.list_messages(123, task["id"])[0]["text"], "测试消息")

        response = await self.client.post(f"/api/tasks/{task['id']}/stop", headers=self.auth())
        self.assertEqual(response.status, 200)
        self.assertEqual(self.history.list_messages(123, task["id"])[-1]["kind"], "stopped")

    async def test_rejects_unknown_task_bad_agent_and_missing_directory(self):
        bad_agent = await self.client.post(
            "/api/tasks", headers=self.auth(),
            json={"name": "bad", "agent": "other", "workdir": self.temp.name},
        )
        self.assertEqual(bad_agent.status, 400)
        missing = await self.client.post(
            "/api/tasks", headers=self.auth(),
            json={"name": "missing", "agent": "codex", "workdir": str(Path(self.temp.name) / "none")},
        )
        self.assertEqual(missing.status, 400)
        unknown = await self.client.post(
            "/api/tasks/no-such-task/stop", headers=self.auth()
        )
        self.assertEqual(unknown.status, 404)


class MultiSchedulerWebApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.history = HistoryStore(root / "history.sqlite3")
        self.codex = Scheduler(
            FakeBot(), settings(str(root)), self.history,
            state_file=root / "state.json", fixed_agent="codex",
        )
        self.claude = Scheduler(
            FakeBot(), settings(str(root)), self.history,
            state_file=root / "state.claude.json", fixed_agent="claude",
        )
        self.codex.get(123)
        self.claude.get(123)
        app = create_web_app({"codex": self.codex, "claude": self.claude}, self.history, "test-token")
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.history.close()
        self.temp.cleanup()

    async def test_lists_both_bots_and_routes_new_task_by_agent(self):
        response = await self.client.get("/api/state", headers={"Authorization": "Bearer test-token"})
        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual({task["agent"] for task in payload["tasks"]}, {"codex", "claude"})
        self.assertEqual(set(payload["telegramCurrentByAgent"]), {"codex", "claude"})

        response = await self.client.post(
            "/api/tasks", headers={"Authorization": "Bearer test-token"},
            json={"name": "claude-web", "agent": "claude", "workdir": self.temp.name},
        )
        self.assertEqual(response.status, 201)
        self.assertIn("claude-web", self.claude.get(123).sessions)
        self.assertNotIn("claude-web", self.codex.get(123).sessions)


class PeerSchedulerWebApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.history = HistoryStore(root / "history.sqlite3")
        self.codex = Scheduler(
            FakeBot(), settings(str(root)), self.history,
            state_file=root / "state.json", fixed_agent="codex",
        )
        self.claude = Scheduler(
            FakeBot(), settings(str(root)), self.history,
            state_file=root / "state.claude.json", fixed_agent="claude",
        )
        self.codex.get(123)
        self.claude.get(123)

        self.peer_server = TestServer(
            create_web_app(self.claude, self.history, "test-token")
        )
        await self.peer_server.start_server()
        primary_app = create_web_app(
            self.codex,
            self.history,
            "test-token",
            peer_url=str(self.peer_server.make_url("/")).rstrip("/"),
        )
        self.client = TestClient(TestServer(primary_app))
        await self.client.start_server()

    async def asyncTearDown(self):
        for scheduler in (self.codex, self.claude):
            for chat in scheduler.chats.values():
                for session in chat.sessions.values():
                    if session.worker_task:
                        session.worker_task.cancel()
        await asyncio.sleep(0)
        await self.client.close()
        await self.peer_server.close()
        self.history.close()
        self.temp.cleanup()

    def auth(self):
        return {"Authorization": "Bearer test-token"}

    async def test_primary_gui_aggregates_peer_and_routes_claude_actions(self):
        response = await self.client.get("/api/state", headers=self.auth())
        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual({task["agent"] for task in payload["tasks"]}, {"codex", "claude"})
        self.assertTrue(payload["agents"]["codex"]["available"])
        self.assertTrue(payload["agents"]["claude"]["available"])

        response = await self.client.post(
            "/api/tasks", headers=self.auth(),
            json={"name": "claude-peer", "agent": "claude", "workdir": self.temp.name},
        )
        self.assertEqual(response.status, 201)
        task = (await response.json())["task"]
        self.assertIn("claude-peer", self.claude.get(123).sessions)
        self.assertNotIn("claude-peer", self.codex.get(123).sessions)

        response = await self.client.post(
            f"/api/tasks/{task['id']}/submit", headers=self.auth(), json={"prompt": "继续测试"},
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(self.history.list_messages(123, task["id"])[0]["text"], "继续测试")

        response = await self.client.post(
            f"/api/tasks/{task['id']}/stop", headers=self.auth(), json={},
        )
        self.assertEqual(response.status, 200)

    async def test_primary_gui_reports_peer_unavailable_without_hiding_codex(self):
        await self.peer_server.close()

        response = await self.client.get("/api/state", headers=self.auth())
        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual({task["agent"] for task in payload["tasks"]}, {"codex"})
        self.assertFalse(payload["agents"]["claude"]["available"])

        response = await self.client.post(
            "/api/tasks", headers=self.auth(),
            json={"name": "no-peer", "agent": "claude", "workdir": self.temp.name},
        )
        self.assertEqual(response.status, 503)
        self.assertIn("Claude", (await response.json())["error"])


if __name__ == "__main__":
    unittest.main()
