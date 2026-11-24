"""Infrastructure tools: cowork, retrieval, context and the audit trail."""

from __future__ import annotations

from typing import Any

from ..tools.registry import ToolContext, ToolError, ToolRegistry, ToolSpec, prop, schema


def _spec(
    name: str,
    description: str,
    parameters: dict[str, Any],
    handler: Any,
    *,
    risk: str = "read",
    dangerous: bool = False,
    icon: str = "◆",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        dangerous=dangerous,
        category="agent",
        icon=icon,
        source="agent",
        risk=risk,
    )


def register_agent_tools(registry: ToolRegistry, *, plan_store: Any = None) -> None:
    async def cowork_run(args: dict[str, Any], ctx: ToolContext) -> str:
        task = str(args.get("task") or "").strip()
        if not task:
            raise ToolError("task is required")
        runner = ctx.extras.get("run_cowork")
        if not callable(runner):
            raise ToolError("Cowork orchestration is not wired into this session.")
        execute = bool(args.get("execute", True))
        output = await runner(task, execute)
        return str(output)

    async def cowork_status(args: dict[str, Any], ctx: ToolContext) -> str:
        provider = ctx.extras.get("cowork_status")
        if callable(provider):
            return str(provider())
        store = plan_store or ctx.extras.get("plan_store")
        if store is None:
            return "No cowork plans."
        plans = store.list(5)
        if not plans:
            return "No cowork plans yet."
        return "\n".join(
            f"{plan.id}  {plan.status:<9} {plan.completed}/{len(plan.steps)}  {plan.task[:80]}"
            for plan in plans
        )

    async def cowork_plans(args: dict[str, Any], ctx: ToolContext) -> str:
        store = plan_store or ctx.extras.get("plan_store")
        if store is None:
            raise ToolError("No plan store is configured.")
        plans = store.list(max(1, min(30, int(args.get("limit") or 10))))
        if not plans:
            return "No cowork plans yet."
        lines = []
        for plan in plans:
            lines.append(f"{plan.id}  {plan.status:<9} {plan.completed}/{len(plan.steps)}  {plan.task[:90]}")
            if plan.error:
                lines.append(f"    error: {plan.error[:200]}")
        return "\n".join(lines)

    async def context_retrieve(args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query is required")
        retriever = ctx.extras.get("retriever")
        if retriever is None:
            raise ToolError("No code retriever is configured.")
        limit = max(1, min(12, int(args.get("limit") or 5)))
        result = await retriever.retrieve(query, limit=limit)
        if result.empty:
            return f"No relevant code found for {query!r} (mode: {result.mode})."
        return f"{result.block}\n\nMode: {result.mode}. Prefer code_outline before reading full files."

    async def context_report(args: dict[str, Any], ctx: ToolContext) -> str:
        provider = ctx.extras.get("context_usage")
        lines = []
        if callable(provider):
            data = provider() or {}
            lines.append(
                f"context   {int(data.get('tokens') or 0):,} / {int(data.get('budget') or 0):,} tokens "
                f"({int(data.get('percent') or 0)}%)"
            )
            if data.get("dropped"):
                lines.append(f"trimmed   {data['dropped']} earlier entries")
        settings = ctx.settings
        if settings is not None:
            lines.append(f"budget    {int(getattr(settings, 'context_budget_tokens', 0) or 0):,} tokens")
            lines.append(f"memory    {'on' if getattr(settings, 'auto_memory', True) else 'off'}")
            lines.append(f"index     {'on' if getattr(settings, 'auto_context', True) else 'off'}")
        memory = ctx.memory
        if memory is not None:
            lines.append(f"notes     {len(memory.entries())} stored")
        index = ctx.index
        if index is not None and getattr(index, "files", None):
            lines.append(index.summary(600))
        else:
            lines.append("index     not built yet (call code_index)")
        return "\n".join(lines) if lines else "No context information available."

    async def audit_tail(args: dict[str, Any], ctx: ToolContext) -> str:
        audit = ctx.audit
        if audit is None:
            raise ToolError("Audit log is disabled in this session.")
        limit = max(1, min(100, int(args.get("limit") or 20)))
        prefix = str(args.get("prefix") or "")
        return audit.render(limit, prefix=prefix)

    registry.add(
        _spec(
            "cowork_run",
            "Run a multi-step job with Gemini Cowork: planner → scoped worker agents → verifier. "
            "Use it for jobs that need several tools and a final verification.",
            schema(
                {
                    "task": prop("string", "The complete job to accomplish"),
                    "execute": prop("boolean", "false = plan only (default true)"),
                },
                ["task"],
            ),
            cowork_run,
            risk="write",
            dangerous=True,
            icon="⇄",
        )
    )
    registry.add(
        _spec(
            "cowork_status",
            "Show the current or most recent cowork plan and its step states.",
            schema({}, []),
            cowork_status,
            icon="⇄",
        )
    )
    registry.add(
        _spec(
            "cowork_plans",
            "List persisted cowork plans with status and progress.",
            schema({"limit": prop("integer", "Rows", minimum=1, maximum=30)}, []),
            cowork_plans,
            icon="▤",
        )
    )
    registry.add(
        _spec(
            "context_retrieve",
            "Retrieve the most relevant code snippets for a query (embedding search when available, "
            "lexical otherwise). Cheaper than reading many files.",
            schema(
                {
                    "query": prop("string", "What you are looking for"),
                    "limit": prop("integer", "Max files/snippets", minimum=1, maximum=12),
                },
                ["query"],
            ),
            context_retrieve,
            icon="⌕",
        )
    )
    registry.add(
        _spec(
            "context_report",
            "Report context budget usage, memory size and index status.",
            schema({}, []),
            context_report,
            icon="◔",
        )
    )
    registry.add(
        _spec(
            "audit_tail",
            "Read the most recent audit entries (tool calls, permission decisions, agent steps).",
            schema(
                {
                    "limit": prop("integer", "Rows", minimum=1, maximum=100),
                    "prefix": prop("string", "Only tools starting with this prefix"),
                },
                [],
            ),
            audit_tail,
            icon="▥",
        )
    )
