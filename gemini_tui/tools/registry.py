"""Tool registry shared by builtin tools, MCP bridges, skills and plugins."""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..animations import truncate


class ToolError(Exception):
    """Raised when a tool cannot run or fails in a way the model should see."""


@dataclass
class ToolContext:
    """Everything a tool may need, injected by the app."""

    cwd: str = "."
    workspace: str = "."
    session_id: str = ""
    app: Any = None
    mcp: Any = None
    settings: Any = None
    approve: Callable[[str, dict[str, Any], str], Awaitable[bool]] | None = None
    notify: Callable[[str, str], None] | None = None
    audit: Any = None
    permissions: Any = None
    memory: Any = None
    index: Any = None
    computer: Any = None
    extras: dict[str, Any] = field(default_factory=dict)

    def emit(self, message: str, severity: str = "info") -> None:
        if self.notify is not None:
            try:
                self.notify(message, severity)
            except Exception:
                pass


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[str]]
    dangerous: bool = False
    category: str = "core"
    icon: str = "◆"
    source: str = "builtin"
    enabled: bool = True
    risk: str = ""                    # read | network | write | execute | screen | computer

    def declaration(self) -> Any:
        from google.genai import types

        schema = self.parameters or {"type": "object", "properties": {}}
        return types.FunctionDeclaration(
            name=self.name,
            description=truncate(self.description, 1000),
            parameters_json_schema=schema,
        )

    @property
    def signature(self) -> str:
        props = (self.parameters or {}).get("properties", {}) or {}
        required = set((self.parameters or {}).get("required", []) or [])
        parts = []
        for key, spec in props.items():
            label = key
            kind = (spec or {}).get("type", "any")
            if kind == "array":
                kind = "array"
            label = f"{label}: {kind}"
            if key not in required:
                label += "?"
            parts.append(label)
        return f"{self.name}({', '.join(parts)})"

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").upper()


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._lock = asyncio.Lock()

    # ── registration ──────────────────────────────────────────────
    def add(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        *,
        dangerous: bool = False,
        category: str = "core",
        icon: str = "◆",
        source: str = "builtin",
        risk: str = "",
    ) -> Callable:
        def decorator(func: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
            self.add(
                ToolSpec(
                    name=name,
                    description=description,
                    parameters=parameters,
                    handler=func,
                    dangerous=dangerous,
                    category=category,
                    icon=icon,
                    source=source,
                    risk=risk,
                )
            )
            return func

        return decorator

    def remove_source(self, source: str) -> int:
        names = [name for name, spec in self._tools.items() if spec.source == source]
        for name in names:
            self._tools.pop(name, None)
        return len(names)

    def remove(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def remove_prefix(self, prefix: str) -> None:
        for name in [n for n in self._tools if n.startswith(prefix)]:
            self._tools.pop(name, None)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda spec: (spec.category, spec.name))

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

    def categories(self) -> dict[str, list[ToolSpec]]:
        grouped: dict[str, list[ToolSpec]] = {}
        for spec in self.all():
            grouped.setdefault(spec.category, []).append(spec)
        return grouped

    @property
    def dangerous_names(self) -> set[str]:
        return {spec.name for spec in self._tools.values() if spec.dangerous}

    # ── execution ─────────────────────────────────────────────────
    async def call(self, name: str, arguments: dict[str, Any] | None, ctx: ToolContext) -> str:
        spec = self.get(name)
        if spec is None:
            raise ToolError(f"Unknown tool: {name}")
        if not spec.enabled:
            raise ToolError(f"Tool disabled: {name}")
        arguments = arguments or {}
        if not isinstance(arguments, dict):
            raise ToolError(f"Tool arguments must be an object, got {type(arguments).__name__}")

        from ..agent.permissions import PermissionPolicy

        policy = getattr(ctx, "permissions", None)
        if not isinstance(policy, PermissionPolicy):
            policy = PermissionPolicy(getattr(ctx, "settings", None))
            ctx.permissions = policy
        extras = getattr(ctx, "extras", {}) or {}
        ceiling = str(extras.get("risk_ceiling", "") or "")
        verdict = policy.decide(spec, ceiling=ceiling)
        audit = getattr(ctx, "audit", None)
        session = getattr(ctx, "session_id", "")
        agent = str(extras.get("agent", ""))

        if not verdict.allowed:
            _audit(
                audit,
                kind="permission",
                tool=name,
                ok=False,
                risk=verdict.risk,
                args=arguments,
                detail=verdict.reason,
                session=session,
                agent=agent,
            )
            raise ToolError(
                verdict.reason
                or f"`{name}` was denied by the execution policy. Ask the user to change permissions."
            )

        if verdict.mode == "ask":
            if ctx.approve is None:
                raise ToolError(
                    f"`{name}` requires human approval, but no approval UI is attached to this tool call."
                )
            approved = await ctx.approve(name, arguments, spec.description)
            _audit(
                audit,
                kind="permission",
                tool=name,
                ok=bool(approved),
                risk=verdict.risk,
                args=arguments,
                detail="approved" if approved else "denied by user",
                session=session,
                agent=agent,
            )
            if not approved:
                raise ToolError(
                    f"The user denied permission to run `{name}`. Do not retry; "
                    "explain what you wanted to do instead."
                )

        started = time.perf_counter()
        output = ""
        ok = True
        error_text = ""
        try:
            result = spec.handler(arguments, ctx)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=600)
            output = "" if result is None else str(result)
            return output
        except ToolError as error:
            ok = False
            error_text = str(error)
            raise
        except asyncio.TimeoutError as error:
            ok = False
            error_text = "timeout after 600s"
            raise ToolError(f"Tool `{name}` timed out after 600s") from error
        except asyncio.CancelledError:
            ok = False
            error_text = "cancelled"
            raise
        except Exception as error:  # surface the real error to the model
            ok = False
            error_text = f"{type(error).__name__}: {error}"
            raise ToolError(error_text) from error
        finally:
            _audit(
                audit,
                kind="tool",
                tool=name,
                ok=ok,
                elapsed=time.perf_counter() - started,
                risk=verdict.risk,
                args=arguments,
                detail=error_text or output[:600],
                session=session,
                agent=agent,
            )


def _audit(audit: Any, **fields: Any) -> None:
    if audit is None:
        return
    try:
        audit.record(**fields)
    except Exception:
        pass


def schema(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
    }
    if required:
        payload["required"] = required
    payload.update(extra)
    return payload


def prop(kind: str, description: str, **extra: Any) -> dict[str, Any]:
    payload = {"type": kind, "description": description}
    payload.update(extra)
    return payload
