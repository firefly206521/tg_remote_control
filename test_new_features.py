"""新功能测试：编号切换、模型/思考强度覆盖、cc-switch 联动、状态持久化。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import ccswitch
import restart
import runner
from config import Settings
from scheduler import ChatState, Scheduler, SessionState

FIXTURES = Path(__file__).parent / "fixtures"


def make_settings(root: Path) -> Settings:
    return Settings(
        bot_token="1:test", claude_bot_token="2:test", allowed_user_ids=frozenset(),
        proxy_url="", default_workdir=str(root), codex_sandbox="workspace-write",
        codex_network=True, codex_model="", claude_permission_mode="bypassPermissions",
        claude_model="", codex_models=("gpt-5.6-sol", "gpt-5.6-terra"),
        claude_models=("opus", "sonnet", "haiku"),
        codex_efforts=("minimal", "low", "medium", "high"),
        claude_efforts=("low", "medium", "high", "xhigh", "max"),
        claude_auto_compact_seconds=0,
        claude_auto_compact_retries=1,
        queue_limit=3, max_sessions=8, file_threshold=3500, status_edit_interval=2,
        gui_host="127.0.0.1", gui_port=8765, gui_port_claude=8766, gui_token="t",
    )


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text))
        return SimpleNamespace(edit_text=lambda *a, **k: None)


class SessionIndexTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bot = FakeBot()
        self.scheduler = Scheduler(
            self.bot, make_settings(root), state_file=root / "state.json"
        )
        self.chat = self.scheduler.get(1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_render_list_has_indices(self):
        self.scheduler.new_session(self.chat, "alpha")
        text = self.scheduler.render_list(self.chat)
        self.assertIn("1. main", text)
        self.assertIn("2. alpha", text)

    def test_switch_by_index(self):
        self.scheduler.new_session(self.chat, "alpha")
        self.scheduler.switch_by_index(self.chat, 1)
        self.assertIn("已切换到会话「alpha」", self.scheduler.switch_by_index(self.chat, 2))
        self.assertEqual(self.chat.current, "alpha")
        self.assertEqual(self.scheduler.switch_by_index(self.chat, 99), "")

    def test_model_effort_persisted(self):
        self.scheduler.set_model(self.chat, "gpt-5.6-terra")
        self.scheduler.set_effort(self.chat, "high")
        self.assertIn('"model": "gpt-5.6-terra"', self.scheduler.state_file.read_text("utf-8"))
        restored = Scheduler(
            self.bot, make_settings(Path(self.tmp.name)), state_file=self.scheduler.state_file
        )
        self.assertEqual(restored.get(1).cur.model, "gpt-5.6-terra")
        self.assertEqual(restored.get(1).cur.effort, "high")

    def test_deleted_main_stays_deleted_and_order_survives_restart(self):
        self.scheduler.new_session(self.chat, "alpha")
        self.assertIn("已删除", self.scheduler.delete(self.chat, "main"))
        self.scheduler.new_session(self.chat, "beta")
        restored = Scheduler(
            self.bot, make_settings(Path(self.tmp.name)), state_file=self.scheduler.state_file
        )
        restored_chat = restored.get(1)
        self.assertEqual(list(restored_chat.sessions), ["alpha", "beta"])
        self.assertEqual(restored_chat.current, "beta")

    def test_busy_count_filters_agent(self):
        self.scheduler.new_session(self.chat, "alpha")
        self.chat.sessions["alpha"].agent = "claude"
        self.chat.sessions["alpha"].running_task = SimpleNamespace(done=lambda: False)
        self.chat.sessions["main"].running_task = SimpleNamespace(done=lambda: False)
        self.assertEqual(self.scheduler.busy_count("claude"), 1)
        self.assertEqual(self.scheduler.busy_count("codex"), 1)
        self.assertEqual(self.scheduler.busy_count(), 2)

    async def test_shutdown_cancels_workers_and_clears_queues(self):
        async def wait_forever():
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        self.chat.cur.worker_task = task
        self.chat.cur.running_task = task
        await self.chat.cur.queue.put("queued")
        await self.scheduler.shutdown()
        self.assertTrue(task.cancelled())
        self.assertTrue(self.chat.cur.queue.empty())
        self.assertIsNone(self.chat.cur.worker_task)
        self.assertIsNone(self.chat.cur.running_task)


class BuildCommandOverrideTests(unittest.TestCase):
    def setUp(self):
        self.s = make_settings(Path("."))

    def test_codex_model_and_effort_override(self):
        argv, cwd, env = runner.build_command(
            "codex", "hi", ".", None, self.s, model="gpt-5.6-terra", effort="high"
        )
        self.assertIn("gpt-5.6-terra", argv)
        self.assertIn('model_reasoning_effort="high"', argv)

    def test_codex_resume_gets_overrides_too(self):
        argv, _, _ = runner.build_command(
            "codex", "hi", ".", "sess-1", self.s, model="m1", effort="low"
        )
        self.assertIn("resume", argv)
        self.assertIn("m1", argv)
        self.assertIn('model_reasoning_effort="low"', argv)

    def test_claude_effort_uses_cli_flag(self):
        argv, _, env = runner.build_command("claude", "hi", ".", None, self.s, effort="max")
        i = argv.index("--effort")
        self.assertEqual(argv[i + 1], "max")

    def test_no_override_keeps_env_model(self):
        self.s = Settings(**{**self.s.__dict__, "codex_model": "env-model"})
        argv, _, _ = runner.build_command("codex", "hi", ".", None, self.s)
        self.assertIn("env-model", argv)


class CcSwitchFixtureTests(unittest.TestCase):
    """用临时目录模拟 cc-switch db + CLI 配置，验证切换写入语义。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old = (
            ccswitch.DB_PATH, ccswitch.SETTINGS_PATH, ccswitch.CLAUDE_SETTINGS,
            ccswitch.CODEX_DIR, ccswitch.CODEX_AUTH, ccswitch.CODEX_CONFIG,
            ccswitch.CODEX_CATALOG, ccswitch.BACKUP_ROOT, restart.ROOT,
        )
        ccswitch.DB_PATH = root / "cc-switch.db"
        ccswitch.SETTINGS_PATH = root / "cc-switch-settings.json"
        ccswitch.CLAUDE_SETTINGS = root / "claude" / "settings.json"
        ccswitch.CODEX_DIR = root / "codex"
        ccswitch.CODEX_AUTH = ccswitch.CODEX_DIR / "auth.json"
        ccswitch.CODEX_CONFIG = ccswitch.CODEX_DIR / "config.toml"
        ccswitch.CODEX_CATALOG = ccswitch.CODEX_DIR / "cc-switch-model-catalog.json"
        ccswitch.BACKUP_ROOT = root / "backups"

        con = sqlite3.connect(ccswitch.DB_PATH)
        con.executescript(
            """
            create table providers (
                id TEXT, app_type TEXT, name TEXT, settings_config TEXT,
                category TEXT, is_current INTEGER DEFAULT 0, meta TEXT DEFAULT '{}',
                sort_index INTEGER
            );
            create table settings (key TEXT PRIMARY KEY, value TEXT);
            create table proxy_live_backup (app_type TEXT PRIMARY KEY, original_config TEXT, backed_up_at TEXT);
            """
        )
        con.execute("insert into settings values ('common_config_claude', ?)",
                    (json.dumps({"env": {"CLAUDE_CODE_EFFORT_LEVEL": "max"}, "theme": "dark"}),))
        con.execute("insert into settings values ('common_config_codex', ?)",
                    ('model_reasoning_effort = "medium"\n',))
        con.execute(
            "insert into providers values ('p1','claude','供应商A','{\"env\":{\"ANTHROPIC_BASE_URL\":\"https://a\",\"ANTHROPIC_AUTH_TOKEN\":\"test-anthropic-key\"}}',NULL,1,'{\"commonConfigEnabled\":true}',NULL)")
        con.execute(
            "insert into providers values ('p2','codex','供应商B',?,NULL,0,'{\"commonConfigEnabled\":true}',NULL)",
            (json.dumps({"auth": {"OPENAI_API_KEY": "test-openai-key"},
                         "config": 'model_provider = "custom"\nmodel = "gpt-5.6-terra"\n'}),))
        con.commit()
        con.close()
        ccswitch.CLAUDE_SETTINGS.parent.mkdir(parents=True)
        ccswitch.CLAUDE_SETTINGS.write_text("{}", "utf-8")

    def tearDown(self):
        (ccswitch.DB_PATH, ccswitch.SETTINGS_PATH, ccswitch.CLAUDE_SETTINGS,
         ccswitch.CODEX_DIR, ccswitch.CODEX_AUTH, ccswitch.CODEX_CONFIG,
         ccswitch.CODEX_CATALOG, ccswitch.BACKUP_ROOT, restart.ROOT) = self.old
        self.tmp.cleanup()

    def test_list_providers_claude(self):
        providers = ccswitch.list_providers("claude")
        self.assertEqual(providers[0]["name"], "供应商A")
        self.assertEqual(providers[0]["base_url"], "https://a")
        self.assertTrue(providers[0]["is_current"])

    def test_switch_claude_merges_common_config(self):
        reply = ccswitch.switch_provider("claude", 1)
        self.assertIn("已切换", reply)
        written = json.loads(ccswitch.CLAUDE_SETTINGS.read_text("utf-8"))
        self.assertEqual(written["env"]["ANTHROPIC_BASE_URL"], "https://a")
        self.assertEqual(written["env"]["CLAUDE_CODE_EFFORT_LEVEL"], "max")  # 通用配置覆盖
        self.assertEqual(written["theme"], "dark")

    def test_switch_codex_writes_auth_and_merged_toml(self):
        reply = ccswitch.switch_provider("codex", 1)
        self.assertIn("已切换", reply)
        auth = json.loads(ccswitch.CODEX_AUTH.read_text("utf-8"))
        self.assertEqual(auth["OPENAI_API_KEY"], "test-openai-key")
        toml_text = ccswitch.CODEX_CONFIG.read_text("utf-8")
        self.assertIn('model_provider = "custom"', toml_text)
        self.assertIn('model_reasoning_effort = "medium"', toml_text)  # 通用配置合并进来
        # 备份目录已建（源文件不存在时为空目录也行）
        self.assertTrue(any(ccswitch.BACKUP_ROOT.iterdir()))

    def test_switch_blocked_when_proxy_takeover(self):
        con = sqlite3.connect(ccswitch.DB_PATH)
        con.execute("insert into proxy_live_backup values ('claude','x','now')")
        con.commit()
        con.close()
        reply = ccswitch.switch_provider("claude", 1)
        self.assertIn("代理接管", reply)

    def test_sync_current_marker(self):
        ccswitch.SETTINGS_PATH.write_text(json.dumps({"language": "zh"}), "utf-8")
        ccswitch.switch_provider("claude", 1)
        data = json.loads(ccswitch.SETTINGS_PATH.read_text("utf-8"))
        self.assertEqual(data["currentProviderClaude"], "p1")
        self.assertEqual(data["language"], "zh")

    def test_official_login_is_listed_but_refused(self):
        con = sqlite3.connect(ccswitch.DB_PATH)
        con.execute(
            "insert into providers values ('official','codex','OpenAI Official',?,"
            "'official',0,'{}',NULL)",
            (json.dumps({"auth": {"auth_mode": "chatgpt"}, "config": "model = 'x'\n"}),),
        )
        con.commit()
        con.close()
        text = ccswitch.render_list("codex")
        self.assertIn("需在 GUI 中切换", text)
        self.assertIn("请在 cc-switch GUI 中切换", ccswitch.preflight_switch("codex", 2))


if __name__ == "__main__":
    unittest.main()
