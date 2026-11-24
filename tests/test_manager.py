"""Live agent state: lifecycle, logging, pause/resume and cooperative cancel."""

from __future__ import annotations

import asyncio
import unittest

from gemini_tui.orchestration.manager import (
    STATE_CANCELLED,
    STATE_CANCELLING,
    STATE_DONE,
    STATE_FAILED,
    STATE_PAUSED,
    STATE_QUEUED,
    STATE_RUNNING,
    AgentManager,
)


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = AgentManager()
        self.agent = self.manager.create(
            "developer", "coder", task_id="t1", task_title="Build it"
        )

    def test_create_assigns_role_and_identity(self) -> None:
        self.assertEqual(self.agent.id, "agent-01")
        self.assertEqual(self.agent.role, "developer")
        self.assertEqual(self.agent.label, "Developer")
        self.assertEqual(self.agent.state, STATE_QUEUED)
        second = self.manager.create("review")
        self.assertEqual(second.id, "agent-02")
        self.assertEqual(second.role, "security")

    def test_update_progress_counter_and_finish(self) -> None:
        self.manager.set_state(self.agent.id, STATE_RUNNING)
        self.manager.update(self.agent.id, model="coder-v2")
        self.manager.progress(self.agent.id, 4.2)
        self.manager.progress(self.agent.id, -1)
        self.assertEqual(self.agent.progress, 0.0)
        self.manager.progress(self.agent.id, 0.5)
        self.manager.tool_call(self.agent.id, "fs_read")
        self.manager.tool_call(self.agent.id, "fs_write", ok=False)
        self.assertEqual(self.agent.tool_calls, 2)

        self.manager.finish(self.agent.id, STATE_DONE, summary="changed app.py")
        self.assertEqual(self.agent.state, STATE_DONE)
        self.assertEqual(self.agent.progress, 1.0)
        self.assertEqual(self.agent.summary, "changed app.py")
        self.assertGreater(self.agent.ended_at, 0.0)
        self.assertIn("fs_read", self.manager.render_log(self.agent.id))

    def test_failure_does_not_mark_full_progress(self) -> None:
        self.manager.set_state(self.agent.id, STATE_RUNNING)
        self.manager.progress(self.agent.id, 0.4)
        self.manager.finish(self.agent.id, STATE_FAILED, error="boom")
        self.assertEqual(self.agent.state, STATE_FAILED)
        self.assertEqual(self.agent.error, "boom")
        self.assertAlmostEqual(self.agent.progress, 0.4)

    def test_unknown_agent_updates_are_ignored(self) -> None:
        self.manager.update("missing", model="x")
        self.manager.log("missing", "x")
        self.manager.progress("missing", 1)
        self.manager.finish("missing", STATE_DONE)
        self.assertIsNone(self.manager.get("missing"))

    def test_snapshot_and_summary(self) -> None:
        self.manager.set_state(self.agent.id, STATE_RUNNING)
        self.manager.tool_call(self.agent.id, "fs_read")
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot[0]["task_id"], "t1")
        self.assertEqual(snapshot[0]["tool_calls"], 1)
        summary = self.manager.summary()
        self.assertIn("1 agents", summary)
        self.assertIn("1 active", summary)


class ControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = AgentManager()
        self.agent = self.manager.create("developer", "coder", task_id="t1")
        self.manager.set_state(self.agent.id, STATE_RUNNING)

    async def test_pause_blocks_checkpoint_until_resume(self) -> None:
        self.assertTrue(self.manager.pause(self.agent.id))
        self.assertEqual(self.agent.state, STATE_PAUSED)
        # Individual pause is per-agent; only pause_all flips the team flag.
        self.assertFalse(self.manager.globally_paused)

        waiter = asyncio.create_task(self.manager.wait_ready(self.agent.id))
        await asyncio.sleep(0.12)
        self.assertFalse(waiter.done())

        self.assertTrue(self.manager.resume(self.agent.id))
        await asyncio.wait_for(waiter, timeout=1)
        self.assertEqual(self.agent.state, STATE_RUNNING)
        self.assertFalse(self.manager.globally_paused)

    async def test_pause_all_marks_every_live_agent(self) -> None:
        idle = self.manager.create("writer", task_id="t2")
        self.assertEqual(self.manager.pause_all(), 2)
        self.assertTrue(self.manager.globally_paused)
        self.assertEqual(self.agent.state, STATE_PAUSED)
        self.assertEqual(idle.state, STATE_QUEUED)
        self.assertEqual(self.manager.resume_all(), 2)
        self.assertFalse(self.manager.globally_paused)
        self.assertEqual(self.agent.state, STATE_RUNNING)

    async def test_cancel_interrupts_a_paused_checkpoint(self) -> None:
        self.manager.pause(self.agent.id)
        waiter = asyncio.create_task(self.manager.wait_ready(self.agent.id))
        await asyncio.sleep(0.05)
        self.assertTrue(self.manager.request_cancel(self.agent.id))
        self.assertEqual(self.agent.state, STATE_CANCELLING)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, timeout=1)
        # Cancelling one agent is not a team-wide cancel.
        self.assertFalse(self.manager.globally_cancelled)
        self.assertTrue(self.manager.is_cancelled(self.agent.id))

        self.manager.reset_cancellation()
        self.assertFalse(self.manager.is_cancelled(self.agent.id))
        await self.manager.wait_ready(self.agent.id)

    async def test_cancel_all_sets_the_global_flag(self) -> None:
        second = self.manager.create("writer", task_id="t2")
        self.assertEqual(self.manager.cancel_all(), 2)
        self.assertTrue(self.manager.globally_cancelled)
        self.assertEqual(self.agent.state, STATE_CANCELLING)
        self.assertEqual(second.state, STATE_CANCELLING)
        with self.assertRaises(asyncio.CancelledError):
            await self.manager.wait_ready(second.id)

    async def test_cancel_all_only_counts_live_agents(self) -> None:
        done = self.manager.create("writer", task_id="t2")
        self.manager.finish(done.id, STATE_DONE)
        count = self.manager.cancel_all()
        self.assertEqual(count, 1)
        self.assertEqual(done.state, STATE_DONE)

    def test_pause_and_resume_ignore_terminal_agents(self) -> None:
        self.manager.finish(self.agent.id, STATE_DONE)
        self.assertFalse(self.manager.pause(self.agent.id))
        self.assertFalse(self.manager.resume(self.agent.id))
        self.assertEqual(self.manager.pause_all(), 0)
        self.assertEqual(self.manager.resume_all(), 0)
        self.assertEqual(self.agent.state, STATE_DONE)

    def test_for_task_returns_latest_attempt(self) -> None:
        second = self.manager.create("developer", task_id="t1")
        self.assertIs(self.manager.for_task("t1"), second)
        self.assertIsNone(self.manager.for_task("missing"))


if __name__ == "__main__":
    unittest.main()
