import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from runner import ProgressUpdate, _iter_stream_lines, run_turn


class FakeStdin:
    def write(self, data: bytes) -> None:
        self.data = data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeProcess:
    def __init__(self, events=None) -> None:
        self.stdin = FakeStdin()
        self.stdout = asyncio.StreamReader()
        for event in events or [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
        ]:
            self.stdout.feed_data(json.dumps(event).encode() + b"\n")
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode = 0

    async def wait(self) -> int:
        return self.returncode


class HangingClaudeProcess(FakeProcess):
    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(json.dumps({
            "type": "system", "subtype": "init", "session_id": "session-live",
        }).encode() + b"\n")
        self.stderr = asyncio.StreamReader()
        self.returncode = None

    async def wait(self) -> int:
        await asyncio.Event().wait()
        return 0


class PlainOutputProcess(FakeProcess):
    def __init__(self, text: str, returncode: int = 0) -> None:
        self.stdin = FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(text.encode("utf-8") + b"\n")
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode = returncode


class StreamLineTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_json_line_larger_than_default_asyncio_limit(self) -> None:
        payload = {"type": "item.completed", "item": {"text": "x" * 100_000}}
        encoded = json.dumps(payload).encode("utf-8") + b"\n"
        stream = asyncio.StreamReader(limit=64 * 1024)
        stream.feed_data(encoded)
        stream.feed_eof()

        lines = [line async for line in _iter_stream_lines(stream)]

        self.assertEqual(lines, [encoded[:-1]])

    async def test_preserves_multiple_and_unterminated_final_lines(self) -> None:
        stream = asyncio.StreamReader(limit=8)
        stream.feed_data(b"first\nsecond\nlast")
        stream.feed_eof()

        lines = [line async for line in _iter_stream_lines(stream, chunk_size=3)]

        self.assertEqual(lines, [b"first", b"second", b"last"])

    async def test_run_turn_has_no_automatic_duration_limit(self) -> None:
        process = FakeProcess()
        settings = SimpleNamespace()
        with (
            patch("runner.build_command", return_value=(["codex", "prompt"], ".", {})),
            patch("runner.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)),
            patch("runner.asyncio.wait_for", side_effect=AssertionError("turn must not use a timeout")),
        ):
            result = await run_turn("codex", "prompt", ".", None, settings)

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "done")

    async def test_codex_text_and_tool_activity_are_streamed(self) -> None:
        process = FakeProcess([
            {"type": "item.completed", "item": {"type": "agent_message", "text": "先检查配置"}},
            {"type": "item.completed", "item": {
                "type": "command_execution", "command": "python -m unittest", "exit_code": 0,
            }},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "检查完成"}},
        ])
        updates: list[ProgressUpdate] = []
        async def collect(update: ProgressUpdate) -> None:
            updates.append(update)
        with (
            patch("runner.build_command", return_value=(["codex", "prompt"], ".", {})),
            patch("runner.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)),
        ):
            result = await run_turn("codex", "prompt", ".", None, SimpleNamespace(), collect)

        self.assertTrue(result.ok)
        self.assertEqual([u.kind for u in updates], ["text", "activity", "text"])
        self.assertEqual(updates[0].text, "先检查配置")
        self.assertIn("python -m unittest", updates[1].text)

    async def test_claude_streams_all_text_and_tool_blocks(self) -> None:
        process = FakeProcess([
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "正在定位"},
                {"type": "tool_use", "name": "Read"},
                {"type": "text", "text": "定位完成"},
            ]}},
            {"type": "result", "session_id": "session-1", "result": "最终结论"},
        ])
        updates: list[ProgressUpdate] = []
        async def collect(update: ProgressUpdate) -> None:
            updates.append(update)
        with (
            patch("runner.build_command", return_value=(["claude", "placeholder"], ".", {})),
            patch("runner.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)),
        ):
            result = await run_turn("claude", "prompt", ".", None, SimpleNamespace(), collect)

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "最终结论")
        self.assertEqual([u.kind for u in updates], ["text", "activity", "text"])

    async def test_timed_turn_returns_live_session_for_resume(self) -> None:
        process = HangingClaudeProcess()
        with (
            patch("runner.build_command", return_value=(["claude", "placeholder"], ".", {})),
            patch("runner.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)),
            patch("runner._kill_tree", new=AsyncMock()),
        ):
            result = await run_turn(
                "claude", "prompt", ".", None, SimpleNamespace(), timeout_seconds=0.01
            )

        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.session_id, "session-live")

    async def test_compact_plain_output_accepts_clean_exit(self) -> None:
        process = PlainOutputProcess("Compacted")
        with (
            patch("runner.build_command", return_value=(["claude", "placeholder"], ".", {})),
            patch("runner.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)),
        ):
            result = await run_turn(
                "claude", "/compact", ".", "session-1", SimpleNamespace(),
                accept_exit_zero=True,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Compacted")


if __name__ == "__main__":
    unittest.main()
