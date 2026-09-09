"""Loopback-only HTTP API and static web UI for the task scheduler."""
from __future__ import annotations

import hmac
import json
import logging
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from history import HistoryStore
from runner import AGENTS
from scheduler import ChatState, Scheduler, SessionState

log = logging.getLogger("webui")
WEB_ROOT = Path(__file__).resolve().parent / "gui-prototype"
PEER_SESSION_KEY = web.AppKey("peer_session", ClientSession)


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _task_payload(scheduler: Scheduler, chat: ChatState, ss: SessionState) -> dict:
    return next(item for item in scheduler.snapshot(chat) if item["id"] == ss.key)


def create_web_app(
    scheduler: Scheduler | dict[str, Scheduler],
    history: HistoryStore,
    token: str,
    *,
    peer_url: str = "",
    peer_agent: str = "claude",
) -> web.Application:
    schedulers = scheduler if isinstance(scheduler, dict) else {"default": scheduler}
    peer_url = peer_url.rstrip("/")

    @web.middleware
    async def secure_api(request: web.Request, handler):
        if request.path.startswith("/api/"):
            supplied = request.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            if not hmac.compare_digest(supplied, expected):
                return _error("网页访问凭据无效，请从启动日志中的完整地址重新打开。", 401)
        return await handler(request)

    app = web.Application(middlewares=[secure_api], client_max_size=256 * 1024)

    if peer_url:
        async def peer_session_context(application: web.Application):
            application[PEER_SESSION_KEY] = ClientSession(timeout=ClientTimeout(total=4))
            yield
            await application[PEER_SESSION_KEY].close()

        app.cleanup_ctx.append(peer_session_context)

    async def peer_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        if not peer_url:
            raise ClientError("peer is not configured")
        session = app[PEER_SESSION_KEY]
        kwargs = {
            "headers": {"Authorization": f"Bearer {token}"},
        }
        if body is not None:
            kwargs["json"] = body
        async with session.request(method, peer_url + path, **kwargs) as response:
            try:
                payload = await response.json()
            except (json.JSONDecodeError, ValueError):
                payload = {"error": f"{peer_agent} 调度服务返回了无效响应"}
            return response.status, payload

    async def forward_to_peer(method: str, path: str, body: dict | None = None) -> web.Response:
        try:
            status, payload = await peer_request(method, path, body)
        except (ClientError, TimeoutError, OSError):
            return _error(f"{peer_agent.capitalize()} 调度服务暂时不可用，请稍后重试", 503)
        return web.json_response(payload, status=status)

    def chat_for_ui(owner: Scheduler) -> ChatState:
        if owner.settings.allowed_user_ids:
            chat_id = min(owner.settings.allowed_user_ids)
            return owner.get(chat_id)
        if owner.chats:
            return owner.chats[next(iter(owner.chats))]
        raise web.HTTPServiceUnavailable(text=json.dumps({"error": "尚未配置 Telegram 白名单用户"}), content_type="application/json")

    def find_local_task(task_id: str) -> tuple[Scheduler, ChatState, SessionState] | None:
        for owner in schedulers.values():
            chat = chat_for_ui(owner)
            ss = owner.find_session(chat, task_id)
            if ss is not None:
                return owner, chat, ss
        return None

    def resolve_task(request: web.Request) -> tuple[Scheduler, ChatState, SessionState]:
        found = find_local_task(request.match_info["task_id"])
        if found is not None:
            return found
        raise web.HTTPNotFound(text=json.dumps({"error": "任务不存在或已被删除"}), content_type="application/json")

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_ROOT / "index.html")

    async def state(_: web.Request) -> web.Response:
        tasks = []
        current_by_agent = {}
        agents = {}
        for name, owner in schedulers.items():
            chat = chat_for_ui(owner)
            tasks.extend(owner.snapshot(chat))
            if owner.fixed_agent:
                current_by_agent[owner.fixed_agent] = chat.cur.key
                agents[owner.fixed_agent] = {"available": True, "telegramCurrent": chat.cur.key}
            elif len(schedulers) == 1:
                current_by_agent.update({agent: chat.cur.key for agent in AGENTS})
                agents.update({
                    agent: {"available": True, "telegramCurrent": chat.cur.key}
                    for agent in AGENTS
                })
            else:
                current_by_agent[name] = chat.cur.key
                agents[name] = {"available": True, "telegramCurrent": chat.cur.key}
        if peer_url:
            try:
                status, peer = await peer_request("GET", "/api/state")
                if status != 200:
                    raise ClientError(f"peer state returned {status}")
                tasks.extend(peer.get("tasks") or [])
                current_by_agent.update(peer.get("telegramCurrentByAgent") or {})
                agents.update(peer.get("agents") or {})
                agents.setdefault(peer_agent, {})["available"] = True
            except (ClientError, TimeoutError, OSError):
                agents[peer_agent] = {"available": False, "telegramCurrent": None}
        first_current = next(iter(current_by_agent.values()), None)
        return web.json_response({
            "tasks": tasks,
            "telegramCurrent": first_current,
            "telegramCurrentByAgent": current_by_agent,
            "agents": agents,
        })

    async def messages(request: web.Request) -> web.Response:
        found = find_local_task(request.match_info["task_id"])
        if found is None and peer_url:
            query = request.query_string
            suffix = f"?{query}" if query else ""
            return await forward_to_peer(
                "GET", f"/api/tasks/{request.match_info['task_id']}/messages{suffix}"
            )
        if found is None:
            return _error("任务不存在或已被删除", 404)
        _, chat, ss = found
        try:
            after = int(request.query.get("after", "0"))
        except ValueError:
            return _error("after 必须是整数", 400)
        return web.json_response({"messages": history.list_messages(chat.chat_id, ss.key, after=after)})

    async def create_task(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return _error("请求内容不是有效 JSON", 400)
        name = str(body.get("name", "")).strip()
        agent = str(body.get("agent", "")).strip().lower()
        raw_workdir = str(body.get("workdir", "")).strip().strip('"')
        if not name or len(name) > 20 or any(c.isspace() for c in name) or "/" in name or "\\" in name:
            return _error("任务名称限 20 字以内，不能含空格和斜杠", 400)
        if agent not in AGENTS:
            return _error(f"执行助手必须是：{' / '.join(AGENTS)}", 400)
        owner = schedulers.get(agent)
        if owner is None:
            if peer_url and agent == peer_agent:
                return await forward_to_peer("POST", "/api/tasks", body)
            owner = next(iter(schedulers.values()))
            if owner.fixed_agent and owner.fixed_agent != agent:
                return _error(f"{agent} Bot 尚未启用", 503)
        chat = chat_for_ui(owner)
        workdir = Path(raw_workdir).expanduser()
        if not workdir.is_absolute():
            workdir = Path(chat.cur.workdir) / workdir
        workdir = workdir.resolve()
        if not workdir.is_dir():
            return _error(f"目录不存在：{workdir}", 400)
        ss, reason = owner.create_session(chat, name, agent, str(workdir))
        if ss is None:
            status = 409 if name in chat.sessions else 400
            return _error(reason, status)
        return web.json_response({"task": _task_payload(owner, chat, ss)}, status=201)

    async def submit(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return _error("请求内容不是有效 JSON", 400)
        found = find_local_task(request.match_info["task_id"])
        if found is None and peer_url:
            return await forward_to_peer(
                "POST", f"/api/tasks/{request.match_info['task_id']}/submit", body
            )
        if found is None:
            return _error("任务不存在或已被删除", 404)
        owner, chat, ss = found
        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            return _error("消息不能为空", 400)
        if len(prompt) > 100_000:
            return _error("消息过长，请控制在 100000 字以内", 400)
        notice = await owner.submit_to(chat, ss, prompt)
        if "队列已满" in notice:
            return _error(notice, 409)
        return web.json_response({"notice": notice}, status=202)

    async def stop(request: web.Request) -> web.Response:
        found = find_local_task(request.match_info["task_id"])
        if found is None and peer_url:
            return await forward_to_peer(
                "POST", f"/api/tasks/{request.match_info['task_id']}/stop", {}
            )
        if found is None:
            return _error("任务不存在或已被删除", 404)
        owner, chat, ss = found
        if not owner.busy(ss) and ss.queue.empty():
            return web.json_response({"notice": f"任务「{ss.name}」当前没有运行或排队内容"})
        await owner.stop_current_for(chat, ss)
        return web.json_response({"notice": f"已停止任务「{ss.name}」并清空队列"})

    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/tasks/{task_id}/messages", messages)
    app.router.add_post("/api/tasks", create_task)
    app.router.add_post("/api/tasks/{task_id}/submit", submit)
    app.router.add_post("/api/tasks/{task_id}/stop", stop)
    app.router.add_static("/assets/", WEB_ROOT, show_index=False)
    return app


async def start_web_ui(
    scheduler: Scheduler | dict[str, Scheduler],
    history: HistoryStore,
    host: str,
    port: int,
    token: str,
    *,
    peer_url: str = "",
    peer_agent: str = "claude",
) -> web.AppRunner:
    app = create_web_app(
        scheduler, history, token, peer_url=peer_url, peer_agent=peer_agent
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("电脑 GUI 已启动：http://%s:%s/?token=%s", host, port, token)
    return runner
