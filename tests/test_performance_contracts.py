"""Performance contracts that can be checked without a stopwatch.

These are behavioural guarantees, not benchmarks: tokens must be batched,
rapid thoughts must be throttled, and a finished tool card must stop waking the
event loop. A timing benchmark belongs in the test report, not here.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from textual.app import App, ComposeResult

from gemini_tui.agent.permissions import PermissionPolicy
from gemini_tui.tools.builtin import register_builtin_tools
from gemini_tui.tools.registry import ToolContext, ToolRegistry
from gemini_tui.widgets.chat import AssistantMessage, ToolCard

FILE_SETTINGS = SimpleNamespace(
    approval_mode="auto",
    computer_enabled=False,
    computer_approval="ask",
    computer_rate_limit=90,
    tool_policies={},
    denied_tools=[],
    agent_permissions={},
)


class FakeStream:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.stopped = False

    async def write(self, text: str) -> None:
        self.writes.append(text)

    async def stop(self) -> None:
        self.stopped = True


class FakeThought:
    def __init__(self) -> None:
        self.updates: list[str] = []
        self.classes: list[bool] = []

    def set_class(self, active: bool, name: str) -> None:
        self.classes.append(active)

    def update(self, value) -> None:
        self.updates.append(str(value))


class StreamingBufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_many_tokens_become_one_stream_write(self) -> None:
        message = AssistantMessage("test-model")
        stream = FakeStream()
        message._stream = stream

        for _ in range(1000):
            await message.write("x ")
        await message._flush_task

        self.assertEqual(len(stream.writes), 1)
        self.assertEqual(stream.writes[0], "x " * 1000)
        self.assertEqual(message.answer_text, "x " * 1000)
        self.assertEqual(message._pending, [])

    async def test_a_single_write_never_exceeds_the_chunk_limit(self) -> None:
        message = AssistantMessage("test-model")
        stream = FakeStream()
        message._stream = stream
        await message.write("y" * 30_000)
        await message._flush_task
        self.assertEqual(len(stream.writes), 3)
        self.assertEqual(sum(len(chunk) for chunk in stream.writes), 30_000)
        self.assertTrue(all(len(chunk) <= 12_000 for chunk in stream.writes))

    async def test_rapid_thoughts_are_throttled(self) -> None:
        message = AssistantMessage("test-model")
        thought = FakeThought()
        message._thought = thought
        for _ in range(200):
            message.add_thought("t")
        self.assertLess(len(thought.updates), 20)
        self.assertEqual(len(message.thought_text), 200)

    async def test_closed_message_drains_pending_text(self) -> None:
        message = AssistantMessage("test-model")
        stream = FakeStream()
        message._stream = stream
        message._closed = True
        message._pending = ["tail"]
        await message.stop_stream()
        self.assertEqual(stream.writes, ["tail"])
        self.assertTrue(stream.stopped)


class ToolCardTimerTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_card_stops_its_refresh_timer(self) -> None:
        class CardApp(App):
            def compose(self) -> ComposeResult:
                yield ToolCard("call-1", "fs_read", {"path": "a.py"}, False)

        app = CardApp()
        # ToolCard reads the host app's animation frame for its running
        # spinner; a bare App does not have one.
        app.frame = 0
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            card = app.query_one(ToolCard)
            self.assertIsNotNone(card._timer)
            card.finish(True, "read 3 lines", 0.12)
            await asyncio.sleep(0.35)
            self.assertIsNone(card._timer)
            self.assertEqual(card.status, "ok")


class FilesystemOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="perf-fs-"))
        (self.tmp / "sample.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
        self.registry = ToolRegistry()
        register_builtin_tools(self.registry)
        self.context = ToolContext(
            cwd=str(self.tmp),
            workspace=str(self.tmp),
            settings=FILE_SETTINGS,
            permissions=PermissionPolicy(FILE_SETTINGS),
        )

    def _record_reads(self):
        seen: list[int] = []
        original = Path.read_text

        def wrapper(path, *args, **kwargs):
            seen.append(threading.get_ident())
            return original(path, *args, **kwargs)

        return seen, patch.object(Path, "read_text", wrapper)

    async def test_fs_read_does_not_read_on_the_event_loop_thread(self) -> None:
        seen, patcher = self._record_reads()
        with patcher:
            output = await self.registry.call("fs_read", {"path": "sample.py"}, self.context)
        self.assertIn("alpha = 1", output)
        self.assertTrue(seen)
        self.assertTrue(all(thread != threading.get_ident() for thread in seen))

    async def test_fs_grep_does_not_scan_on_the_event_loop_thread(self) -> None:
        seen, patcher = self._record_reads()
        with patcher:
            output = await self.registry.call(
                "fs_grep", {"pattern": "alpha", "path": "."}, self.context
            )
        self.assertIn("sample.py:1", output)
        self.assertTrue(seen)
        self.assertTrue(all(thread != threading.get_ident() for thread in seen))


if __name__ == "__main__":
    unittest.main()
