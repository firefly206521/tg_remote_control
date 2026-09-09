import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import scheduler as scheduler_module
from history import HistoryStore
from runner import ProgressUpdate, TurnResult
from scheduler import Scheduler, SessionState


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.edits = []

    async def edit_text(self, text):
        self.text = text
        self.edits.append(text)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        message = FakeMessage(text)
        self.sent.append(message)
        return message

    async def send_document(self, *args, **kwargs):
        raise AssertionError("short streamed output must not be sent as a document")


def settings(root):
    return SimpleNamespace(
        default_workdir=str(root), max_sessions=8, queue_limit=3,
        status_edit_interval=2, file_threshold=3500,
        claude_auto_compact_seconds=0,
        claude_auto_compact_retries=1,
    )


class SchedulerStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old_state = scheduler_module.STATE_FILE
        scheduler_module.STATE_FILE = root / "state.json"
        self.history = HistoryStore(root / "history.sqlite3")
        self.bot = FakeBot()
        self.scheduler = Scheduler(self.bot, settings(root), self.history)
        self.session = SessionState("main", 123, str(root))

    async def asyncTearDown(self):
        self.history.close()
        scheduler_module.STATE_FILE = self.old_state
        self.temp.cleanup()

    async def test_partial_output_survives_failure_and_precedes_error(self):
        async def failed_turn(agent, prompt, workdir, session_id, settings, on_progress, model="", effort=""):
            await on_progress(ProgressUpdate("text", "已经完成第一步"))
            await on_progress(ProgressUpdate("activity", "✅ python check.py"))
            return TurnResult(False, "session-1", "已经完成第一步", "余额不足")

        with patch("scheduler.run_turn", new=failed_turn):
            await self.scheduler._run_one(self.session, "开始")

        sent_texts = [message.text for message in self.bot.sent]
        progress_index = next(i for i, text in enumerate(sent_texts) if "已经完成第一步" in text)
        error_index = next(i for i, text in enumerate(sent_texts) if "余额不足" in text)
        self.assertLess(progress_index, error_index)
        rows = self.history.list_messages(123, self.session.key)
        self.assertTrue(any(row["kind"] == "progress" and "第一步" in row["text"] for row in rows))
        self.assertEqual(rows[-1]["kind"], "failed")

    async def test_exact_streamed_success_is_not_sent_twice(self):
        async def successful_turn(agent, prompt, workdir, session_id, settings, on_progress, model="", effort=""):
            await on_progress(ProgressUpdate("text", "最终回复"))
            return TurnResult(True, "session-1", "最终回复", "")

        with patch("scheduler.run_turn", new=successful_turn):
            await self.scheduler._run_one(self.session, "开始")

        containing_final = [message.text for message in self.bot.sent if "最终回复" in message.text]
        self.assertEqual(len(containing_final), 1)

    async def test_long_stream_is_split_without_losing_previous_output(self):
        self.session.stream_text = "x" * 8005

        await self.scheduler._flush_stream(self.session)

        prefix = f"📢 [{self.session.name}] {self.session.agent} · 实时输出\n\n"
        bodies = [message.text.removeprefix(prefix) for message in self.bot.sent]
        self.assertEqual(len(bodies), 3)
        self.assertEqual("".join(bodies), self.session.stream_text)

    async def test_claude_timeout_compacts_same_session_then_continues(self):
        self.session.agent = "claude"
        self.scheduler.settings.claude_auto_compact_seconds = 600
        calls = []

        async def segmented_turn(
            agent, prompt, workdir, session_id, settings, on_progress=None,
            model="", effort="", timeout_seconds=0, accept_exit_zero=False,
        ):
            calls.append((prompt, session_id, timeout_seconds, on_progress is not None, accept_exit_zero))
            if len(calls) == 1:
                return TurnResult(False, "session-1", "", "segment elapsed", timed_out=True)
            if len(calls) == 2:
                return TurnResult(True, "session-1", "Compacted", "")
            return TurnResult(True, "session-1", "完成", "")

        with patch("scheduler.run_turn", new=segmented_turn):
            await self.scheduler._run_one(self.session, "执行长任务")

        self.assertEqual([call[0] for call in calls], [
            "执行长任务",
            "/compact focus on preserving the active task, completed changes, test evidence, unresolved work, and the exact next action",
            "继续",
        ])
        self.assertEqual(calls[1][1], "session-1")
        self.assertEqual(calls[2][1], "session-1")
        self.assertEqual(calls[0][2], 600)
        self.assertEqual(calls[1][2], 600)
        self.assertFalse(calls[1][3])
        self.assertTrue(calls[1][4])
        self.assertEqual(self.session.contexts["claude"], "session-1")

    async def test_real_compact_failure_is_retried_before_continue(self):
        self.session.agent = "claude"
        self.scheduler.settings.claude_auto_compact_seconds = 600
        self.scheduler.settings.claude_auto_compact_retries = 1
        calls = []

        async def retrying_turn(
            agent, prompt, workdir, session_id, settings, on_progress=None,
            model="", effort="", timeout_seconds=0, accept_exit_zero=False,
        ):
            calls.append((prompt, accept_exit_zero))
            if len(calls) == 1:
                return TurnResult(False, "session-1", "", "segment elapsed", timed_out=True)
            if len(calls) == 2:
                return TurnResult(False, "session-1", "", "temporary compact failure")
            if len(calls) == 3:
                return TurnResult(True, "session-1", "Compacted", "")
            return TurnResult(True, "session-1", "完成", "")

        with patch("scheduler.run_turn", new=retrying_turn):
            await self.scheduler._run_one(self.session, "执行长任务")

        self.assertEqual([call[0] for call in calls], [
            "执行长任务",
            "/compact focus on preserving the active task, completed changes, test evidence, unresolved work, and the exact next action",
            "/compact focus on preserving the active task, completed changes, test evidence, unresolved work, and the exact next action",
            "继续",
        ])
        self.assertEqual([call[1] for call in calls], [False, True, True, False])

    async def test_codex_does_not_get_claude_compaction_timeout(self):
        self.scheduler.settings.claude_auto_compact_seconds = 600
        calls = []

        async def successful_turn(
            agent, prompt, workdir, session_id, settings, on_progress=None,
            model="", effort="", **kwargs,
        ):
            calls.append(kwargs)
            return TurnResult(True, "codex-session", "完成", "")

        with patch("scheduler.run_turn", new=successful_turn):
            await self.scheduler._run_one(self.session, "执行")

        self.assertEqual(calls, [{}])

    async def test_manual_cancel_does_not_compact_or_continue(self):
        self.session.agent = "claude"
        self.scheduler.settings.claude_auto_compact_seconds = 600
        entered = asyncio.Event()
        calls = []

        async def blocking_turn(
            agent, prompt, workdir, session_id, settings, on_progress=None,
            model="", effort="", timeout_seconds=0, accept_exit_zero=False,
        ):
            calls.append(prompt)
            entered.set()
            await asyncio.Event().wait()

        with patch("scheduler.run_turn", new=blocking_turn):
            task = asyncio.create_task(self.scheduler._run_one(self.session, "执行长任务"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(calls, ["执行长任务"])


if __name__ == "__main__":
    unittest.main()
