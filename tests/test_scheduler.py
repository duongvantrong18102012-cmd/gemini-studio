"""Dependency graph, retries, skip propagation and cancellation."""

from __future__ import annotations

import unittest

from gemini_tui.orchestration.scheduler import (
    ScheduleError,
    ScheduledTask,
    TASK_CANCELLED,
    TASK_DONE,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_SKIPPED,
    TaskScheduler,
)


def task(task_id: str, *deps: str, **kwargs) -> ScheduledTask:
    return ScheduledTask(id=task_id, title=task_id, depends_on=tuple(deps), **kwargs)


class ValidationTests(unittest.TestCase):
    def test_duplicate_ids_are_rejected(self) -> None:
        with self.assertRaises(ScheduleError):
            TaskScheduler([task("t1"), task("t1")])

    def test_unknown_dependency_is_rejected(self) -> None:
        with self.assertRaises(ScheduleError) as caught:
            TaskScheduler([task("t1", "missing")])
        self.assertIn("unknown", str(caught.exception))

    def test_self_dependency_is_rejected(self) -> None:
        with self.assertRaises(ScheduleError):
            TaskScheduler([task("t1", "t1")])

    def test_cycle_is_rejected(self) -> None:
        with self.assertRaises(ScheduleError) as caught:
            TaskScheduler([task("a", "c"), task("b", "a"), task("c", "b")])
        self.assertIn("cycle", str(caught.exception))

    def test_defaults_are_filled_but_explicit_values_kept(self) -> None:
        explicit = task("t1", timeout_s=12.5, max_attempts=4)
        scheduler = TaskScheduler(
            [explicit, task("t2")], default_timeout=99.0, default_attempts=3
        )
        self.assertEqual(explicit.timeout_s, 12.5)
        self.assertEqual(explicit.max_attempts, 4)
        self.assertEqual(scheduler.get("t2").timeout_s, 99.0)
        self.assertEqual(scheduler.get("t2").max_attempts, 3)


class GraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = TaskScheduler(
            [
                task("build"),
                task("docs"),
                task("research"),
                task("design", "research"),
                task("ship", "build", "docs", "design"),
            ]
        )

    def test_layers_group_independent_work(self) -> None:
        self.assertEqual(
            self.scheduler.topological_layers(),
            [["build", "docs", "research"], ["design"], ["ship"]],
        )

    def test_only_dependency_free_tasks_are_ready(self) -> None:
        self.assertEqual(
            [item.id for item in self.scheduler.ready()], ["build", "docs", "research"]
        )
        self.scheduler.mark_running("research", "agent-1")
        self.scheduler.mark_done("research", "evidence")
        self.assertIn("design", [item.id for item in self.scheduler.ready()])
        self.assertNotIn("ship", [item.id for item in self.scheduler.ready()])

    def test_parallel_slots_are_bounded(self) -> None:
        scheduler = TaskScheduler([task("a"), task("b")], max_parallel=1)
        self.assertEqual(scheduler.slots, 1)
        scheduler.mark_running("a")
        self.assertEqual(scheduler.slots, 0)
        self.assertEqual(scheduler.running_count, 1)

    def test_progress_and_completion(self) -> None:
        self.assertEqual(self.scheduler.progress, 0.0)
        self.scheduler.mark_running("build")
        self.scheduler.mark_done("build", "ok")
        self.assertAlmostEqual(self.scheduler.progress, 0.2)
        self.assertFalse(self.scheduler.complete())
        for item in self.scheduler.all():
            if item.status == TASK_PENDING:
                self.scheduler.mark_skipped(item.id, "test")
        self.assertTrue(self.scheduler.complete())
        self.assertEqual(self.scheduler.progress, 1.0)


class FailureTests(unittest.TestCase):
    def test_retry_then_terminal_failure_skips_dependents(self) -> None:
        scheduler = TaskScheduler(
            [task("work"), task("verify", "work")], default_attempts=2
        )
        scheduler.mark_running("work")
        self.assertEqual(scheduler.mark_failed("work", "boom"), "retry")
        self.assertEqual(scheduler.get("work").status, TASK_PENDING)
        self.assertEqual(scheduler.get("work").attempts, 1)
        self.assertEqual(scheduler.get("verify").status, TASK_PENDING)

        scheduler.mark_running("work")
        self.assertEqual(scheduler.mark_failed("work", "boom again"), TASK_FAILED)
        work = scheduler.get("work")
        self.assertEqual(work.status, TASK_FAILED)
        self.assertEqual(work.attempts, 2)
        self.assertEqual(work.error, "boom again")
        self.assertEqual(scheduler.get("verify").status, TASK_SKIPPED)
        self.assertIn("dependency", scheduler.get("verify").error)

    def test_terminal_failure_never_retries(self) -> None:
        scheduler = TaskScheduler([task("work"), task("after", "work")], default_attempts=5)
        scheduler.mark_running("work")
        scheduler.fail_terminal("work", "configuration error")
        self.assertEqual(scheduler.get("work").status, TASK_FAILED)
        self.assertEqual(scheduler.get("work").attempts, 1)
        self.assertEqual(scheduler.get("after").status, TASK_SKIPPED)

    def test_skip_propagates_through_chain(self) -> None:
        scheduler = TaskScheduler([task("a"), task("b", "a"), task("c", "b")])
        scheduler.mark_running("a")
        scheduler.fail_terminal("a", "failed")
        self.assertEqual(scheduler.get("b").status, TASK_SKIPPED)
        self.assertEqual(scheduler.get("c").status, TASK_SKIPPED)

    def test_cancel_pending_and_running(self) -> None:
        scheduler = TaskScheduler([task("a"), task("b")])
        scheduler.mark_running("a")
        scheduler.cancel_pending("user cancelled")
        self.assertEqual(scheduler.get("a").status, TASK_CANCELLED)
        self.assertEqual(scheduler.get("b").status, TASK_CANCELLED)
        self.assertTrue(scheduler.complete())
        self.assertEqual(scheduler.failed_count, 0)
        self.assertEqual(scheduler.skipped_count, 0)

    def test_deadlock_detection(self) -> None:
        scheduler = TaskScheduler([task("a"), task("b", "a")])
        self.assertFalse(scheduler.deadlocked())
        scheduler.mark_skipped("a", "skipped without failing dependents")
        self.assertFalse(scheduler.complete())
        self.assertTrue(scheduler.deadlocked())

    def test_unknown_task_raises(self) -> None:
        scheduler = TaskScheduler([task("a")])
        with self.assertRaises(ScheduleError):
            scheduler.mark_done("missing")

    def test_summary_is_derived_from_real_state(self) -> None:
        scheduler = TaskScheduler([task("a"), task("b")])
        scheduler.mark_running("a")
        scheduler.mark_done("a", "ok")
        scheduler.mark_running("b")
        scheduler.fail_terminal("b", "bad command")
        summary = scheduler.summary_text()
        self.assertIn("1/2 done", summary)
        self.assertIn("1 failed", summary)


if __name__ == "__main__":
    unittest.main()
