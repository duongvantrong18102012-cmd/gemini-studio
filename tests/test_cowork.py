from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gemini_tui.agent.cowork import (
    CoworkOrchestrator,
    CoworkResult,
    ScopedRegistry,
    WorkerOutput,
)
from gemini_tui.agent.plan import PlanStep, PlanStore, TaskPlan
from gemini_tui.tools.registry import ToolContext, ToolRegistry, ToolSpec


def settings(**overrides):
    base = dict(
        model="gemini-test",
        context_budget_tokens=4000,
        cowork_max_steps=8,
        cowork_step_tool_steps=4,
        computer_enabled=False,
        approval_mode="ask",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_plan(*titles: str) -> TaskPlan:
    return TaskPlan(
        task="demo task",
        steps=[
            PlanStep(id=f"s{index}", title=title, goal=title, agent="code")
            for index, title in enumerate(titles, 1)
        ],
    )


def make_orchestrator(tmp: Path, **kwargs):
    return CoworkOrchestrator(
        service=None,
        registry=ToolRegistry(),
        settings=settings(),
        workspace=str(tmp),
        plan_store=PlanStore(tmp / "plans"),
        **kwargs,
    )


async def _ok_verdict() -> tuple[str, str]:
    return "ok", "verified"


class ExecuteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cowork-test-"))
        self.events: list = []
        self.verifier_calls: list[TaskPlan] = []

    async def test_full_run_completes_and_reports(self) -> None:
        worker_calls: list[str] = []

        async def planner(task: str, context: str) -> TaskPlan:
            self.assertIn("demo", task)
            return make_plan("read files", "write answer")

        async def worker(step: PlanStep, plan: TaskPlan, context: str) -> WorkerOutput:
            worker_calls.append(step.id)
            return WorkerOutput(text=f"did {step.title}\nRESULT: {step.title} verified")

        async def verifier(plan: TaskPlan) -> tuple[str, str]:
            self.verifier_calls.append(plan)
            return "ok", "all steps verified"

        orchestrator = make_orchestrator(
            self.tmp,
            emit=self.events.append,
            planner=planner,
            worker=worker,
            verifier=verifier,
        )
        ctx = ToolContext(workspace=str(self.tmp), cwd=str(self.tmp), session_id="sess-1")
        result = await orchestrator.run("demo task", execute=True, base_ctx=ctx)

        self.assertIsInstance(result, CoworkResult)
        self.assertEqual(result.status, "done")
        self.assertEqual(worker_calls, ["s1", "s2"])
        self.assertEqual(result.plan.completed, 2)
        self.assertEqual(result.plan.failed, 0)
        self.assertEqual(result.plan.status, "done")
        self.assertIn("all steps verified", result.summary)
        self.assertEqual(len(self.verifier_calls), 1)
        self.assertTrue(result.report_path)
        self.assertTrue(Path(result.report_path).exists())
        self.assertEqual(orchestrator.last_result, result)

        kinds = [event.kind for event in self.events]
        for expected in ("status", "plan", "step_start", "step_done", "verify", "done"):
            self.assertIn(expected, kinds)
        step_events = [event for event in self.events if event.kind == "step_start"]
        self.assertEqual([event.step_id for event in step_events], ["s1", "s2"])

        stored = orchestrator.plan_store.load(result.plan.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "done")

    async def test_computer_step_uses_injected_driver(self) -> None:
        async def planner(task: str, context: str) -> TaskPlan:
            plan = make_plan("open the app")
            plan.steps[0].agent = "computer"
            return plan

        calls: list[tuple[str, int]] = []

        class FakeDriver:
            async def run(self, goal: str, max_steps: int = 10):
                calls.append((goal, max_steps))
                return SimpleNamespace(status="done", error="", render=lambda: "DONE: app opened")

        orchestrator = make_orchestrator(
            self.tmp,
            planner=planner,
            computer_driver_factory=lambda ctx: FakeDriver(),
            verifier=lambda plan: _ok_verdict(),
        )
        orchestrator.settings.computer_enabled = True
        result = await orchestrator.run("demo task", execute=True)

        self.assertEqual(result.status, "done")
        self.assertEqual(result.plan.steps[0].status, "done")
        self.assertEqual(calls, [("open the app", 10)])

    async def test_computer_step_without_consent_is_blocked(self) -> None:
        async def planner(task: str, context: str) -> TaskPlan:
            plan = make_plan("open the app")
            plan.steps[0].agent = "computer"
            return plan

        async def driver_factory(ctx):
            raise AssertionError("driver must not be built without consent")

        orchestrator = make_orchestrator(
            self.tmp, planner=planner, computer_driver_factory=driver_factory
        )
        result = await orchestrator.run("demo task", execute=True)

        self.assertEqual(result.status, "failed")
        self.assertIn("disabled", result.plan.steps[0].error.lower())

    async def test_plan_only_does_not_execute(self) -> None:
        async def planner(task: str, context: str) -> TaskPlan:
            return make_plan("one", "two")

        async def worker(step: PlanStep, plan: TaskPlan, context: str) -> WorkerOutput:
            raise AssertionError("worker must not run in plan-only mode")

        orchestrator = make_orchestrator(
            self.tmp, emit=self.events.append, planner=planner, worker=worker
        )
        result = await orchestrator.run("demo task", execute=False)

        self.assertEqual(result.status, "planned")
        self.assertEqual(result.plan.status, "draft")
        self.assertEqual(result.plan.completed, 0)
        self.assertEqual(result.report_path, "")
        self.assertEqual(orchestrator.plan_store.load(result.plan.id).status, "draft")

    async def test_failed_step_marks_plan_failed_and_skips_verifier(self) -> None:
        async def planner(task: str, context: str) -> TaskPlan:
            return make_plan("first", "second")

        async def worker(step: PlanStep, plan: TaskPlan, context: str) -> WorkerOutput:
            if step.id == "s2":
                return WorkerOutput(ok=False, error="missing file")
            return WorkerOutput(text="fine\nRESULT: ok")

        async def verifier(plan: TaskPlan) -> tuple[str, str]:
            raise AssertionError("verifier must not run when a step failed")

        orchestrator = make_orchestrator(
            self.tmp, planner=planner, worker=worker, verifier=verifier
        )
        result = await orchestrator.run("demo task", execute=True)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.plan.failed, 1)
        self.assertEqual(result.plan.steps[0].status, "done")
        self.assertEqual(result.plan.steps[1].status, "failed")
        self.assertEqual(result.plan.steps[1].error, "missing file")
        self.assertIn("failed", result.summary)

    async def test_worker_exception_is_contained(self) -> None:
        async def planner(task: str, context: str) -> TaskPlan:
            return make_plan("boom")

        async def worker(step: PlanStep, plan: TaskPlan, context: str) -> WorkerOutput:
            raise RuntimeError("kernel panic")

        orchestrator = make_orchestrator(self.tmp, planner=planner, worker=worker)
        result = await orchestrator.run("demo task", execute=True)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.plan.steps[0].status, "failed")
        self.assertIn("RuntimeError", result.plan.steps[0].error)
        self.assertIn("kernel panic", result.plan.steps[0].error)

    async def test_blocked_worker_output_fails_step(self) -> None:
        async def planner(task: str, context: str) -> TaskPlan:
            return make_plan("blocked")

        async def worker(step: PlanStep, plan: TaskPlan, context: str) -> WorkerOutput:
            return WorkerOutput(text="BLOCKED: no access", ok=False, blocked=True)

        orchestrator = make_orchestrator(self.tmp, planner=planner, worker=worker)
        result = await orchestrator.run("demo task", execute=True)

        self.assertEqual(result.status, "failed")
        self.assertIn("BLOCKED", result.plan.steps[0].error.upper())

    async def test_empty_plan_is_an_error(self) -> None:
        async def planner(task: str, context: str) -> TaskPlan:
            return TaskPlan(task=task)

        orchestrator = make_orchestrator(self.tmp, emit=self.events.append, planner=planner)
        result = await orchestrator.run("demo task", execute=True)

        self.assertEqual(result.status, "failed")
        self.assertIn("empty plan", result.error.lower())
        self.assertIn("error", [event.kind for event in self.events])

    async def test_cancel_returns_cancelled_result(self) -> None:
        async def planner(task: str, context: str) -> TaskPlan:
            return make_plan("one", "two")

        async def worker(step: PlanStep, plan: TaskPlan, context: str) -> WorkerOutput:
            orchestrator.cancel()
            return WorkerOutput(text="ok\nRESULT: ok")

        orchestrator = make_orchestrator(self.tmp, planner=planner, worker=worker)
        result = await orchestrator.run("demo task", execute=True)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.plan.status, "cancelled")


class ScopedRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        for name, category, risk in (
            ("fs_read", "files", "read"),
            ("fs_write", "files", "write"),
            ("web_search", "web", "network"),
            ("computer_click", "computer", "computer"),
            ("cowork_run", "agent", "write"),
        ):
            self.registry.add(
                ToolSpec(
                    name=name,
                    description=f"{name} tool",
                    parameters={"type": "object", "properties": {}},
                    handler=_noop,
                    category=category,
                    risk=risk,
                )
            )

    def test_filters_by_name_and_reports_dangerous(self) -> None:
        scoped = ScopedRegistry(self.registry, {"fs_read"})
        self.assertEqual([spec.name for spec in scoped.all()], ["fs_read"])
        self.assertEqual(scoped.dangerous_names, set())
        self.assertIn("fs_read", scoped.catalogue())

    def test_unknown_tool_is_rejected(self) -> None:
        import asyncio

        scoped = ScopedRegistry(self.registry, {"fs_read"})
        with self.assertRaises(Exception):
            asyncio.run(scoped.call("shell_run", {}, ToolContext()))

    def test_planner_cannot_widen_role_scope(self) -> None:
        plan = make_plan("narrow")
        plan.steps[0].agent = "code"
        plan.steps[0].tools = ["fs_read", "web_search", "cowork_run"]
        orchestrator = make_orchestrator(Path(tempfile.mkdtemp(prefix="scope-test-")))
        orchestrator.registry = self.registry
        scoped = orchestrator.worker_registry(plan.steps[0])
        names = {spec.name for spec in scoped.all()}
        self.assertEqual(names, {"fs_read"})

    def test_computer_role_needs_consent(self) -> None:
        plan = make_plan("click")
        plan.steps[0].agent = "computer"
        plan.steps[0].tools = ["computer_click", "fs_read"]
        orchestrator = make_orchestrator(Path(tempfile.mkdtemp(prefix="scope-test-")))
        orchestrator.registry = self.registry
        names = {spec.name for spec in orchestrator.worker_registry(plan.steps[0]).all()}
        self.assertEqual(names, {"fs_read"})

        orchestrator.settings.computer_enabled = True
        names = {spec.name for spec in orchestrator.worker_registry(plan.steps[0]).all()}
        self.assertEqual(names, {"computer_click", "fs_read"})


async def _noop(arguments, ctx) -> str:
    return "ok"


class ReportTests(unittest.TestCase):
    def test_render_includes_error_and_report(self) -> None:
        result = CoworkResult(plan=make_plan("one"), status="failed", error="boom", report_path="C:/x.md")
        rendered = result.render()
        self.assertIn("ERROR: boom", rendered)
        self.assertIn("Report: C:/x.md", rendered)


if __name__ == "__main__":
    unittest.main()
