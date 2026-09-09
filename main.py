"""入口：每个 AI 一个独立进程，互不影响（/api 切换 API 只重启自己）。

用法：
  python main.py           配置了 CLAUDE_BOT_TOKEN 时拉起 codex / claude 两个独立进程后退出；
                           未配置则进入旧单 Bot 兼容模式（本进程内可用 /agent 切换 codex/claude）
  python main.py codex     只运行 My_codex（双 Bot 模式）
  python main.py claude    只运行 My_claude（双 Bot 模式）

每个进程：自己的 Bot + 调度器 + 状态文件（state.json / state.claude.json）。
Codex 的 Web UI（GUI_PORT）是统一入口，通过本机回环代理聚合 Claude 后端
（GUI_PORT_CLAUDE，默认 +1）；两个进程共享 history.sqlite3（WAL 模式）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from config import Settings
from handlers import create_router
from history import HistoryStore
from restart import RESTART_EXIT_CODE
from scheduler import STATE_FILE, Scheduler, split_state_by_agent
from webui import start_web_ui

ROOT = Path(__file__).resolve().parent
HISTORY_FILE = ROOT / "history.sqlite3"
TOKEN_FILE = ROOT / ".gui-token"
CLAUDE_STATE_FILE = ROOT / "state.claude.json"


def _notice_file(agent: str) -> Path:
    # claude 进程的通知单独放，避免和 codex 进程（或旧单 Bot 模式）互相覆盖
    return ROOT / (".restart-notice.claude" if agent == "claude" else ".restart-notice")


def _load_gui_token(configured: str) -> str:
    if configured:
        TOKEN_FILE.write_text(configured, "utf-8")
        return configured
    try:
        saved = TOKEN_FILE.read_text("utf-8").strip()
        if saved:
            return saved
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(token, "utf-8")
    return token


def _make_bot(token: str, settings: Settings) -> Bot:
    if settings.proxy_url:
        return Bot(token=token, session=AiohttpSession(proxy=settings.proxy_url))
    return Bot(token=token)


def _peer_gui_url(settings: Settings, fixed_agent: str | None) -> str:
    """只有 Codex GUI 聚合 Claude；Claude 后端不反向代理，避免形成路由环。"""
    if fixed_agent == "codex" and settings.claude_bot_token:
        return f"http://{settings.gui_host}:{settings.gui_port_claude}"
    return ""


async def _send_restart_notice(bot: Bot, agent: str, settings: Settings) -> None:
    """重启后向白名单用户发一条系统回复（通知文件由 restart.py 在退出前写入）。"""
    notice_file = _notice_file(agent)
    if not notice_file.exists() or not settings.allowed_user_ids:
        return
    notice = notice_file.read_text("utf-8").strip()
    try:
        for chat_id in settings.allowed_user_ids:
            await bot.send_message(chat_id, notice)
    except Exception:
        logging.exception("发送重启通知失败，保留通知文件供下次启动重试")
    else:
        notice_file.unlink(missing_ok=True)


async def run_bot(
    settings: Settings,
    *,
    token: str,
    state_file: Path,
    fixed_agent: str | None,
    gui_port: int,
) -> None:
    agent_label = fixed_agent or "codex(兼容)"
    history = HistoryStore(HISTORY_FILE)
    if fixed_agent == "claude" and not state_file.exists() and STATE_FILE.exists():
        moved = split_state_by_agent(STATE_FILE, CLAUDE_STATE_FILE, "claude")
        if moved:
            logging.info("已把 %s 个 Claude 会话迁移到独立状态文件", moved)

    bot = _make_bot(token, settings)
    scheduler = Scheduler(bot, settings, history, state_file=state_file, fixed_agent=fixed_agent)
    dp = Dispatcher()
    dp.include_router(create_router(scheduler))

    peer_url = _peer_gui_url(settings, fixed_agent)
    gui_runner = await start_web_ui(
        scheduler,
        history,
        settings.gui_host,
        gui_port,
        _load_gui_token(settings.gui_token),
        peer_url=peer_url,
        peer_agent="claude",
    )
    await _send_restart_notice(bot, fixed_agent or "codex", settings)
    gui_label = "统一 GUI" if peer_url else "GUI"
    logging.info(
        "调度台[%s] 已启动，等待消息…（%s: http://%s:%s）",
        agent_label, gui_label, settings.gui_host, gui_port,
    )

    async def poll_forever() -> None:
        while True:
            try:
                await dp.start_polling(bot, allowed_updates=["message"])
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error("Bot 轮询中断：%r，5 秒后重试", e)
                await asyncio.sleep(5)

    try:
        await poll_forever()
    finally:
        await scheduler.shutdown()
        await gui_runner.cleanup()
        history.close()
        await bot.session.close()


async def _stop_supervised_children(processes: list[subprocess.Popen]) -> None:
    """先请求子 Bot 优雅退出，超时后才按已知 PID 回收整个进程树。"""
    live = [proc for proc in processes if proc.poll() is None]
    for proc in live:
        with contextlib.suppress(OSError):
            proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
    if live:
        try:
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.to_thread(proc.wait) for proc in live)), timeout=30
            )
        except asyncio.TimeoutError:
            for proc in live:
                if proc.poll() is None and sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True, timeout=15, check=False,
                    )
                elif proc.poll() is None:
                    with contextlib.suppress(OSError):
                        proc.kill()


async def supervise_dual_bots(settings: Settings | None = None) -> None:
    """在 start.bat 的控制台里常驻监督双 Bot，使一次 Ctrl+C 能关闭两边。"""
    # 子进程几乎同时启动；由父进程预先确定共享 token，避免首次启动时竞态生成两个值。
    if settings is not None:
        _load_gui_token(settings.gui_token)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    processes: dict[str, subprocess.Popen] = {}

    def start_agent(agent: str) -> subprocess.Popen:
        env = {**os.environ, "TG_DISPATCH_SUPERVISED": "1"}
        return subprocess.Popen(
            [sys.executable, str(ROOT / "main.py"), agent],
            cwd=str(ROOT), creationflags=creationflags, env=env,
        )

    try:
        for agent in ("codex", "claude"):
            processes[agent] = start_agent(agent)
        logging.info("双 Bot 已启动；在此窗口按 Ctrl+C 可同时安全退出")
        while True:
            await asyncio.sleep(0.5)
            for agent, proc in tuple(processes.items()):
                code = proc.poll()
                if code is None:
                    continue
                if code == RESTART_EXIT_CODE:
                    logging.info("Bot[%s] 请求重启，正在重新拉起", agent)
                    processes[agent] = start_agent(agent)
                    break
                logging.warning("Bot[%s] 已退出（exit=%s），正在关闭另一边", agent, code)
                return
    finally:
        await _stop_supervised_children(list(processes.values()))


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    settings = Settings.load()
    Path(settings.default_workdir).mkdir(parents=True, exist_ok=True)

    if settings.proxy_url:
        logging.info("使用代理：%s", settings.proxy_url)
    else:
        logging.warning("未配置代理，将直连 api.telegram.org")

    arg = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""
    if arg not in ("", "codex", "claude"):
        raise SystemExit("用法：python main.py [codex|claude]")

    dual = bool(settings.claude_bot_token)
    if dual and not arg:
        # 双 Bot 模式：保留独立进程，但父进程常驻监督，Ctrl+C 能完整回收。
        await supervise_dual_bots(settings)
        return
    if arg == "claude" and not dual:
        raise SystemExit("CLAUDE_BOT_TOKEN 未配置，无法单独运行 My_claude。")

    if dual:
        await run_bot(
            settings,
            token=settings.bot_token if arg != "claude" else settings.claude_bot_token,
            state_file=STATE_FILE if arg != "claude" else CLAUDE_STATE_FILE,
            fixed_agent=arg or "codex",
            gui_port=settings.gui_port if arg != "claude" else settings.gui_port_claude,
        )
    else:
        # 旧单 Bot 兼容模式：一个 Bot 兼跑两种 AI（/agent 可切），保留原行为
        await run_bot(
            settings,
            token=settings.bot_token,
            state_file=STATE_FILE,
            fixed_agent=None,
            gui_port=settings.gui_port,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出")
