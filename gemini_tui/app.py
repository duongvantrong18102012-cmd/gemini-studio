"""Gemini Studio — a professional, flicker-free Gemini dashboard for the terminal."""

from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import ContentSwitcher

from . import theme
from .agent.audit import AuditLog
from .agent.code import CodeStore, register_code_tools
from .agent.computer import ComputerDriver, create_backend, register_computer_tools
from .agent.context import (
    ContextManager,
    LazyIndex,
    Retriever,
    build_context_block,
    estimate_tokens,
    load_project_brief,
)
from .agent.cowork import CoworkOrchestrator
from .agent.memory import MemoryStore, register_memory_tools
from .agent.permissions import ComputerGuard, PermissionPolicy
from .agent.plan import PlanStore
from .agent.tools import register_agent_tools
from .ai.client import GeminiService, friendly_error
from .ai.models import ModelInfo, fallback_models, find_model
from .ai.sessions import Message, Session, SessionStore, Usage
from .animations import format_tokens
from .clipboard import ClipboardManager
from .commands import CommandRegistry, parse_slash
from .config import (
    APP_TITLE,
    APP_VERSION,
    CONFIG_DIR,
    COWORK_DIR,
    TEAM_DIR,
    Settings,
    ensure_layout,
    get_api_key,
    load_dotenv_files,
    system_prompt,
)
from .orchestration import (
    CAP_TEXT,
    AgentManager,
    AgentTeamOrchestrator,
    CapabilityRegistry,
    FileOwnership,
    GeminiWorkerRunner,
    ModelRegistry,
    ModelRouter,
    ModelSpec,
    OpenAICompatibleAdapter,
    OrchestrationError,
    build_registry,
    role_or_default,
)
from .tools.builtin import register_builtin_tools
from .tools.mcp_client import MCPManager
from .tools.plugins import PluginManager
from .tools.registry import ToolContext, ToolError, ToolRegistry
from .tools.skills import SkillLibrary
from .widgets.chat import ChatView
from .widgets.chrome import HeaderBar, Sidebar, StatusBar
from .widgets.overlays import (
    ApprovalScreen,
    HelpScreen,
    PaletteScreen,
    SplashScreen,
    ToastHost,
)
from .widgets.views import (
    ComputerView,
    CoworkView,
    DashboardView,
    MCPView,
    ModelsView,
    PluginsView,
    SkillsView,
    TeamView,
    ToolsView,
)

VIEW_KEYS = {
    "chat": "ctrl+1",
    "models": "ctrl+2",
    "tools": "ctrl+3",
    "mcp": "ctrl+4",
    "skills": "ctrl+5",
    "plugins": "ctrl+6",
    "dashboard": "ctrl+7",
    "team": "ctrl+8",
    "cowork": "ctrl+0",
    "computer": "ctrl+9",
}

APPROVAL_CYCLE = ["ask", "auto", "readonly"]
THINKING_CYCLE = ["minimal", "low", "medium", "high"]
COMPUTER_CONTROL_TOOLS = (
    "computer_screenshot",
    "computer_focus",
    "computer_click",
    "computer_move",
    "computer_drag",
    "computer_type",
    "computer_key",
    "computer_scroll",
    "computer_open",
    "computer_task",
)
TEAM_TRIGGERS = (
    "refactor",
    "migrate",
    "implement",
    "integrate",
    "architecture",
    "across the codebase",
    "multiple files",
    "end to end",
    "end-to-end",
    "debug",
    "root cause",
    "test suite",
    "write tests",
    "review the",
    "security audit",
    "build a",
    "add a feature",
    "step by step",
)


def looks_complex(text: str) -> bool:
    """Conservative heuristic used only to *suggest* /team, never to run it."""
    body = " ".join((text or "").split())
    lowered = body.lower()
    if len(body) < 90:
        return False
    if len(body) > 320:
        return True
    numbered = sum(1 for line in (text or "").splitlines() if line.strip()[:2].rstrip(".").isdigit())
    if numbered >= 2:
        return True
    return any(trigger in lowered for trigger in TEAM_TRIGGERS)



class GeminiStudio(App):
    CSS = theme.CSS
    TITLE = APP_TITLE
    SUB_TITLE = "local agent console"
    ENABLE_COMMAND_PALETTE = False
    AUTO_FOCUS = None
    BINDINGS = [
        Binding("ctrl+k", "palette", "Palette", priority=True),
        Binding("ctrl+n", "new_session", "New session"),
        Binding("ctrl+r", "resume_session", "Resume"),
        Binding("ctrl+m", "show_view('models')", "Models"),
        Binding("ctrl+1", "show_view('chat')", "Chat"),
        Binding("ctrl+2", "show_view('models')", "Models"),
        Binding("ctrl+3", "show_view('tools')", "Tools"),
        Binding("ctrl+4", "show_view('mcp')", "MCP"),
        Binding("ctrl+5", "show_view('skills')", "Skills"),
        Binding("ctrl+6", "show_view('plugins')", "Plugins"),
        Binding("ctrl+7", "show_view('dashboard')", "Dashboard"),
        Binding("ctrl+8", "show_view('team')", "Agent team"),
        Binding("ctrl+0", "show_view('cowork')", "Cowork"),
        Binding("ctrl+9", "show_view('computer')", "Computer"),
        Binding("ctrl+l", "clear_transcript", "Clear"),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar", priority=True),
        Binding("ctrl+y", "copy_last", "Copy reply", priority=True),
        Binding("ctrl+shift+c", "copy_selection", "Copy selection"),
        Binding("ctrl+t", "cycle_thinking", "Thinking"),
        Binding("ctrl+o", "cycle_approval", "Approval"),
        Binding("escape", "escape", "Back / cancel"),
        Binding("f1", "help", "Help"),
        Binding("question_mark", "help", "Help"),
        Binding("ctrl+q", "quit_app", "Quit", priority=True),
    ]

    def __init__(
        self,
        workspace: str | None = None,
        model: str | None = None,
        splash: bool = True,
        *,
        computer: bool | None = None,
        memory: bool | None = None,
        audit: bool | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        load_dotenv_files()
        ensure_layout()

        self.settings = Settings.load()
        if workspace:
            self.settings.workspace = str(Path(workspace).expanduser().resolve())
        if model:
            self.settings.model = model
        if computer is not None:
            self.settings.computer_enabled = bool(computer)
        if memory is not None:
            self.settings.auto_memory = bool(memory)
        if audit is not None:
            self.settings.audit_enabled = bool(audit)
        self.show_splash = splash

        self.started_at = time.time()
        self.frame = 0
        self._state = "READY"
        self._boot_finished = not splash
        self.splash_started = 0.0
        self.last_latency = 0.0
        self.todos: list[dict[str, Any]] = []
        self.models: list[ModelInfo] = fallback_models()
        self.stream_elapsed = 0.0
        self._turn_started = 0.0
        self._turn_worker = None
        self._last_assistant_text = ""
        self._session_list: list[Session] = []
        self._current_view = "chat"

        self.store = SessionStore()

        # ── agent infrastructure ──────────────────────────────────────
        self.audit = AuditLog(enabled=self.settings.audit_enabled)
        self.memory = MemoryStore(workspace=self.settings.workspace)
        self.permissions = PermissionPolicy(self.settings)
        self.context_manager = ContextManager(self.settings.context_budget_tokens)
        self.plan_store = PlanStore(COWORK_DIR)
        self.code_store = CodeStore(
            self.settings.workspace,
            max_files=max(100, int(self.settings.code_index_max_files or 5000)),
        )
        self.lazy_index = LazyIndex(self.code_store)
        self.retriever = Retriever(
            self.lazy_index,
            self.settings.workspace,
            embed=self._embed_texts,
        )
        self.computer_backend = create_backend()
        self.computer_guard = ComputerGuard(
            self.settings,
            backend_available=bool(getattr(self.computer_backend, "available", False)),
        )
        self.cowork_events: deque[Any] = deque(maxlen=500)
        self.computer_events: deque[Any] = deque(maxlen=250)
        self.cowork_running = False
        self._ambient_tokens = 0

        self.registry = ToolRegistry()
        register_builtin_tools(self.registry)

        self.skills = SkillLibrary(self.settings.workspace)
        self.skills.register_tools(self.registry)
        register_memory_tools(self.registry, self.memory)
        register_code_tools(self.registry, self.code_store)
        register_agent_tools(self.registry, plan_store=self.plan_store)
        self._sync_computer_tools()

        self.commands = CommandRegistry()
        self.plugins = PluginManager(self.registry, self.commands, self.settings.workspace)

        self.mcp = MCPManager(self.registry, notify=self.notify_user)
        self.mcp.load(
            self.settings.workspace,
            trusted=self.settings.is_workspace_trusted(self.settings.workspace),
        )

        self.plugins.load_all(self)

        self.service = GeminiService(self.settings)
        self.cowork = CoworkOrchestrator(
            service=self.service,
            registry=self.registry,
            settings=self.settings,
            workspace=self.settings.workspace,
            memory=self.memory,
            retriever=self.retriever,
            audit=self.audit,
            emit=self._cowork_event,
            plan_store=self.plan_store,
            computer_driver_factory=self.make_computer_driver,
        )

        # ── orchestration runtime ─────────────────────────────────────
        self.clipboard_manager = ClipboardManager(self)
        self.model_registry = build_registry(self.models, service=self.service)
        self._register_providers()
        self.model_router = ModelRouter(
            self.model_registry,
            preferences=dict(self.settings.team_model_roles or {}),
        )
        self.capability_registry = CapabilityRegistry.from_components(
            self.registry, skills=self.skills, plugins=self.plugins, mcp=self.mcp
        )
        self.ownership = FileOwnership()
        self.agent_manager = AgentManager()
        self.team_events: deque[dict[str, Any]] = deque(maxlen=800)
        self._team_worker = None
        self._team_suggested: set[str] = set()
        self._approval_lock = asyncio.Lock()
        self.team_runner = GeminiWorkerRunner(
            self.service,
            self.registry,
            self.team_tool_context,
            model_registry=self.model_registry,
        )
        self.team = AgentTeamOrchestrator(
            settings=self.settings,
            router=self.model_router,
            registry=self.registry,
            capabilities=self.capability_registry,
            ownership=self.ownership,
            manager=self.agent_manager,
            runner=self.team_runner,
            emit=self._team_event,
            report_dir=TEAM_DIR,
            verify=bool(self.settings.team_verify),
            max_parallel=int(self.settings.team_max_parallel),
            default_timeout=float(self.settings.team_task_timeout),
            default_attempts=int(self.settings.team_max_retries),
        )
        self.session = self._restore_or_new()
        self._register_commands()

    # ══════════════════════════════════════════════════════════════
    # Layout
    # ══════════════════════════════════════════════════════════════
    def compose(self) -> ComposeResult:
        yield HeaderBar(id="app-header")
        with Horizontal(id="body"):
            yield Sidebar(id="sidebar")
            with Container(id="main-pane"):
                with ContentSwitcher(id="views", initial="chat"):
                    yield ChatView(id="chat", classes="view")
                    yield ModelsView(id="models", classes="view")
                    yield ToolsView(id="tools", classes="view")
                    yield MCPView(id="mcp", classes="view")
                    yield SkillsView(id="skills", classes="view")
                    yield PluginsView(id="plugins", classes="view")
                    yield DashboardView(id="dashboard", classes="view")
                    yield CoworkView(id="cowork", classes="view")
                    yield TeamView(id="team", classes="view")
                    yield ComputerView(id="computer", classes="view")
        yield StatusBar(id="status-bar")
        yield ToastHost(id="toast-host")

    def on_mount(self) -> None:
        self.chat = self.query_one(ChatView)
        self.switcher = self.query_one("#views", ContentSwitcher)
        self.sidebar = self.query_one(Sidebar)
        self.header = self.query_one(HeaderBar)
        self.status = self.query_one(StatusBar)
        self.toast_host = self.query_one(ToastHost)
        self.plugins_dir = CONFIG_DIR / "plugins"

        self.sidebar.update_nav("chat")
        self._apply_layout(self.size.width, self.size.height)
        self.run_worker(self._render_session(), exclusive=False, group="render")

        self.set_interval(1 / 24, self._tick)
        self.run_worker(self._boot(), exclusive=True, group="boot")
        if self.show_splash:
            self.splash_started = time.time()
            self.push_screen(SplashScreen(), callback=self._after_splash)
        else:
            self.chat.focus_composer()

    def on_resize(self, event: Any = None) -> None:
        size = getattr(event, "size", None) or self.size
        self._apply_layout(size.width, size.height)

    def _apply_layout(self, width: int, height: int) -> None:
        """Keep the command center usable down to a small terminal.

        Only classes are toggled; the widgets are never remounted, so resizing
        cannot drop the current view or the conversation.
        """
        try:
            compact = width < 118
            narrow = width < 92
            short = height < 30
            for widget, active in ((self.header, compact), (self.sidebar, compact)):
                widget.set_class(active, "-compact")
            self.sidebar.set_class(narrow, "-narrow")
            self.header.set_class(short, "-compact")
            body = self.query_one("#body", Horizontal)
            body.set_class(short, "-short")
            body.set_class(width < 76, "-stacked")
        except Exception:
            pass

    def action_toggle_sidebar(self) -> None:
        try:
            self.sidebar.toggle_class("-hidden")
        except Exception:
            pass

    def _after_splash(self, _: Any = None) -> None:
        self.finish_boot()

    def finish_boot(self) -> None:
        if self._boot_finished:
            return
        self._boot_finished = True
        if self._current_view == "chat":
            try:
                self.chat.focus_composer()
            except Exception:
                pass

    async def _boot(self) -> None:
        try:
            models = await self.service.list_models()
            if models:
                self.models = models
                self.model_registry.sync_catalogue(models, provider="gemini")
                if find_model(models, self.settings.model) is None:
                    self.settings.model = models[0].id
                    self.settings.save()
        except Exception as error:
            self.notify_user(f"Model catalogue unavailable: {error}", "warn")
        self._sync_team_runtime()
        self.refresh_chrome()
        if self.settings.auto_connect_mcp:
            for name in list(self.mcp.states)[:4]:
                try:
                    await self.mcp.connect(name, timeout=30)
                except Exception:
                    pass
            self._sync_team_runtime()
            self.refresh_chrome()

    def _tick(self) -> None:
        self.frame += 1
        if self.is_streaming:
            self.stream_elapsed = time.time() - self._turn_started

    # ══════════════════════════════════════════════════════════════
    # State
    # ══════════════════════════════════════════════════════════════
    @property
    def state_label(self) -> str:
        return self._state

    @property
    def is_streaming(self) -> bool:
        return bool(self._turn_worker is not None and not self._turn_worker.is_finished)

    @property
    def tool_count(self) -> int:
        return len(self.registry.enabled())

    @property
    def current_view(self) -> str:
        return self._current_view

    def set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            try:
                self.status._tick()
            except Exception:
                pass

    def refresh_chrome(self) -> None:
        try:
            self.sidebar.update_nav(self._current_view)
            self.header._render_brand()
            self.header._render_right(force=True)
            self.status._tick()
            self.chat._refresh_head()
        except Exception:
            pass

    def notify_user(self, message: str, severity: str = "info", timeout: float = 4.0) -> None:
        try:
            if getattr(self, "toast_host", None) is not None:
                self.toast_host.push(message, severity, timeout)
        except Exception:
            pass

    def set_todos(self, items: list[dict[str, Any]]) -> None:
        self.todos = items
        self.refresh_chrome()

    # ══════════════════════════════════════════════════════════════
    # Agent infrastructure
    # ══════════════════════════════════════════════════════════════
    def _sync_computer_tools(self) -> None:
        """Declare only the computer tools the current consent level allows."""
        self.registry.remove_prefix("computer_")
        register_computer_tools(self.registry, self.computer_backend, self.computer_guard)
        if not self.settings.computer_enabled:
            for name in COMPUTER_CONTROL_TOOLS:
                self.registry.remove(name)

    async def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        service = getattr(self, "service", None)
        if service is None or not service.has_key:
            return []
        try:
            return await service.embed("gemini-embedding-002", texts)
        except Exception:
            return []

    def tool_context(
        self,
        *,
        agent: str = "",
        extras: dict[str, Any] | None = None,
    ) -> ToolContext:
        payload: dict[str, Any] = {
            "todos": self.todos,
            "retriever": self.retriever,
            "context_usage": self.context_usage,
            "plan_store": self.plan_store,
            "run_cowork": self.run_cowork_tool,
            "cowork_status": self.cowork_status_text,
            "computer_driver": self.make_computer_driver,
            "computer_guard": self.computer_guard,
        }
        payload.update(extras or {})
        if agent:
            payload["agent"] = agent
        return ToolContext(
            cwd=self.settings.workspace,
            workspace=self.settings.workspace,
            session_id=self.session.id,
            app=self,
            mcp=self.mcp,
            settings=self.settings,
            approve=self.approve_tool,
            notify=self.notify_user,
            audit=self.audit,
            permissions=self.permissions,
            memory=self.memory,
            index=self.lazy_index,
            computer=self.computer_backend,
            extras=payload,
        )

    def context_usage(self) -> dict[str, int]:
        system_tokens = estimate_tokens(system_prompt(self.settings)) + self._ambient_tokens
        usage = self.context_manager.usage(self.session.contents, system_tokens)
        usage["dropped"] = int(getattr(self, "_context_dropped", 0) or 0)
        return usage

    async def _ambient_context(self, prompt: str) -> str:
        """Fresh, budgeted context for one turn (injected as a system suffix)."""
        memory = ""
        if self.settings.auto_memory and self.memory is not None:
            try:
                memory = await asyncio.to_thread(self.memory.digest, prompt, 6, 1100)
            except Exception:
                memory = ""

        skills = ""
        try:
            if self.skills.skills:
                skills = "\n".join(
                    f"- {skill.name}: {' '.join(skill.description.split())[:100]}"
                    for skill in self.skills.skills[:10]
                )[:1400]
        except Exception:
            skills = ""

        brief = ""
        try:
            brief = load_project_brief(self.settings.workspace, 4000)
        except Exception:
            brief = ""

        retrieval = ""
        fingerprint = ""
        if self.settings.auto_context:
            index = self.code_store.index
            if index is None:
                try:
                    index = await asyncio.to_thread(self.code_store.ensure, False)
                except Exception:
                    index = None
            if index is not None:
                try:
                    fingerprint = index.summary(700)
                except Exception:
                    fingerprint = ""
            try:
                result = await self.retriever.retrieve(prompt, limit=4, max_chars=2600)
                retrieval = result.block
            except Exception:
                retrieval = ""

        block = build_context_block(
            brief=brief,
            memory=memory,
            skills=skills,
            retrieval=retrieval,
            fingerprint=fingerprint,
            max_chars=6000,
        )
        self._ambient_tokens = estimate_tokens(block)
        return block

    # ── cowork ────────────────────────────────────────────────────
    def _cowork_event(self, event: Any) -> None:
        self.cowork_events.append(event)
        try:
            self.query_one(CoworkView).on_agent_event(event)
        except Exception:
            pass

    def start_cowork(self, task: str, execute: bool = True) -> None:
        task = (task or "").strip()
        if not task:
            self.notify_user("Usage: /cowork <task>", "warn")
            return
        if self.cowork_running:
            self.notify_user("A cowork run is already in progress", "warn")
            return
        self.cowork_events.clear()
        self.switch_view("cowork")
        try:
            self.query_one(CoworkView).begin(task, execute)
        except Exception:
            pass
        self.run_worker(self._run_cowork(task, execute), exclusive=False, group="cowork")

    async def _run_cowork(self, task: str, execute: bool) -> None:
        self.cowork_running = True
        self.set_state("THINKING")
        try:
            ctx = self.tool_context(agent="cowork")
            result = await self.cowork.run(task, execute=execute, base_ctx=ctx)
        except asyncio.CancelledError:
            self.cowork.cancel()
            raise
        except Exception as error:
            self.chat.add_notice(f"Cowork failed: {error}", "error")
            self.notify_user(f"Cowork failed: {error}", "error")
        else:
            severity = "ok" if result.status in ("done", "unverified", "planned") else "warn"
            self.chat.add_notice(result.render(), severity)
            try:
                self.query_one(CoworkView).finish(result)
            except Exception:
                pass
            head = (result.summary or result.status).splitlines()[0][:90]
            self.notify_user(f"Cowork {result.status} · {head}", severity, timeout=8)
        finally:
            self.cowork_running = False
            self.set_state("READY")
            self.refresh_chrome()

    def cancel_cowork(self) -> None:
        if not self.cowork_running:
            self.notify_user("No cowork run to cancel", "info")
            return
        self.cowork.cancel()
        self.notify_user("Cowork cancellation requested", "warn")

    async def run_cowork_tool(self, task: str, execute: bool = True) -> str:
        ctx = self.tool_context(agent="cowork")
        self.cowork_events.clear()
        result = await self.cowork.run(task, execute=bool(execute), base_ctx=ctx)
        try:
            self.query_one(CoworkView).finish(result)
            self.refresh_chrome()
        except Exception:
            pass
        return result.render()

    def cowork_status_text(self) -> str:
        plan = getattr(self.cowork, "current", None)
        if plan is not None:
            return plan.summary_text()
        plans = self.plan_store.list(5)
        if not plans:
            return "No cowork plans yet. Run /cowork <task>."
        return "\n".join(
            f"{item.id}  {item.status:<9} {item.completed}/{len(item.steps)}  {item.task[:80]}"
            for item in plans
        )

    # ── agent team ────────────────────────────────────────────────
    def _register_providers(self) -> None:
        """Register an OpenAI-compatible endpoint only when fully configured."""
        endpoints = getattr(self.settings, "provider_endpoints", None) or {}
        if not isinstance(endpoints, dict):
            return
        for name, config in endpoints.items():
            if not isinstance(config, dict):
                continue
            base_url = str(config.get("base_url") or "").strip()
            key = str(config.get("api_key") or "").strip()
            key_env = str(config.get("api_key_env") or "").strip()
            if key_env and not key:
                key = os.environ.get(key_env, "").strip()
            raw_models = config.get("models") or []
            specs: list[ModelSpec] = []
            for item in raw_models:
                if isinstance(item, str):
                    item = {"id": item}
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                specs.append(
                    ModelSpec(
                        id=str(item["id"]),
                        provider=str(name),
                        display_name=str(item.get("display_name") or item["id"]),
                        capabilities=frozenset({CAP_TEXT}),
                        context_limit=int(item.get("context_limit") or 0),
                        output_limit=int(item.get("output_limit") or 0),
                        supports_tools=False,
                        supports_images=False,
                        supports_thinking=False,
                        speed=str(item.get("speed") or "medium"),
                        cost_tier=str(item.get("cost_tier") or "medium"),
                        notes="OpenAI-compatible endpoint · text/planning only, no tool calls",
                    )
                )
            adapter = OpenAICompatibleAdapter(base_url, key, name=str(name), models=specs)
            if not adapter.available():
                continue
            self.model_registry.register_provider(adapter)
            for spec in specs:
                self.model_registry.register(spec)

    def _sync_team_runtime(self) -> None:
        """Re-read settings and discovery state before a team run."""
        self.capability_registry = CapabilityRegistry.from_components(
            self.registry, skills=self.skills, plugins=self.plugins, mcp=self.mcp
        )
        self.team.capabilities = self.capability_registry
        self.model_router.preferences = dict(self.settings.team_model_roles or {})
        self.team.max_parallel = max(1, min(4, int(self.settings.team_max_parallel or 2)))
        self.team.verify = bool(self.settings.team_verify)
        self.team.default_timeout = max(10.0, float(self.settings.team_task_timeout or 300))
        self.team.default_attempts = max(1, min(4, int(self.settings.team_max_retries or 2)))

    def _team_provider_ready(self) -> bool:
        for adapter in self.model_registry.providers():
            try:
                if adapter.available():
                    return True
            except Exception:
                continue
        return False

    def team_tool_context(self, request: Any) -> ToolContext:
        """Scoped context for one worker: role ceiling, lease identity, audit."""
        role = role_or_default(str(getattr(request, "role", "developer")))
        ceiling = (self.settings.agent_permissions or {}).get(role.key, role.ceiling)
        task_id = str(getattr(request, "task_id", "") or "")
        extras = {
            "ownership": self.ownership,
            "ownership_agent": f"task:{task_id}",
            "team_task": task_id,
            "risk_ceiling": ceiling,
            "team_goal": self.team.plan.goal if self.team.plan else "",
        }
        return self.tool_context(agent=role.key, extras=extras)

    def _team_event(self, event: dict[str, Any]) -> None:
        self.team_events.append(event)
        try:
            self.query_one(TeamView).apply_event(event)
        except Exception:
            pass
        try:
            self.sidebar.update_nav(self._current_view)
        except Exception:
            pass
        if event.get("kind") == "team_done":
            result = dict(event.get("result") or {})
            status = str(result.get("status") or "failed")
            severity = "ok" if status == "done" else "warn" if status == "partial" else "error"
            summary = str(result.get("summary") or "")
            try:
                self.chat.add_notice(summary or f"Team run {status}", severity)
            except Exception:
                pass
            self.notify_user(
                f"Team {status} · {sum(1 for t in result.get('tasks') or [] if t.get('status') == 'done')}"
                f"/{len(result.get('tasks') or [])} tasks",
                severity,
                timeout=8,
            )

    def start_team(self, goal: str, execute: bool = True) -> None:
        goal = (goal or "").strip()
        if not goal:
            self.notify_user("Usage: /team <goal>", "warn")
            return
        if self.team.running:
            self.notify_user("A team run is already in progress — /team cancel first", "warn")
            return
        self._sync_team_runtime()
        self.team_events.clear()
        self.switch_view("team")
        try:
            self.query_one(TeamView).begin(goal)
        except Exception:
            pass
        if not self._team_provider_ready():
            message = (
                "No model provider is configured. Add a Gemini API key, or configure "
                "settings.provider_endpoints (openai-compatible) before using the team."
            )
            try:
                self.query_one(TeamView).fail(message)
            except Exception:
                pass
            self.notify_user(message, "error", timeout=8)
            return
        worker = self._plan_team(goal) if not execute else self._run_team(goal)
        self._team_worker = self.run_worker(worker, exclusive=False, group="team")

    async def _plan_team(self, goal: str) -> None:
        self.set_state("THINKING")
        try:
            plan = await self.team.plan_goal(goal)
            self.team.plan = plan
            try:
                self.query_one(TeamView).apply_event(
                    {"kind": "team_plan", "plan": plan.to_dict(), "summary": plan.summary_text}
                )
            except Exception:
                pass
            self.notify_user(f"Plan ready · {len(plan.tasks)} task(s), not executed", "ok", timeout=6)
        except OrchestrationError as error:
            self.notify_user(str(error), "error", timeout=8)
            try:
                self.query_one(TeamView).fail(str(error))
            except Exception:
                pass
        except Exception as error:
            message = f"Planning failed: {type(error).__name__}: {error}"
            self.notify_user(message, "error", timeout=8)
        finally:
            self.set_state("READY")
            self.refresh_chrome()

    async def _run_team(self, goal: str) -> None:
        self.set_state("TEAM")
        try:
            result = await self.team.run(goal)
            severity = "ok" if result.status == "done" else "warn" if result.status == "partial" else "error"
            head = (result.summary or result.status).splitlines()[0][:100]
            self.notify_user(f"Team {result.status} · {head}", severity, timeout=8)
        except asyncio.CancelledError:
            self.team.cancel()
            raise
        except OrchestrationError as error:
            self.notify_user(str(error), "error", timeout=8)
            try:
                self.query_one(TeamView).fail(str(error))
            except Exception:
                pass
        except Exception as error:
            message = f"Team run failed: {type(error).__name__}: {error}"
            self.notify_user(message, "error", timeout=8)
            try:
                self.query_one(TeamView).fail(message)
            except Exception:
                pass
        finally:
            self._team_worker = None
            self.set_state("READY")
            self.refresh_chrome()

    def cancel_team(self) -> None:
        if not self.team.running:
            self.notify_user("No team run to cancel", "info")
            return
        self.team.cancel()
        self.notify_user("Team cancellation requested — workers stop at their next checkpoint", "warn")

    def toggle_team_pause(self) -> None:
        if not self.team.running:
            self.notify_user("No team run in progress", "info")
            return
        if self.agent_manager.globally_paused:
            count = self.team.resume()
            self.notify_user(f"Resumed {count} agent(s)", "ok")
        else:
            count = self.team.pause()
            self.notify_user(f"Paused {count} agent(s) at their next checkpoint", "warn")

    def team_status_text(self) -> str:
        if self.team.scheduler is not None:
            return "\n".join(
                [
                    self.team.scheduler.summary_text(),
                    self.agent_manager.summary(),
                    self.ownership.summary(),
                ]
            )
        return "Team idle. /team <goal> starts a supervised run."

    def team_roles_text(self) -> str:
        table = self.model_router.assignment_table()
        if not table:
            return "No models available for role routing. Configure a provider first."
        lines = ["ROLE                 MODEL"]
        for role in ("architect", "researcher", "planner", "developer", "debugger", "tester", "security", "writer"):
            spec = self.model_registry.get(table.get(role, ""))
            label = spec.label if spec is not None else "—"
            lines.append(f"{role:<20} {label}")
        lines.append("")
        lines.append("override with settings.team_model_roles = {\"role\": \"model-id\"}")
        return "\n".join(lines)

    def show_model_roles(self) -> None:
        text = self.team_roles_text()
        self.chat.add_notice(text, "info")
        self.switch_view("chat")

    def agents_text(self) -> str:
        agents = self.agent_manager.snapshot()
        if not agents:
            return "No team agents have run yet. /team <goal> starts a run."
        lines = [
            f"{'AGENT':<8} {'ROLE':<10} {'STATE':<9} {'MODEL':<24} {'TOOLS':>5} {'TIME':>7} TASK"
        ]
        for agent in agents:
            lines.append(
                f"{agent['id']:<8} {agent['role']:<10} {agent['state']:<9} "
                f"{(agent['model'] or '—').replace('models/', '')[:24]:<24} "
                f"{agent['tool_calls']:>5} {agent['elapsed']:>6.0f}s {agent['task_title'][:40]}"
            )
        if self.team.scheduler is not None:
            lines.append("")
            lines.append(self.team.scheduler.summary_text())
        return "\n".join(lines)

    def copy_text(self, text: str, label: str = "text") -> Any:
        """Copy through the real clipboard manager; never pretend it worked.

        The OS call is blocking (PowerShell can take ~0.5 s), so it runs in a
        worker and reports the result when it returns.
        """
        if not (text or "").strip():
            self.notify_user(f"Nothing to copy ({label})", "warn", timeout=2)
            return None
        try:
            return self.run_worker(
                self._copy_text_worker(text, label), group="clipboard", exclusive=False
            )
        except Exception:
            # No running app loop (unit context): fall back to the direct call.
            result = self.clipboard_manager.copy(text)
            self._show_copy_result(result, label)
            return None

    async def _copy_text_worker(self, text: str, label: str) -> None:
        result = await self.clipboard_manager.copy_async(text)
        self._show_copy_result(result, label)

    def _show_copy_result(self, result: Any, label: str) -> None:
        self.notify_user(
            f"{label}: {result.message}" if result.ok else f"Copy failed: {result.message}",
            "ok" if result.ok else "error",
            timeout=3,
        )

    def copy_selection(self) -> None:
        for screen in reversed(self.screen_stack):
            getter = getattr(screen, "get_selected_text", None)
            if callable(getter):
                try:
                    text = getter()
                except Exception:
                    text = ""
                if text:
                    self.copy_text(text, "selection")
                    return
        self.notify_user("Drag to select text first, or click an answer footer", "warn", timeout=3)

    # ── computer ──────────────────────────────────────────────────
    def _computer_event(self, kind: str, text: str, data: dict[str, Any]) -> None:
        self.computer_events.append((time.time(), kind, text, data))
        try:
            self.query_one(ComputerView).push_event(kind, text, data)
        except Exception:
            pass

    def make_computer_driver(self, ctx: ToolContext | None = None) -> ComputerDriver:
        return ComputerDriver(
            self.service,
            self.registry,
            self.settings,
            self.computer_backend,
            self.computer_guard,
            ctx or self.tool_context(agent="computer"),
            on_event=self._computer_event,
        )

    def set_computer_enabled(self, enabled: bool) -> None:
        self.settings.computer_enabled = bool(enabled)
        self.settings.save()
        self._sync_computer_tools()
        self.refresh_chrome()
        try:
            self.query_one(ComputerView).refresh_state()
        except Exception:
            pass
        if enabled:
            self.notify_user("Computer control enabled — input still asks for approval", "ok", timeout=6)
            if not self.computer_guard.backend_available:
                self.notify_user(
                    "This platform has no input backend; observation may still work.", "warn", timeout=6
                )
        else:
            self.notify_user("Computer control disabled", "info")

    async def computer_action(self, kind: str, argument: str = "") -> None:
        ctx = self.tool_context(agent="computer")
        try:
            if kind == "screenshot":
                output = await self.registry.call(
                    "computer_screenshot", {"preview": True, "preview_width": 72}, ctx
                )
            elif kind == "windows":
                output = await self.registry.call("computer_windows", {"limit": 30}, ctx)
            elif kind == "task":
                output = await self.registry.call("computer_task", {"task": argument}, ctx)
            else:
                return
        except ToolError as error:
            self.notify_user(str(error), "error", timeout=8)
            return
        self.chat.add_notice(output, "ok")
        try:
            self.query_one(ComputerView).push_event(kind, output[:1200], {})
        except Exception:
            pass

    def computer_status_text(self) -> str:
        guard = self.computer_guard.state()
        lines = [
            f"enabled     {'yes' if guard['enabled'] else 'no'}",
            f"backend     {getattr(self.computer_backend, 'name', '?')} · "
            f"{'available' if guard['available'] else 'no input backend'}",
            f"actions     {guard['actions']} total · {guard['per_minute']}/min · {guard['blocked']} blocked",
            f"screenshots {len(list((CONFIG_DIR / 'logs' / 'screens').glob('shot-*.png')))} saved",
        ]
        try:
            info = self.computer_backend.screen_info()
            lines.insert(0, f"screen      {info.width}x{info.height} px")
        except Exception as error:
            lines.insert(0, f"screen      unavailable ({error})")
        return "\n".join(lines)

    # ── workspace rebinding ───────────────────────────────────────
    def rebind_workspace(self) -> None:
        workspace = self.settings.workspace
        self.memory.workspace = workspace
        self.registry.remove_source("skills")
        self.skills = SkillLibrary(workspace)
        self.skills.register_tools(self.registry)
        self.code_store = CodeStore(
            workspace, max_files=max(100, int(self.settings.code_index_max_files or 5000))
        )
        self.lazy_index = LazyIndex(self.code_store)
        self.retriever = Retriever(self.lazy_index, workspace, embed=self._embed_texts)
        self.registry.remove_prefix("code_")
        register_code_tools(self.registry, self.code_store)
        self.cowork.workspace = workspace
        self.cowork.retriever = self.retriever
        self.cowork.registry = self.registry
        self.refresh_chrome()

    # ══════════════════════════════════════════════════════════════
    # Overlays (modal screens: the base screen is never remounted)
    # ══════════════════════════════════════════════════════════════
    def open_palette(self) -> None:
        if len(self.screen_stack) > 1:
            return
        self.push_screen(PaletteScreen())

    def open_help(self) -> None:
        if len(self.screen_stack) > 1:
            return
        self.push_screen(HelpScreen())

    # ══════════════════════════════════════════════════════════════
    # Views
    # ══════════════════════════════════════════════════════════════
    def switch_view(self, key: str) -> None:
        if key not in VIEW_KEYS:
            return
        if len(self.screen_stack) > 1:
            return
        self._current_view = key
        try:
            self.switcher.current = key
            view = self.query_one(f"#{key}")
            if self.settings.animations:
                view.styles.opacity = 0.6
                view.animate("styles.opacity", 1.0, duration=0.18, easing="out_cubic")
        except Exception:
            pass
        self.sidebar.update_nav(key)
        if key == "chat":
            self.chat.focus_composer()
        else:
            try:
                self.set_focus(self.query_one(f"#{key}", Container))
            except Exception:
                pass
        self.refresh_chrome()

    def action_show_view(self, key: str) -> None:
        if len(self.screen_stack) > 1:
            return
        self.switch_view(key)

    def focus_view(self) -> None:
        if self._current_view == "chat":
            self.chat.focus_composer()

    # ══════════════════════════════════════════════════════════════
    # Sessions
    # ══════════════════════════════════════════════════════════════
    def _restore_or_new(self) -> Session:
        last = self.settings.last_session
        if last:
            session = self.store.load(last)
            if session is not None:
                session.model = self.settings.model
                return session
        return Session(model=self.settings.model, workspace=self.settings.workspace)

    async def _render_session(self) -> None:
        await self.chat.clear()
        if self.session.messages:
            for message in self.session.messages:
                await self.chat.add_restored(message)
        else:
            self.chat.add_welcome()
        self.refresh_chrome()

    async def new_session(self) -> None:
        if self.is_streaming:
            self.cancel_turn()
        self.store.save(self.session)
        self.session = Session(model=self.settings.model, workspace=self.settings.workspace)
        self.settings.last_session = self.session.id
        self.settings.save()
        await self._render_session()
        self.switch_view("chat")
        self.notify_user("New session started", "ok")

    async def resume_session(self, identifier: str = "") -> None:
        sessions = self.store.list(40)
        self._session_list = sessions
        if not sessions:
            self.notify_user("No saved sessions yet", "warn")
            return
        if not identifier:
            lines = []
            for index, session in enumerate(sessions[:12], 1):
                lines.append(
                    f"{index:>2}. {session.title[:44]:<44} {session.model.replace('gemini-', '')[:18]:<18} "
                    f"{format_tokens(session.usage.total):>6} tok  {session.age_label}"
                )
            self.chat.add_notice(
                "Saved sessions (use /resume <number|id>):\n" + "\n".join(lines), "info"
            )
            return
        chosen = None
        if identifier.isdigit():
            index = int(identifier) - 1
            if 0 <= index < len(sessions):
                chosen = sessions[index]
        else:
            chosen = next((session for session in sessions if session.id.startswith(identifier)), None)
        if chosen is None:
            self.notify_user(f"No session matching {identifier!r}", "error")
            return
        self.store.save(self.session)
        self.session = chosen
        self.session.model = self.settings.model
        self.settings.last_session = chosen.id
        self.settings.save()
        await self._render_session()
        self.switch_view("chat")
        self.notify_user(f"Resumed {chosen.title[:40]}", "ok")

    def save_session(self) -> None:
        try:
            self.session.model = self.settings.model
            self.session.workspace = self.settings.workspace
            self.store.save(self.session)
            self.settings.last_session = self.session.id
            self.settings.save()
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════
    # Prompt flow
    # ══════════════════════════════════════════════════════════════
    def submit_prompt(self, text: str) -> None:
        self.run_worker(self._handle_submit(text), exclusive=False, group="submit")

    async def _handle_submit(self, text: str) -> None:
        parsed = parse_slash(text)
        if parsed is not None:
            name, argument = parsed
            self.chat.clear_composer()
            await self.run_command(name, argument)
            return
        if self.is_streaming:
            self.notify_user("Still generating — press esc to cancel first", "warn")
            return
        self._maybe_suggest_team(text)
        await self._start_turn(text)

    def _maybe_suggest_team(self, text: str) -> None:
        """Suggest the supervised team for genuinely multi-step prompts only."""
        if not self.settings.team_auto or self.team.running:
            return
        if not looks_complex(text):
            return
        key = " ".join(text.split())[:200]
        if key in self._team_suggested:
            return
        self._team_suggested.add(key)
        if len(self._team_suggested) > 50:
            self._team_suggested.clear()
        self.chat.add_notice(
            "This looks multi-step. Run /team <goal> to let the supervisor plan it, "
            "split it across roles and route each task to a capable model. "
            "Send it here to answer solo, or /solo to hide these hints.",
            "info",
        )

    async def _start_turn(self, prompt: str) -> None:
        from google.genai import types

        if not self.session.messages:
            self.session.messages.clear()
        self.chat.clear_composer()
        self.chat.composer.remember(prompt)
        self.chat.hide_slash_hints()
        self.chat.add_user(prompt)
        self.session.messages.append(Message(role="user", text=prompt))
        self.session.contents.append(
            types.Content(role="user", parts=[types.Part(text=prompt)])
        )
        self.session.auto_title()
        self.settings.last_session = self.session.id
        self.save_session()
        self.refresh_chrome()
        self._turn_worker = self.run_worker(self._run_turn(prompt), exclusive=False, group="turn")

    async def _run_turn(self, prompt: str = "") -> None:
        from google.genai import types

        self._turn_started = time.time()
        self.stream_elapsed = 0.0
        self.set_state("THINKING")
        active = None
        turn_text: list[str] = []
        turn_thoughts: list[str] = []
        turn_tools: list[dict[str, Any]] = []
        final_usage: dict[str, Any] = {}
        cards: dict[str, Any] = {}

        ctx = self.tool_context(extras={"todos": self.todos})

        ambient = ""
        try:
            ambient = await self._ambient_context(prompt)
        except Exception:
            ambient = ""
        if not prompt:
            for content in reversed(self.session.contents):
                for part in getattr(content, "parts", None) or []:
                    if getattr(part, "text", None):
                        prompt = str(part.text)
                        break
                if prompt:
                    break
        try:
            trimmed, report = self.context_manager.trim(self.session.contents)
            if report.dropped:
                self.session.contents = trimmed
                self._context_dropped = report.dropped
                if report.note:
                    ambient = (ambient + "\n\n" + report.note).strip()
                self.notify_user(
                    f"Context trimmed: {report.dropped} earlier entries condensed", "warn", timeout=5
                )
            else:
                self._context_dropped = 0
        except Exception:
            pass

        try:
            async for event in self.service.run_turn(
                self.session.contents,
                self.settings.model,
                self.registry,
                ctx,
                system_suffix=ambient,
            ):
                if event.kind == "text":
                    if active is None:
                        active = await self.chat.begin_assistant(self.settings.model)
                    self.set_state("STREAMING")
                    turn_text.append(event.text)
                    await active.write(event.text)
                    self.chat._scroll_to_end()
                elif event.kind == "thought":
                    if active is None:
                        active = await self.chat.begin_assistant(self.settings.model)
                    turn_thoughts.append(event.text)
                    active.add_thought(event.text)
                elif event.kind == "tool_start":
                    if active is not None:
                        await self.chat.end_assistant(final_usage or None)
                        active = None
                    self.set_state("TOOL")
                    card = self.chat.add_tool_card(
                        event.data["id"],
                        event.data["name"],
                        event.data["args"],
                        event.data.get("dangerous", False),
                    )
                    cards[event.data["id"]] = card
                elif event.kind == "tool_end":
                    card = cards.get(event.data["id"])
                    if card is not None:
                        card.finish(
                            event.data.get("ok", False),
                            event.data.get("output", ""),
                            float(event.data.get("elapsed", 0)),
                        )
                    turn_tools.append(
                        {
                            "id": event.data["id"],
                            "name": event.data["name"],
                            "args": event.data.get("args", {}),
                            "ok": event.data.get("ok", False),
                            "output": event.data.get("output", ""),
                            "elapsed": event.data.get("elapsed", 0),
                        }
                    )
                    self.set_state("THINKING")
                elif event.kind == "usage":
                    final_usage = event.data
                elif event.kind == "notice":
                    self.chat.add_notice(event.text, "warn")
                elif event.kind == "retry":
                    self.notify_user(event.text, "warn")
                elif event.kind == "error":
                    if active is not None:
                        await self.chat.end_assistant(final_usage or None)
                        active = None
                    self.chat.add_notice(event.text, "error")
                    self.set_state("ERROR")
                elif event.kind == "done":
                    final_usage = event.data.get("usage", final_usage)
                    self.last_latency = float(event.data.get("elapsed", 0) or 0)
                elif event.kind == "status":
                    if self._state not in ("TOOL", "WAITING"):
                        self.set_state("THINKING")
        except asyncio.CancelledError:
            if active is not None:
                await self.chat.end_assistant(final_usage or None)
                active = None
            self.chat.add_notice("Generation cancelled", "warn")
            self.set_state("READY")
            self.save_session()
            raise
        except Exception as error:
            if active is not None:
                await self.chat.end_assistant(final_usage or None)
                active = None
            self.chat.add_notice(friendly_error(error), "error")
            self.set_state("ERROR")
        finally:
            if active is not None:
                await self.chat.end_assistant(final_usage or None)
            text = "".join(turn_text)
            if text or turn_thoughts or turn_tools:
                usage = Usage()
                usage.merge(final_usage, calls=max(1, int((final_usage or {}).get("calls") or 1)))
                self.session.usage.merge(final_usage, calls=usage.calls)
                self.session.messages.append(
                    Message(
                        role="assistant",
                        text=text,
                        thoughts="".join(turn_thoughts),
                        usage=usage,
                        tools=turn_tools,
                        meta={"model": self.settings.model},
                    )
                )
                if text:
                    self._last_assistant_text = text
            self.save_session()
            self._turn_worker = None
            self.stream_elapsed = 0.0
            if self._state != "ERROR":
                self.set_state("READY")
            else:
                self.set_timer(2.5, lambda: self.set_state("READY"))
            self.refresh_chrome()

    def cancel_turn(self) -> None:
        if self._turn_worker is not None and not self._turn_worker.is_finished:
            self._turn_worker.cancel()
            self._turn_worker = None
            self.set_state("READY")
            self.notify_user("Cancelled", "warn")

    async def approve_tool(self, name: str, args: dict[str, Any], description: str) -> bool:
        if not self.is_mounted:
            return False
        # Team workers may request approvals concurrently; serialize the modal.
        async with self._approval_lock:
            previous = self._state
            self.set_state("WAITING")
            try:
                allowed = await self.push_screen_wait(ApprovalScreen(name, args, description))
            except Exception:
                allowed = False
            finally:
                self.set_state(previous if previous in ("THINKING", "STREAMING", "TOOL", "TEAM") else "THINKING")
            return bool(allowed)

    # ══════════════════════════════════════════════════════════════
    # Commands
    # ══════════════════════════════════════════════════════════════
    def _register_commands(self) -> None:
        commands = self.commands
        from .commands import Command

        def add(name: str, description: str, handler: Any, category: str = "general", usage: str = "", aliases: tuple[str, ...] = ()) -> None:
            commands.add(
                Command(
                    name=name,
                    description=description,
                    usage=usage,
                    category=category,
                    handler=handler,
                    aliases=aliases,
                )
            )

        async def cmd_help(app: "GeminiStudio", argument: str) -> None:
            app.show_help()

        async def cmd_new(app: "GeminiStudio", argument: str) -> None:
            await app.new_session()

        async def cmd_resume(app: "GeminiStudio", argument: str) -> None:
            await app.resume_session(argument)

        async def cmd_sessions(app: "GeminiStudio", argument: str) -> None:
            await app.resume_session("")

        async def cmd_rename(app: "GeminiStudio", argument: str) -> None:
            if not argument:
                app.notify_user(f"Current title: {app.session.title}", "info")
                return
            app.session.title = argument.strip()[:80]
            app.save_session()
            app.refresh_chrome()
            app.notify_user("Session renamed", "ok")

        async def cmd_clear(app: "GeminiStudio", argument: str) -> None:
            app.action_clear_transcript()

        async def cmd_models(app: "GeminiStudio", argument: str) -> None:
            if argument:
                app.select_model(argument)
            else:
                app.switch_view("models")

        async def cmd_model(app: "GeminiStudio", argument: str) -> None:
            await cmd_models(app, argument)

        async def cmd_think(app: "GeminiStudio", argument: str) -> None:
            if not argument:
                app.notify_user(f"Thinking level: {app.settings.thinking_level}", "info")
                return
            level = argument.strip().lower()
            if level not in THINKING_CYCLE:
                app.notify_user(f"Use one of: {', '.join(THINKING_CYCLE)}", "warn")
                return
            app.settings.thinking_level = level
            app.settings.save()
            app.refresh_chrome()
            app.notify_user(f"Thinking level → {level}", "ok")

        async def cmd_temp(app: "GeminiStudio", argument: str) -> None:
            if not argument:
                app.notify_user(f"Temperature: {app.settings.temperature}", "info")
                return
            try:
                value = float(argument)
            except ValueError:
                app.notify_user("Temperature must be a number between 0 and 2", "warn")
                return
            app.settings.temperature = max(0.0, min(2.0, value))
            app.settings.save()
            app.notify_user(f"Temperature → {app.settings.temperature}", "ok")

        async def cmd_system(app: "GeminiStudio", argument: str) -> None:
            if not argument:
                current = app.settings.system_prompt or system_prompt(app.settings)
                app.chat.add_notice("SYSTEM PROMPT\n" + current[:4000], "info")
                return
            app.settings.system_prompt = argument
            app.settings.save()
            app.notify_user("System prompt updated for new turns", "ok")

        async def cmd_tokens(app: "GeminiStudio", argument: str) -> None:
            count = await app.service.count_tokens(app.settings.model, app.session.contents)
            app.notify_user(f"Context: {count:,} tokens (API count)", "info", timeout=6)

        async def cmd_export(app: "GeminiStudio", argument: str) -> None:
            path = await app.export_markdown(argument)
            app.notify_user(f"Exported to {path}", "ok", timeout=6)

        async def cmd_copy(app: "GeminiStudio", argument: str) -> None:
            app.action_copy_last()

        async def cmd_mcp(app: "GeminiStudio", argument: str) -> None:
            await app.mcp_command(argument)

        async def cmd_skills(app: "GeminiStudio", argument: str) -> None:
            app.switch_view("skills")

        async def cmd_skill(app: "GeminiStudio", argument: str) -> None:
            if not argument:
                app.switch_view("skills")
                return
            await app.load_skill_into_chat(argument)

        async def cmd_tools(app: "GeminiStudio", argument: str) -> None:
            app.switch_view("tools")

        async def cmd_plugins(app: "GeminiStudio", argument: str) -> None:
            app.switch_view("plugins")

        async def cmd_dashboard(app: "GeminiStudio", argument: str) -> None:
            app.switch_view("dashboard")

        async def cmd_chat(app: "GeminiStudio", argument: str) -> None:
            app.switch_view("chat")

        async def cmd_run(app: "GeminiStudio", argument: str) -> None:
            if not argument:
                app.notify_user("Usage: /run <shell command>", "warn")
                return
            ctx = ToolContext(
                cwd=app.settings.workspace,
                workspace=app.settings.workspace,
                app=app,
                settings=app.settings,
                approve=app.approve_tool,
                notify=app.notify_user,
            )
            try:
                output = await app.registry.call("shell_run", {"command": argument}, ctx)
                app.chat.add_notice(output, "ok")
            except ToolError as error:
                app.chat.add_notice(str(error), "error")

        async def cmd_cwd(app: "GeminiStudio", argument: str) -> None:
            if not argument:
                app.notify_user(app.settings.workspace, "info", timeout=6)
                return
            path = Path(argument).expanduser()
            if not path.is_absolute():
                path = Path(app.settings.workspace) / path
            if not path.is_dir():
                app.notify_user(f"Not a directory: {path}", "error")
                return
            app.settings.workspace = str(path.resolve())
            app.settings.save()
            app.rebind_workspace()
            app.refresh_chrome()
            app.notify_user(f"Workspace → {app.settings.workspace}", "ok", timeout=6)

        async def cmd_image(app: "GeminiStudio", argument: str) -> None:
            if not argument:
                app.notify_user("Usage: /image <prompt>", "warn")
                return
            await app.generate_image(argument)

        async def cmd_embed(app: "GeminiStudio", argument: str) -> None:
            if not argument:
                app.notify_user("Usage: /embed <text>", "warn")
                return
            await app.embed_text(argument)

        async def cmd_status(app: "GeminiStudio", argument: str) -> None:
            app.show_status()

        async def cmd_key(app: "GeminiStudio", argument: str) -> None:
            app.set_api_key(argument)

        async def cmd_init(app: "GeminiStudio", argument: str) -> None:
            app.init_project_file()

        async def cmd_compact(app: "GeminiStudio", argument: str) -> None:
            app.compact_session()

        async def cmd_about(app: "GeminiStudio", argument: str) -> None:
            app.show_about()

        async def cmd_quit(app: "GeminiStudio", argument: str) -> None:
            app.exit()

        # ── agent commands ────────────────────────────────────────
        async def cmd_cowork(app: "GeminiStudio", argument: str) -> None:
            if not argument.strip():
                app.switch_view("cowork")
                return
            app.start_cowork(argument, execute=True)

        async def cmd_plan(app: "GeminiStudio", argument: str) -> None:
            if not argument.strip():
                app.notify_user("Usage: /plan <task>", "warn")
                return
            app.start_cowork(argument, execute=False)

        async def cmd_team(app: "GeminiStudio", argument: str) -> None:
            text = argument.strip()
            if not text:
                app.switch_view("team")
                return
            parts = text.split(maxsplit=1)
            verb = parts[0].lower()
            rest = parts[1].strip() if len(parts) > 1 else ""
            if verb == "plan":
                if not rest:
                    app.notify_user("Usage: /team plan <goal>", "warn")
                    return
                app.start_team(rest, execute=False)
            elif verb == "status":
                app.chat.add_notice("AGENT TEAM\n" + app.team_status_text(), "info")
            elif verb == "models":
                app.chat.add_notice("MODEL ROLES\n" + app.team_roles_text(), "info")
            elif verb == "cancel":
                app.cancel_team()
            elif verb == "pause":
                count = app.team.pause() if app.team.running else 0
                app.notify_user(
                    f"Paused {count} agent(s)" if count else "No active agents to pause", "warn" if count else "info"
                )
            elif verb == "resume":
                count = app.team.resume() if app.team.running else 0
                app.notify_user(
                    f"Resumed {count} agent(s)" if count else "No paused agents", "ok" if count else "info"
                )
            elif verb == "auto":
                if rest.lower() in ("on", "off"):
                    app.settings.team_auto = rest.lower() == "on"
                    app.settings.save()
                    app.notify_user(f"Team suggestions {'on' if app.settings.team_auto else 'off'}", "ok")
                else:
                    app.notify_user(f"Team suggestions are {'on' if app.settings.team_auto else 'off'} · /team auto on|off", "info")
            else:
                app.start_team(text, execute=True)

        async def cmd_solo(app: "GeminiStudio", argument: str) -> None:
            app.settings.team_auto = False
            app.settings.save()
            app.notify_user("Solo mode: team suggestions off; /team still runs explicitly", "ok", timeout=5)

        async def cmd_agents(app: "GeminiStudio", argument: str) -> None:
            if argument.strip().lower() in ("view", "team"):
                app.switch_view("team")
                return
            app.chat.add_notice("AGENT COMMAND CENTER\n" + app.agents_text(), "info")

        async def cmd_computer(app: "GeminiStudio", argument: str) -> None:
            parts = argument.strip().split(maxsplit=1)
            verb = parts[0].lower() if parts else ""
            rest = parts[1].strip() if len(parts) > 1 else ""
            if verb in ("", "status"):
                if verb == "status":
                    app.chat.add_notice("COMPUTER\n" + app.computer_status_text(), "info")
                else:
                    app.switch_view("computer")
                return
            if verb == "enable":
                app.set_computer_enabled(True)
            elif verb == "disable":
                app.set_computer_enabled(False)
            elif verb in ("screenshot", "shot"):
                await app.computer_action("screenshot")
            elif verb in ("windows", "list"):
                await app.computer_action("windows")
            elif verb == "task":
                if not rest:
                    app.notify_user("Usage: /computer task <what to do on screen>", "warn")
                    return
                await app.computer_action("task", rest)
            else:
                app.notify_user(
                    "Usage: /computer [status|enable|disable|screenshot|windows|task <text>]", "warn"
                )

        async def cmd_code(app: "GeminiStudio", argument: str) -> None:
            parts = argument.strip().split(maxsplit=1)
            verb = parts[0].lower() if parts else ""
            rest = parts[1].strip() if len(parts) > 1 else ""
            ctx = app.tool_context()
            try:
                if verb == "index":
                    force = rest.lower() in ("force", "--force", "rebuild")
                    output = await app.registry.call("code_index", {"force": force}, ctx)
                elif verb == "search":
                    output = await app.registry.call("code_search", {"query": rest}, ctx)
                elif verb == "outline":
                    output = await app.registry.call("code_outline", {"path": rest}, ctx)
                elif verb == "review":
                    output = await app.registry.call(
                        "code_review", {"glob": rest} if rest else {}, ctx
                    )
                elif verb == "test":
                    output = await app.registry.call(
                        "code_test", {"command": rest} if rest else {}, ctx
                    )
                else:
                    index = getattr(app.code_store, "index", None)
                    if index is None:
                        app.chat.add_notice(
                            "No code index yet. Run /code index (or call code_index) first.", "info"
                        )
                        return
                    output = index.summary(1400)
            except ToolError as error:
                app.notify_user(str(error), "error", timeout=8)
                return
            app.chat.add_notice(output, "ok")

        async def cmd_context(app: "GeminiStudio", argument: str) -> None:
            data = app.context_usage()
            lines = [
                f"context   {data['tokens']:,} / {data['budget']:,} tokens ({data['percent']}%)",
                f"trimmed   {data['dropped']} earlier entries" if data.get("dropped") else "trimmed   0",
                f"memory    {len(app.memory.entries())} notes · {app.memory.path}",
                f"cowork    {app.cowork_status_text().splitlines()[0][:100]}",
            ]
            index = getattr(app.code_store, "index", None)
            lines.append(index.summary(700) if index is not None else "index     not built (run /code index)")
            app.chat.add_notice("CONTEXT\n" + "\n".join(lines), "info")

        async def cmd_memory(app: "GeminiStudio", argument: str) -> None:
            parts = argument.strip().split(maxsplit=1)
            verb = parts[0].lower() if parts else ""
            rest = parts[1].strip() if len(parts) > 1 else ""
            if verb in ("", "list", "ls"):
                rows = app.memory.entries(scope=rest.lower() if rest in ("global", "project", "session") else "")
                if not rows:
                    app.notify_user(f"No notes yet. File: {app.memory.path}", "info")
                    return
                lines = [
                    f"{entry.key:<30} [{entry.scope:<7}] {entry.summary(110)}" for entry in rows[:60]
                ]
                app.chat.add_notice("MEMORY\n" + "\n".join(lines), "info")
            elif verb == "save":
                key, _, value = rest.partition("::")
                if not key.strip() or not value.strip():
                    app.notify_user("Usage: /memory save <key> :: <value>", "warn")
                    return
                try:
                    entry = app.memory.save(key.strip(), value.strip(), scope="project")
                except ValueError as error:
                    app.notify_user(str(error), "error")
                    return
                app.notify_user(f"Saved memory {entry.key!r} (project)", "ok")
            elif verb == "search":
                if not rest:
                    app.notify_user("Usage: /memory search <query>", "warn")
                    return
                rows = app.memory.search(rest, limit=10)
                if not rows:
                    app.notify_user("No matching memory", "info")
                    return
                app.chat.add_notice(
                    "MEMORY\n" + "\n\n".join(f"[{row.scope}] {row.key}\n{row.value[:800]}" for row in rows),
                    "info",
                )
            elif verb in ("forget", "delete", "rm"):
                if not rest:
                    app.notify_user("Usage: /memory forget <key>", "warn")
                    return
                ok = app.memory.forget(rest)
                app.notify_user(f"Forgetting {rest!r}" if ok else f"No memory named {rest!r}", "ok" if ok else "info")
            else:
                app.notify_user(
                    "Usage: /memory [list [scope]|save <key> :: <value>|search <q>|forget <key>]", "warn"
                )

        async def cmd_audit(app: "GeminiStudio", argument: str) -> None:
            parts = argument.strip().split()
            if parts and parts[0].lower() == "file":
                limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 60
                rows = app.audit.tail_file(limit)
                if not rows:
                    app.notify_user("No audit file entries yet", "info")
                    return
                body = "\n".join(
                    f"{row.time_label} {'ok ' if row.ok else 'ERR'} {row.kind:<9} "
                    f"{(row.tool or row.agent):<26} {(row.detail or row.args)[:150]}"
                    for row in rows
                )
                app.chat.add_notice("AUDIT FILE\n" + body, "info")
                return
            limit = int(parts[0]) if parts and parts[0].isdigit() else 30
            app.chat.add_notice("AUDIT\n" + app.audit.render(limit), "info")

        add("help", "Show keys, commands and tools", cmd_help, "core", aliases=("keys",))
        add("new", "Start a fresh session", cmd_new, "session", aliases=("n",))
        add("resume", "Resume a saved session (/resume <n|id>)", cmd_resume, "session", usage="/resume [n|id]")
        add("sessions", "List saved sessions", cmd_sessions, "session")
        add("rename", "Rename the current session", cmd_rename, "session", usage="/rename <title>")
        add("clear", "Clear the transcript", cmd_clear, "session")
        add("chat", "Open the chat view", cmd_chat, "views")
        add("models", "Switch model (/models <query>)", cmd_models, "model", usage="/models [query]")
        add("model", "Alias for /models", cmd_model, "model", usage="/model [query]")
        add("think", "Set thinking level", cmd_think, "model", usage="/think minimal|low|medium|high")
        add("temp", "Set temperature", cmd_temp, "model", usage="/temp 0.8")
        add("system", "Show or set the system prompt", cmd_system, "model", usage="/system [prompt]")
        add("tokens", "Count current context tokens", cmd_tokens, "model")
        add("export", "Export the session as Markdown", cmd_export, "session", usage="/export [path]")
        add("copy", "Copy the last reply to the clipboard", cmd_copy, "session")
        add("tools", "Open the tools view", cmd_tools, "views")
        add("mcp", "MCP servers: list|connect|disconnect|reload", cmd_mcp, "mcp", usage="/mcp [list|connect name|disconnect name|reload]")
        add("skills", "Open the skills view", cmd_skills, "skills")
        add("skill", "Load a skill into the conversation", cmd_skill, "skills", usage="/skill <name>")
        add("plugins", "Open the plugins view", cmd_plugins, "plugins")
        add("dashboard", "Open the system dashboard", cmd_dashboard, "views", aliases=("dash",))
        add("run", "Run a shell command (approval required)", cmd_run, "tools", usage="/run <command>")
        add("cwd", "Show or change the workspace", cmd_cwd, "tools", usage="/cwd [path]")
        add("image", "Generate an image with Nano Banana", cmd_image, "media", usage="/image <prompt>")
        add("embed", "Embed text with gemini-embedding", cmd_embed, "media", usage="/embed <text>")
        add("key", "Set the Gemini API key", cmd_key, "core", usage="/key <api-key>")
        add("init", "Create a GEMINI.md project brief", cmd_init, "tools")
        add("compact", "Trim old turns from the context", cmd_compact, "session")
        add("status", "Show runtime status", cmd_status, "core")
        add("about", "About Gemini Studio", cmd_about, "core")
        add("cowork", "Run a multi-step job: plan → workers → verifier", cmd_cowork, "agent", usage="/cowork <task>")
        add("plan", "Draft a cowork plan without executing it", cmd_plan, "agent", usage="/plan <task>")
        add("team", "Supervisor + agent team: run|plan|cancel|pause|resume|models|status", cmd_team, "agent", usage="/team [goal|plan <goal>|models|status|cancel]")
        add("solo", "Disable team suggestions and work solo", cmd_solo, "agent")
        add("agents", "Show live agent state and task progress", cmd_agents, "agent", usage="/agents [view]")
        add("computer", "Desktop control: status|enable|disable|screenshot|windows|task", cmd_computer, "agent", usage="/computer [status|enable|screenshot|task <text>]")
        add("code", "Code index: status|index|search|outline|review|test", cmd_code, "agent", usage="/code [index|search <q>|review [glob]|test]")
        add("context", "Show context budget, memory and index status", cmd_context, "agent")
        add("memory", "Memory: list|save key :: value|search <q>|forget <key>", cmd_memory, "agent", usage="/memory [list|save ...]")
        add("audit", "Show the audit trail of tool calls and approvals", cmd_audit, "agent", usage="/audit [n|file [n]]")
        add("quit", "Exit Gemini Studio", cmd_quit, "core", aliases=("exit", "q"))

    async def run_command(self, name: str, argument: str = "") -> None:
        command = self.commands.get(name)
        if command is None:
            self.notify_user(f"Unknown command /{name} — try ^K", "error")
            return
        try:
            await command.run(self, argument)
        except Exception as error:
            self.notify_user(f"/{name} failed: {error}", "error")

    # ══════════════════════════════════════════════════════════════
    # Actions
    # ══════════════════════════════════════════════════════════════
    def action_palette(self) -> None:
        self.open_palette()

    def action_help(self) -> None:
        self.show_help()

    def show_help(self) -> None:
        self.open_help()

    def action_new_session(self) -> None:
        self.run_worker(self.new_session(), exclusive=False, group="session")

    def action_resume_session(self) -> None:
        self.run_worker(self.resume_session(""), exclusive=False, group="session")

    def action_clear_transcript(self) -> None:
        self.run_worker(self._clear_transcript(), exclusive=False, group="session")

    async def _clear_transcript(self) -> None:
        await self.chat.clear()
        self.chat.add_welcome()
        self.chat.focus_composer()
        self.notify_user("Transcript cleared")

    def action_copy_last(self) -> None:
        if not self._last_assistant_text:
            self.notify_user("Nothing to copy yet", "warn")
            return
        self.copy_text(self._last_assistant_text, "last reply")

    def action_copy_selection(self) -> None:
        self.copy_selection()

    def action_cycle_thinking(self) -> None:
        index = THINKING_CYCLE.index(self.settings.thinking_level) if self.settings.thinking_level in THINKING_CYCLE else 1
        self.settings.thinking_level = THINKING_CYCLE[(index + 1) % len(THINKING_CYCLE)]
        self.settings.save()
        self.refresh_chrome()
        self.notify_user(f"Thinking → {self.settings.thinking_level}", "info", timeout=2)

    def action_cycle_approval(self) -> None:
        index = APPROVAL_CYCLE.index(self.settings.approval_mode) if self.settings.approval_mode in APPROVAL_CYCLE else 0
        self.settings.approval_mode = APPROVAL_CYCLE[(index + 1) % len(APPROVAL_CYCLE)]
        self.settings.save()
        self.refresh_chrome()
        self.notify_user(f"Approval mode → {self.settings.approval_mode}", "info", timeout=3)

    def action_escape(self) -> None:
        if len(self.screen_stack) > 1:
            return
        if not self._boot_finished:
            self.finish_boot()
            return
        if self.is_streaming:
            self.cancel_turn()
            return
        if self._current_view != "chat":
            self.switch_view("chat")

    def action_quit_app(self) -> None:
        self.exit()

    # ══════════════════════════════════════════════════════════════
    # Feature helpers
    # ══════════════════════════════════════════════════════════════
    def select_model(self, query: str) -> None:
        model = find_model(self.models, query)
        if model is None:
            self.notify_user(f"No model matches {query!r}", "error")
            return
        if not model.is_chat:
            self.notify_user(f"{model.id} cannot chat — pick a generateContent model", "warn")
            return
        self.settings.model = model.id
        self.settings.save()
        self.refresh_chrome()
        self.notify_user(f"Model → {model.id}", "ok")

    def stage_prompt(self, text: str) -> None:
        self.switch_view("chat")
        composer = self.chat.composer
        composer.text = text
        try:
            composer.move_cursor(composer.document.end)
        except Exception:
            pass
        composer.focus()

    def update_slash_hints(self, text: str) -> None:
        stripped = text.lstrip()
        if stripped.startswith("/") and "\n" not in stripped:
            query = stripped[1:]
            matches = self.commands.search(query, limit=6)
            self.chat.show_slash_hints(query, matches)
        else:
            self.chat.hide_slash_hints()

    async def mcp_command(self, argument: str) -> None:
        argument = argument.strip()
        if not argument:
            self.switch_view("mcp")
            return
        parts = argument.split(maxsplit=1)
        verb = parts[0].lower()
        name = parts[1].strip() if len(parts) > 1 else ""
        if verb in ("list", "ls"):
            rows = self.mcp.summary()
            if not rows:
                self.notify_user("No MCP servers configured", "warn")
                return
            lines = [f"{row['name']:<18} {row['status']:<12} {row['tools']:>3} tools  {row['source']}" for row in rows]
            self.chat.add_notice("MCP SERVERS\n" + "\n".join(lines), "info")
        elif verb == "connect" and name:
            ok, message = await self.mcp.connect(name)
            self.notify_user(message, "ok" if ok else "error", timeout=8)
            self.refresh_chrome()
        elif verb == "disconnect" and name:
            await self.mcp.disconnect(name)
            self.notify_user(f"{name} disconnected", "info")
            self.refresh_chrome()
        elif verb == "reload":
            self.reload_mcp()
            self.notify_user("MCP config reloaded", "ok")
        else:
            self.notify_user("Usage: /mcp [list|connect name|disconnect name|reload]", "warn")

    def reload_mcp(self) -> None:
        self.mcp.load(self.settings.workspace)
        self.refresh_chrome()

    def reload_plugins(self) -> None:
        self.plugins.reload(self)
        self.refresh_chrome()

    async def load_skill_into_chat(self, name: str) -> None:
        from google.genai import types

        skill = self.skills.get(name)
        if skill is None:
            self.notify_user(f"Skill {name!r} not found", "error")
            return
        body = skill.body.strip()[:30000]
        block = (
            f"<skill name=\"{skill.name}\" source=\"{skill.path}\">\n{body}\n</skill>\n\n"
            "Acknowledge internally and follow this skill for the next task. "
            "Do not repeat the skill text to the user."
        )
        self.session.contents.append(
            types.Content(role="user", parts=[types.Part(text=block)])
        )
        self.session.messages.append(
            Message(role="notice", text=f"Loaded skill: {skill.name}", meta={"severity": "ok"})
        )
        self.chat.add_notice(f"Skill loaded: {skill.name} ({skill.words} words)", "ok")
        self.switch_view("chat")
        self.refresh_chrome()

    async def generate_image(self, prompt: str) -> None:
        candidates = [model for model in self.models if model.is_image]
        preferred = "gemini-3.1-flash-image"
        model = next((item.id for item in candidates if item.id == preferred), None)
        if model is None and candidates:
            model = candidates[0].id
        if model is None:
            model = "gemini-3.1-flash-image"
        self.notify_user(f"Generating image with {model}…", "info", timeout=8)
        try:
            output_dir = Path(self.settings.workspace) / "gemini-images"
            paths = await self.service.generate_image(prompt, model, output_dir)
        except Exception as error:
            self.notify_user(f"Image generation failed: {error}", "error", timeout=10)
            return
        self.chat.add_notice(
            "Image generated:\n" + "\n".join(str(path) for path in paths), "ok"
        )
        self.notify_user(f"{len(paths)} image(s) saved", "ok", timeout=6)

    async def embed_text(self, text: str) -> None:
        try:
            vectors = await self.service.embed("gemini-embedding-002", [text])
        except Exception as error:
            self.notify_user(f"Embedding failed: {error}", "error", timeout=8)
            return
        if not vectors:
            self.notify_user("No embedding returned", "warn")
            return
        vector = vectors[0]
        preview = ", ".join(f"{value:.4f}" for value in vector[:8])
        self.chat.add_notice(
            f"Embedding · {len(vector)} dims · gemini-embedding-002\n[{preview}, …]", "ok"
        )

    def set_api_key(self, argument: str) -> None:
        argument = argument.strip()
        if not argument:
            state = "configured" if get_api_key() else "missing"
            self.notify_user(f"API key {state}. Use /key <key> to set it.", "info", timeout=6)
            return
        env_file = CONFIG_DIR / ".env"
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
        except OSError:
            lines = []
        lines = [line for line in lines if not line.startswith("GEMINI_API_KEY=")]
        lines.append(f"GEMINI_API_KEY={argument}")
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ["GEMINI_API_KEY"] = argument
        self.service.reset_client()
        self.notify_user("API key saved to ~/.config/gemini-tui/.env", "ok")
        self.run_worker(self._boot(), exclusive=True, group="boot")

    def show_status(self) -> None:
        rows = [
            f"model        {self.settings.model}",
            f"thinking     {self.settings.thinking_level}",
            f"temperature  {self.settings.temperature}",
            f"approval     {self.settings.approval_mode}",
            f"workspace    {self.settings.workspace}",
            f"session      {self.session.id} · {self.session.turns} turns · {format_tokens(self.session.usage.total)} tokens",
            f"tools        {self.tool_count} enabled / {len(self.registry.all())} registered",
            f"mcp          {self.mcp.ready_count} ready / {len(self.mcp.states)} configured · {self.mcp.total_tools} tools",
            f"skills       {len(self.skills.skills)}",
            f"plugins      {self.plugins.loaded_count} native · {self.plugins.external_count} external",
            f"memory       {len(self.memory.entries())} notes · auto {'on' if self.settings.auto_memory else 'off'}",
            f"context      {self.context_usage()['tokens']:,} / {self.context_usage()['budget']:,} tokens",
            f"computer     {'enabled' if self.settings.computer_enabled else 'disabled'} · "
            f"{getattr(self.computer_backend, 'name', '?')}",
            f"cowork       {'running' if self.cowork_running else 'idle'} · {len(self.plan_store.list(100))} plans",
            f"audit        {'on' if self.audit.enabled else 'off'} · {self.audit.path_for().name}",
            f"api key      {'configured' if self.service.has_key else 'missing'}",
        ]
        self.chat.add_notice("STATUS\n" + "\n".join(rows), "info")

    def show_about(self) -> None:
        self.chat.add_notice(
            f"Gemini Studio v{APP_VERSION}\n"
            "A RED HACKER terminal console for the Gemini API: chat, code and a supervised agent team.\n"
            f"Python {platform.python_version()} · {platform.system()} · {platform.release()}\n"
            "Real OS clipboard copy/paste, capped streaming, syntax-highlighted code blocks,\n"
            "MCP, skills, plugins, sessions, computer control and risk-gated tools.",
            "ok",
        )

    def compact_session(self) -> None:
        contents = self.session.contents
        if len(contents) <= 10:
            self.notify_user("Context is already small", "info")
            return
        keep = contents[-8:]
        dropped = len(contents) - len(keep)
        self.session.contents = keep
        self.notify_user(f"Trimmed {dropped} context entries (kept last 8)", "ok")

    def init_project_file(self) -> None:
        target = Path(self.settings.workspace) / "GEMINI.md"
        if target.exists():
            self.notify_user(f"{target} already exists", "warn")
            return
        content = (
            "# Project brief\n\n"
            "## What this project is\n\n"
            "Describe the product and the problem it solves.\n\n"
            "## Stack\n\n"
            "- language / framework\n- package manager\n- test command\n\n"
            "## Conventions\n\n"
            "- formatting and naming rules\n- files that must not be touched\n\n"
            "## Useful commands\n\n```bash\n# install\n# dev\n# test\n```\n"
        )
        target.write_text(content, encoding="utf-8")
        self.notify_user(f"Created {target}", "ok", timeout=6)

    async def export_markdown(self, argument: str) -> Path:
        target = Path(argument).expanduser() if argument.strip() else Path(self.settings.workspace) / f"gemini-{self.session.id}.md"
        lines = [f"# {self.session.title}", "", f"- session: `{self.session.id}`", f"- model: `{self.settings.model}`", ""]
        for message in self.session.messages:
            if message.role == "user":
                lines += ["## You", "", message.text, ""]
            elif message.role == "assistant":
                lines += ["## Gemini", "", message.text, ""]
                for tool in message.tools:
                    lines.append(f"> tool `{tool.get('name')}` · {'ok' if tool.get('ok') else 'error'}")
                lines.append("")
            elif message.role == "notice":
                lines += [f"> {message.text}", ""]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
        return target

    def open_path(self, path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as error:
            self.notify_user(f"Cannot open {path}: {error}", "error")

    # ══════════════════════════════════════════════════════════════
    # Lifecycle
    # ══════════════════════════════════════════════════════════════
    async def on_unmount(self) -> None:
        try:
            if self.is_streaming:
                self.cancel_turn()
        except Exception:
            pass
        self.save_session()
        try:
            await self.mcp.stop()
        except Exception:
            pass


def run(workspace: str | None = None, model: str | None = None, splash: bool = True) -> None:
    app = GeminiStudio(workspace=workspace, model=model, splash=splash)
    app.run()
