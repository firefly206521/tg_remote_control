import asyncio
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from restart import RESTART_EXIT_CODE
from config import Settings
from history import HistoryStore
from scheduler import Scheduler, split_state_by_agent


class FakeBot:
    async def send_message(self, chat_id, text):
        return None


def settings(root):
    return SimpleNamespace(
        default_workdir=str(root), max_sessions=8, queue_limit=3,
        status_edit_interval=2, file_threshold=3500,
        allowed_user_ids=frozenset({123}),
    )


class DualBotIsolationTests(unittest.TestCase):
    def test_loads_separate_bot_tokens_with_legacy_codex_fallback(self):
        environment = {
            "BOT_TOKEN": "123:codex",
            "CLAUDE_BOT_TOKEN": "456:claude",
            "ALLOWED_USER_IDS": "123",
        }
        with patch("config.load_dotenv"), patch.dict(os.environ, environment, clear=True):
            loaded = Settings.load()

        self.assertEqual(loaded.bot_token, "123:codex")
        self.assertEqual(loaded.claude_bot_token, "456:claude")

    def test_fixed_agent_schedulers_keep_same_chat_separate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            history = HistoryStore(root / "history.sqlite3")
            try:
                codex = Scheduler(
                    FakeBot(), settings(root), history,
                    state_file=root / "state.json", fixed_agent="codex",
                )
                claude = Scheduler(
                    FakeBot(), settings(root), history,
                    state_file=root / "state.claude.json", fixed_agent="claude",
                )
                codex_chat = codex.get(123)
                claude_chat = claude.get(123)

                self.assertEqual(codex_chat.cur.agent, "codex")
                self.assertEqual(claude_chat.cur.agent, "claude")
                codex.new_session(codex_chat, "codex-only")
                self.assertNotIn("codex-only", claude_chat.sessions)
                created, reason = claude.create_session(claude_chat, "wrong", "codex", str(root))
                self.assertIsNone(created)
                self.assertIn("claude", reason)
            finally:
                history.close()

    def test_only_codex_gui_aggregates_claude_backend(self):
        configured = SimpleNamespace(
            claude_bot_token="2:test", gui_host="127.0.0.1", gui_port_claude=8766,
        )
        self.assertEqual(main._peer_gui_url(configured, "codex"), "http://127.0.0.1:8766")
        self.assertEqual(main._peer_gui_url(configured, "claude"), "")

    def test_split_moves_only_claude_sessions_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "state.json"
            target = root / "state.claude.json"
            source.write_text(json.dumps({
                "123": {
                    "current": "claude-task",
                    "sessions": {
                        "codex-task": {"key": "c", "agent": "codex", "workdir": str(root), "contexts": {}},
                        "claude-task": {"key": "d", "agent": "claude", "workdir": str(root), "contexts": {"claude": "sid"}},
                    },
                }
            }), encoding="utf-8")

            self.assertEqual(split_state_by_agent(source, target, "claude"), 1)
            codex_data = json.loads(source.read_text("utf-8"))
            claude_data = json.loads(target.read_text("utf-8"))
            self.assertEqual(list(codex_data["123"]["sessions"]), ["codex-task"])
            self.assertEqual(list(claude_data["123"]["sessions"]), ["claude-task"])
            self.assertEqual(claude_data["123"]["sessions"]["claude-task"]["contexts"]["claude"], "sid")
            self.assertEqual(split_state_by_agent(source, target, "claude"), 0)


class FakeChild:
    def __init__(self, pid, exit_code=None):
        self.pid = pid
        self.exit_code = exit_code
        self.signals = []

    def poll(self):
        return self.exit_code

    def send_signal(self, value):
        self.signals.append(value)
        self.exit_code = 0

    def wait(self):
        self.exit_code = 0
        return 0


class DualBotSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_children_sends_console_break(self):
        child = FakeChild(101)
        await main._stop_supervised_children([child])
        expected = signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
        self.assertEqual(child.signals, [expected])

    async def test_requested_restart_replaces_only_that_agent(self):
        first_codex = FakeChild(101, RESTART_EXIT_CODE)
        claude = FakeChild(102)
        next_codex = FakeChild(103)
        children = iter((first_codex, claude, next_codex))
        sleeps = 0

        async def short_run(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise asyncio.CancelledError

        with (
            patch("main.subprocess.Popen", side_effect=lambda *a, **k: next(children)) as popen,
            patch("main.asyncio.sleep", side_effect=short_run),
            patch("main._load_gui_token", return_value="shared-token") as load_token,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main.supervise_dual_bots(SimpleNamespace(gui_token=""))

        self.assertEqual(popen.call_count, 3)
        agents = [call.args[0][-1] for call in popen.call_args_list]
        self.assertEqual(agents, ["codex", "claude", "codex"])
        self.assertTrue(all(call.kwargs["env"]["TG_DISPATCH_SUPERVISED"] == "1"
                            for call in popen.call_args_list))
        load_token.assert_called_once_with("")
        self.assertEqual(claude.signals, [signal.CTRL_BREAK_EVENT])
        self.assertEqual(next_codex.signals, [signal.CTRL_BREAK_EVENT])


if __name__ == "__main__":
    unittest.main()
