"""AI CLI 运行器：以子进程驱动 codex / claude，解析 JSONL 事件流。

一个「回合（turn）」= 用户的一条消息。会话上下文由 CLI 自己持久化：
codex 通过 `exec resume <thread_id>`，claude 通过 `-p --resume <session_id>`，
因此调度器只需保存会话 ID，即可让一个 Telegram 聊天对应一段连续对话。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, Optional

from config import Settings

AGENTS = ("codex", "claude")

async def _iter_stream_lines(
    stream: asyncio.StreamReader, chunk_size: int = 64 * 1024
):
    """按块读取并自行切行，避免 StreamReader 的单行长度上限。"""
    pending = bytearray()
    while chunk := await stream.read(chunk_size):
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            yield bytes(pending[:newline])
            del pending[: newline + 1]
    if pending:
        yield bytes(pending)


@dataclass
class TurnResult:
    ok: bool
    session_id: Optional[str] = None
    text: str = ""
    error: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class ProgressUpdate:
    kind: Literal["text", "activity"]
    text: str


ProgressCallback = Callable[[ProgressUpdate], Awaitable[None]]


@dataclass
class _TurnState:
    session_id: Optional[str] = None
    texts: list[str] = field(default_factory=list)
    error: Optional[str] = None


def _resolve_exe(name: str) -> list[str]:
    """Windows 下 npm 安装的 CLI 是 .cmd 垫片，需经 cmd.exe 启动。"""
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"找不到 {name} 命令，请确认它已安装并在 PATH 中")
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/d", "/c", path]
    return [path]


def build_command(
    agent: str,
    prompt: str,
    workdir: str,
    session_id: Optional[str],
    s: Settings,
    model: str = "",
    effort: str = "",
) -> tuple[list[str], str, dict[str, str]]:
    """返回 (argv, 子进程 cwd, 需注入子进程的环境变量)。

    model/effort 为会话级覆盖（空串 = 跟随 .env / CLI 默认）。
    """
    extra_env: dict[str, str] = {}
    if agent == "codex":
        if session_id:
            # resume 子命令不支持 -C/-s：工作目录跟随会话记录，沙箱用 -c 覆盖
            argv = [
                *_resolve_exe("codex"), "exec", "resume", session_id,
                "--json", "--skip-git-repo-check",
                "-c", f'sandbox_mode="{s.codex_sandbox}"',
            ]
        else:
            argv = [
                *_resolve_exe("codex"), "exec",
                "--json", "--skip-git-repo-check", "-C", workdir, "-s", s.codex_sandbox,
            ]
        if s.codex_network and s.codex_sandbox == "workspace-write":
            argv += ["-c", "sandbox_workspace_write.network_access=true"]
        # CLI 的 -m / -c 覆盖优先级高于 config.toml，会话覆盖优先于 .env
        chosen_model = model or s.codex_model
        if chosen_model:
            argv += ["-m", chosen_model]
        if effort:
            argv += ["-c", f'model_reasoning_effort="{effort}"']
        argv.append(prompt)
        return argv, workdir, extra_env

    if agent == "claude":
        argv = [
            *_resolve_exe("claude"), "-p",
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", s.claude_permission_mode,
        ]
        if session_id:
            argv += ["--resume", session_id]
        chosen_model = model or s.claude_model
        if chosen_model:
            argv += ["-m", chosen_model]
        # Claude Code 2.1+ 原生支持会话级 --effort；resume 时同样生效。
        if effort:
            argv += ["--effort", effort]
        argv.append(prompt)  # 仅占位，run_turn 会去掉它并改走 stdin
        return argv, workdir, extra_env

    raise ValueError(f"未知 agent: {agent}")


def _parse_codex_event(evt: dict, st: _TurnState) -> list[ProgressUpdate]:
    """处理一条 codex 事件，返回应立即展示的增量。"""
    updates: list[ProgressUpdate] = []
    etype = evt.get("type")
    if etype == "thread.started":
        st.session_id = evt.get("thread_id") or st.session_id
    elif etype == "item.completed":
        item = evt.get("item") or {}
        itype = item.get("type")
        if itype == "agent_message":
            txt = (item.get("text") or "").strip()
            if txt:
                st.texts.append(txt)
                updates.append(ProgressUpdate("text", txt))
        elif itype == "command_execution":
            cmd = " ".join((item.get("command") or "").split())
            if cmd:
                mark = "✅" if item.get("exit_code") == 0 else "⚠️"
                updates.append(ProgressUpdate("activity", f"{mark} {cmd[:120]}"))
        elif itype == "file_change":
            changes = item.get("changes") or []
            if changes:
                updates.append(ProgressUpdate("activity", f"📝 更新 {len(changes)} 个文件"))
        elif itype == "web_search":
            updates.append(ProgressUpdate("activity", "🔍 联网搜索"))
    elif etype == "turn.failed":
        err = evt.get("error") or {}
        st.error = err.get("message") or json.dumps(evt, ensure_ascii=False)[:300]
        updates.append(ProgressUpdate("activity", f"❌ {st.error[:240]}"))
    return updates


def _parse_claude_event(evt: dict, st: _TurnState) -> list[ProgressUpdate]:
    """处理一条 claude 事件，返回应立即展示的全部增量。"""
    updates: list[ProgressUpdate] = []
    etype = evt.get("type")
    if etype == "system" and evt.get("subtype") == "init":
        st.session_id = evt.get("session_id") or st.session_id
    elif etype == "assistant":
        for blk in (evt.get("message") or {}).get("content") or []:
            btype = blk.get("type")
            if btype == "text":
                txt = (blk.get("text") or "").strip()
                if txt:
                    st.texts.append(txt)
                    updates.append(ProgressUpdate("text", txt))
            elif btype == "tool_use":
                updates.append(ProgressUpdate("activity", f"🔧 {blk.get('name') or 'tool'}"))
    elif etype == "result":
        st.session_id = evt.get("session_id") or st.session_id
        if evt.get("is_error"):
            st.error = (evt.get("result") or "claude 返回了错误").strip()
            updates.append(ProgressUpdate("activity", f"❌ {st.error[:240]}"))
        elif evt.get("result") is not None:
            # result 是权威最终回复，替换流式过程中累积的文本，避免重复
            final = str(evt["result"]).strip()
            if final:
                had_streamed_text = bool(st.texts)
                st.texts = [final]
                if not had_streamed_text:
                    updates.append(ProgressUpdate("text", final))
    return updates


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=15, check=False,
            )
        else:
            proc.kill()
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=10)


async def run_turn(
    agent: str,
    prompt: str,
    workdir: str,
    session_id: Optional[str],
    settings: Settings,
    on_progress: Optional[ProgressCallback] = None,
    model: str = "",
    effort: str = "",
    timeout_seconds: float = 0,
    accept_exit_zero: bool = False,
) -> TurnResult:
    """执行一个回合：把 prompt 交给对应 CLI，流式解析事件，返回最终结果。"""
    st = _TurnState(session_id=session_id)
    argv, cwd, extra_env = build_command(
        agent, prompt, workdir, session_id, settings, model=model, effort=effort
    )

    # 提示词一律走 stdin，不作为命令行参数：Windows 下 CLI 是 .cmd 垫片，
    # 参数要经 cmd.exe 二次解析——换行符之后的内容会被截断，
    # & % ^ 等字符也会被 cmd 吃掉或变形
    if agent == "claude":
        argv = argv[:-1]  # 去掉末尾占位的 prompt，claude -p 自行从 stdin 读取
    else:
        argv[-1] = "-"  # codex exec 用 - 表示从 stdin读取

    popen_kwargs: dict = {}
    if extra_env:
        popen_kwargs["env"] = {**os.environ, **extra_env}
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **popen_kwargs,
    )
    assert proc.stdin is not None
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    with contextlib.suppress(Exception):
        proc.stdin.close()

    stderr_tail: deque[str] = deque(maxlen=15)
    plain_stdout_tail: deque[str] = deque(maxlen=15)

    async def pump_out() -> None:
        assert proc.stdout is not None
        async for raw in _iter_stream_lines(proc.stdout):
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):
                if line:
                    plain_stdout_tail.append(line[-2000:])
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            parser = _parse_codex_event if agent == "codex" else _parse_claude_event
            updates = parser(evt, st)
            if on_progress:
                for update in updates:
                    await on_progress(update)

    async def pump_err() -> None:
        assert proc.stderr is not None
        async for raw in _iter_stream_lines(proc.stderr):
            stderr_tail.append(raw.decode("utf-8", "replace").rstrip()[-2000:])

    async def wait_for_completion() -> None:
        await asyncio.gather(pump_out(), pump_err(), proc.wait())

    try:
        if timeout_seconds > 0:
            async with asyncio.timeout(timeout_seconds):
                await wait_for_completion()
        else:
            await wait_for_completion()
    except TimeoutError:
        await _kill_tree(proc)
        text = "\n\n".join(t for t in st.texts if t).strip()
        return TurnResult(
            False, st.session_id, text,
            f"连续运行已达到 {timeout_seconds:g} 秒", timed_out=True,
        )
    except asyncio.CancelledError:
        await _kill_tree(proc)
        raise
    except Exception as exc:
        await _kill_tree(proc)
        tail = "\n".join(stderr_tail).strip()[-500:]
        detail = f"读取 AI 进程输出失败：{exc!r}"
        return TurnResult(False, st.session_id, "", detail + (f"\n{tail}" if tail else ""))

    text = "\n\n".join(t for t in st.texts if t).strip()
    if st.error:
        tail = "\n".join(stderr_tail).strip()[-500:]
        return TurnResult(False, st.session_id, text, st.error + (f"\n{tail}" if tail and not text else ""))
    if text:
        return TurnResult(True, st.session_id, text, "")
    plain_text = "\n".join(plain_stdout_tail).strip()
    if accept_exit_zero and proc.returncode == 0:
        # Claude 的 /compact 是 CLI 本地命令：成功时可能只输出普通文本
        # "Compacted"，不会产生 stream-json 的 result 事件。
        return TurnResult(True, st.session_id, plain_text, "")
    tail = "\n".join(stderr_tail).strip()[-800:]
    return TurnResult(
        False,
        st.session_id,
        "",
        f"进程退出码 {proc.returncode}，没有产生回复" + (f":\n{tail}" if tail else ""),
    )
