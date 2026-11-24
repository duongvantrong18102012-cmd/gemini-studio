"""Gemini Cowork: plan → scoped worker agents → verification.

The orchestrator is deliberately independent from Textual and from the Gemini
SDK at construction time. Planner/worker/verifier are injectable, which is what
the unit tests use to exercise the real orchestration path without the network.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import COWORK_DIR, Settings, system_prompt
from ..tools.registry import ToolContext, ToolError, ToolRegistry, ToolSpec
from .audit import AuditLog
from .context import ContextManager, Retriever, build_context_block, estimate_tokens, load_project_brief
from .events import AgentEvent
from .memory import MemoryStore
from .plan import PlanStep, TaskPlan, plan_from_model_json

EventSink = Callable[[AgentEvent], Any]

PLANNER_SYSTEM = """You are the planning lead of Gemini Cowork, a desktop agent on Windows.
Split the user's request into the smallest number of concrete steps that can be executed and verified.
Return ONLY JSON with this shape:
{"goal": "...", "notes": "...", "steps": [
  {"title": "short imperative", "goal": "exact outcome and acceptance check",
   "agent": "code|computer|research|review|general",
   "tools": ["tool_name", "..."], "risk": "read|write|execute|computer"}
]}
Rules:
- 2 to 8 steps. No filler steps. Every step must have a verifiable outcome.
- Use agent "computer" only for GUI work; "code" for files/shell; "research" for web; "review" for checking.
- List only tools you actually need, from the provided catalogue.
- Do not plan secrets handling or destructive operations without a verification step.
"""

WORKER_RULES = """You are a worker agent inside Gemini Cowork. Execute exactly one plan step.

Rules:
- Work only on this step. Do not expand the scope.
- Read before writing. Verify with a tool whenever possible.
- Do not ask the user questions; make the smallest reasonable decision and state it.
- If the step cannot be completed, stop and write BLOCKED: <reason>.
- End with:
  RESULT: <what changed, with file paths and exact verification evidence>
"""

VERIFIER_SYSTEM = """You are the verifier of Gemini Cowork.
Given a task, its plan and the workers' results, judge whether the outcome is complete.
Return ONLY JSON: {"status": "ok|issues", "summary": "...", "followups": ["..."]}
Be strict and factual. Do not claim success without evidence in the results.
"""

EXCLUDED_WORKER_TOOLS = {
    "cowork_run",
    "cowork_status",
    "cowork_plans",
    "computer_task",
    "opencode_run",
    "skill_list",
}

ROLE_TOOLS = {
    "code": {
        "fs_read", "fs_list", "fs_glob", "fs_grep", "fs_write", "fs_edit",
        "shell_run", "git_status", "git_diff", "git_log",
        "code_index", "code_search", "code_outline", "code_pack", "code_review", "code_test",
        "memory_save", "memory_search", "memory_list", "calc", "time_now",
    },
    "review": {
        "fs_read", "fs_glob", "fs_grep", "git_diff", "git_status", "git_log",
        "code_search", "code_outline", "code_review", "code_test", "shell_run",
        "memory_search", "memory_save",
    },
    "research": {
        "web_search", "web_fetch", "fs_read", "fs_write", "memory_save",
        "memory_search", "memory_list", "time_now",
    },
    "computer": {
        "computer_status", "computer_screenshot", "computer_windows", "computer_focus",
        "computer_click", "computer_move", "computer_drag", "computer_type",
        "computer_key", "computer_scroll", "computer_open", "computer_wait",
        "memory_save", "memory_search", "time_now", "fs_read",
    },
    "general": {
        "fs_read", "fs_list", "fs_glob", "fs_grep", "fs_write", "fs_edit",
        "shell_run", "git_status", "git_diff", "git_log", "web_search", "web_fetch",
        "code_index", "code_search", "code_outline", "code_review", "code_test",
        "memory_save", "memory_search", "memory_list", "todo_write", "time_now",
    },
}


class ScopedRegistry:
    """A read-only view over ToolRegistry that only exposes selected tools."""

    def __init__(self, registry: ToolRegistry, names: set[str] | None) -> None:
        self.base = registry
        self.names = names
        self._specs: dict[str, ToolSpec] = {}
        for spec in registry.all():
            if names is not None and spec.name not in names:
                continue
            self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def all(self) -> list[ToolSpec]:
        return sorted(self._specs.values(), key=lambda spec: (spec.category, spec.name))

    def enabled(self) -> list[ToolSpec]:
        return [spec for spec in self.all() if spec.enabled]

    def declarations(self) -> list[Any]:
        declarations = []
        for spec in self.enabled():
            try:
                declarations.append(spec.declaration())
            except Exception:
                continue
        return declarations

    @property
    def dangerous_names(self) -> set[str]:
        return {spec.name for spec in self._specs.values() if spec.dangerous}

    async def call(self, name: str, arguments: dict[str, Any] | None, ctx: ToolContext) -> str:
        if name not in self._specs:
            raise ToolError(f"Tool `{name}` is not available to this worker")
        return await self.base.call(name, arguments, ctx)

    def catalogue(self, limit: int = 40) -> str:
        rows = []
        for spec in self.enabled()[:limit]:
            rows.append(f"- {spec.name}: {spec.description.splitlines()[0][:110]}")
        return "\n".join(rows)


@dataclass
class WorkerOutput:
    text: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    blocked: bool = False

    @property
    def result_text(self) -> str:
        text = self.text.strip()
        marker = text.upper().rfind("RESULT:")
        if marker >= 0:
            return text[marker + 7 :].strip()[:4000]
        return text[:4000]


@dataclass
class CoworkResult:
    plan: TaskPlan
    status: str = "done"
    summary: str = ""
    report_path: str = ""
    elapsed: float = 0.0
    error: str = ""

    def render(self, limit: int = 4000) -> str:
        body = self.plan.summary_text(limit)
        if self.summary:
            body += "\n\n" + self.summary[:2000]
        if self.error:
            body += "\n\nERROR: " + self.error[:1000]
        if self.report_path:
            body += f"\n\nReport: {self.report_path}"
        return body


class CoworkOrchestrator:
    def __init__(
        self,
        service: Any,
        registry: ToolRegistry,
        settings: Settings,
        workspace: str,
        *,
        memory: MemoryStore | None = None,
        retriever: Retriever | None = None,
        audit: AuditLog | None = None,
        emit: EventSink | None = None,
        planner: Callable[[str, str], Awaitable[TaskPlan]] | None = None,
        worker: Callable[[PlanStep, TaskPlan, str], Awaitable[WorkerOutput]] | None = None,
        verifier: Callable[[TaskPlan], Awaitable[tuple[str, str]]] | None = None,
        computer_driver_factory: Callable[[ToolContext], Any] | None = None,
        plan_store: Any = None,
    ) -> None:
        self.service = service
        self.registry = registry
        self.settings = settings
        self.workspace = str(workspace)
        self.memory = memory
        self.retriever = retriever
        self.audit = audit
        self._emit = emit
        self.planner = planner
        self.worker = worker
        self.verifier = verifier
        self.computer_driver_factory = computer_driver_factory
        from .plan import PlanStore

        self.plan_store = plan_store or PlanStore(COWORK_DIR)
        self.context = ContextManager(
            int(getattr(settings, "context_budget_tokens", 120_000) or 120_000)
        )
        self.current: TaskPlan | None = None
        self._cancelled = False
        self.last_result: CoworkResult | None = None

    # ── events ────────────────────────────────────────────────────
    async def _event(self, kind: str, text: str = "", *, agent: str = "lead", step: str = "", **data: Any) -> None:
        if self._emit is None:
            return
        event = AgentEvent(kind=kind, text=text, agent=agent, step_id=step, data=data)
        try:
            result = self._emit(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    def cancel(self) -> None:
        self._cancelled = True

    # ── context building ──────────────────────────────────────────
    async def build_context(self, task: str, *, budget: int = 5000) -> str:
        brief = load_project_brief(self.workspace)
        memory_digest = self.memory.digest(task, limit=6, max_chars=1200) if self.memory else ""
        retrieval = ""
        if self.retriever is not None:
            try:
                result = await self.retriever.retrieve(task, limit=4, max_chars=2800)
                retrieval = result.block
            except Exception:
                retrieval = ""
        fingerprint = ""
        index = getattr(self.retriever, "index", None) if self.retriever is not None else None
        if index is not None and getattr(index, "files", None):
            fingerprint = index.summary(700)
        return build_context_block(
            brief=brief,
            memory=memory_digest,
            retrieval=retrieval,
            fingerprint=fingerprint,
            max_chars=budget,
        )

    # ── planner ───────────────────────────────────────────────────
    async def make_plan(self, task: str, context: str = "") -> TaskPlan:
        if self.planner is not None:
            plan = await self.planner(task, context)
        else:
            plan = await self._model_plan(task, context)
        if not plan.steps:
            raise ValueError("The planner returned an empty plan")
        plan.model = plan.model or str(getattr(self.settings, "model", ""))
        self.current = plan
        return plan

    async def _model_plan(self, task: str, context: str) -> TaskPlan:
        from google.genai import types

        if self.service is None:
            raise RuntimeError("No Gemini service is configured")
        catalogue = ScopedRegistry(self.registry, None).catalogue(60)
        prompt = (
            f"TASK:\n{task}\n\n"
            f"CONTEXT:\n{context or '(none)'}\n\n"
            f"TOOL CATALOGUE:\n{catalogue}\n\n"
            f"Return the JSON plan now. Maximum {int(getattr(self.settings, 'cowork_max_steps', 8) or 8)} steps."
        )
        config = types.GenerateContentConfig(
            system_instruction=PLANNER_SYSTEM,
            temperature=0.2,
            max_output_tokens=2000,
            response_mime_type="application/json",
        )
        response = await asyncio.wait_for(
            self.service.client().aio.models.generate_content(
                model=str(getattr(self.settings, "model", "")),
                contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                config=config,
            ),
            timeout=120.0,
        )
        text = getattr(response, "text", "") or ""
        plan = plan_from_model_json(
            text,
            task,
            max_steps=int(getattr(self.settings, "cowork_max_steps", 8) or 8),
            model=str(getattr(self.settings, "model", "")),
        )
        plan.context_notes = context[:4000]
        return plan

    # ── worker selection ──────────────────────────────────────────
    def worker_registry(self, step: PlanStep) -> ScopedRegistry:
        available = {spec.name for spec in self.registry.all() if spec.enabled}
        role = ROLE_TOOLS.get(step.agent, ROLE_TOOLS["general"])
        requested = {name for name in step.tools if name in available}
        # The role is authoritative: a planner may narrow the tool set but never
        # widen it beyond what the worker's role is allowed to use.
        names = (requested & role) if step.tools else role
        names = {name for name in names if name in available} - EXCLUDED_WORKER_TOOLS
        if step.agent == "computer" and not getattr(self.settings, "computer_enabled", False):
            names = names - {name for name in names if name.startswith("computer_")}
        return ScopedRegistry(self.registry, names)

    async def _default_worker(self, step: PlanStep, plan: TaskPlan, context: str) -> WorkerOutput:
        if step.agent == "computer":
            return await self._computer_worker(step, plan, context)
        if self.service is None:
            raise RuntimeError("No Gemini service is configured")

        scoped = self.worker_registry(step)
        if not scoped.enabled():
            return WorkerOutput(ok=False, error="No tools available for this step", blocked=True)

        digest = "\n".join(
            f"- {other.title}: {other.summary[:300]}"
            for other in plan.steps
            if other.status == "done" and other.id != step.id
        )
        worker_prompt = (
            f"WORKSPACE: {self.workspace}\n"
            f"PLAN: {plan.task}\n"
            f"STEP {plan.steps.index(step) + 1}/{len(plan.steps)} [{step.agent}]: {step.title}\n"
            f"GOAL: {step.goal}\n\n"
            f"AVAILABLE TOOLS:\n{scoped.catalogue()}\n\n"
            f"CONTEXT:\n{context or '(none)'}\n\n"
            f"PREVIOUS RESULTS:\n{digest or '(none)'}\n\n"
            "Execute the step now."
        )
        contents: list[Any] = [self._user_content(worker_prompt)]
        ctx = self._worker_ctx(step)
        text_parts: list[str] = []
        tools: list[dict[str, Any]] = []
        errors: list[str] = []

        max_steps = max(1, int(getattr(self.settings, "cowork_step_tool_steps", 10) or 10))
        worker_system = f"{system_prompt(self.settings)}\n\n{WORKER_RULES}\nRole: {step.agent}."
        async for event in self.service.run_turn(
            contents,
            str(getattr(self.settings, "model", "")),
            scoped,
            ctx,
            max_steps=max_steps,
            system_suffix=worker_system,
        ):
            if self._cancelled:
                raise asyncio.CancelledError
            if event.kind == "text":
                text_parts.append(event.text)
                await self._event("step_text", event.text, agent=step.agent, step=step.id, worker=step.id)
            elif event.kind == "tool_start":
                await self._event(
                    "step_tool",
                    f"{event.data.get('name')}",
                    agent=step.agent,
                    step=step.id,
                    phase="start",
                    **event.data,
                )
            elif event.kind == "tool_end":
                tools.append(
                    {
                        "id": event.data.get("id"),
                        "name": event.data.get("name"),
                        "args": event.data.get("args", {}),
                        "ok": event.data.get("ok"),
                        "output": event.data.get("output", "")[:4000],
                        "elapsed": event.data.get("elapsed"),
                    }
                )
                await self._event(
                    "step_tool",
                    str(event.data.get("output", ""))[:600],
                    agent=step.agent,
                    step=step.id,
                    phase="end",
                    **{key: value for key, value in event.data.items() if key != "output"},
                )
            elif event.kind == "error":
                errors.append(event.text)
                await self._event("notice", event.text, agent=step.agent, step=step.id, severity="error")

        text = "".join(text_parts)
        blocked = "BLOCKED:" in text.upper()
        ok = not errors and not blocked
        return WorkerOutput(text=text, tools=tools, ok=ok, error="; ".join(errors)[:1000], blocked=blocked)

    def _user_content(self, text: str) -> Any:
        from google.genai import types

        return types.Content(role="user", parts=[types.Part(text=text)])

    async def _computer_worker(self, step: PlanStep, plan: TaskPlan, context: str) -> WorkerOutput:
        if self.computer_driver_factory is None:
            return WorkerOutput(ok=False, error="Computer driver unavailable", blocked=True)
        if not getattr(self.settings, "computer_enabled", False):
            return WorkerOutput(
                ok=False,
                error="Computer control is disabled (enable with /computer enable)",
                blocked=True,
            )
        ctx = self._worker_ctx(step)

        def on_event(kind: str, text: str, data: dict[str, Any]) -> None:
            event = AgentEvent(
                kind="step_tool" if kind == "tool" else "step_text",
                text=text,
                agent="computer",
                step_id=step.id,
                data=data,
            )
            if self._emit is not None:
                try:
                    result = self._emit(event)
                    if inspect.isawaitable(result):
                        asyncio.create_task(result)
                except Exception:
                    pass

        driver = self.computer_driver_factory(ctx)
        try:
            result = await driver.run(
                step.goal or step.title,
                max_steps=int(getattr(self.settings, "computer_max_steps", 10) or 10),
            )
        except Exception as error:
            return WorkerOutput(ok=False, error=f"{type(error).__name__}: {error}", blocked=True)
        text = result.render()
        ok = result.status == "done"
        return WorkerOutput(text=text, ok=ok, error=result.error, blocked=result.status in ("blocked", "error"))

    def _worker_ctx(self, step: PlanStep) -> ToolContext:
        base = getattr(self, "base_ctx", None)
        extras = dict(getattr(base, "extras", {}) or {}) if base is not None else {}
        extras["agent"] = step.agent
        extras["step_id"] = step.id
        if base is None:
            return ToolContext(workspace=self.workspace, cwd=self.workspace, extras=extras)
        return replace(base, extras=extras)

    # ── verification ──────────────────────────────────────────────
    async def _default_verifier(self, plan: TaskPlan) -> tuple[str, str]:
        if self.service is None:
            return "unverified", "No service configured"
        summaries = "\n".join(
            f"{index}. {step.title} [{step.status}] {step.summary[:700]}"
            for index, step in enumerate(plan.steps, 1)
        )
        prompt = f"TASK:\n{plan.task}\n\nSTEP RESULTS:\n{summaries}\n\nVerify."
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=VERIFIER_SYSTEM,
            temperature=0.0,
            max_output_tokens=900,
            response_mime_type="application/json",
        )
        try:
            response = await self.service.client().aio.models.generate_content(
                model=str(getattr(self.settings, "model", "")),
                contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                config=config,
            )
            text = getattr(response, "text", "") or ""
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(text[start : end + 1])
                status = str(data.get("status", "ok")).lower()
                summary = str(data.get("summary", ""))
                followups = data.get("followups") or []
                if followups:
                    summary += "\nFollow-ups: " + "; ".join(str(item)[:200] for item in followups[:6])
                return status, summary[:2000]
        except Exception as error:
            return "unverified", f"Verifier call failed: {error}"
        return "unverified", "Verifier returned no verdict"

    # ── main entry ────────────────────────────────────────────────
    async def run(
        self,
        task: str,
        *,
        execute: bool = True,
        base_ctx: ToolContext | None = None,
    ) -> CoworkResult:
        started = time.perf_counter()
        self._cancelled = False
        self.base_ctx = base_ctx
        if base_ctx is not None and base_ctx.cwd:
            self.workspace = base_ctx.cwd
        result = CoworkResult(plan=TaskPlan(task=task))
        try:
            await self._event("status", "cowork: assembling context")
            context = await self.build_context(task)
            await self._event("status", "cowork: planning")
            plan = await self.make_plan(task, context)
            result.plan = plan
            plan.status = "running"
            self.plan_store.save(plan)
            await self._event(
                "plan",
                plan.summary_text(),
                data={"plan_id": plan.id, "steps": [step.to_dict() for step in plan.steps]},
            )
            if self.audit is not None:
                self.audit.record(
                    "agent",
                    agent="cowork",
                    tool="plan",
                    ok=True,
                    detail=f"{len(plan.steps)} steps for: {task[:300]}",
                    session=getattr(base_ctx, "session_id", "") if base_ctx else "",
                    args={"plan": plan.id},
                )

            if not execute:
                plan.status = "draft"
                self.plan_store.save(plan)
                result.status = "planned"
                result.summary = "Plan created (not executed)."
                result.elapsed = time.perf_counter() - started
                self.last_result = result
                await self._event("done", result.summary, data={"plan_id": plan.id, "executed": False})
                return result

            for index, step in enumerate(plan.steps, 1):
                if self._cancelled:
                    raise asyncio.CancelledError
                step.status = "active"
                step.attempts += 1
                step.started_at = time.time()
                self.plan_store.save(plan)
                await self._event(
                    "step_start",
                    step.title,
                    agent=step.agent,
                    step=step.id,
                    index=index,
                    total=len(plan.steps),
                )
                step_context = context
                if self.retriever is not None:
                    try:
                        retrieval = await self.retriever.retrieve(step.goal or step.title, limit=3, max_chars=2400)
                        if retrieval.block:
                            step_context = build_context_block(
                                brief=load_project_brief(self.workspace),
                                memory=self.memory.digest(step.goal, limit=4) if self.memory else "",
                                retrieval=retrieval.block,
                                max_chars=min(6000, len(context) + 2600),
                            )
                    except Exception:
                        pass

                try:
                    if self.worker is not None:
                        output = await self.worker(step, plan, step_context)
                    else:
                        output = await self._default_worker(step, plan, step_context)
                except asyncio.CancelledError:
                    step.status = "failed"
                    step.error = "cancelled"
                    plan.status = "cancelled"
                    raise
                except Exception as error:
                    output = WorkerOutput(ok=False, error=f"{type(error).__name__}: {error}")

                if self._cancelled:
                    raise asyncio.CancelledError
                step.ended_at = time.time()
                step.result = output.result_text
                if output.ok and not output.blocked:
                    step.status = "done"
                    step.error = ""
                else:
                    step.status = "failed"
                    step.error = output.error or ("worker reported BLOCKED" if output.blocked else "worker failed")
                self.plan_store.save(plan)
                await self._event(
                    "step_done",
                    step.summary,
                    agent=step.agent,
                    step=step.id,
                    status=step.status,
                    elapsed=step.elapsed,
                    tools=len(output.tools),
                )

            if self._cancelled:
                raise asyncio.CancelledError

            verdict_status, verdict_text = ("ok", "")
            if plan.failed == 0 and any(step.status == "done" for step in plan.steps):
                await self._event("status", "cowork: verifying")
                try:
                    if self.verifier is not None:
                        verdict_status, verdict_text = await self.verifier(plan)
                    else:
                        verdict_status, verdict_text = await self._default_verifier(plan)
                except Exception as error:
                    verdict_status, verdict_text = "unverified", f"Verifier failed: {error}"
            elif plan.failed:
                verdict_status = "issues"
                verdict_text = f"{plan.failed} step(s) failed; see the plan for details."

            plan.verdict = f"{verdict_status}: {verdict_text}".strip(": ").strip()
            if plan.failed == 0 and verdict_status in ("ok", "unverified"):
                plan.status = "done"
                result.status = "done" if verdict_status == "ok" else "unverified"
            elif plan.failed == 0:
                plan.status = "done"
                result.status = "issues"
            else:
                plan.status = "failed"
                result.status = "failed"
            result.summary = (
                f"{plan.completed}/{len(plan.steps)} steps done · verdict {verdict_status}"
                + (f"\n{verdict_text}" if verdict_text else "")
            )
            self.plan_store.save(plan)
            result.report_path = str(self.write_report(plan))
            result.elapsed = time.perf_counter() - started
            await self._event(
                "verify",
                plan.verdict,
                data={"status": verdict_status, "verdict": verdict_text},
            )
            if self.audit is not None:
                self.audit.record(
                    "agent",
                    agent="cowork",
                    tool="run",
                    ok=result.status in ("done", "unverified"),
                    elapsed=result.elapsed,
                    detail=f"{plan.id} · {result.status} · {result.summary[:400]}",
                    session=getattr(base_ctx, "session_id", "") if base_ctx else "",
                    args={"task": task[:300], "plan": plan.id},
                )
            self.last_result = result
            await self._event(
                "done",
                result.render(),
                data={"plan_id": plan.id, "status": result.status, "report": result.report_path},
            )
            return result
        except asyncio.CancelledError:
            result.status = "cancelled"
            result.plan.status = "cancelled"
            result.error = "cancelled by user"
            self.plan_store.save(result.plan)
            result.elapsed = time.perf_counter() - started
            self.last_result = result
            await self._event("error", "cowork cancelled", data={"plan_id": result.plan.id})
            return result
        except Exception as error:
            result.status = "failed"
            result.error = f"{type(error).__name__}: {error}"
            result.plan.error = result.error
            result.plan.status = "failed"
            self.plan_store.save(result.plan)
            result.elapsed = time.perf_counter() - started
            self.last_result = result
            await self._event("error", result.error, data={"plan_id": result.plan.id})
            return result

    # ── reports ───────────────────────────────────────────────────
    def write_report(self, plan: TaskPlan) -> Path:
        COWORK_DIR.mkdir(parents=True, exist_ok=True)
        target = COWORK_DIR / f"{plan.id}.md"
        lines = [
            f"# Cowork report · {plan.task[:120]}",
            "",
            f"- plan: `{plan.id}`",
            f"- status: `{plan.status}`",
            f"- model: `{plan.model}`",
            f"- updated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(plan.updated))}",
            "",
            "## Steps",
            "",
            "| # | status | step | agent | result |",
            "| --- | --- | --- | --- | --- |",
        ]
        for index, step in enumerate(plan.steps, 1):
            result = step.summary.replace("|", "\\|")[:220]
            lines.append(f"| {index} | {step.status} | {step.title} | {step.agent} | {result} |")
        lines += ["", "## Verdict", "", plan.verdict or "(not verified)", ""]
        lines += ["## Detail", ""]
        for index, step in enumerate(plan.steps, 1):
            lines += [f"### {index}. {step.title}", "", f"- agent: {step.agent}", f"- status: {step.status}"]
            if step.error:
                lines.append(f"- error: {step.error}")
            lines += ["", step.result or "(no result)", ""]
        try:
            target.write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            return Path()
        return target
