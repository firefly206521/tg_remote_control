"""多会话调度：每个聊天可建多个命名会话，独立上下文与工作目录，可并行运行。

每个会话：
- 各自记住 codex / claude 的上下文（会话 ID 持久化在 state.json）
- 各自的工作目录（/cd 只影响当前会话）
- 各自的串行队列；不同会话之间并行执行，互不阻塞
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile, Message

from config import Settings
from history import HistoryStore
from runner import AGENTS, ProgressUpdate, TurnResult, run_turn

STATE_FILE = Path(__file__).resolve().parent / "state.json"
DEFAULT_SESSION = "main"


@dataclass
class SessionState:
    name: str
    chat_id: int
    workdir: str
    agent: str = "codex"
    # 会话级覆盖：空串 = 跟随 .env / CLI 默认
    model: str = ""
    effort: str = ""
    contexts: dict[str, Optional[str]] = field(default_factory=lambda: {a: None for a in AGENTS})
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker_task: Optional[asyncio.Task] = None
    running_task: Optional[asyncio.Task] = None
    status_message: Optional[Message] = None
    stream_message: Optional[Message] = None
    stream_text: str = ""
    stream_committed: int = 0
    stream_visible: int = 0
    stream_last_rendered: str = ""
    streamed_agent_texts: list[str] = field(default_factory=list)
    activity: str = ""
    started_at: float = 0.0
    key: str = field(default_factory=lambda: uuid.uuid4().hex[:16])


class ChatState:
    def __init__(self, chat_id: int, workdir: str, default_agent: str = "codex") -> None:
        self.chat_id = chat_id
        self.current = DEFAULT_SESSION
        self.sessions: dict[str, SessionState] = {
            DEFAULT_SESSION: SessionState(DEFAULT_SESSION, chat_id, workdir, agent=default_agent)
        }

    @property
    def cur(self) -> SessionState:
        return self.sessions[self.current]


def split_state_by_agent(source: Path, target: Path, agent: str) -> int:
    """首次启用独立 Bot 时，把该 agent 的会话从旧状态移到独立状态文件。"""
    if target.exists() or not source.exists():
        return 0
    try:
        data = json.loads(source.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    remaining: dict = {}
    extracted: dict = {}
    moved = 0
    for chat_id, raw_chat in data.items():
        sessions = raw_chat.get("sessions") or {}
        selected = {name: sd for name, sd in sessions.items() if sd.get("agent") == agent}
        kept = {name: sd for name, sd in sessions.items() if name not in selected}
        if selected:
            moved += len(selected)
            current = raw_chat.get("current")
            extracted[chat_id] = {
                "current": current if current in selected else next(iter(selected)),
                "sessions": selected,
            }
        if kept:
            current = raw_chat.get("current")
            remaining[chat_id] = {
                "current": current if current in kept else next(iter(kept)),
                "sessions": kept,
            }
    if not moved:
        return 0
    backup = source.with_name(source.name + ".pre-dual-bot.bak")
    if not backup.exists():
        shutil.copy2(source, backup)
    target.write_text(json.dumps(extracted, ensure_ascii=False, indent=2), "utf-8")
    source.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), "utf-8")
    return moved


class Scheduler:
    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        history: HistoryStore | None = None,
        *,
        state_file: Path | None = None,
        fixed_agent: str | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.history = history
        self.state_file = state_file or STATE_FILE
        self.fixed_agent = fixed_agent
        self.chats: dict[int, ChatState] = {}
        self._load_state()

    # ---------- 持久化：重启后会话不丢（含旧版单会话格式自动迁移） ----------

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        for cid, s in data.items():
            chat = ChatState(int(cid), self.settings.default_workdir, self.fixed_agent or "codex")
            sessions_data = s.get("sessions") or {}
            if sessions_data and all(isinstance(v, dict) for v in sessions_data.values()):
                # 新格式：{"current": ..., "sessions": {name: {agent, workdir, contexts}}}
                # 以文件中的插入顺序为准；不能保留 ChatState 临时创建的 main，
                # 否则用户删除 main 后重启会被意外复活，编号也会变化。
                chat.sessions = {}
                for name, sd in sessions_data.items():
                    ss = SessionState(
                        name, chat.chat_id, sd.get("workdir") or self.settings.default_workdir
                    )
                    ss.key = sd.get("key") or uuid.uuid4().hex[:16]
                    if self.fixed_agent:
                        ss.agent = self.fixed_agent
                    elif sd.get("agent") in AGENTS:
                        ss.agent = sd["agent"]
                    ss.model = sd.get("model") or ""
                    ss.effort = sd.get("effort") or ""
                    for a in AGENTS:
                        ss.contexts[a] = sd.get("contexts", {}).get(a)
                    chat.sessions[name] = ss
                chat.current = (
                    s["current"] if s.get("current") in chat.sessions else next(iter(chat.sessions))
                )
            else:
                # 旧格式（单会话）：整体迁移为名为 main 的会话
                ss = chat.sessions[DEFAULT_SESSION]
                ss.workdir = s.get("workdir") or self.settings.default_workdir
                if self.fixed_agent:
                    ss.agent = self.fixed_agent
                elif s.get("agent") in AGENTS:
                    ss.agent = s["agent"]
                for a in AGENTS:
                    ss.contexts[a] = sessions_data.get(a)
            self.chats[chat.chat_id] = chat
        self._save_state()  # 旧格式迁移后立即落盘为新格式

    def _save_state(self) -> None:
        data = {
            str(cid): {
                "current": chat.current,
                "sessions": {
                    name: {
                        "key": ss.key, "agent": ss.agent, "workdir": ss.workdir,
                        "model": ss.model, "effort": ss.effort, "contexts": ss.contexts,
                    }
                    for name, ss in chat.sessions.items()
                },
            }
            for cid, chat in self.chats.items()
        }
        with contextlib.suppress(OSError):
            self.state_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def get(self, chat_id: int) -> ChatState:
        chat = self.chats.get(chat_id)
        if chat is None:
            chat = ChatState(chat_id, self.settings.default_workdir, self.fixed_agent or "codex")
            self.chats[chat_id] = chat
            self._save_state()
        return chat

    # ---------- 会话管理 ----------

    def busy(self, ss: SessionState) -> bool:
        return ss.running_task is not None and not ss.running_task.done()

    def find_session(self, chat: ChatState, key: str) -> SessionState | None:
        return next((ss for ss in chat.sessions.values() if ss.key == key), None)

    def create_session(self, chat: ChatState, name: str, agent: str, workdir: str) -> tuple[SessionState | None, str]:
        if self.fixed_agent and agent != self.fixed_agent:
            return None, f"此 Bot 固定使用 {self.fixed_agent}"
        if name in chat.sessions:
            return None, f"会话「{name}」已存在"
        if len(chat.sessions) >= self.settings.max_sessions:
            return None, f"会话数已达上限（{self.settings.max_sessions}）"
        ss = SessionState(name, chat.chat_id, workdir, agent=self.fixed_agent or agent)
        chat.sessions[name] = ss
        self._save_state()
        return ss, ""

    def snapshot(self, chat: ChatState) -> list[dict]:
        now = time.time()
        result = []
        for ss in chat.sessions.values():
            if self.busy(ss):
                status = "running"
            elif not ss.queue.empty():
                status = "queued"
            else:
                status = "idle"
            result.append({
                "id": ss.key,
                "name": ss.name,
                "agent": ss.agent,
                "workdir": ss.workdir,
                "status": status,
                "queueSize": ss.queue.qsize(),
                "activity": ss.activity,
                "startedAt": ss.started_at if self.busy(ss) else None,
                "elapsed": int(now - ss.started_at) if self.busy(ss) else 0,
            })
        return result

    def new_session(self, chat: ChatState, name: Optional[str]) -> str:
        if name:
            if name in chat.sessions:
                return f"会话「{name}」已存在，用 /sw {name} 切换即可"
            if len(chat.sessions) >= self.settings.max_sessions:
                return f"会话数已达上限（{self.settings.max_sessions}），请先 /del 清理不用的会话"
            ss = SessionState(name, chat.chat_id, chat.cur.workdir, agent=self.fixed_agent or chat.cur.agent)
            chat.sessions[name] = ss
            chat.current = name
            self._save_state()
            return (
                f"🆕 已创建并切换到会话「{name}」（全新上下文）\n"
                f"📁 工作目录继承自当前会话：{ss.workdir}\n"
                f"可先 /cd 换到项目目录，然后发消息开工"
            )
        cur = chat.cur
        cur.contexts = {a: None for a in AGENTS}
        self._save_state()
        return f"🆕 会话「{cur.name}」已清空，下一条消息开始全新上下文（工作目录不变）"

    def switch(self, chat: ChatState, name: str) -> str:
        """切换当前会话；名字不存在时返回空串（由调用方展示列表）。"""
        if name not in chat.sessions:
            return ""
        if name == chat.current:
            return f"已经在会话「{name}」里了"
        chat.current = name
        self._save_state()
        ss = chat.cur
        extra = "（🟢 该会话有任务正在运行，结果稍后回传）" if self.busy(ss) else ""
        return f"🔁 已切换到会话「{name}」｜{ss.agent}｜📁 {ss.workdir}{extra}"

    def switch_by_index(self, chat: ChatState, index: int) -> str:
        """按编号切换（编号 = render_list 展示顺序）；无效编号返回空串。"""
        names = list(chat.sessions)
        if not 1 <= index <= len(names):
            return ""
        return self.switch(chat, names[index - 1])

    def delete(self, chat: ChatState, name: str) -> str:
        if name not in chat.sessions:
            return f"没有叫「{name}」的会话，/sw 查看列表"
        if name == chat.current:
            return f"「{name}」是当前会话，先 /sw 切到别的会话再删除"
        ss = chat.sessions[name]
        if self.busy(ss) or not ss.queue.empty():
            return f"会话「{name}」还有任务在跑或排队，处理完再删（或 /sw {name} 后 /stop）"
        del chat.sessions[name]
        self._save_state()
        return f"🗑 已删除会话「{name}」"

    def render_list(self, chat: ChatState) -> str:
        lines = [f"📋 会话列表（{len(chat.sessions)}/{self.settings.max_sessions}）"]
        for idx, (name, ss) in enumerate(chat.sessions.items(), 1):
            mark = "👉" if name == chat.current else "  "
            if self.busy(ss):
                state = f"🟢 运行中（{self._fmt(time.time() - ss.started_at)}）"
            elif not ss.queue.empty():
                state = f"⏳ 队列 {ss.queue.qsize()} 个"
            else:
                state = "💤 空闲"
            lines.append(f"{mark}{idx}. {name} — {ss.agent}｜📁 {ss.workdir}｜{state}")
        return "\n".join(lines)

    # ---------- 对外操作 ----------

    async def submit(self, chat_id: int, prompt: str) -> str:
        """把一条消息排入当前会话的队列；返回需要回给用户的提示（空串表示无需提示）。"""
        chat = self.get(chat_id)
        return await self.submit_to(chat, chat.cur, prompt)

    async def submit_to(self, chat: ChatState, ss: SessionState, prompt: str) -> str:
        """Submit to an explicit session so web task selection cannot affect Telegram."""
        if self.busy(ss) or not ss.queue.empty():
            if ss.queue.qsize() >= self.settings.queue_limit:
                return (
                    f"会话「{ss.name}」队列已满（上限 {self.settings.queue_limit}），"
                    f"可发 /stop 清空，或 /sw 到其他会话继续干活"
                )
            if self.history:
                self.history.append(ss.chat_id, ss.key, "user", prompt, "queued")
            await ss.queue.put(prompt)
            return f"📥 已加入会话「{ss.name}」队列（前面还有 {ss.queue.qsize()} 个任务）"
        self._ensure_worker(ss)
        if self.history:
            self.history.append(ss.chat_id, ss.key, "user", prompt)
        await ss.queue.put(prompt)
        return ""

    async def stop_current(self, chat: ChatState) -> str:
        ss = chat.cur
        if not self.busy(ss) and ss.queue.empty():
            return f"会话「{ss.name}」当前没有正在运行或排队的任务"
        await self.stop_current_for(chat, ss)
        return f"🛑 会话「{ss.name}」：已终止任务并清空队列"

    async def stop_all(self, chat: ChatState) -> str:
        touched = []
        for name, ss in chat.sessions.items():
            if self.busy(ss) or not ss.queue.empty():
                await self.stop_current_for(chat, ss)
                touched.append(name)
        if not touched:
            return "所有会话都空闲"
        return "🛑 已停止会话：" + "、".join(touched)

    async def stop_everything(self, agent: str | None = None) -> int:
        """终止会话任务（切换 API 重启前调用）；指定 agent 时只停该 agent 的会话。"""
        touched = 0
        for chat in self.chats.values():
            for ss in chat.sessions.values():
                if agent and ss.agent != agent:
                    continue
                if self.busy(ss) or not ss.queue.empty():
                    await self.stop_current_for(chat, ss)
                    touched += 1
        return touched

    async def shutdown(self) -> None:
        """关闭调度器：取消所有 worker，确保其正在等待的 AI 子进程收到取消。"""
        tasks: set[asyncio.Task] = set()
        sessions = [ss for chat in self.chats.values() for ss in chat.sessions.values()]
        for ss in sessions:
            for task in (ss.running_task, ss.worker_task):
                if task is not None and not task.done():
                    tasks.add(task)
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=30
                )
        for ss in sessions:
            while not ss.queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    ss.queue.get_nowait()
                    ss.queue.task_done()
            ss.running_task = None
            ss.worker_task = None

    def busy_count(self, agent: str | None = None) -> int:
        """运行中或排队的会话数；指定 agent 时只统计该 agent。"""
        return sum(
            1
            for chat in self.chats.values()
            for ss in chat.sessions.values()
            if (not agent or ss.agent == agent) and (self.busy(ss) or not ss.queue.empty())
        )

    async def stop_current_for(self, chat: ChatState, ss: SessionState) -> None:
        had_work = self.busy(ss) or not ss.queue.empty()
        if self.busy(ss):
            assert ss.running_task is not None
            ss.running_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.gather(ss.running_task, return_exceptions=True), timeout=15)
        while not ss.queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                ss.queue.get_nowait()
                ss.queue.task_done()
        if had_work and self.history:
            self.history.append(ss.chat_id, ss.key, "system", "任务已停止，等待队列已清空。", "stopped")

    def set_agent(self, chat: ChatState, name: str) -> str:
        if self.fixed_agent:
            return f"此 Bot 固定使用 {self.fixed_agent}，不能切换到其他 AI"
        ss = chat.cur
        ss.agent = name
        self._save_state()
        sid = ss.contexts.get(name)
        extra = f"，继续已有会话 {sid[:8]}…" if sid else "，下一条消息开始新会话"
        return f"🤖 会话「{ss.name}」的 AI 已切换为 {name}{extra}"

    def set_model(self, chat: ChatState, model: str) -> str:
        ss = chat.cur
        ss.model = model
        self._save_state()
        label = f"🧠 会话「{ss.name}」模型已设为 {model}" if model else f"🧠 会话「{ss.name}」模型已恢复默认（跟随 .env / CLI）"
        return label + "，下一条消息生效"

    def set_effort(self, chat: ChatState, effort: str) -> str:
        ss = chat.cur
        ss.effort = effort
        self._save_state()
        label = f"🧠 会话「{ss.name}」思考强度已设为 {effort}" if effort else f"🧠 会话「{ss.name}」思考强度已恢复默认"
        return label + "，下一条消息生效"

    def set_workdir(self, chat: ChatState, path: str) -> str:
        ss = chat.cur
        ss.workdir = path
        # codex 的工作目录在会话创建时固定，换目录后只能开新会话
        ss.contexts["codex"] = None
        self._save_state()
        return (
            f"📁 会话「{ss.name}」工作目录已设为 {path}\n"
            f"（codex 将从下一条消息开始新会话；claude 继续原会话）"
        )

    # ---------- 内部执行 ----------

    def _ensure_worker(self, ss: SessionState) -> None:
        if ss.worker_task is None or ss.worker_task.done():
            ss.worker_task = asyncio.create_task(self._worker(ss))

    async def _worker(self, ss: SessionState) -> None:
        while True:
            prompt = await ss.queue.get()
            try:
                await self._run_one(ss, prompt)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 保护 worker 不因单次错误退出
                if self.history:
                    self.history.append(ss.chat_id, ss.key, "system", f"调度器内部错误：{e!r}", "failed")
                await self._safe_send(ss.chat_id, f"❌ 调度器内部错误：{e!r}")
            finally:
                ss.queue.task_done()

    async def _run_one(self, ss: SessionState, prompt: str) -> None:
        ss.started_at = time.time()
        ss.activity = ""
        ss.stream_message = None
        ss.stream_text = ""
        ss.stream_committed = 0
        ss.stream_visible = 0
        ss.stream_last_rendered = ""
        ss.streamed_agent_texts = []
        overrides = "｜".join(
            f"{label} {value}" for label, value in (("🧠", ss.model), ("🎚", ss.effort)) if value
        )
        ss.status_message = await self._safe_send(
            ss.chat_id,
            f"🚀 [{ss.name}] {ss.agent} 开始处理{'｜' + overrides if overrides else ''}\n📁 {ss.workdir}\n"
            f"✏️ {prompt[:300]}{'…' if len(prompt) > 300 else ''}",
        )

        async def on_progress(update: ProgressUpdate) -> None:
            text = update.text.strip()
            if not text:
                return
            ss.activity = text if update.kind == "activity" else "✍️ 正在输出回复"
            ss.stream_text += ("\n\n" if ss.stream_text else "") + text
            if update.kind == "text":
                ss.streamed_agent_texts.append(text)
            if self.history:
                role = "assistant" if update.kind == "text" else "system"
                with contextlib.suppress(Exception):
                    self.history.append(ss.chat_id, ss.key, role, text, "progress")

        task = asyncio.current_task()
        assert task is not None
        ss.running_task = task
        edit_task = asyncio.create_task(self._edit_status_loop(ss))
        session_id = ss.contexts.get(ss.agent)

        try:
            result = await self._run_with_auto_compact(
                ss, prompt, session_id, on_progress,
            )
        except asyncio.CancelledError:
            ss.running_task = None
            edit_task.cancel()
            await self._flush_stream(ss)
            await self._finish_status(ss, f"🛑 [{ss.name}] 已停止")
            raise
        ss.running_task = None
        edit_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await edit_task

        elapsed = time.time() - ss.started_at
        await self._flush_stream(ss)
        if result.session_id and result.session_id != session_id:
            ss.contexts[ss.agent] = result.session_id
            self._save_state()

        mark = "✅" if result.ok else "❌"
        await self._finish_status(ss, f"{mark} [{ss.name}] {ss.agent} 结束，用时 {self._fmt(elapsed)}")

        if result.ok:
            streamed_text = "\n\n".join(ss.streamed_agent_texts).strip()
            history_has_final = bool(result.text) and streamed_text == result.text.strip()
            if self.history:
                if result.text and not history_has_final:
                    self.history.append(ss.chat_id, ss.key, "assistant", result.text)
                self.history.append(ss.chat_id, ss.key, "system", f"{ss.agent} 已完成，用时 {self._fmt(elapsed)}", "completed")
            telegram_has_final = history_has_final and ss.stream_visible >= len(ss.stream_text)
            if result.text and not telegram_has_final:
                await self._deliver(ss, result.text)
        else:
            if self.history:
                streamed_text = "\n\n".join(ss.streamed_agent_texts).strip()
                if result.text and streamed_text != result.text.strip():
                    self.history.append(ss.chat_id, ss.key, "assistant", result.text, "progress")
                self.history.append(ss.chat_id, ss.key, "system", result.error or "未知错误", "failed")
            await self._safe_send(ss.chat_id, f"❌ [{ss.name}] {ss.agent} 执行失败：\n{result.error or '未知错误'}")

    async def _run_with_auto_compact(
        self,
        ss: SessionState,
        prompt: str,
        session_id: str | None,
        on_progress,
    ):
        """按配置分段运行 Claude；每个工作段超时后压缩同一会话并发送“继续”。"""
        compact_seconds = max(
            0, int(getattr(self.settings, "claude_auto_compact_seconds", 0) or 0)
        )
        compact_retries = max(
            0, int(getattr(self.settings, "claude_auto_compact_retries", 1) or 0)
        )
        enabled = ss.agent == "claude" and compact_seconds > 0
        next_prompt = prompt
        compact_count = 0

        while True:
            kwargs = {"model": ss.model, "effort": ss.effort}
            if enabled:
                kwargs["timeout_seconds"] = compact_seconds
            result = await run_turn(
                ss.agent, next_prompt, ss.workdir, session_id, self.settings,
                on_progress, **kwargs,
            )
            if result.session_id and result.session_id != session_id:
                session_id = result.session_id
                ss.contexts[ss.agent] = session_id
                self._save_state()

            if not (enabled and result.timed_out):
                return result
            if not session_id:
                result.error = "自动压缩触发，但 Claude 尚未返回会话 ID，无法安全恢复。"
                return result

            compact_count += 1
            ss.activity = f"🗜️ 第 {compact_count} 次自动压缩上下文"
            notice = (
                f"🗜️ [{ss.name}] Claude 已连续运行 {self._fmt(compact_seconds)}，"
                "正在中断当前工作段、压缩上下文并自动继续…"
            )
            if self.history:
                self.history.append(ss.chat_id, ss.key, "system", notice, "auto_compact")
            await self._safe_send(ss.chat_id, notice)

            compact_result = None
            for attempt in range(compact_retries + 1):
                compact_result = await run_turn(
                    "claude",
                    "/compact focus on preserving the active task, completed changes, test evidence, unresolved work, and the exact next action",
                    ss.workdir,
                    session_id,
                    self.settings,
                    model=ss.model,
                    effort=ss.effort,
                    timeout_seconds=compact_seconds,
                    accept_exit_zero=True,
                )
                if compact_result.session_id and compact_result.session_id != session_id:
                    session_id = compact_result.session_id
                    ss.contexts[ss.agent] = session_id
                    self._save_state()
                if compact_result.ok:
                    break
                if attempt < compact_retries:
                    retry_notice = (
                        f"⚠️ [{ss.name}] 第 {attempt + 1} 次压缩未成功，"
                        f"正在重试（{attempt + 2}/{compact_retries + 1}）…"
                    )
                    ss.activity = retry_notice
                    if self.history:
                        self.history.append(
                            ss.chat_id, ss.key, "system", retry_notice, "auto_compact"
                        )
                    await self._safe_send(ss.chat_id, retry_notice)
            assert compact_result is not None
            if not compact_result.ok:
                reason = compact_result.error or "未知错误"
                if compact_result.timed_out:
                    reason = f"压缩操作在 {self._fmt(compact_seconds)} 内未完成"
                return TurnResult(
                    False, session_id, result.text,
                    f"自动压缩失败：{reason}",
                )

            ss.activity = f"▶️ 第 {compact_count} 次压缩完成，已发送继续"
            if self.history:
                self.history.append(
                    ss.chat_id, ss.key, "system",
                    f"第 {compact_count} 次自动压缩完成，已发送：继续", "auto_compact",
                )
            next_prompt = "继续"

    async def _edit_status_loop(self, ss: SessionState) -> None:
        last_text = ""
        while True:
            await asyncio.sleep(self.settings.status_edit_interval)
            await self._flush_stream(ss)
            msg = ss.status_message
            if msg is None:
                continue
            lines = [f"⏳ [{ss.name}] {ss.agent} 工作中… 已运行 {self._fmt(time.time() - ss.started_at)}"]
            if ss.activity:
                lines.append(f"最近：{ss.activity}")
            text = "\n".join(lines)
            if text == last_text:
                continue
            last_text = text
            try:
                await msg.edit_text(text)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception:
                pass  # 消息可能被删除等，编辑失败不影响任务

    async def _flush_stream(self, ss: SessionState) -> None:
        """把累计进度限频写入 Telegram；已完成的分片不再覆盖。"""
        prefix = f"📢 [{ss.name}] {ss.agent} · 实时输出\n\n"
        body_limit = 3800
        while ss.stream_committed < len(ss.stream_text):
            segment = ss.stream_text[ss.stream_committed:ss.stream_committed + body_limit]
            rendered = prefix + segment
            if ss.stream_message is None:
                message = await self._safe_send(ss.chat_id, rendered)
                if message is None:
                    return
                ss.stream_message = message
            elif rendered != ss.stream_last_rendered:
                if not await self._safe_edit(ss.stream_message, rendered):
                    return
            ss.stream_last_rendered = rendered
            ss.stream_visible = ss.stream_committed + len(segment)
            if len(segment) < body_limit:
                return
            ss.stream_committed += len(segment)
            ss.stream_message = None
            ss.stream_last_rendered = ""

    @staticmethod
    async def _safe_edit(message: Message, text: str) -> bool:
        try:
            await message.edit_text(text)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await message.edit_text(text)
                return True
            except Exception:
                return False
        except Exception:
            return False

    async def _finish_status(self, ss: SessionState, text: str) -> None:
        msg, ss.status_message = ss.status_message, None
        if msg is None:
            return
        with contextlib.suppress(Exception):
            await msg.edit_text(text)

    async def _deliver(self, ss: SessionState, text: str) -> None:
        labeled = f"📢 [{ss.name}] {ss.agent}\n\n{text}"
        if len(labeled) > self.settings.file_threshold:
            try:
                doc = BufferedInputFile(
                    labeled.encode("utf-8"),
                    filename=f"{ss.name}_{time.strftime('%m%d_%H%M%S')}.md",
                )
                await self.bot.send_document(ss.chat_id, doc, caption=labeled[:600] + "…")
                return
            except Exception:
                pass  # 发文件失败则退回分片发送
        for part in self._chunk(labeled, 3800):
            await self._safe_send(ss.chat_id, part)

    async def _safe_send(self, chat_id: int, text: str) -> Message | None:
        try:
            return await self.bot.send_message(chat_id, text[:4096])
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            with contextlib.suppress(Exception):
                return await self.bot.send_message(chat_id, text[:4096])
        except Exception:
            return None

    @staticmethod
    def _chunk(text: str, size: int) -> list[str]:
        parts: list[str] = []
        while text:
            if len(text) <= size:
                parts.append(text)
                break
            cut = text.rfind("\n", 0, size)
            if cut < size // 2:
                cut = size
            parts.append(text[:cut].rstrip())
            text = text[cut:].lstrip("\n")
        return parts

    @staticmethod
    def _fmt(seconds: float) -> str:
        s = int(seconds)
        return f"{s // 60}分{s % 60:02d}秒" if s >= 60 else f"{s}秒"
