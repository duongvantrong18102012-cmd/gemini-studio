"""Supervisor + agent-team orchestration.

The supervisor plans a dependency graph, routes each task to a model by
capability, runs independent tasks in parallel under a fixed worker budget, and
records evidence for a verification pass. It is deliberately independent of
Textual: the UI subscribes through `emit`, and every external effect (model
call, tool execution) goes through injected collaborators.

Design rules that are enforced here, not merely documented:

* planner output is data, never authority — tasks still pass through the
  scheduler and every tool call still passes through `PermissionPolicy`;
* a task is only `done` when its worker returned normally;
* a failure is reported as a failure, with retries bounded;
* cancellation releases file leases and stops pending dependents;
* the report is written from real task state, including errors.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from ..config import TEAM_DIR
from ..tools.registry import ToolContext, ToolRegistry
from .capabilities import CapabilityRegistry, role_or_default, tools_for_role
from .manager import (
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_RUNNING,
    AgentManager,
)
from .models import CAP_TOOLS, ModelRouter, ModelSpec
from .ownership import FileOwnership, OwnershipConflict
from .scheduler import (
    ScheduleError,
    ScheduledTask,
    TASK_CANCELLED,
    TASK_DONE,
    TASK_FAILED,
    TASK_PENDING,
    TASK_SKIPPED,
    TaskScheduler,
)

MAX_TASKS = 8
MAX_CONCURRENCY = 4
PLAN_ATTEMPTS = 2
WORKER_MAX_STEPS = 12
VERIFY_MAX_CHARS = 9000
DEPENDENCY_RESULT_CHARS = 2500


class OrchestrationError(RuntimeError):
    """The team could not plan or execute; the message is user-facing."""


# ══════════════════════════════════════════════════════════════════════
# Data
# ══════════════════════════════════════════════════════════════════════
@dataclass
class WorkerOutput:
    text: str = ""
    tool_calls: int = 0
    artifacts: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class WorkerRequest:
    task_id: str
    title: str
    goal: str
    role: str
    model: str
    prompt: str
    system: str
    allowed_tools: tuple[str, ...]
    timeout: float
    checkpoint: Callable[[], Awaitable[None]]
    emit: Callable[[str, str], None]
    provider: str = "gemini"


@dataclass
class TeamPlan:
    goal: str
    tasks: list[ScheduledTask]
    notes: str = ""
    planner_model: str = ""
    fallback: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    created: float = field(default_factory=time.time)

    @property
    def summary_text(self) -> str:
        lines = [f"goal: {self.goal}"]
        for index, task in enumerate(self.tasks, 1):
            deps = f" ← {', '.join(task.depends_on)}" if task.depends_on else ""
            lines.append(f"{index}. [{task.role}] {task.title}{deps}")
        if self.notes:
            lines.append(f"notes: {self.notes}")
        if self.fallback:
            lines.append("planner: rule-based fallback (model planner unavailable)")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "notes": self.notes,
            "planner_model": self.planner_model,
            "fallback": self.fallback,
            "created": self.created,
            "tasks": [task.to_dict() for task in self.tasks],
        }


@dataclass
class TeamResult:
    plan: TeamPlan
    status: str = "failed"          # done | partial | failed | cancelled
    summary: str = ""
    verification: str = ""
    error: str = ""
    elapsed: float = 0.0
    tasks: list[dict[str, Any]] = field(default_factory=list)
    report_path: str = ""
    verified: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "done"

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "status": self.status,
            "summary": self.summary,
            "verification": self.verification,
            "verified": self.verified,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
            "tasks": self.tasks,
            "report_path": self.report_path,
        }


# ══════════════════════════════════════════════════════════════════════
# Planning
# ══════════════════════════════════════════════════════════════════════
PLANNER_SYSTEM = """You are the supervisor of a small engineering agent team.
Turn the user's goal into a dependency-aware task graph. Return JSON only.

Roles and what they may do:
- architect: read-only design, file map, risks.
- researcher: web and workspace evidence, citations. Read-only.
- planner: ordering and acceptance criteria. Read-only.
- developer: read/write/edit files, shell, tests, computer tools.
- debugger: reproduce and isolate failures, shell and edits.
- tester: write and run tests, report exact output.
- security: audit permissions, secrets, injection, destructive paths.
- writer: documentation only, no source edits.

Rules:
- 1 to %(max_tasks)d tasks. Prefer the smallest graph that can actually finish.
- Use "depends_on" task ids for real ordering only; independent tasks run in parallel.
- Every task must be verifiable. "acceptance" states the evidence required.
- "scope" lists workspace paths the task may write (empty for read-only tasks).
- One task should own a file; never let two parallel tasks write the same path.
- Do not invent tools. Use only tools from the capability list.
- If the request is trivial, return a single task.

Schema:
{"summary":"one line","notes":"assumptions/risks","tasks":[
  {"id":"t1","title":"...","role":"architect|researcher|planner|developer|debugger|tester|security|writer",
   "goal":"what to do, concrete","depends_on":[],"scope":["path"],"acceptance":"evidence required",
   "tools":["tool_name"],"timeout_s":300}
]}""" % {"max_tasks": MAX_TASKS}

VERIFIER_SYSTEM = """You are the independent verifier of an engineering agent team.
You receive the goal, the task results and the recorded file changes. Decide whether
the stated acceptance criteria are actually supported by evidence in the results.

Rules:
- Do not assume success. Quote the specific evidence for each completed task.
- Distinguish "passed" (evidence present) from "unverified" (no output shown).
- Fail the run if any task failed, if a file change was never tested, or if the answer
  contradicts the evidence.
- Be concise. Return JSON only:
{"verdict":"pass|fail|partial","reason":"2-4 sentences","followups":["..."]}"""


def _clean(text: str, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _task_ids(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        items = re.split(r"[,\s]+", raw)
    elif isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = []
    result: list[str] = []
    for item in items:
        value = item.strip().strip("`")
        if value and value not in result:
            result.append(value[:40])
    return tuple(result[:8])


def _scope(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = []
    cleaned = [item.strip() for item in items if str(item).strip()]
    return tuple(dict.fromkeys(cleaned))[:16]


def plan_from_json(
    text: str,
    goal: str,
    *,
    default_timeout: float = 300.0,
    default_attempts: int = 2,
    max_tasks: int = MAX_TASKS,
) -> TeamPlan:
    """Parse a model plan tolerantly, then validate it through the scheduler."""
    from ..agent.plan import extract_json

    payload = extract_json(text)
    raw_tasks = payload.get("tasks") or payload.get("steps") or []
    if isinstance(raw_tasks, dict):
        raw_tasks = list(raw_tasks.values())
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise OrchestrationError("The supervisor returned no tasks")

    tasks: list[ScheduledTask] = []
    for index, item in enumerate(raw_tasks[:max_tasks], 1):
        if isinstance(item, str):
            item = {"title": item, "goal": item}
        if not isinstance(item, dict):
            continue
        identifier = _clean(item.get("id") or f"t{index}", 40) or f"t{index}"
        if any(task.id == identifier for task in tasks):
            identifier = f"t{index}"
        role = role_or_default(str(item.get("role") or item.get("agent") or "developer"))
        title = _clean(item.get("title") or item.get("name") or f"Task {index}", 120)
        task_goal = str(item.get("goal") or item.get("detail") or item.get("description") or title)[:2000]
        tasks.append(
            ScheduledTask(
                id=identifier,
                title=title,
                role=role.key,
                goal=task_goal,
                depends_on=_task_ids(item.get("depends_on") or item.get("dependencies")),
                scope=_scope(item.get("scope") or item.get("paths")),
                acceptance=str(item.get("acceptance") or item.get("done_when") or "")[:1000],
                max_attempts=max(1, min(4, int(item.get("max_attempts") or default_attempts))),
                timeout_s=max(
                    10.0, min(3600.0, float(item.get("timeout_s") or default_timeout))
                ),
            )
        )
    if not tasks:
        raise OrchestrationError("Every task in the supervisor plan was unusable")
    # Reject unknown dependencies and cycles before execution.
    TaskScheduler(tasks, default_timeout=default_timeout, default_attempts=default_attempts)
    return TeamPlan(
        goal=goal,
        tasks=tasks,
        notes=str(payload.get("notes") or payload.get("risks") or "")[:2000],
        planner_model="",
    )


def heuristic_plan(goal: str, *, max_tasks: int = 3) -> TeamPlan:
    """Rule-based plan used only when no planner model can be reached.

    It is labelled `fallback=True` everywhere it is shown. The tasks are still
    executed for real; nothing about their result is pre-filled.
    """
    goal = _clean(goal, 4000)
    lowered = goal.lower()
    tasks: list[ScheduledTask] = []
    if re.search(r"\b(research|investigate|compare|find out|docs?|sources?)\b", lowered):
        tasks.append(
            ScheduledTask(
                id="t1",
                title="Gather evidence",
                role="researcher",
                goal=f"Collect concrete evidence for: {goal}",
                acceptance="Findings with URLs or file paths, no unsupported claims.",
            )
        )
    tasks.append(
        ScheduledTask(
            id=f"t{len(tasks) + 1}",
            title="Do the work",
            role="developer",
            goal=goal,
            depends_on=tuple(task.id for task in tasks),
            scope=(),
            acceptance="The requested change/output exists and is verified with a real command.",
        )
    )
    if re.search(r"\b(test|verify|check|lint|build|run)\b", lowered):
        # The verifier depends on every earlier task, including the developer
        # task. Depending only on the research step would let verification run
        # before the change exists.
        tasks.append(
            ScheduledTask(
                id=f"t{len(tasks) + 1}",
                title="Verify the result",
                role="tester",
                goal=f"Independently verify the outcome for: {goal}",
                depends_on=tuple(task.id for task in tasks),
                acceptance="Exact command, exit status and relevant output.",
            )
        )
    return TeamPlan(
        goal=goal,
        tasks=tasks[:max_tasks],
        notes="model planner unavailable — rule-based fallback graph",
        fallback=True,
    )


# ══════════════════════════════════════════════════════════════════════
# Default Gemini worker (tool loop through the existing service)
# ══════════════════════════════════════════════════════════════════════
class GeminiWorkerRunner:
    """Runs one task through `GeminiService.run_turn` with a scoped toolset."""

    def __init__(
        self,
        service: Any,
        registry: ToolRegistry,
        context_factory: Callable[[WorkerRequest], ToolContext],
        *,
        max_steps: int = WORKER_MAX_STEPS,
        model_registry: Any = None,
    ) -> None:
        self.service = service
        self.registry = registry
        self.context_factory = context_factory
        self.max_steps = max(1, int(max_steps))
        self.model_registry = model_registry

    def scoped_registry(self, allowed: Iterable[str]) -> ToolRegistry:
        scoped = ToolRegistry()
        for name in allowed:
            spec = self.registry.get(name)
            if spec is not None and spec.enabled:
                scoped.add(spec)
        return scoped

    async def run(self, request: WorkerRequest) -> WorkerOutput:
        if request.provider and request.provider != "gemini":
            adapter = (
                self.model_registry.provider(request.provider)
                if self.model_registry is not None
                else None
            )
            if adapter is None or not adapter.available():
                raise OrchestrationError(
                    f"Provider {request.provider!r} is not available for model {request.model}"
                )
            await request.checkpoint()
            text = await adapter.complete(
                request.prompt,
                request.model,
                system=request.system,
                timeout=request.timeout,
            )
            return WorkerOutput(text.strip(), 0)
        if not getattr(self.service, "has_key", False):
            raise OrchestrationError("No Gemini API key is configured for team workers")
        from google.genai import types

        registry = self.scoped_registry(request.allowed_tools)
        if not registry.enabled():
            # A text-only model can still reason; it just cannot use tools.
            pass
        ctx = self.context_factory(request)
        contents = [types.Content(role="user", parts=[types.Part(text=request.prompt)])]
        text_parts: list[str] = []
        tool_calls = 0
        usage: dict[str, int] = {}

        async for event in self.service.run_turn(
            contents,
            request.model,
            registry,
            ctx,
            max_steps=self.max_steps,
            system_suffix=request.system,
        ):
            await request.checkpoint()
            if event.kind == "text":
                text_parts.append(event.text)
            elif event.kind == "tool_start":
                tool_calls += 1
                name = str(event.data.get("name") or "")
                request.emit(f"tool {name}", "tool")
            elif event.kind == "tool_end":
                name = str(event.data.get("name") or "")
                ok = bool(event.data.get("ok"))
                request.emit(f"{'✔' if ok else '✖'} {name}", "tool" if ok else "error")
            elif event.kind == "usage":
                usage = {
                    key: int(event.data.get(key) or 0)
                    for key in ("prompt", "output", "total")
                }
            elif event.kind == "error":
                raise OrchestrationError(event.text or "The worker model failed")
        return WorkerOutput("".join(text_parts).strip(), tool_calls, usage=usage)


# ══════════════════════════════════════════════════════════════════════
# Orchestrator
# ══════════════════════════════════════════════════════════════════════
Emit = Callable[[dict[str, Any]], Any]


class AgentTeamOrchestrator:
    """Plan → schedule → execute → verify → report."""

    def __init__(
        self,
        *,
        settings: Any,
        router: ModelRouter,
        registry: ToolRegistry,
        capabilities: CapabilityRegistry | None = None,
        ownership: FileOwnership | None = None,
        manager: AgentManager | None = None,
        runner: Any = None,
        completer: Callable[..., Awaitable[str]] | None = None,
        emit: Emit | None = None,
        report_dir: Path | None = None,
        verify: bool = True,
        max_parallel: int = 2,
        default_timeout: float = 300.0,
        default_attempts: int = 2,
        allow_fallback_plan: bool = True,
    ) -> None:
        self.settings = settings
        self.router = router
        self.registry = registry
        self.capabilities = capabilities or CapabilityRegistry.from_components(registry)
        self.ownership = ownership or FileOwnership()
        self.manager = manager or AgentManager()
        self.runner = runner
        self.completer = completer
        self.emit = emit
        self.report_dir = Path(report_dir or TEAM_DIR)
        self.verify = bool(verify)
        self.max_parallel = max(1, min(MAX_CONCURRENCY, int(max_parallel)))
        self.default_timeout = max(10.0, float(default_timeout))
        self.default_attempts = max(1, min(4, int(default_attempts)))
        self.allow_fallback_plan = allow_fallback_plan

        self.plan: TeamPlan | None = None
        self.scheduler: TaskScheduler | None = None
        self.result: TeamResult | None = None
        self.running = False
        self._cancel_requested = False
        self._workers: dict[str, asyncio.Task[Any]] = {}
        self._worker_meta: dict[str, tuple[str, str]] = {}
        self._started_at = 0.0

    # ── helpers ───────────────────────────────────────────────────
    def _publish(self, kind: str, **payload: Any) -> None:
        if self.emit is None:
            return
        try:
            result = self.emit({"kind": kind, "time": time.time(), **payload})
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception:
            pass

    async def _complete(
        self,
        prompt: str,
        model: ModelSpec,
        *,
        system: str,
        json_mode: bool,
        timeout: float,
    ) -> str:
        if self.completer is not None:
            return await self.completer(
                prompt,
                model.id,
                system=system,
                json_mode=json_mode,
                timeout=timeout,
            )
        adapter = self.router.registry.provider(model.provider)
        if adapter is None:
            raise OrchestrationError(
                f"No provider adapter is registered for {model.provider!r}. "
                "Configure the provider in settings before using the team."
            )
        if not adapter.available():
            raise OrchestrationError(
                f"Provider {model.provider!r} is not configured. Set its endpoint and key first."
            )
        return await adapter.complete(
            prompt,
            model.id,
            system=system,
            temperature=0.1,
            json_mode=json_mode,
            timeout=timeout,
        )

    def _planner_model(self) -> ModelSpec:
        spec = self.router.route("planner")
        if spec is None:
            spec = self.router.route("developer")
        if spec is None:
            raise OrchestrationError(
                "No chat model is available. Add a Gemini API key or configure a provider."
            )
        return spec

    # ── planning ──────────────────────────────────────────────────
    async def plan_goal(self, goal: str) -> TeamPlan:
        goal = (goal or "").strip()
        if not goal:
            raise OrchestrationError("Usage: /team <goal>")
        model = self._planner_model()
        catalogue = self.capabilities.catalogue(include_mcp=True)
        prompt = (
            f"Goal:\n{goal}\n\n"
            f"Workspace: {getattr(self.settings, 'workspace', '.')}\n"
            f"Approval mode: {getattr(self.settings, 'approval_mode', 'ask')}\n"
            f"Max parallel workers: {self.max_parallel}\n\n"
            f"Capabilities actually registered right now:\n{catalogue or '(none)'}\n\n"
            "Return the JSON task graph."
        )
        errors: list[str] = []
        text = ""
        for attempt in range(PLAN_ATTEMPTS):
            try:
                text = await self._complete(
                    prompt if attempt == 0 else f"{prompt}\n\nYour previous answer was invalid: {errors[-1]}\nReturn JSON only.",
                    model,
                    system=PLANNER_SYSTEM,
                    json_mode=True,
                    timeout=min(180.0, self.default_timeout),
                )
                plan = plan_from_json(
                    text,
                    goal,
                    default_timeout=self.default_timeout,
                    default_attempts=self.default_attempts,
                )
                plan.planner_model = model.id
                self._publish("team_plan", plan=plan.to_dict(), summary=plan.summary_text)
                return plan
            except Exception as error:  # noqa: BLE001 - provider/API errors also fall back
                # Bad JSON, a missing task list, a dependency cycle and a dead
                # endpoint are all "no usable plan" from the caller's point of
                # view. CancelledError is BaseException, so cancellation still
                # propagates immediately.
                errors.append(str(error) or type(error).__name__)
                continue
        message = errors[-1] if errors else "planning failed"
        if self.allow_fallback_plan:
            plan = heuristic_plan(goal, max_tasks=MAX_TASKS)
            plan.notes = f"{plan.notes}; planner error: {_clean(message, 300)}"
            self._publish("team_plan", plan=plan.to_dict(), summary=plan.summary_text)
            return plan
        raise OrchestrationError(f"Supervisor could not produce a usable plan: {message}")

    # ── execution ─────────────────────────────────────────────────
    async def run(self, goal: str = "", plan: TeamPlan | None = None) -> TeamResult:
        if self.running:
            raise OrchestrationError("A team run is already in progress")
        self.running = True
        self._cancel_requested = False
        self._started_at = time.time()
        self.ownership.clear()
        self.manager.reset_cancellation()
        try:
            plan = plan or await self.plan_goal(goal)
            self.plan = plan
            self.scheduler = TaskScheduler(
                plan.tasks,
                max_parallel=self.max_parallel,
                default_timeout=self.default_timeout,
                default_attempts=self.default_attempts,
            )
            self._publish("team_start", goal=plan.goal, tasks=len(plan.tasks))
            await self._execute()
            result = await self._finalise()
            self.result = result
            self._publish("team_done", result=result.to_dict())
            return result
        except asyncio.CancelledError:
            self._cancel_requested = True
            for worker in list(self._workers.values()):
                if not worker.done():
                    worker.cancel()
            if self._workers:
                await asyncio.gather(*self._workers.values(), return_exceptions=True)
            for task_id, (agent_id, _) in list(self._worker_meta.items()):
                self.manager.finish(agent_id, "cancelled", "run cancelled")
                self.ownership.release_agent(f"task:{task_id}")
            self._workers.clear()
            self._worker_meta.clear()
            self.ownership.clear_leases()
            if self.scheduler is not None:
                self.scheduler.cancel_pending("cancelled")
            result = self.result or TeamResult(
                plan=self.plan or TeamPlan(goal=goal, tasks=[]),
                status="cancelled",
                summary="Run cancelled",
                elapsed=time.time() - self._started_at,
            )
            result.status = "cancelled"
            self.result = result
            self._publish("team_done", result=result.to_dict())
            raise
        except OrchestrationError:
            raise
        except Exception as error:
            raise OrchestrationError(f"Team run failed: {type(error).__name__}: {error}") from error
        finally:
            self.running = False
            self._workers.clear()
            self._worker_meta.clear()

    async def _execute(self) -> None:
        scheduler = self.scheduler
        assert scheduler is not None
        assert self.plan is not None

        while not scheduler.complete():
            if self._cancel_requested or self.manager.globally_cancelled:
                scheduler.cancel_pending("cancelled by user")
                break
            if self.manager.globally_paused:
                await asyncio.sleep(0.2)
                continue

            # Fill free slots with ready tasks, deterministically by id.
            for task in scheduler.ready():
                if scheduler.slots <= 0:
                    break
                if task.status != TASK_PENDING:
                    continue
                self._launch(task)

            if scheduler.running_count == 0:
                if scheduler.ready():
                    continue
                if not scheduler.complete():
                    # No runnable work and nothing running: dependency failure.
                    for task in scheduler.all():
                        if task.status == TASK_PENDING:
                            scheduler.mark_skipped(task.id, "dependency failed; task never ran")
                    break
                break

            pending = [worker for worker in self._workers.values() if not worker.done()]
            if pending:
                await asyncio.wait(pending, timeout=0.2, return_when=asyncio.FIRST_COMPLETED)
            self._reap()
            await asyncio.sleep(0)

        self._reap()
        # Let any late cancellations settle.
        if self._workers:
            await asyncio.gather(*self._workers.values(), return_exceptions=True)
            self._reap()

    def _launch(self, task: ScheduledTask) -> None:
        assert self.scheduler is not None
        assert self.plan is not None
        model = self.router.route(
            task.role,
            require=frozenset({CAP_TOOLS}) if task.role in ("developer", "tester", "debugger") else frozenset(),
        )
        if model is None:
            self.scheduler.fail_terminal(task.id, "no model satisfies this role's requirements")
            self._publish("team_task", task=task.to_dict(), status=task.status)
            return
        if task.role in ("developer", "tester", "debugger") and not model.supports_tools:
            self.scheduler.fail_terminal(
                task.id,
                f"{model.id} does not support tool calls; assign a tool-capable model to {task.role}",
            )
            self._publish("team_task", task=task.to_dict(), status=task.status)
            return

        # Pre-claim declared scope so two parallel tasks cannot both write it.
        claimed: list[str] = []
        try:
            for path in task.scope:
                self.ownership.check_write(path, f"task:{task.id}", task.id)
                claimed.append(path)
        except (OwnershipConflict, ValueError) as error:
            for path in claimed:
                self.ownership.release(path, f"task:{task.id}")
            self.scheduler.fail_terminal(task.id, f"file ownership: {error}")
            self._publish("team_task", task=task.to_dict(), status=task.status)
            return

        agent = self.manager.create(
            task.role, model.id, task_id=task.id, task_title=task.title
        )
        self.manager.set_state(agent.id, STATE_RUNNING)
        self.scheduler.mark_running(task.id, agent.id, model.id)
        request = self._build_request(task, agent.id, model)
        worker = asyncio.create_task(self._run_worker(task, agent.id, model, request))
        self._workers[task.id] = worker
        self._worker_meta[task.id] = (agent.id, model.id)
        self.manager.log(agent.id, f"started · {task.title} · {model.id}", "info")
        self._publish(
            "team_task",
            task=task.to_dict(),
            status=task.status,
            agent=agent.to_dict(),
            model=model.id,
        )

    def _build_request(self, task: ScheduledTask, agent_id: str, model: ModelSpec) -> WorkerRequest:
        assert self.plan is not None
        role = role_or_default(task.role)
        available = {
            spec.name for spec in self.registry.enabled()
        }
        allowed = set(tools_for_role(task.role, available))
        # Discovered MCP tools are not in the static role tool sets, but the
        # capability registry assigns them to roles. Only include names that
        # are actually registered and enabled right now.
        for item in self.capabilities.for_role(task.role, kind="tool"):
            if item.enabled and item.name in available:
                allowed.add(item.name)
        allowed = sorted(allowed)
        deps: list[str] = []
        if self.scheduler is not None:
            for dep_id in task.depends_on:
                dep = self.scheduler.get(dep_id)
                if dep is not None and dep.result:
                    deps.append(f"### {dep.id} · {dep.title}\n{dep.result[:DEPENDENCY_RESULT_CHARS]}")
        context = "\n\n".join(deps) if deps else "(no upstream results)"
        scope = ", ".join(task.scope) if task.scope else "(none declared)"
        prompt = (
            f"Team goal:\n{self.plan.goal}\n\n"
            f"Your task ({task.id}): {task.title}\n"
            f"Role: {role.label}\n"
            f"Goal:\n{task.goal}\n\n"
            f"Depends on results:\n{context}\n\n"
            f"Write scope: {scope}\n"
            f"Acceptance evidence: {task.acceptance or '(not specified)'}\n"
            f"Tools available to you: {', '.join(allowed) or '(none — reason only)'}\n\n"
            f"Permissions: approval mode is {getattr(self.settings, 'approval_mode', 'ask')}. "
            "Write/execute actions may be denied or require the human. Do not bypass that.\n"
            "Finish with a short report: what changed, the exact verification command, "
            "its output/status, and anything still unverified."
        )
        system = (
            "You are one worker in a supervised agent team. Work only on your assigned task "
            "and declared scope. Read before writing. Never claim a test passed without its "
            "output. If a tool is denied, stop and report why. Do not touch files owned by "
            "another worker.\n\n" + role.system_suffix
        )
        return WorkerRequest(
            task_id=task.id,
            title=task.title,
            goal=task.goal,
            role=task.role,
            model=model.id,
            prompt=prompt,
            system=system,
            allowed_tools=tuple(allowed),
            timeout=task.timeout_s,
            checkpoint=lambda: self._checkpoint(agent_id),
            emit=lambda line, level="info": self._worker_log(agent_id, line, level),
            provider=model.provider,
        )

    async def _run_worker(
        self, task: ScheduledTask, agent_id: str, model: ModelSpec, request: WorkerRequest
    ) -> WorkerOutput:
        assert self.scheduler is not None
        try:
            await self._checkpoint(agent_id)
            if self.runner is None:
                raise OrchestrationError("No worker runner is configured")
            output = await asyncio.wait_for(self.runner.run(request), timeout=task.timeout_s)
            if not isinstance(output, WorkerOutput):
                output = WorkerOutput(text=str(output or ""))
            self.manager.update(agent_id, tool_calls=output.tool_calls)
            self.manager.progress(agent_id, 0.9)
            return output
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise OrchestrationError(f"task timed out after {task.timeout_s:.0f}s")
        except OrchestrationError:
            raise
        except Exception as error:
            raise OrchestrationError(f"{type(error).__name__}: {error}") from error

    def _reap(self) -> None:
        assert self.scheduler is not None
        for task_id, worker in list(self._workers.items()):
            if not worker.done():
                continue
            self._workers.pop(task_id, None)
            agent_id, model_id = self._worker_meta.pop(task_id, ("", ""))
            task = self.scheduler.get(task_id)
            if task is None:
                continue
            if task.status == TASK_CANCELLED:
                if agent_id:
                    self.manager.finish(agent_id, STATE_CANCELLED, error="cancelled")
                self._release_scope(task)
                continue
            try:
                output = worker.result()
            except asyncio.CancelledError:
                if task.status not in (TASK_DONE, TASK_FAILED, TASK_SKIPPED):
                    self.scheduler.mark_skipped(task.id, "cancelled")
                if agent_id:
                    self.manager.finish(agent_id, STATE_CANCELLED, error="cancelled")
                self._release_scope(task)
                continue
            except Exception as error:  # noqa: BLE001 - report the real cause
                message = str(error) or type(error).__name__
                if model_id:
                    # Take the failing model out of rotation so a retry can be
                    # routed to a different capable model instead of hammering
                    # the same broken endpoint.
                    self.router.mark_failed(model_id, message)
                outcome = self.scheduler.mark_failed(task.id, message)
                if outcome == "retry":
                    if agent_id:
                        self.manager.finish(agent_id, STATE_FAILED, error=message)
                        self.manager.log(agent_id, f"retry scheduled · {message[:200]}", "warn")
                    self._release_scope(task)
                    self._publish(
                        "team_task",
                        task=task.to_dict(),
                        status="retry",
                        error=message,
                    )
                else:
                    if agent_id:
                        self.manager.finish(agent_id, STATE_FAILED, error=message)
                    self._release_scope(task)
                    self._publish("team_task", task=task.to_dict(), status=task.status)
                continue

            # A worker must produce some evidence; an empty answer is not success.
            text = (output.text or "").strip()
            if not text:
                outcome = self.scheduler.mark_failed(task.id, "worker returned no report")
                if agent_id:
                    self.manager.finish(agent_id, STATE_FAILED, error="worker returned no report")
                self._release_scope(task)
                if outcome != "retry":
                    self._publish("team_task", task=task.to_dict(), status=task.status)
                continue

            artifacts = sorted(set(output.artifacts) | set(self._task_changes(task.id)))
            if model_id:
                self.router.mark_ok(model_id)
            self.scheduler.mark_done(task.id, text, artifacts)
            if agent_id:
                self.manager.update(agent_id, tool_calls=output.tool_calls)
                self.manager.finish(agent_id, STATE_DONE, summary=text[:1500])
            self._release_scope(task)
            self.ownership.checkpoint(f"{task.id} done", list(task.scope))
            self._publish(
                "team_task",
                task=task.to_dict(),
                status=task.status,
                artifacts=artifacts,
            )

    def _task_changes(self, task_id: str) -> list[str]:
        return [row.path for row in self.ownership.changes(limit=200, task=task_id) if row.path]

    def _release_scope(self, task: ScheduledTask) -> None:
        for path in task.scope:
            self.ownership.release(path, f"task:{task.id}")
        self.ownership.release_agent(f"task:{task.id}")

    async def _checkpoint(self, agent_id: str) -> None:
        if self._cancel_requested:
            raise asyncio.CancelledError("cancelled by user")
        # Blocks while paused, raises when cancel was requested.
        await self.manager.wait_ready(agent_id)

    def _worker_log(self, agent_id: str, line: str, level: str = "info") -> None:
        self.manager.log(agent_id, line, level)
        self._publish("team_agent_log", agent_id=agent_id, line=line, level=level)

    # ── verification and report ───────────────────────────────────
    async def _verify(self, result: TeamResult) -> None:
        scheduler = self.scheduler
        if scheduler is None or not self.verify:
            result.verification = "verification disabled"
            return
        if result.status == "cancelled":
            # A cancelled run must not spend another model call, and a model
            # cannot verify work that was stopped.
            result.verification = "not run: run cancelled"
            return
        failed = scheduler.failed_count + scheduler.skipped_count
        evidence_lines: list[str] = []
        for task in scheduler.all():
            evidence_lines.append(
                f"### {task.id} · {task.title} [{task.role}] — {task.status}\n"
                f"acceptance: {task.acceptance or '(none)'}\n"
                f"result: {(task.result or task.error or '(no output)')[:3000]}"
            )
        evidence = "\n\n".join(evidence_lines)[:VERIFY_MAX_CHARS]
        changes = self.ownership.changes(limit=60)
        change_text = "\n".join(
            f"- {row.path} · {row.agent} · {row.task}" for row in changes
        ) or "(no recorded file changes)"
        prompt = (
            f"Goal:\n{self.plan.goal if self.plan else ''}\n\n"
            f"Failed or skipped tasks: {failed}\n\n"
            f"Task evidence:\n{evidence}\n\n"
            f"Recorded file changes:\n{change_text}"
        )
        model = self.router.route("security") or self.router.route("planner")
        if model is None:
            result.verification = "not run: no verifier model available"
            return
        try:
            text = await self._complete(
                prompt, model, system=VERIFIER_SYSTEM, json_mode=True, timeout=180.0
            )
            result.verification = text.strip()[:4000]
            parsed = _extract_verdict(text)
            result.verified = parsed == "pass"
            if parsed in ("fail", "partial") and failed == 0:
                result.status = "partial"
        except Exception as error:
            result.verification = f"not run: {type(error).__name__}: {error}"
            result.verified = False

    async def _finalise(self) -> TeamResult:
        assert self.scheduler is not None
        assert self.plan is not None
        scheduler = self.scheduler
        result = TeamResult(
            plan=self.plan,
            tasks=[task.to_dict() for task in scheduler.all()],
            elapsed=time.time() - self._started_at,
        )
        done = scheduler.done_count
        failed = scheduler.failed_count
        skipped = scheduler.skipped_count
        cancelled = sum(1 for task in scheduler.all() if task.status == TASK_CANCELLED)
        if cancelled or self._cancel_requested:
            result.status = "cancelled"
            result.error = "run cancelled"
        elif done == scheduler.total:
            result.status = "done"
        elif done:
            result.status = "partial"
            result.error = f"{failed + skipped} task(s) failed or skipped"
        else:
            result.status = "failed"
            result.error = "no task completed"

        await self._verify(result)

        lines = [f"TEAM · {result.status.upper()} · {done}/{scheduler.total} done"]
        if result.error:
            lines.append(result.error)
        for task in scheduler.all():
            glyph = {
                TASK_DONE: "✔",
                TASK_FAILED: "✖",
                TASK_SKIPPED: "→",
                TASK_CANCELLED: "■",
            }.get(task.status, "○")
            lines.append(f"{glyph} {task.id} {task.title} [{task.role}] · {task.attempts} attempt(s)")
            if task.error:
                lines.append(f"   {task.error[:220]}")
        if result.verification:
            lines.append("")
            lines.append("VERIFY: " + result.verification[:1500])
        result.summary = "\n".join(lines)[:6000]

        try:
            result.report_path = str(self._write_report(result))
        except OSError as error:
            result.summary += f"\n(report not written: {error})"
        return result

    def _write_report(self, result: TeamResult) -> Path:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        payload = result.to_dict()
        payload["ownership"] = {
            "leases": self.ownership.leases(),
            "changes": [row.__dict__ for row in self.ownership.changes(limit=200)],
            "checkpoints": self.ownership.checkpoints(),
        }
        payload["agents"] = self.manager.snapshot()
        stem = f"team-{time.strftime('%Y%m%d-%H%M%S')}-{result.plan.id}"
        json_path = self.report_dir / f"{stem}.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [
            f"# Agent Team run — {result.status}",
            "",
            f"**Goal:** {result.plan.goal}",
            "",
            f"- Plan: `{result.plan.id}`",
            f"- Planner model: `{result.plan.planner_model or 'fallback'}`",
            f"- Verification: `{'pass' if result.verified else 'not verified'}`",
            f"- Elapsed: {result.elapsed:.1f}s",
            "",
            "## Tasks",
            "",
        ]
        for task in self.scheduler.all() if self.scheduler else []:
            lines.append(f"### {task.id} · {task.title}")
            lines.append(f"- role: `{task.role}` · model: `{task.model or '-'}` · state: **{task.status}**")
            lines.append(f"- attempts: {task.attempts}/{task.max_attempts} · elapsed: {task.elapsed:.1f}s")
            if task.acceptance:
                lines.append(f"- acceptance: {task.acceptance}")
            if task.error:
                lines.append(f"- error: {task.error}")
            if task.result:
                lines.append("")
                lines.append(task.result[:4000])
            lines.append("")
        if result.verification:
            lines.extend(["## Verification", "", result.verification, ""])
        changes = self.ownership.changes(limit=100)
        if changes:
            lines.extend(["## File changes", ""])
            lines.extend(f"- `{row.path}` — {row.agent} ({row.task})" for row in changes)
            lines.append("")
        lines.extend(
            [
                "## Honest limits",
                "",
                "- Task success means the worker returned a report; check its evidence before trusting it.",
                "- Verification is a model review of recorded evidence, not a substitute for tests.",
                "- This run did not publish, commit or push anything.",
                "",
            ]
        )
        md_path = self.report_dir / f"{stem}.md"
        md_path.write_text("\n".join(lines), encoding="utf-8")
        return md_path

    # ── control ───────────────────────────────────────────────────
    def pause(self) -> int:
        return self.manager.pause_all()

    def resume(self) -> int:
        return self.manager.resume_all()

    def cancel(self) -> None:
        self._cancel_requested = True
        self.manager.cancel_all()
        if self.scheduler is not None:
            self.scheduler.cancel_pending("cancelled by user")
        for worker in self._workers.values():
            if not worker.done():
                worker.cancel()

    def status_text(self) -> str:
        if self.scheduler is None:
            return "team: idle"
        return f"team: {self.scheduler.summary_text}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "plan": self.plan.to_dict() if self.plan else None,
            "progress": self.scheduler.progress if self.scheduler else 0.0,
            "agents": self.manager.snapshot(),
            "ownership": self.ownership.summary(),
        }


def _extract_verdict(text: str) -> str:
    try:
        from ..agent.plan import extract_json

        payload = extract_json(text)
        verdict = str(payload.get("verdict") or "").strip().lower()
        if verdict in ("pass", "fail", "partial"):
            return verdict
    except Exception:
        pass
    match = re.search(r"\b(pass|fail|partial)\b", (text or "").lower())
    return match.group(1) if match else ""
