"""调度台自重启：拉起一个全新的 main.py <agent> 进程，然后旧进程退出。

重启后会话不丢：会话状态已在各自 state 文件里，由新进程自动恢复；
"回到当前对话"的系统回复通过 main.py 的 restart-notice 机制发送
（每个 agent 有独立的通知文件，互不覆盖）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESTART_EXIT_CODE = 75


def spawn_replacement(agent: str = "") -> None:
    """以独立进程启动新的调度台（Windows 下开新控制台，便于看日志）。

    agent 为空表示旧单 Bot 兼容模式（python main.py 不带参数）。
    """
    argv = [sys.executable, str(ROOT / "main.py")]
    if agent:
        argv.append(agent)
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, cwd=str(ROOT), close_fds=True, **kwargs)


def write_restart_notice(sender_agent: str, text: str) -> None:
    """写重启通知文件；sender_agent 是重启后负责发通知的那个 Bot 的 agent。"""
    name = ".restart-notice.claude" if sender_agent == "claude" else ".restart-notice"
    (ROOT / name).write_text(text, "utf-8")
