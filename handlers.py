"""Telegram 命令与消息处理。"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from aiogram import BaseMiddleware, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

import ccswitch
from restart import RESTART_EXIT_CODE, spawn_replacement, write_restart_notice
from runner import AGENTS
from scheduler import Scheduler

log = logging.getLogger("dispatch")

HELP = """🤖 Telegram → 本机 AI 调度台（多会话版）

直接发消息 = 交给当前会话的 AI 处理（上下文连续）。
每个会话有独立的记忆和工作目录，可随时来回切换；
不同会话的任务能同时运行，回传的消息都标明来自哪个会话。

/sw            列出所有会话（👉=当前，🟢=运行中）
/sw 编号|名称  切换会话，如 /sw 1（正在跑的任务不受影响）
/new 名称      新建会话并切换（/new 不带名字 = 清空当前会话上下文）
/del 编号|名称 删除会话
/stop          终止当前会话的任务并清空其队列
/stopall       终止所有会话的任务
/agent         查看/切换 AI：/agent codex 或 /agent claude
/model         查看/切换模型：/model 2（/model 0 = 恢复默认）
/think         查看/切换思考强度：/think 3（/think 0 = 恢复默认）
/api           查看 cc-switch 供应商：/api 2 = 切换 API 并重启调度台
/cd            切换当前会话工作目录：/cd C:\\projects\\某项目
/status        当前状态详情"""


def _help_for(fixed_agent: str | None) -> str:
    if not fixed_agent:
        return HELP
    return HELP.replace(
        "/agent         查看/切换 AI：/agent codex 或 /agent claude",
        f"/agent         当前 Bot 固定使用 {fixed_agent}",
    )


class Gate(BaseMiddleware):
    """白名单拦截：白名单内放行；白名单为空时进入配对模式（只告知 ID，不执行任何任务）。"""

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler

    async def __call__(self, handler, event: Message, data):
        user = data.get("event_from_user")
        uid = user.id if user else None
        if uid is None:
            return None
        allowed = self.scheduler.settings.allowed_user_ids
        if uid in allowed:
            return await handler(event, data)
        if not allowed and event.chat.type == ChatType.PRIVATE:
            log.warning("配对模式：收到用户 %s 的消息（请把该 ID 填入 .env 的 ALLOWED_USER_IDS）", uid)
            await event.answer(
                "🔐 调度台还没配置白名单，暂时不能执行任务。\n\n"
                f"你的 Telegram ID 是：{uid}\n"
                "把它填入 .env 的 ALLOWED_USER_IDS= 后重启调度台即可。"
            )
        return None  # 其余情况一律静默忽略


def _valid_name(name: str) -> bool:
    return 0 < len(name) <= 20 and not any(c.isspace() for c in name) and "/" not in name and "\\" not in name


def _arg_of(m: Message) -> str:
    parts = (m.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _resolve_session_arg(arg: str, chat) -> str | None:
    """/sw、/del 的参数：纯数字按编号（render_list 展示顺序），否则按名称。"""
    if arg.isdigit():
        names = list(chat.sessions)
        return names[int(arg) - 1] if 1 <= int(arg) <= len(names) else None
    return arg if arg in chat.sessions else None


def create_router(scheduler: Scheduler) -> Router:
    fixed_agent = scheduler.fixed_agent
    help_text = _help_for(fixed_agent)
    router = Router(name=f"dispatch-{fixed_agent or 'combined'}")
    router.message.outer_middleware(Gate(scheduler))

    def session_detail(chat) -> str:
        ss = chat.cur
        lines = [
            f"👉 会话「{ss.name}」｜AI：{ss.agent}",
            f"🧠 模型：{ss.model or '默认'}｜思考强度：{ss.effort or '默认'}",
            f"🧵 " + " | ".join(
                f"{a}: {(ss.contexts.get(a) or '无')[:13]}…"
                for a in ((fixed_agent,) if fixed_agent else AGENTS)
            ),
            f"📁 工作目录：{ss.workdir}",
        ]
        if scheduler.busy(ss):
            lines.append(f"⚡ 状态：任务运行中（{Scheduler._fmt(time.time() - ss.started_at)}）")
            if ss.activity:
                lines.append(f"   最近：{ss.activity}")
        elif not ss.queue.empty():
            lines.append(f"⚡ 状态：队列中 {ss.queue.qsize()} 个任务")
        else:
            lines.append("⚡ 状态：空闲")
        return "\n".join(lines)

    @router.message(Command("start"))
    async def cmd_start(m: Message):
        await m.answer("已连接本机调度台 ✅\n\n" + help_text + "\n\n" + scheduler.render_list(scheduler.get(m.chat.id)))

    @router.message(Command("help"))
    async def cmd_help(m: Message):
        await m.answer(help_text)

    @router.message(Command("sw"))
    async def cmd_sw(m: Message):
        chat = scheduler.get(m.chat.id)
        arg = _arg_of(m)
        if not arg:
            await m.answer(scheduler.render_list(chat))
            return
        reply = (
            scheduler.switch_by_index(chat, int(arg))
            if arg.isdigit()
            else scheduler.switch(chat, arg)
        )
        await m.answer(reply if reply else scheduler.render_list(chat) + "\n\n💡 没找到，编号或名字输一个即可；/new 新建")

    @router.message(Command("new"))
    async def cmd_new(m: Message):
        chat = scheduler.get(m.chat.id)
        parts = (m.text or "").split(maxsplit=1)
        name = parts[1].strip() if len(parts) > 1 else None
        if name and not _valid_name(name):
            await m.answer("名称限 20 字以内，不能含空格和斜杠")
            return
        await m.answer(scheduler.new_session(chat, name))

    @router.message(Command("del"))
    async def cmd_del(m: Message):
        chat = scheduler.get(m.chat.id)
        arg = _arg_of(m)
        if not arg:
            await m.answer(scheduler.render_list(chat) + "\n\n用法：/del 编号或名称")
            return
        name = _resolve_session_arg(arg, chat)
        if not name:
            await m.answer(f"没有叫「{arg}」的会话，/sw 查看编号列表")
            return
        await m.answer(scheduler.delete(chat, name))

    @router.message(Command("stop"))
    async def cmd_stop(m: Message):
        await m.answer(await scheduler.stop_current(scheduler.get(m.chat.id)))

    @router.message(Command("stopall"))
    async def cmd_stopall(m: Message):
        await m.answer(await scheduler.stop_all(scheduler.get(m.chat.id)))

    @router.message(Command("agent"))
    async def cmd_agent(m: Message):
        chat = scheduler.get(m.chat.id)
        ss = chat.cur
        if fixed_agent:
            await m.answer(f"此 Bot 固定使用 {fixed_agent}；请到另一个 Bot 使用另一种 AI。")
            return
        parts = (m.text or "").split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        if not arg:
            await m.answer(f"会话「{ss.name}」当前 AI：{ss.agent}\n可用：{' / '.join(AGENTS)}（用法 /agent codex）")
            return
        if arg in AGENTS:
            if scheduler.busy(ss):
                await m.answer("⚠️ 该会话有任务正在运行，等它结束或 /stop 后再切换")
                return
            await m.answer(scheduler.set_agent(chat, arg))
        else:
            await m.answer(f"未知的 AI：{arg}。可用：{' / '.join(AGENTS)}")

    def _choices_for(agent: str, kind: str) -> tuple[str, ...]:
        s = scheduler.settings
        if kind == "model":
            return s.codex_models if agent == "codex" else s.claude_models
        return s.codex_efforts if agent == "codex" else s.claude_efforts

    def _pick_menu(title: str, current: str, choices: tuple[str, ...]) -> str:
        lines = [title]
        for i, item in enumerate(choices, 1):
            mark = "👉" if item == current else "  "
            lines.append(f"{mark}{i}. {item}")
        lines.append(" 0. 恢复默认")
        return "\n".join(lines) + "\n0. 恢复默认"

    @router.message(Command("model"))
    async def cmd_model(m: Message):
        chat = scheduler.get(m.chat.id)
        ss = chat.cur
        choices = _choices_for(ss.agent, "model")
        arg = _arg_of(m)
        if not arg:
            configured = scheduler.settings.codex_model if ss.agent == "codex" else scheduler.settings.claude_model
            effective = ss.model or configured or "CLI 默认"
            await m.answer(_pick_menu(
                f"🧠 会话「{ss.name}」（{ss.agent}）当前模型：{effective}"
                f"{'（跟随默认）' if not ss.model else ''}", ss.model or configured, choices))
            return
        if arg.isdigit() and int(arg) == 0:
            await m.answer(scheduler.set_model(chat, ""))
            return
        if arg.isdigit() and 1 <= int(arg) <= len(choices):
            await m.answer(scheduler.set_model(chat, choices[int(arg) - 1]))
            return
        if arg in choices:
            await m.answer(scheduler.set_model(chat, arg))
            return
        await m.answer(f"未知的模型/编号：{arg}，/model 查看列表")

    @router.message(Command("think"))
    async def cmd_think(m: Message):
        chat = scheduler.get(m.chat.id)
        ss = chat.cur
        choices = _choices_for(ss.agent, "effort")
        arg = _arg_of(m)
        if not arg:
            await m.answer(_pick_menu(
                f"🎚 会话「{ss.name}」（{ss.agent}）当前思考强度：{ss.effort or '默认'}", ss.effort, choices))
            return
        if arg.isdigit() and int(arg) == 0:
            await m.answer(scheduler.set_effort(chat, ""))
            return
        if arg.isdigit() and 1 <= int(arg) <= len(choices):
            await m.answer(scheduler.set_effort(chat, choices[int(arg) - 1]))
            return
        if arg in choices:
            await m.answer(scheduler.set_effort(chat, arg))
            return
        await m.answer(f"未知的档位/编号：{arg}，/think 查看列表")

    @router.message(Command("api"))
    async def cmd_api(m: Message):
        chat = scheduler.get(m.chat.id)
        agent = fixed_agent or chat.cur.agent
        arg = _arg_of(m)
        if not arg:
            await m.answer(ccswitch.render_list(agent))
            return
        if not arg.isdigit():
            await m.answer("用法：/api 查看供应商列表；/api 编号 切换并重启调度台")
            return
        preflight_error = ccswitch.preflight_switch(agent, int(arg))
        if preflight_error:
            await m.answer(preflight_error)
            return
        if fixed_agent is None:
            # 旧单 Bot 兼容模式：一个进程里兼跑两种 AI，其他 agent 有任务在跑则中止
            total_busy = scheduler.busy_count()
            target_busy = scheduler.busy_count(agent)
            if total_busy > target_busy:
                await m.answer(
                    f"⚠️ 另一种 AI 还有 {total_busy - target_busy} 个会话在运行或排队。\n"
                    f"重启会一并中断它们且队列不保留——等任务结束、或 /stopall 后再 /api {arg}"
                )
                return
            stopped = await scheduler.stop_everything(agent)
        else:
            # 独立进程模式：本进程就是该 agent，任务全停、重启只影响自己
            stopped = await scheduler.stop_everything()
        reply = ccswitch.switch_provider(agent, int(arg))
        if reply.startswith("❌"):
            await m.answer(reply)
            return
        write_restart_notice(
            fixed_agent or "codex",
            f"✅ 重启完成，API 已生效，当前会话「{chat.current}」已恢复，继续发消息即可",
        )
        await m.answer(reply + f"\n🛑 已停止 {stopped} 个会话的任务\n🔄 正在重启调度台，几秒后自动回来…")
        supervised = os.getenv("TG_DISPATCH_SUPERVISED") == "1"
        if not supervised:
            spawn_replacement(fixed_agent or "")
        await asyncio.sleep(1.5)  # 给上面的 Telegram 回复留出发送时间
        os._exit(RESTART_EXIT_CODE if supervised else 0)

    @router.message(Command("cd"))
    async def cmd_cd(m: Message):
        chat = scheduler.get(m.chat.id)
        ss = chat.cur
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await m.answer(f"当前工作目录：{ss.workdir}\n用法：/cd C:\\projects\\某项目")
            return
        raw = parts[1].strip().strip('"')
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = Path(ss.workdir) / path
        path = path.resolve()
        if not path.is_dir():
            await m.answer(f"❌ 目录不存在：{path}")
            return
        await m.answer(scheduler.set_workdir(chat, str(path)))

    @router.message(Command("status"))
    async def cmd_status(m: Message):
        chat = scheduler.get(m.chat.id)
        await m.answer(session_detail(chat) + "\n\n" + scheduler.render_list(chat))

    @router.message(lambda msg: msg.text is not None)
    async def on_text(m: Message):
        notice = await scheduler.submit(m.chat.id, (m.text or "").strip())
        if notice:
            await m.answer(notice)

    @router.message()
    async def on_other(m: Message):
        await m.answer("⚠️ 暂时只支持文本消息（图片/文件支持在计划中）")

    return router
