"""配置加载：从 .env 读取机器人、代理与 agent 设置。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    bot_token: str
    claude_bot_token: str
    allowed_user_ids: frozenset[int]
    proxy_url: str
    default_workdir: str
    codex_sandbox: str
    codex_network: bool
    codex_model: str
    claude_permission_mode: str
    claude_model: str
    codex_models: tuple[str, ...]
    claude_models: tuple[str, ...]
    codex_efforts: tuple[str, ...]
    claude_efforts: tuple[str, ...]
    claude_auto_compact_seconds: int
    claude_auto_compact_retries: int
    queue_limit: int
    max_sessions: int
    file_threshold: int
    status_edit_interval: int
    gui_host: str
    gui_port: int
    gui_port_claude: int
    gui_token: str

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")

        token = os.getenv("CODEX_BOT_TOKEN", "").strip() or os.getenv("BOT_TOKEN", "").strip()
        if ":" not in token:
            raise SystemExit(
                "CODEX_BOT_TOKEN/BOT_TOKEN 未配置。请先在 Telegram 里找 @BotFather 用 /newbot 创建机器人，"
                "把拿到的 token 填入 .env 后重新运行。"
            )
        claude_token = os.getenv("CLAUDE_BOT_TOKEN", "").strip()
        if claude_token and ":" not in claude_token:
            raise SystemExit("CLAUDE_BOT_TOKEN 格式不正确，请填写 My_claude 的完整 Bot token。")

        raw_ids = os.getenv("ALLOWED_USER_IDS", "").replace("，", ",")
        try:
            ids = {int(x) for x in raw_ids.split(",") if x.strip()}
        except ValueError:
            raise SystemExit("ALLOWED_USER_IDS 格式不对，应为数字 ID，多个用英文逗号分隔。")
        # 白名单为空不报错：进入配对模式（机器人只回复来访者的 ID，不执行任何任务）

        def _int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except ValueError:
                return default

        workdir = os.getenv("DEFAULT_WORKDIR", "").strip() or str(PROJECT_ROOT / "workspace")

        def _list(name: str, default: str) -> tuple[str, ...]:
            raw = os.getenv(name, default).strip()
            return tuple(x.strip() for x in raw.split(",") if x.strip())

        gui_host = "127.0.0.1"
        gui_port = max(1, min(65535, _int("GUI_PORT", 8765)))
        return cls(
            bot_token=token,
            claude_bot_token=claude_token,
            allowed_user_ids=frozenset(ids),
            proxy_url=os.getenv("PROXY_URL", "").strip(),
            default_workdir=workdir,
            codex_sandbox=os.getenv("CODEX_SANDBOX", "workspace-write").strip(),
            codex_network=os.getenv("CODEX_NETWORK", "true").strip().lower() in ("1", "true", "yes"),
            codex_model=os.getenv("CODEX_MODEL", "").strip(),
            claude_permission_mode=os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions").strip(),
            claude_model=os.getenv("CLAUDE_MODEL", "").strip(),
            codex_models=_list("CODEX_MODELS", "gpt-5.1-codex-max,gpt-5.1-codex,gpt-5.1-codex-mini"),
            claude_models=_list("CLAUDE_MODELS", "opus,sonnet,haiku"),
            # codex 档位对应 -c model_reasoning_effort=...；claude 档位对应 --effort
            codex_efforts=_list("CODEX_EFFORT_LEVELS", "minimal,low,medium,high"),
            claude_efforts=_list("CLAUDE_EFFORT_LEVELS", "low,medium,high,xhigh,max"),
            # 0 表示关闭；启用后只打断 Claude 的工作段，/compact 本身不递归触发。
            claude_auto_compact_seconds=max(0, _int("CLAUDE_AUTO_COMPACT_SECONDS", 0)),
            claude_auto_compact_retries=max(0, _int("CLAUDE_AUTO_COMPACT_RETRIES", 1)),
            queue_limit=_int("QUEUE_LIMIT", 3),
            max_sessions=_int("MAX_SESSIONS", 8),
            file_threshold=_int("FILE_THRESHOLD", 3500),
            status_edit_interval=max(2, _int("STATUS_EDIT_INTERVAL", 4)),
            gui_host=gui_host,
            gui_port=gui_port,
            gui_port_claude=max(1, min(65535, _int("GUI_PORT_CLAUDE", gui_port + 1))),
            gui_token=os.getenv("GUI_TOKEN", "").strip(),
        )
