"""Agent roles and a live capability registry.

The registry is a *discovery* layer: it describes what tools, skills, MCP
servers and plugins actually exist. It does not grant permission. Every call
still passes through `PermissionPolicy` and the registry's own enabled flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..agent.permissions import CATEGORY_RISK, RISK_READ

# ══════════════════════════════════════════════════════════════════════
# Roles
# ══════════════════════════════════════════════════════════════════════
RISK_RANK = {"read": 0, "network": 1, "write": 2, "execute": 3, "screen": 4, "computer": 5}
ROLE_ORDER = (
    "architect",
    "researcher",
    "planner",
    "developer",
    "debugger",
    "tester",
    "security",
    "writer",
)

READ_TOOLS = frozenset(
    {
        "fs_read",
        "fs_list",
        "fs_glob",
        "fs_grep",
        "git_status",
        "git_diff",
        "git_log",
        "time_now",
        "calc",
        "hash_text",
        "json_query",
        "sys_info",
        "proc_list",
        "net_stats",
        "memory_list",
        "memory_search",
        "code_index",
        "code_search",
        "code_outline",
        "code_pack",
        "code_review",
        "context_retrieve",
        "context_report",
        "audit_tail",
        "skill_list",
        "skill_load",
    }
)

DEVELOPER_TOOLS = READ_TOOLS | frozenset(
    {
        "fs_write",
        "fs_edit",
        "shell_run",
        "code_test",
        "todo_write",
        "memory_save",
        "computer_status",
        "computer_windows",
        "computer_screenshot",
        "computer_focus",
        "computer_click",
        "computer_move",
        "computer_drag",
        "computer_type",
        "computer_key",
        "computer_scroll",
        "computer_open",
        "computer_wait",
    }
)


@dataclass(frozen=True)
class AgentRole:
    key: str
    label: str
    description: str
    ceiling: str
    tools: frozenset[str]
    capabilities: tuple[str, ...]
    system_suffix: str = ""

    @property
    def risk_rank(self) -> int:
        return RISK_RANK.get(self.ceiling, 0)


ROLES: dict[str, AgentRole] = {
    "architect": AgentRole(
        key="architect",
        label="Architect",
        description="Reads the codebase, maps constraints and writes the technical design.",
        ceiling="read",
        tools=READ_TOOLS,
        capabilities=("planning", "reasoning", "long_context"),
        system_suffix=(
            "You are the architect. Produce a concrete design: components, data flow, file map, "
            "risks and acceptance checks. Do not modify files."
        ),
    ),
    "researcher": AgentRole(
        key="researcher",
        label="Researcher",
        description="Gathers facts from the web, docs and the workspace, with sources.",
        ceiling="read",
        tools=READ_TOOLS | frozenset({"web_search", "web_fetch"}),
        capabilities=("research", "long_context"),
        system_suffix=(
            "You are the researcher. Gather evidence with tools. Cite URLs and file paths. "
            "Separate facts from inference. Do not modify files."
        ),
    ),
    "planner": AgentRole(
        key="planner",
        label="Planner",
        description="Turns a goal into an ordered, verifiable task graph.",
        ceiling="read",
        tools=READ_TOOLS,
        capabilities=("planning", "reasoning"),
        system_suffix=(
            "You are the planner. Return an ordered task graph where every task has a verifiable "
            "outcome, explicit dependencies and a role. Do not modify files."
        ),
    ),
    "developer": AgentRole(
        key="developer",
        label="Developer",
        description="Implements changes with tools, reads before writing and verifies with tests.",
        ceiling="computer",
        tools=DEVELOPER_TOOLS,
        capabilities=("coding", "tools", "debugging"),
        system_suffix=(
            "You are the developer. Read before writing, make the smallest correct change, keep "
            "unrelated files untouched, and verify with a real command or test. Never claim a "
            "change works without evidence."
        ),
    ),
    "debugger": AgentRole(
        key="debugger",
        label="Debugger",
        description="Reproduces failures, isolates root causes and verifies the fix.",
        ceiling="execute",
        tools=DEVELOPER_TOOLS - frozenset({"computer_status", "computer_windows", "computer_screenshot",
                                            "computer_focus", "computer_click", "computer_move",
                                            "computer_drag", "computer_type", "computer_key",
                                            "computer_scroll", "computer_open", "computer_wait"}),
        capabilities=("debugging", "tools", "reasoning"),
        system_suffix=(
            "You are the debugger. Reproduce first, then isolate the smallest failing unit. "
            "Explain the root cause and prove the fix with a command output or test."
        ),
    ),
    "tester": AgentRole(
        key="tester",
        label="Tester",
        description="Writes and runs tests; reports exact pass/fail evidence.",
        ceiling="execute",
        tools=DEVELOPER_TOOLS - frozenset({"computer_status", "computer_windows", "computer_screenshot",
                                            "computer_focus", "computer_click", "computer_move",
                                            "computer_drag", "computer_type", "computer_key",
                                            "computer_scroll", "computer_open", "computer_wait"}),
        capabilities=("testing", "tools"),
        system_suffix=(
            "You are the tester. Add or run tests. Report exact commands, exit status and failures. "
            "Do not claim a pass without output."
        ),
    ),
    "security": AgentRole(
        key="security",
        label="Security Reviewer",
        description="Audits permissions, secrets, injection and destructive paths; read-mostly.",
        ceiling="write",
        tools=READ_TOOLS | frozenset({"shell_run", "fs_write", "fs_edit", "memory_save"}),
        capabilities=("security", "reasoning", "tools"),
        system_suffix=(
            "You are the security reviewer. Look for real exploitable issues: permission bypasses, "
            "secret handling, injection, destructive commands, unsafe file writes. Rank by severity. "
            "Prefer evidence over speculation."
        ),
    ),
    "writer": AgentRole(
        key="writer",
        label="Technical Writer",
        description="Updates docs, README and release notes from verified results only.",
        ceiling="write",
        tools=READ_TOOLS | frozenset({"fs_write", "fs_edit", "memory_save"}),
        capabilities=("writing", "long_context"),
        system_suffix=(
            "You are the technical writer. Document only verified behaviour. Be concise, concrete "
            "and honest about limitations. Do not touch source code."
        ),
    ),
}


def role_or_default(name: str) -> AgentRole:
    key = (name or "").strip().lower()
    if key in ROLES:
        return ROLES[key]
    alias = {
        "code": "developer",
        "dev": "developer",
        "review": "security",
        "reviewer": "security",
        "docs": "writer",
        "test": "tester",
        "research": "researcher",
        "general": "developer",
        "computer": "developer",
    }.get(key)
    return ROLES[alias or "developer"]


# ══════════════════════════════════════════════════════════════════════
# Capability registry
# ══════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Capability:
    name: str
    kind: str                    # tool | skill | mcp | plugin
    description: str = ""
    risk: str = RISK_READ
    source: str = ""
    enabled: bool = True
    roles: frozenset[str] = frozenset()
    provider: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.name

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.name}"


class CapabilityRegistry:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], Capability] = {}

    # ── registration ──────────────────────────────────────────────
    def register(self, item: Capability) -> None:
        self._items[item.key] = item

    def clear(self, kind: str = "") -> None:
        if not kind:
            self._items.clear()
            return
        self._items = {key: value for key, value in self._items.items() if key[0] != kind}

    def all(self, kind: str = "") -> list[Capability]:
        items = [
            item for item in self._items.values() if not kind or item.kind == kind
        ]
        return sorted(items, key=lambda item: (item.kind, item.name))

    def get(self, name: str, kind: str = "") -> Capability | None:
        if kind:
            return self._items.get((kind, name))
        for candidate in self._items.values():
            if candidate.name == name:
                return candidate
        return None

    def for_role(self, role: str, *, kind: str = "") -> list[Capability]:
        key = role_or_default(role).key
        return [
            item
            for item in self.all(kind)
            if item.enabled and (key in item.roles or (not item.roles and item.kind != "tool"))
        ]

    def find(self, query: str, *, kind: str = "", limit: int = 20) -> list[Capability]:
        needle = (query or "").lower().strip()
        items = self.all(kind)
        if not needle:
            return items[:limit]
        return [
            item
            for item in items
            if needle in item.name.lower() or needle in item.description.lower()
        ][:limit]

    # ── discovery ─────────────────────────────────────────────────
    @classmethod
    def from_components(
        cls,
        registry: Any = None,
        *,
        skills: Any = None,
        plugins: Any = None,
        mcp: Any = None,
    ) -> "CapabilityRegistry":
        capabilities = cls()
        role_tools: dict[str, set[str]] = {}
        for role in ROLES.values():
            for name in role.tools:
                role_tools.setdefault(name, set()).add(role.key)

        for spec in list(getattr(registry, "all", lambda: [])() or []):
            name = str(getattr(spec, "name", "") or "")
            if not name:
                continue
            risk = str(getattr(spec, "risk", "") or "")
            if risk not in RISK_RANK:
                risk = CATEGORY_RISK.get(str(getattr(spec, "category", "") or ""), RISK_READ)
            source = str(getattr(spec, "source", "") or "")
            roles = frozenset(role_tools.get(name, ()))
            if source.startswith("mcp:"):
                roles = frozenset({"researcher", "developer", "debugger", "tester"})
                risk = "write"
            capabilities.register(
                Capability(
                    name=name,
                    kind="tool",
                    description=str(getattr(spec, "description", "") or "")[:300],
                    risk=risk,
                    source=source,
                    enabled=bool(getattr(spec, "enabled", True)),
                    roles=roles,
                )
            )

        for skill in list(getattr(skills, "skills", []) or []):
            name = str(getattr(skill, "name", "") or "")
            if not name:
                continue
            capabilities.register(
                Capability(
                    name=name,
                    kind="skill",
                    description=str(getattr(skill, "description", "") or "")[:300],
                    source=str(getattr(skill, "source", "") or ""),
                    roles=frozenset(ROLES),
                )
            )

        for plugin in list(getattr(plugins, "plugins", []) or []):
            name = str(getattr(plugin, "name", "") or "")
            if not name:
                continue
            capabilities.register(
                Capability(
                    name=name,
                    kind="plugin",
                    description=str(getattr(plugin, "description", "") or "")[:300],
                    source=str(getattr(plugin, "source", "") or ""),
                    enabled=str(getattr(plugin, "status", "")) != "error",
                    roles=frozenset(ROLES),
                )
            )

        states = getattr(mcp, "states", {}) or {}
        for server_name, state in states.items():
            tools = list(getattr(state, "tools", []) or [])
            # A server with no tools is still discoverable as a server.
            capabilities.register(
                Capability(
                    name=str(server_name),
                    kind="mcp",
                    description=f"MCP server · {len(tools)} tool(s) · {getattr(state, 'status', '?')}",
                    risk="write",
                    source=f"mcp:{server_name}",
                    enabled=getattr(state, "status", "") == "ready",
                    roles=frozenset(ROLES),
                )
            )
        return capabilities

    # ── planner-facing text ───────────────────────────────────────
    def catalogue(self, role: str = "", *, limit: int = 80, include_mcp: bool = True) -> str:
        lines: list[str] = []
        items = self.for_role(role) if role else self.all()
        for item in items:
            if not include_mcp and item.kind == "mcp":
                continue
            risk = f" [{item.risk}]" if item.risk and item.risk != "read" else ""
            lines.append(f"- {item.kind}:{item.name}{risk} — {item.description.splitlines()[0][:120]}")
            if len(lines) >= limit:
                break
        return "\n".join(lines)

    def summary(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in self.all():
            result[item.kind] = result.get(item.kind, 0) + 1
        return result


def tools_for_role(role: str, available: set[str]) -> set[str]:
    """Role allowlist narrowed by what is actually registered/enabled."""
    return set(role_or_default(role).tools) & set(available)
