"""Team orchestration: planning, dependency execution, retries, cancellation,
ownership, verification and reports.

Every external effect (model, tools) is a fake. Nothing here needs a network or
an API key, and no test claims a real model produced a real answer.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gemini_tui.orchestration.capabilities import CapabilityRegistry
from gemini_tui.orchestration.models import (
    CAP_CODE,
    CAP_PLANNING,
    CAP_REASONING,
    CAP_TEXT,
    CAP_TOOLS,
    ModelRegistry,
    ModelRouter,
    ModelSpec,
)
from gemini_tui.orchestration.orchestrator import (
    AgentTeamOrchestrator,
    OrchestrationError,
    TeamPlan,
    WorkerOutput,
    heuristic_plan,
    plan_from_json,
)
from gemini_tui.orchestration.scheduler import (
    ScheduleError,
    ScheduledTask,
    TASK_CANCELLED,
    TASK_DONE,
    TASK_FAILED,
    TaskScheduler,
)
from gemini_tui.tools.registry import ToolRegistry, ToolSpec


# ══════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════
def model(
    model_id: str,
    *,
    tools: bool = True,
    reasoning: bool = True,
    planning: bool = True,
    available: bool = True,
) -> ModelSpec:
    capabilities = {CAP_TEXT}
    if tools:
        capabilities |= {CAP_TOOLS, CAP_CODE}
    if reasoning:
        capabilities.add(CAP_REASONING)
    if planning:
        capabilities.add(CAP_PLANNING)
    return ModelSpec(
        id=model_id,
        display_name=model_id.upper(),
        capabilities=frozenset(capabilities),
        supports_tools=tools,
        context_limit=128_000,
        speed="medium",
        cost_tier="low",
        available=available,
    )


def registry(*specs: ModelSpec) -> ModelRegistry:
    result = ModelRegistry()
    for spec in specs or (model("worker"),):
        result.register(spec)
    return result


def task(
    task_id: str,
    role: str = "developer",
    *,
    depends_on: tuple[str, ...] = (),
    scope: tuple[str, ...] = (),
    timeout_s: float = 30.0,
    max_attempts: int = 0,
) -> ScheduledTask:
    return ScheduledTask(
        id=task_id,
        title=f"Task {task_id}",
        role=role,
        goal=f"Do {task_id}",
        depends_on=depends_on,
        scope=scope,
        acceptance="A concrete report.",
        timeout_s=timeout_s,
        max_attempts=max_attempts,
    )


def plan(*tasks: ScheduledTask, goal: str = "demo goal") -> TeamPlan:
    return TeamPlan(goal=goal, tasks=list(tasks))


def settings() -> SimpleNamespace:
    return SimpleNamespace(approval_mode="ask", workspace=".", team_model_roles={})


class CallbackRunner:
    """Runner whose behaviour is a Python callback per request."""

    def __init__(self, callback) -> None:
        self.callback = callback
        self.requests = []
        self.calls: list[str] = []

    async def run(self, request):
        self.calls.append(request.task_id)
        self.requests.append(request)
        output = self.callback(request)
        if asyncio.iscoroutine(output):
            output = await output
        if isinstance(output, BaseException):
            raise output
        if isinstance(output, WorkerOutput):
            return output
        return WorkerOutput(str(output or ""))


class OrchestratorCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="orchestrator-test-"))
        self.events: list[dict] = []

    def make(
        self,
        runner,
        *,
        models: ModelRegistry | None = None,
        completer=None,
        verify: bool = False,
        max_parallel: int = 2,
        default_attempts: int = 1,
        ownership=None,
        capabilities: CapabilityRegistry | None = None,
        preferences: dict[str, str] | None = None,
        failure_cooldown: float = 90.0,
    ) -> AgentTeamOrchestrator:
        model_registry = models or registry()
        router = ModelRouter(
            model_registry,
            preferences=preferences or {},
            failure_cooldown=failure_cooldown,
        )
        return AgentTeamOrchestrator(
            settings=settings(),
            router=router,
            registry=ToolRegistry(),
            capabilities=capabilities,
            ownership=ownership,
            runner=runner,
            completer=completer,
            emit=self.events.append,
            report_dir=self.tmp,
            verify=verify,
            max_parallel=max_parallel,
            default_attempts=default_attempts,
            allow_fallback_plan=True,
        )


# ══════════════════════════════════════════════════════════════════════
# Planning
# ══════════════════════════════════════════════════════════════════════
class PlanParsingTests(unittest.TestCase):
    def test_tolerant_json_with_fences_and_steps(self) -> None:
        text = """Here is the plan:
```json
{"notes":"assume a Python repo","steps":[
  {"id":"scan","name":"Scan code","role":"architect","detail":"Map files",
   "depends_on":"", "paths":["src"], "done_when":"file list", "timeout_s":45},
  {"title":"Implement","agent":"developer","dependencies":["scan"],
   "scope":["src/app.py"], "max_attempts":3}
]}
```"""
        parsed = plan_from_json(text, "goal", default_timeout=120, default_attempts=2)
        self.assertEqual(parsed.goal, "goal")
        self.assertEqual(parsed.notes, "assume a Python repo")
        first, second = parsed.tasks
        self.assertEqual(first.id, "scan")
        self.assertEqual(first.title, "Scan code")
        self.assertEqual(first.role, "architect")
        self.assertEqual(first.scope, ("src",))
        self.assertEqual(first.timeout_s, 45.0)
        self.assertEqual(second.id, "t2")
        self.assertEqual(second.role, "developer")
        self.assertEqual(second.depends_on, ("scan",))
        self.assertEqual(second.max_attempts, 3)
        self.assertEqual(second.timeout_s, 120.0)

    def test_duplicate_ids_are_made_unique(self) -> None:
        parsed = plan_from_json(
            '{"tasks":[{"id":"t1","role":"writer"},{"id":"t1","role":"tester"}]}',
            "goal",
        )
        self.assertEqual([item.id for item in parsed.tasks], ["t1", "t2"])

    def test_missing_tasks_is_an_error(self) -> None:
        with self.assertRaises(OrchestrationError):
            plan_from_json('{"summary":"nothing"}', "goal")
        with self.assertRaises(OrchestrationError):
            plan_from_json('{"tasks":[]}', "goal")

    def test_unusable_tasks_are_an_error(self) -> None:
        with self.assertRaises(OrchestrationError):
            plan_from_json('{"tasks":[1,2,3]}', "goal")

    def test_unknown_dependency_is_rejected_before_execution(self) -> None:
        with self.assertRaises(ScheduleError):
            plan_from_json(
                '{"tasks":[{"id":"t1","depends_on":["ghost"]}]}', "goal"
            )

    def test_cycle_is_rejected_before_execution(self) -> None:
        with self.assertRaises(ScheduleError):
            plan_from_json(
                '{"tasks":[{"id":"a","depends_on":["b"]},{"id":"b","depends_on":["a"]}]}',
                "goal",
            )

    def test_task_count_is_capped(self) -> None:
        payload = {
            "tasks": [{"id": f"t{index}", "title": str(index)} for index in range(30)]
        }
        parsed = plan_from_json(json.dumps(payload), "goal", max_tasks=3)
        self.assertEqual([item.id for item in parsed.tasks], ["t0", "t1", "t2"])

    def test_scope_and_title_are_cleaned(self) -> None:
        parsed = plan_from_json(
            json.dumps(
                {
                    "tasks": [
                        {
                            "id": " t1 ",
                            "title": "  multi\nline   title ",
                            "scope": [" a.py ", "a.py", ""],
                        }
                    ]
                }
            ),
            "goal",
        )
        self.assertEqual(parsed.tasks[0].id, "t1")
        self.assertEqual(parsed.tasks[0].title, "multi line title")
        self.assertEqual(parsed.tasks[0].scope, ("a.py",))


class HeuristicPlanTests(unittest.TestCase):
    def test_research_and_verify_graph_is_ordered(self) -> None:
        parsed = heuristic_plan("research the auth flow and test the fix")
        self.assertTrue(parsed.fallback)
        self.assertEqual([item.role for item in parsed.tasks], ["researcher", "developer", "tester"])
        self.assertEqual(parsed.tasks[1].depends_on, ("t1",))
        self.assertEqual(parsed.tasks[2].depends_on, ("t1", "t2"))

    def test_trivial_goal_is_one_task(self) -> None:
        parsed = heuristic_plan("write a haiku about terminals")
        self.assertEqual(len(parsed.tasks), 1)
        self.assertEqual(parsed.tasks[0].role, "developer")
        self.assertIn("fallback", parsed.summary_text)

    def test_max_tasks_is_respected(self) -> None:
        parsed = heuristic_plan("research, build, test, verify everything", max_tasks=2)
        self.assertEqual(len(parsed.tasks), 2)


class PlannerCallTests(OrchestratorCase):
    async def test_model_plan_is_parsed_and_published(self) -> None:
        async def completer(prompt, model_id, **kwargs):
            self.assertIn("Capabilities actually registered", prompt)
            self.assertTrue(kwargs["json_mode"])
            return json.dumps(
                {
                    "notes": "two steps",
                    "tasks": [
                        {"id": "t1", "title": "Read", "role": "researcher"},
                        {"id": "t2", "title": "Write", "role": "writer", "depends_on": ["t1"]},
                    ],
                }
            )

        team = self.make(CallbackRunner(lambda request: "unused"), completer=completer)
        parsed = await team.plan_goal("document the module")
        self.assertEqual(len(parsed.tasks), 2)
        self.assertEqual(parsed.planner_model, "worker")
        self.assertFalse(parsed.fallback)
        self.assertEqual([event["kind"] for event in self.events], ["team_plan"])

    async def test_invalid_plan_is_retried_once_then_falls_back(self) -> None:
        calls = []

        async def completer(prompt, model_id, **kwargs):
            calls.append(prompt)
            return "not json at all"

        team = self.make(CallbackRunner(lambda request: "unused"), completer=completer)
        parsed = await team.plan_goal("write a haiku")
        self.assertEqual(len(calls), 2)
        self.assertIn("previous answer was invalid", calls[1])
        self.assertTrue(parsed.fallback)
        self.assertIn("planner error", parsed.notes)

    async def test_fallback_can_be_disabled(self) -> None:
        calls = []

        async def completer(prompt, model_id, **kwargs):
            calls.append(prompt)
            raise RuntimeError("provider down")

        team = self.make(CallbackRunner(lambda request: "unused"), completer=completer)
        team.allow_fallback_plan = False
        with self.assertRaises(OrchestrationError) as caught:
            await team.plan_goal("do the thing")
        self.assertIn("could not produce a usable plan", str(caught.exception))
        self.assertEqual(len(calls), 2)


# ══════════════════════════════════════════════════════════════════════
# Execution
# ══════════════════════════════════════════════════════════════════════
class ExecutionTests(OrchestratorCase):
    async def test_independent_tasks_run_in_parallel(self) -> None:
        class ParallelRunner:
            def __init__(self) -> None:
                self.active = 0
                self.peak = 0
                self.both = asyncio.Event()

            async def run(self, request):
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    if self.active >= 2:
                        self.both.set()
                    else:
                        await asyncio.wait_for(self.both.wait(), timeout=2.0)
                finally:
                    self.active -= 1
                return WorkerOutput(f"report for {request.task_id}")

        runner = ParallelRunner()
        team = self.make(runner, max_parallel=2)
        result = await team.run(
            plan=plan(task("t1", "researcher"), task("t2", "writer"), goal="parallel")
        )
        self.assertEqual(result.status, "done")
        self.assertEqual(runner.peak, 2)
        self.assertEqual({item.status for item in team.scheduler.all()}, {TASK_DONE})

    async def test_dependent_task_receives_upstream_report(self) -> None:
        outputs = {"t1": "T1 EVIDENCE: auth.py:42", "t2": "documented"}

        def callback(request):
            return WorkerOutput(outputs[request.task_id])

        runner = CallbackRunner(callback)
        team = self.make(runner)
        result = await team.run(
            plan=plan(
                task("t1", "researcher"),
                task("t2", "writer", depends_on=("t1",)),
            )
        )
        self.assertEqual(result.status, "done")
        self.assertEqual(runner.calls, ["t1", "t2"])
        self.assertIn("T1 EVIDENCE: auth.py:42", runner.requests[1].prompt)
        self.assertIn("(no upstream results)", runner.requests[0].prompt)

    async def test_retry_uses_remaining_attempts(self) -> None:
        class FlakyRunner:
            def __init__(self) -> None:
                self.calls = 0

            async def run(self, request):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient worker failure")
                return WorkerOutput("recovered report")

        runner = FlakyRunner()
        team = self.make(runner, default_attempts=2, failure_cooldown=0.0)
        result = await team.run(plan=plan(task("t1")))
        self.assertEqual(result.status, "done")
        self.assertEqual(runner.calls, 2)
        record = team.scheduler.get("t1")
        self.assertEqual(record.attempts, 2)
        self.assertEqual(record.result, "recovered report")
        self.assertEqual(record.error, "")
        self.assertEqual(team.router.health(), {})

    async def test_failed_model_is_cooled_down_and_retry_falls_back(self) -> None:
        class ModelAwareRunner:
            def __init__(self) -> None:
                self.models: list[str] = []

            async def run(self, request):
                self.models.append(request.model)
                if request.model == "primary":
                    raise RuntimeError("primary endpoint down")
                return WorkerOutput("fallback report")

        runner = ModelAwareRunner()
        models = registry(model("primary"), model("backup"))
        team = self.make(
            runner,
            models=models,
            default_attempts=2,
            preferences={"developer": "primary"},
        )
        result = await team.run(plan=plan(task("t1")))
        self.assertEqual(result.status, "done")
        self.assertEqual(runner.models, ["primary", "backup"])
        self.assertTrue(team.router.cooling("primary"))
        self.assertEqual(team.scheduler.get("t1").model, "backup")

    async def test_timeout_is_reported_and_bounded(self) -> None:
        class SlowRunner:
            async def run(self, request):
                await asyncio.sleep(0.6)
                return WorkerOutput("too late")

        team = self.make(SlowRunner(), default_attempts=1)
        result = await team.run(plan=plan(task("t1", timeout_s=0.1)))
        self.assertEqual(result.status, "failed")
        record = team.scheduler.get("t1")
        self.assertEqual(record.status, TASK_FAILED)
        self.assertIn("timed out", record.error)
        self.assertLess(result.elapsed, 5.0)

    async def test_empty_report_is_a_failure(self) -> None:
        runner = CallbackRunner(lambda request: WorkerOutput(""))
        team = self.make(runner, default_attempts=1)
        result = await team.run(plan=plan(task("t1")))
        self.assertEqual(result.status, "failed")
        self.assertIn("no report", team.scheduler.get("t1").error)

    async def test_missing_tool_capable_model_fails_without_running(self) -> None:
        called = []

        async def callback(request):
            called.append(request.task_id)
            return "should not run"

        models = registry(model("text-only", tools=False, reasoning=False, planning=False))
        team = self.make(CallbackRunner(callback), models=models)
        result = await team.run(plan=plan(task("t1", "developer")))
        self.assertEqual(called, [])
        self.assertEqual(result.status, "failed")
        self.assertIn("no model satisfies", team.scheduler.get("t1").error)

    async def test_metadata_without_tool_support_is_rejected_honestly(self) -> None:
        # A model can advertise the tools capability and still declare that the
        # adapter cannot execute tools; the orchestrator must not pretend.
        bogus = ModelSpec(
            id="bogus",
            capabilities=frozenset({CAP_TEXT, CAP_TOOLS}),
            supports_tools=False,
        )
        team = self.make(CallbackRunner(lambda request: "no"), models=registry(bogus))
        await team.run(plan=plan(task("t1", "developer")))
        self.assertIn("does not support tool calls", team.scheduler.get("t1").error)

    async def test_ownership_conflict_prevents_parallel_overwrite(self) -> None:
        class SlowRunner:
            async def run(self, request):
                await asyncio.sleep(0.05)
                return WorkerOutput(f"wrote {request.task_id}")

        team = self.make(SlowRunner(), max_parallel=2)
        result = await team.run(
            plan=plan(
                task("t1", "writer", scope=("shared.py",)),
                task("t2", "writer", scope=("shared.py",)),
            )
        )
        self.assertEqual(result.status, "partial")
        states = {item.id: item for item in team.scheduler.all()}
        self.assertEqual(states["t1"].status, TASK_DONE)
        self.assertEqual(states["t2"].status, TASK_FAILED)
        self.assertIn("ownership", states["t2"].error)
        self.assertIn("task:t1", states["t2"].error)
        self.assertEqual(team.ownership.leases(), {})

    async def test_scope_is_released_after_a_successful_task(self) -> None:
        runner = CallbackRunner(lambda request: WorkerOutput("done"))
        team = self.make(runner)
        await team.run(plan=plan(task("t1", "writer", scope=("notes.md",))))
        self.assertEqual(team.ownership.leases(), {})

    async def test_discovered_mcp_tools_are_offered_to_the_role(self) -> None:
        tool_registry = ToolRegistry()
        tool_registry.add(
            ToolSpec(
                name="mcp_demo_echo",
                description="echo",
                parameters={"type": "object", "properties": {}},
                handler=lambda arguments, ctx: "ok",
                category="mcp",
                source="mcp:demo",
                risk="write",
            )
        )
        capabilities = CapabilityRegistry.from_components(tool_registry)
        captured = {}

        def callback(request):
            captured["allowed"] = request.allowed_tools
            return WorkerOutput("used nothing")

        team = AgentTeamOrchestrator(
            settings=settings(),
            router=ModelRouter(registry()),
            registry=tool_registry,
            capabilities=capabilities,
            runner=CallbackRunner(callback),
            emit=self.events.append,
            report_dir=self.tmp,
            verify=False,
            max_parallel=1,
            default_attempts=1,
        )
        await team.run(plan=plan(task("t1", "developer")))
        self.assertIn("mcp_demo_echo", captured["allowed"])

    async def test_second_run_is_rejected_while_running(self) -> None:
        team = self.make(CallbackRunner(lambda request: "done"))
        team.running = True
        with self.assertRaises(OrchestrationError):
            await team.run(plan=plan(task("t1")))


# ══════════════════════════════════════════════════════════════════════
# Control: cancellation and pause/resume
# ══════════════════════════════════════════════════════════════════════
class ControlTests(OrchestratorCase):
    async def test_cancel_stops_workers_and_skips_verification(self) -> None:
        class BlockingRunner:
            def __init__(self) -> None:
                self.started = asyncio.Event()

            async def run(self, request):
                self.started.set()
                await asyncio.Event().wait()

        runner = BlockingRunner()
        team = self.make(runner, verify=True)
        running = asyncio.create_task(team.run(plan=plan(task("t1"))))
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        team.cancel()
        result = await asyncio.wait_for(running, timeout=5)
        self.assertEqual(result.status, "cancelled")
        self.assertIn("not run", result.verification)
        self.assertEqual(team.scheduler.get("t1").status, TASK_CANCELLED)
        self.assertEqual(team.manager.active_count, 0)

    async def test_pause_blocks_at_checkpoint_until_resume(self) -> None:
        class CheckpointRunner:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.proceed = asyncio.Event()

            async def run(self, request):
                self.started.set()
                await self.proceed.wait()
                await request.checkpoint()
                return WorkerOutput("finished after resume")

        runner = CheckpointRunner()
        team = self.make(runner)
        running = asyncio.create_task(team.run(plan=plan(task("t1"))))
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        self.assertEqual(team.pause(), 1)
        self.assertTrue(team.manager.globally_paused)
        runner.proceed.set()
        await asyncio.sleep(0.25)
        self.assertFalse(running.done())
        self.assertEqual(team.scheduler.get("t1").status, "running")

        self.assertEqual(team.resume(), 1)
        result = await asyncio.wait_for(running, timeout=5)
        self.assertEqual(result.status, "done")
        self.assertFalse(team.manager.globally_paused)


# ══════════════════════════════════════════════════════════════════════
# Verification and reports
# ══════════════════════════════════════════════════════════════════════
class VerificationTests(OrchestratorCase):
    async def test_passing_verdict_is_recorded(self) -> None:
        prompts = []

        async def completer(prompt, model_id, **kwargs):
            prompts.append(prompt)
            return json.dumps({"verdict": "pass", "reason": "test output shown"})

        runner = CallbackRunner(lambda request: WorkerOutput("ran tests: 12 passed"))
        team = self.make(runner, completer=completer, verify=True)
        result = await team.run(plan=plan(task("t1", "tester")))
        self.assertEqual(result.status, "done")
        self.assertTrue(result.verified)
        self.assertIn("test output shown", result.verification)
        self.assertIn("ran tests: 12 passed", prompts[0])

    async def test_failing_verdict_downgrades_status_to_partial(self) -> None:
        async def completer(prompt, model_id, **kwargs):
            return '{"verdict":"fail","reason":"no command output"}'

        runner = CallbackRunner(lambda request: WorkerOutput("I think it works"))
        team = self.make(runner, completer=completer, verify=True)
        result = await team.run(plan=plan(task("t1")))
        self.assertEqual(result.status, "partial")
        self.assertFalse(result.verified)
        self.assertIn("no command output", result.verification)

    async def test_verifier_error_does_not_fake_a_pass(self) -> None:
        async def completer(prompt, model_id, **kwargs):
            raise RuntimeError("verifier endpoint down")

        runner = CallbackRunner(lambda request: WorkerOutput("report"))
        team = self.make(runner, completer=completer, verify=True)
        result = await team.run(plan=plan(task("t1")))
        self.assertEqual(result.status, "done")
        self.assertFalse(result.verified)
        self.assertIn("not run", result.verification)

    async def test_verification_can_be_disabled(self) -> None:
        runner = CallbackRunner(lambda request: WorkerOutput("report"))
        team = self.make(runner, verify=False)
        result = await team.run(plan=plan(task("t1")))
        self.assertEqual(result.verification, "verification disabled")
        self.assertFalse(result.verified)

    async def test_report_files_written_from_real_state(self) -> None:
        runner = CallbackRunner(lambda request: WorkerOutput("report body"))
        team = self.make(runner, verify=False)
        result = await team.run(plan=plan(task("t1", "writer", scope=("doc.md",)), goal="write docs"))

        markdown = Path(result.report_path)
        self.assertTrue(markdown.exists())
        self.assertEqual(markdown.suffix, ".md")
        payload = json.loads(markdown.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "done")
        self.assertIn("ownership", payload)
        self.assertIn("agents", payload)
        self.assertEqual(payload["agents"][0]["state"], "done")

        body = markdown.read_text(encoding="utf-8")
        self.assertIn("write docs", body)
        self.assertIn("## Honest limits", body)
        self.assertIn("not a substitute for tests", body)
        self.assertEqual(result.tasks[0]["status"], "done")

    async def test_events_include_plan_start_tasks_and_result(self) -> None:
        runner = CallbackRunner(lambda request: WorkerOutput("report"))
        team = self.make(runner)
        await team.run(plan=plan(task("t1")))
        kinds = [event["kind"] for event in self.events]
        self.assertIn("team_start", kinds)
        self.assertIn("team_task", kinds)
        self.assertEqual(kinds[-1], "team_done")
        self.assertEqual(self.events[-1]["result"]["status"], "done")

    async def test_snapshot_reports_live_state(self) -> None:
        runner = CallbackRunner(lambda request: WorkerOutput("report"))
        team = self.make(runner)
        await team.run(plan=plan(task("t1")))
        snapshot = team.snapshot()
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["progress"], 1.0)
        self.assertEqual(snapshot["agents"][0]["state"], "done")
        self.assertIn("0 active lease", snapshot["ownership"])


if __name__ == "__main__":
    unittest.main()
