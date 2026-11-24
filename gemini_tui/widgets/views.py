"""The seven views hosted by the single ContentSwitcher."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import psutil
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, ProgressBar, Sparkline, Static

from .. import theme
from ..ai.models import model_icon
from ..animations import (
    format_bytes,
    format_duration,
    format_tokens,
    gradient,
    meter,
    sparkline,
    truncate,
)

STATUS_COLOURS = {
    "ready": theme.GREEN,
    "connecting": theme.AMBER,
    "idle": theme.MUTED,
    "error": theme.RED,
    "disabled": theme.FAINT,
}


class BaseView(Vertical):
    can_focus = False

    def head(self, title: str, subtitle: str = "") -> Static:
        line = Text()
        line.append(f" {title} ", style=f"bold {theme.BG} on {theme.CYAN}")
        line.append("  ")
        line.append(subtitle, style=theme.MUTED)
        widget = Static(line, classes="view-head")
        return widget

    def visible(self) -> bool:
        """Hidden views must not spend CPU polling hardware or timers."""
        try:
            return bool(self.display)
        except Exception:
            return False


# ══════════════════════════════════════════════════════════════════════
# Models
# ══════════════════════════════════════════════════════════════════════
class ModelsView(BaseView):
    def compose(self) -> ComposeResult:
        yield self.head("MODELS", "live catalogue from the Gemini API · enter to switch")
        with Horizontal(classes="filter-row"):
            yield Input(placeholder="Filter models…", id="models-filter")
            yield Button("Refresh", id="models-refresh")
        yield DataTable(id="models-table")
        yield Static(id="models-detail", classes="panel")

    def on_mount(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("", "MODEL", "CTX", "THINK", "TAGS")
        self._models: list[Any] = []
        self._filter = ""
        self._selected: Any = None
        self._render_detail(None)
        self.run_worker(self._load(force=False), exclusive=True)

    async def _load(self, force: bool) -> None:
        detail = self.query_one("#models-detail", Static)
        detail.update(Text("loading model catalogue…", style=theme.MUTED))
        try:
            models = await self.app.service.list_models(force=force)
        except Exception as error:
            detail.update(Text(f"could not load models: {error}", style=theme.RED))
            return
        self._models = models
        self._populate()
        # keep the active model visible
        current = self.app.settings.model
        for index, model in enumerate(self._models):
            if model.id == current:
                try:
                    self.query_one("#models-table", DataTable).move_cursor(row=index)
                except Exception:
                    pass
                self._render_detail(model)
                break
        subtitle = len(self._models)
        self.notify(f"{subtitle} models loaded", timeout=2)

    def _populate(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.clear()
        needle = self._filter.lower().strip()
        active = self.app.settings.model
        count = 0
        for model in self._models:
            haystack = f"{model.id} {model.name} {model.description} {model.tier}".lower()
            if needle and needle not in haystack:
                continue
            icon = model_icon(model)
            name = Text()
            name.append(model.id, style=f"bold {theme.CYAN}" if model.id == active else theme.FG)
            if model.id == active:
                name.append("  active", style=theme.GREEN)
            ctx = Text(model.context_label, style=theme.MUTED)
            think = Text("yes" if model.thinking else "—", style=theme.VIOLET if model.thinking else theme.FAINT)
            tags = Text()
            if model.is_chat:
                tags.append("chat ", style=theme.GREEN)
            if model.is_image:
                tags.append("image ", style=theme.MAGENTA)
            if model.is_embedding:
                tags.append("embed ", style=theme.BLUE)
            if model.is_deep_research:
                tags.append("research ", style=theme.AMBER)
            table.add_row(Text(icon, style=theme.CYAN), name, ctx, think, tags, key=model.id)
            count += 1
        if count == 0:
            table.add_row("", Text("no models match this filter", style=theme.MUTED), "", "", "")

    def _render_detail(self, model: Any) -> None:
        self._selected = model
        detail = self.query_one("#models-detail", Static)
        if model is None:
            detail.update(Text("Select a model to see its limits and capabilities.", style=theme.MUTED))
            return
        body = Text()
        body.append_text(gradient(f"{model_icon(model)} {model.name}", theme.GRADIENT, 0.05, bold=True))
        body.append("\n")
        body.append(model.id, style=theme.CYAN)
        if model.id == self.app.settings.model:
            body.append("   active", style=theme.GREEN)
        body.append("\n\n")
        if model.description:
            body.append(model.description + "\n", style=theme.FG_DIM)
        body.append("\n")
        body.append("context      ", style=theme.FAINT)
        body.append(f"{model.input_limit:,} in", style=theme.FG)
        body.append("   output ", style=theme.FAINT)
        body.append(f"{model.output_limit:,}\n", style=theme.FG)
        body.append("thinking     ", style=theme.FAINT)
        body.append("yes" if model.thinking else "no", style=theme.VIOLET if model.thinking else theme.MUTED)
        body.append("     actions ", style=theme.FAINT)
        body.append(", ".join(model.actions[:4]) or "—", style=theme.MUTED)
        body.append("\n\n")
        body.append("enter ", style=f"bold {theme.CYAN}")
        body.append("use this model    ", style=theme.MUTED)
        body.append("/think ", style=f"bold {theme.CYAN}")
        body.append("change reasoning effort", style=theme.MUTED)
        detail.update(body)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "models-filter":
            return
        self._filter = event.value
        self._populate()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "models-table":
            return
        model = next((item for item in self._models if item.id == event.row_key.value), None)
        if model is not None:
            self._render_detail(model)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "models-table":
            return
        model = next((item for item in self._models if item.id == event.row_key.value), None)
        if model is not None and model.is_chat:
            self.app.select_model(model.id)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "models-refresh":
            self.run_worker(self._load(force=True), exclusive=True)


# ══════════════════════════════════════════════════════════════════════
# Tools
# ══════════════════════════════════════════════════════════════════════
class ToolsView(BaseView):
    def compose(self) -> ComposeResult:
        yield self.head("TOOLS", "capabilities exposed to the model · space toggles")
        yield DataTable(id="tools-table")
        yield Static(id="tools-detail", classes="panel")

    def on_mount(self) -> None:
        table = self.query_one("#tools-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("", "TOOL", "CATEGORY", "SOURCE", "WRITE")
        self._tools: list[Any] = []
        self._refresh()
        self.set_interval(4.0, self._refresh)

    def _refresh(self) -> None:
        if not self.visible():
            return
        table = self.query_one("#tools-table", DataTable)
        tools = self.app.registry.all()
        if [tool.name for tool in tools] == [tool.name for tool in self._tools] and table.row_count:
            return
        position = table.cursor_row
        self._tools = tools
        table.clear()
        for tool in tools:
            name = Text(tool.name, style=theme.FG if tool.enabled else theme.FAINT)
            if not tool.enabled:
                name.append("  off", style=theme.FAINT)
            category = Text(tool.category, style=theme.MUTED)
            source = Text(tool.source, style=theme.FAINT)
            write = Text("write" if tool.dangerous else "read", style=theme.AMBER if tool.dangerous else theme.GREEN)
            table.add_row(Text(tool.icon, style=theme.VIOLET), name, category, source, write, key=tool.name)
        try:
            table.move_cursor(row=min(position, max(0, table.row_count - 1)))
        except Exception:
            pass
        if self._tools:
            self._render_detail(self._tools[min(position, len(self._tools) - 1)])

    def _render_detail(self, tool: Any) -> None:
        detail = self.query_one("#tools-detail", Static)
        if tool is None:
            detail.update(Text("No tools registered.", style=theme.MUTED))
            return
        body = Text()
        body.append(f"{tool.icon} {tool.name}", style=f"bold {theme.CYAN}")
        if tool.dangerous:
            body.append("   needs approval", style=theme.AMBER)
        body.append("\n")
        body.append(tool.signature, style=theme.FG_DIM)
        body.append("\n\n")
        body.append(tool.description, style=theme.FG_DIM)
        body.append("\n\n")
        body.append("category ", style=theme.FAINT)
        body.append(tool.category, style=theme.MUTED)
        body.append("    source ", style=theme.FAINT)
        body.append(tool.source, style=theme.MUTED)
        body.append("    state ", style=theme.FAINT)
        body.append("enabled" if tool.enabled else "disabled", style=theme.GREEN if tool.enabled else theme.RED)
        params = (tool.parameters or {}).get("properties", {})
        if params:
            body.append("\n\nparameters\n", style=theme.FAINT)
            for key, spec in list(params.items())[:8]:
                body.append(f"  {key}", style=theme.CYAN)
                body.append(f": {spec.get('type', 'any')}", style=theme.MUTED)
                if spec.get("description"):
                    body.append(f" — {truncate(str(spec['description']), 90)}", style=theme.FAINT)
                body.append("\n")
        detail.update(body)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "tools-table":
            return
        tool = self.app.registry.get(event.row_key.value or "")
        if tool:
            self._render_detail(tool)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "tools-table":
            return
        self._toggle(event.row_key.value or "")

    def _toggle(self, name: str) -> None:
        tool = self.app.registry.get(name)
        if tool is None:
            return
        tool.enabled = not tool.enabled
        self.notify(f"{name} {'enabled' if tool.enabled else 'disabled'}", timeout=2)
        table = self.query_one("#tools-table", DataTable)
        table.clear()
        self._refresh()


# ══════════════════════════════════════════════════════════════════════
# MCP
# ══════════════════════════════════════════════════════════════════════
class MCPView(BaseView):
    def compose(self) -> ComposeResult:
        yield self.head("MCP", "servers discovered from opencode, claude, cursor and this app")
        with Horizontal(classes="filter-row"):
            yield Button("Connect", id="mcp-connect", classes="-primary")
            yield Button("Disconnect", id="mcp-disconnect")
            yield Button("Reload config", id="mcp-reload")
            yield Button("Connect all", id="mcp-connect-all")
        yield DataTable(id="mcp-table")
        yield Static(id="mcp-detail", classes="panel")
        yield Static("SERVER TOOLS", classes="panel-title")
        yield DataTable(id="mcp-tools-table")

    def on_mount(self) -> None:
        servers = self.query_one("#mcp-table", DataTable)
        servers.cursor_type = "row"
        servers.zebra_stripes = True
        servers.add_columns("", "SERVER", "STATUS", "TOOLS", "SOURCE", "CALLS")
        tools = self.query_one("#mcp-tools-table", DataTable)
        tools.cursor_type = "row"
        tools.zebra_stripes = True
        tools.add_columns("TOOL", "DESCRIPTION")
        self._selected = ""
        self._refresh()
        self.set_interval(2.5, self._refresh)

    def _refresh(self) -> None:
        if not self.visible():
            return
        table = self.query_one("#mcp-table", DataTable)
        rows = self.app.mcp.summary()
        position = table.cursor_row
        table.clear()
        for row in rows:
            colour = STATUS_COLOURS.get(row["status"], theme.MUTED)
            icon = "●" if row["status"] == "ready" else "○" if row["status"] == "idle" else "◍"
            name = Text(row["name"], style=f"bold {theme.FG}")
            status = Text(f"{icon} {row['status']}", style=colour)
            tools = Text(str(row["tools"]), style=theme.CYAN if row["tools"] else theme.FAINT)
            source = Text(row["source"], style=theme.FAINT)
            calls = Text(str(row["calls"]), style=theme.MUTED)
            table.add_row(Text(icon, style=colour), name, status, tools, source, calls, key=row["name"])
        try:
            table.move_cursor(row=min(position, max(0, table.row_count - 1)))
        except Exception:
            pass
        self._refresh_tools()
        if rows and not self._selected:
            self._selected = rows[0]["name"]
        self._render_detail()

    def _refresh_tools(self) -> None:
        table = self.query_one("#mcp-tools-table", DataTable)
        table.clear()
        server = self._selected
        if not server:
            table.add_row(Text("—", style=theme.FAINT), Text("select a server", style=theme.FAINT))
            return
        state = self.app.mcp.states.get(server)
        if state is None:
            table.add_row(Text("—", style=theme.FAINT), Text("unknown server", style=theme.FAINT))
            return
        if not state.tools:
            hint = "connect this server to list its tools" if state.status != "ready" else "no tools"
            table.add_row(Text("—", style=theme.FAINT), Text(hint, style=theme.FAINT))
            return
        for tool in state.tools:
            table.add_row(
                Text(tool.name, style=theme.CYAN),
                Text(truncate(tool.description or "", 90), style=theme.MUTED),
            )

    def _render_detail(self) -> None:
        detail = self.query_one("#mcp-detail", Static)
        server = self._selected
        state = self.app.mcp.states.get(server) if server else None
        if state is None:
            detail.update(Text("Select a server.", style=theme.MUTED))
            return
        config = state.config
        body = Text()
        body.append(f"⬡ {config.name}", style=f"bold {theme.CYAN}")
        body.append(f"   {state.status}", style=STATUS_COLOURS.get(state.status, theme.MUTED))
        body.append("\n")
        body.append("transport  ", style=theme.FAINT)
        body.append(config.transport, style=theme.MUTED)
        body.append("    source ", style=theme.FAINT)
        body.append(config.source, style=theme.MUTED)
        body.append("    tools ", style=theme.FAINT)
        body.append(str(len(state.tools)), style=theme.CYAN)
        body.append("\n")
        body.append("command    ", style=theme.FAINT)
        body.append(config.label[:160], style=theme.FG_DIM)
        if config.env:
            body.append("\n")
            body.append("env        ", style=theme.FAINT)
            body.append(", ".join(config.env)[:140], style=theme.MUTED)
        if state.error:
            body.append("\n")
            body.append("error      ", style=theme.FAINT)
            body.append(state.error[:400], style=theme.RED)
        body.append("\n\n")
        body.append("enter ", style=f"bold {theme.CYAN}")
        body.append("connect / disconnect    ", style=theme.MUTED)
        body.append("mcp tools auto-register for the model", style=theme.FAINT)
        detail.update(body)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "mcp-table":
            return
        self._selected = event.row_key.value or ""
        self._refresh_tools()
        self._render_detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "mcp-table":
            return
        self._selected = event.row_key.value or ""
        self.run_worker(self._toggle_server(self._selected), exclusive=False)

    async def _toggle_server(self, name: str) -> None:
        state = self.app.mcp.states.get(name)
        if state is None:
            return
        if state.status == "ready":
            await self.app.mcp.disconnect(name)
            self.app.notify_user(f"{name} disconnected", "info")
        else:
            ok, message = await self.app.mcp.connect(name)
            self.app.notify_user(message, "ok" if ok else "error")
        self._refresh()
        self.app.refresh_chrome()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button == "mcp-connect" and self._selected:
            self.run_worker(self._toggle_server(self._selected), exclusive=False)
        elif button == "mcp-disconnect" and self._selected:
            self.run_worker(self._disconnect(self._selected), exclusive=False)
        elif button == "mcp-reload":
            self.run_worker(self._reload(), exclusive=True)
        elif button == "mcp-connect-all":
            self.run_worker(self._connect_all(), exclusive=False)

    async def _disconnect(self, name: str) -> None:
        await self.app.mcp.disconnect(name)
        self.app.notify_user(f"{name} disconnected", "info")
        self._refresh()
        self.app.refresh_chrome()

    async def _reload(self) -> None:
        self.app.reload_mcp()
        self.app.notify_user("MCP configuration reloaded", "ok")
        self._refresh()

    async def _connect_all(self) -> None:
        names = [row["name"] for row in self.app.mcp.summary()]
        for name in names:
            ok, message = await self.app.mcp.connect(name)
            self.app.notify_user(message, "ok" if ok else "warn")
        self._refresh()
        self.app.refresh_chrome()


# ══════════════════════════════════════════════════════════════════════
# Skills
# ══════════════════════════════════════════════════════════════════════
class SkillsView(BaseView):
    def compose(self) -> ComposeResult:
        yield self.head("SKILLS", "SKILL.md folders from opencode, claude and this app")
        with Horizontal(classes="filter-row"):
            yield Input(placeholder="Filter skills…", id="skills-filter")
            yield Button("Reload", id="skills-reload")
        yield DataTable(id="skills-table")
        with VerticalScroll(id="skills-detail", classes="panel"):
            yield Static(id="skills-detail-body")

    def on_mount(self) -> None:
        table = self.query_one("#skills-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("SKILL", "SOURCE", "SIZE", "DESCRIPTION")
        self._filter = ""
        self._selected = None
        self._populate()
        self._render_detail(None)

    def _populate(self) -> None:
        table = self.query_one("#skills-table", DataTable)
        table.clear()
        needle = self._filter.lower().strip()
        for skill in self.app.skills.skills:
            haystack = f"{skill.name} {skill.description} {skill.source}".lower()
            if needle and needle not in haystack:
                continue
            name = Text(skill.name, style=f"bold {theme.CYAN}")
            source = Text(skill.source, style=theme.MUTED if skill.source == "gemini-tui" else theme.FAINT)
            size = Text(f"{skill.words} w", style=theme.FAINT)
            description = Text(truncate(skill.description, 74), style=theme.FG_DIM)
            table.add_row(name, source, size, description, key=skill.name)

    def _render_detail(self, skill: Any) -> None:
        detail = self.query_one("#skills-detail-body", Static)
        self._selected = skill
        if skill is None:
            detail.update(Text("Select a skill to preview its instructions.", style=theme.MUTED))
            return
        body = Text()
        body.append(f"◈ {skill.name}", style=f"bold {theme.CYAN}")
        body.append(f"   {skill.source}", style=theme.FAINT)
        body.append("\n")
        body.append(str(skill.path), style=theme.MUTED)
        body.append("\n\n")
        body.append(skill.description or "(no description)", style=theme.FG_DIM)
        body.append("\n\n")
        body.append("enter ", style=f"bold {theme.CYAN}")
        body.append("stage this skill in the composer    ", style=theme.MUTED)
        body.append("/skill ", style=f"bold {theme.CYAN}")
        body.append("load into the conversation", style=theme.MUTED)
        body.append("\n\n")
        body.append(skill.body.strip()[:2600], style=theme.FAINT)
        detail.update(body)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "skills-filter":
            return
        self._filter = event.value
        self._populate()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "skills-table":
            return
        skill = self.app.skills.get(event.row_key.value or "")
        if skill:
            self._render_detail(skill)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "skills-table":
            return
        name = event.row_key.value or ""
        self.app.stage_prompt(f"/skill {name} ")
        self.app.notify_user(f"Staged /skill {name}", "info")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "skills-reload":
            self.app.skills.reload()
            self._populate()
            self.app.notify_user(f"{len(self.app.skills.skills)} skills loaded", "ok")
            self.app.refresh_chrome()


# ══════════════════════════════════════════════════════════════════════
# Plugins
# ══════════════════════════════════════════════════════════════════════
class PluginsView(BaseView):
    def compose(self) -> ComposeResult:
        yield self.head("PLUGINS", "native Python plugins + installed opencode plugins")
        with Horizontal(classes="filter-row"):
            yield Button("Reload plugins", id="plugins-reload", classes="-primary")
            yield Button("Open plugin folder", id="plugins-open")
        yield DataTable(id="plugins-table")
        yield Static(id="plugins-detail", classes="panel")

    def on_mount(self) -> None:
        table = self.query_one("#plugins-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("PLUGIN", "RUNTIME", "KIND", "STATUS", "VERSION")
        self._selected = None
        self._populate()
        self._render_detail(None)

    def _populate(self) -> None:
        table = self.query_one("#plugins-table", DataTable)
        table.clear()
        plugins = self.app.plugins.plugins
        if not plugins:
            table.add_row(
                Text("no plugins found", style=theme.MUTED),
                Text("—"), Text("—"), Text("—"), Text("—"),
            )
            return
        for plugin in plugins:
            name = Text(plugin.name, style=f"bold {theme.FG}")
            runtime = Text(plugin.runtime, style=theme.CYAN if plugin.source == "native" else theme.VIOLET)
            kind = Text(plugin.kind, style=theme.MUTED)
            status = Text(
                plugin.status,
                style=theme.GREEN if plugin.status == "loaded" else theme.RED if plugin.status == "error" else theme.FAINT,
            )
            version = Text(plugin.version or "—", style=theme.FAINT)
            table.add_row(name, runtime, kind, status, version, key=plugin.name)

    def _render_detail(self, plugin: Any) -> None:
        detail = self.query_one("#plugins-detail", Static)
        self._selected = plugin
        if plugin is None:
            detail.update(
                Text(
                    "Drop a .py plugin in ~/.config/gemini-tui/plugins to add tools and commands.\n"
                    "Plugins exposed by opencode are detected here and can be used through the "
                    "opencode bridge.",
                    style=theme.MUTED,
                )
            )
            return
        body = Text()
        body.append(f"⊞ {plugin.name}", style=f"bold {theme.CYAN}")
        body.append(f"   {plugin.runtime} · {plugin.kind}\n", style=theme.FAINT)
        body.append(plugin.path, style=theme.MUTED)
        body.append("\n\n")
        body.append(plugin.description or "(no description)", style=theme.FG_DIM)
        if plugin.version:
            body.append(f"\n\nversion  {plugin.version}", style=theme.MUTED)
        if plugin.status == "error":
            body.append("\n\n")
            body.append(plugin.error, style=theme.RED)
        if plugin.tools:
            body.append("\n\ntools    ", style=theme.FAINT)
            body.append(", ".join(plugin.tools), style=theme.CYAN)
        if plugin.commands:
            body.append("\ncommands ", style=theme.FAINT)
            body.append(", ".join("/" + c for c in plugin.commands), style=theme.CYAN)
        detail.update(body)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "plugins-table":
            return
        name = event.row_key.value
        plugin = next((p for p in self.app.plugins.plugins if p.name == name), None)
        if plugin:
            self._render_detail(plugin)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "plugins-reload":
            self.app.reload_plugins()
            self._populate()
            self._render_detail(None)
            self.notify("plugins reloaded", timeout=2)
        elif event.button.id == "plugins-open":
            self.app.open_path(self.app.plugins_dir)


# ══════════════════════════════════════════════════════════════════════
# Dashboard
# ══════════════════════════════════════════════════════════════════════
class DashboardView(BaseView):
    def compose(self) -> ComposeResult:
        yield self.head("DASHBOARD", "machine, session and task telemetry")
        with Vertical(id="dash-grid"):
            with Vertical(classes="dash-card"):
                yield Static("⚡ SYSTEM", classes="panel-title")
                with Horizontal(classes="meter-row"):
                    yield Static("CPU", classes="meter-label")
                    yield ProgressBar(total=100, show_eta=False, id="dash-cpu-bar")
                    yield Static("0%", id="dash-cpu-value", classes="meter-value")
                yield Sparkline([], id="dash-cpu-spark")
                with Horizontal(classes="meter-row"):
                    yield Static("RAM", classes="meter-label")
                    yield ProgressBar(total=100, show_eta=False, id="dash-ram-bar")
                    yield Static("0%", id="dash-ram-value", classes="meter-value")
                with Horizontal(classes="meter-row"):
                    yield Static("DISK", classes="meter-label")
                    yield ProgressBar(total=100, show_eta=False, id="dash-disk-bar")
                    yield Static("0%", id="dash-disk-value", classes="meter-value")
                yield Static(id="dash-system-extra")
            with Vertical(classes="dash-card"):
                yield Static("⇅ NETWORK", classes="panel-title")
                yield Static(id="dash-net-up")
                yield Static(id="dash-net-down")
                yield Static(id="dash-net-total")
                yield Static("TOKENS", classes="panel-title")
                yield Static(id="dash-tokens")
            with Vertical(classes="dash-card"):
                yield Static("◈ SESSION", classes="panel-title")
                yield Static(id="dash-session")
                yield Static("TASKS", classes="panel-title")
                yield Static(id="dash-todos")
            with Vertical(classes="dash-card"):
                yield Static("⌁ PROCESSES", classes="panel-title")
                yield DataTable(id="dash-processes")

    def on_mount(self) -> None:
        self._last_net = psutil.net_io_counters()
        self._last_net_time = time.time()
        self._cpu_history: list[float] = []
        self._net_up: list[float] = []
        self._net_down: list[float] = []
        table = self.query_one("#dash-processes", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("PID", width=6)
        table.add_column("NAME", width=15)
        table.add_column("CPU%", width=5)
        table.add_column("MEM%", width=5)
        self.set_interval(1.0, self._update_fast)
        self.set_interval(3.0, self._update_processes)
        self._update_fast()
        self._update_processes()

    def _update_fast(self) -> None:
        if not self.visible():
            return
        self.run_worker(self._update_fast_async(), exclusive=True, group="dash-fast")

    async def _update_fast_async(self) -> None:
        if not self.visible():
            return
        anchor = str(self.app.settings.workspace)[:3] or "/"

        def _sample():
            cpu_val = psutil.cpu_percent(interval=None)
            mem_val = psutil.virtual_memory()
            try:
                disk_val = psutil.disk_usage(anchor)
            except OSError:
                disk_val = None
            cpu_cnt = psutil.cpu_count(logical=True)
            boot_ts = psutil.boot_time()
            net_now = psutil.net_io_counters()
            return cpu_val, mem_val, disk_val, cpu_cnt, boot_ts, net_now

        cpu, memory, disk, cpu_count, boot_time, now = await asyncio.to_thread(_sample)
        if not self.is_mounted:
            return

        self.query_one("#dash-cpu-bar", ProgressBar).update(progress=cpu)
        self.query_one("#dash-cpu-value", Static).update(f"{cpu:.0f}%")
        self._cpu_history = (self._cpu_history + [cpu])[-40:]
        self.query_one("#dash-cpu-spark", Sparkline).data = list(self._cpu_history)

        self.query_one("#dash-ram-bar", ProgressBar).update(progress=memory.percent)
        self.query_one("#dash-ram-value", Static).update(
            f"{memory.percent:.0f}% · {format_bytes(memory.used)}"
        )
        if disk:
            self.query_one("#dash-disk-bar", ProgressBar).update(progress=disk.percent)
            self.query_one("#dash-disk-value", Static).update(
                f"{disk.percent:.0f}% · {format_bytes(disk.free)} free"
            )

        extra = Text()
        extra.append(f"threads {cpu_count}", style=theme.FAINT)
        extra.append(f"  ·  uptime {format_duration(time.time() - boot_time)}", style=theme.FAINT)
        self.query_one("#dash-system-extra", Static).update(extra)

        elapsed = max(1e-6, time.time() - self._last_net_time)
        up = (now.bytes_sent - self._last_net.bytes_sent) / elapsed
        down = (now.bytes_recv - self._last_net.bytes_recv) / elapsed
        self._last_net, self._last_net_time = now, time.time()
        self._net_up = (self._net_up + [up])[-40:]
        self._net_down = (self._net_down + [down])[-40:]

        net_up = Text()
        net_up.append("↑ ", style=theme.BLUE)
        net_up.append(f"{format_bytes(up)}/s", style=theme.FG)
        net_up.append("  ", style="")
        net_up.append(sparkline(self._net_up, 22), style=theme.BLUE)
        self.query_one("#dash-net-up", Static).update(net_up)

        net_down = Text()
        net_down.append("↓ ", style=theme.GREEN)
        net_down.append(f"{format_bytes(down)}/s", style=theme.FG)
        net_down.append("  ", style="")
        net_down.append(sparkline(self._net_down, 22), style=theme.GREEN)
        self.query_one("#dash-net-down", Static).update(net_down)

        total = Text()
        total.append(f"total ↑ {format_bytes(now.bytes_sent)} · ↓ {format_bytes(now.bytes_recv)}", style=theme.FAINT)
        self.query_one("#dash-net-total", Static).update(total)

        session = self.app.session
        tokens = Text()
        usage = session.usage if session else None
        total_tokens = usage.total if usage else 0
        tokens.append(f"{format_tokens(total_tokens)} tokens", style=f"bold {theme.CYAN}")
        tokens.append(f"   {usage.calls if usage else 0} calls", style=theme.MUTED)
        tokens.append(f"   {format_tokens(usage.thoughts if usage else 0)} thinking", style=theme.VIOLET)
        tokens.append("\n")
        tokens.append(meter(min(1.0, total_tokens / 1_000_000), 30), style=theme.CYAN_DIM)
        tokens.append("  context", style=theme.FAINT)
        self.query_one("#dash-tokens", Static).update(tokens)

        info = Text()
        info.append(f"model      {self.app.settings.model}\n", style=theme.FG_DIM)
        info.append(f"thinking   {self.app.settings.thinking_level}\n", style=theme.FG_DIM)
        info.append(f"approval   {self.app.settings.approval_mode}\n", style=theme.FG_DIM)
        info.append(f"mcp        {self.app.mcp.ready_count}/{len(self.app.mcp.states)} ready\n", style=theme.FG_DIM)
        info.append(f"tools      {self.app.tool_count} registered\n", style=theme.FG_DIM)
        info.append(f"latency    {self.app.last_latency:.2f}s" if self.app.last_latency else "latency    —", style=theme.FG_DIM)
        self.query_one("#dash-session", Static).update(info)

        todos = self.app.todos
        if todos:
            body = Text()
            for item in todos[:8]:
                status = item.get("status", "pending")
                glyph = "●" if status == "active" else "✔" if status == "done" else "○"
                colour = theme.CYAN if status == "active" else theme.GREEN if status == "done" else theme.MUTED
                body.append(f"{glyph} ", style=colour)
                body.append(item.get("text", "")[:60] + "\n", style=theme.FG_DIM if status != "done" else theme.FAINT)
            self.query_one("#dash-todos", Static).update(body)
        else:
            self.query_one("#dash-todos", Static).update(Text("no tasks yet", style=theme.FAINT))

    def _update_processes(self) -> None:
        if not self.visible():
            return
        self.run_worker(self._update_processes_async(), exclusive=True, group="dash-procs")

    async def _update_processes_async(self) -> None:
        if not self.visible():
            return

        def _collect():
            rows = []
            for process in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
                rows.append(process.info)
            rows.sort(key=lambda row: row.get("cpu_percent") or 0, reverse=True)
            return rows[:7]

        rows = await asyncio.to_thread(_collect)
        if not self.is_mounted:
            return
        table = self.query_one("#dash-processes", DataTable)
        table.clear()
        for row in rows:
            table.add_row(
                Text(str(row["pid"]), style=theme.FAINT),
                Text((row.get("name") or "?")[:15], style=theme.FG_DIM),
                Text(f"{row.get('cpu_percent') or 0:.1f}", style=theme.CYAN),
                Text(f"{row.get('memory_percent') or 0:.1f}", style=theme.VIOLET),
            )


# ══════════════════════════════════════════════════════════════════════
# Cowork
# ══════════════════════════════════════════════════════════════════════
STEP_COLOURS = {
    "pending": theme.MUTED,
    "active": theme.AMBER,
    "done": theme.GREEN,
    "failed": theme.RED,
    "skipped": theme.FAINT,
}


class CoworkView(BaseView):
    """Live view of a cowork plan: steps, worker events and the final verdict."""

    def compose(self) -> ComposeResult:
        yield self.head("COWORK", "plan → scoped worker agents → verifier")
        with Horizontal(id="cowork-toolbar"):
            yield Input(placeholder="Describe a multi-step job…", id="cowork-task")
            yield Button("Run", id="cowork-run", classes="-primary")
            yield Button("Plan only", id="cowork-plan")
            yield Button("Cancel", id="cowork-cancel")
        with Horizontal(id="cowork-body"):
            with Vertical(id="cowork-side", classes="panel"):
                yield Static("PLAN", classes="panel-title")
                yield Static(id="cowork-summary")
            yield DataTable(id="cowork-steps")
        yield Static(id="cowork-stream", classes="panel")
        yield Static("AGENT LOG", classes="panel-title")
        yield Static(id="cowork-log", classes="panel")

    def on_mount(self) -> None:
        table = self.query_one("#cowork-steps", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("#", key="num", width=3)
        table.add_column("STEP", key="title")
        table.add_column("AGENT", key="agent", width=15)
        table.add_column("STATUS", key="status", width=10)
        table.add_column("TIME", key="time", width=7)
        self._log_lines: list[str] = []
        self._streams: dict[str, str] = {}
        self._rows: dict[str, str] = {}
        self._last_log = ""
        self._show_idle()

    # ── lifecycle ─────────────────────────────────────────────────
    def _show_idle(self) -> None:
        self.query_one("#cowork-summary", Static).update(
            Text(
                "No run yet.\n\nDescribe a job that needs several tools, then press Run.\n"
                "The planner splits it, each step gets only the tools it needs, and the "
                "verifier checks the result before it is reported done.",
                style=theme.MUTED,
            )
        )
        self.query_one("#cowork-stream", Static).update(Text("", style=theme.FAINT))
        self.query_one("#cowork-log", Static).update(Text("", style=theme.FAINT))

    def begin(self, task: str, execute: bool) -> None:
        self._log_lines = []
        self._streams = {}
        self._rows = {}
        self._last_log = ""
        table = self.query_one("#cowork-steps", DataTable)
        table.clear()
        summary = Text()
        summary.append("TASK\n", style=f"bold {theme.CYAN}")
        summary.append(task + "\n\n", style=theme.FG_DIM)
        summary.append("mode  ", style=theme.FAINT)
        summary.append("execute" if execute else "plan only", style=theme.AMBER if execute else theme.MUTED)
        summary.append("\nstatus  ", style=theme.FAINT)
        summary.append("starting…", style=theme.MUTED)
        self.query_one("#cowork-summary", Static).update(summary)
        self.query_one("#cowork-stream", Static).update(Text("assembling context…", style=theme.FAINT))
        self._set_log("queued")

    def _set_log(self, line: str = "") -> None:
        if line:
            self._log_lines.append(line)
            self._log_lines = self._log_lines[-120:]
        body = "\n".join(self._log_lines)[-7000:]
        self.query_one("#cowork-log", Static).update(Text(body, style=theme.FG_DIM))

    def _append(self, line: str) -> None:
        if line == self._last_log:
            return
        self._last_log = line
        self._set_log(line)

    # ── events ────────────────────────────────────────────────────
    def on_agent_event(self, event: Any) -> None:
        kind = getattr(event, "kind", "")
        text = str(getattr(event, "text", "") or "")
        agent = str(getattr(event, "agent", "") or "lead")
        step_id = str(getattr(event, "step_id", "") or "")
        data = dict(getattr(event, "data", {}) or {})

        if kind == "plan":
            steps = data.get("steps") or []
            table = self.query_one("#cowork-steps", DataTable)
            table.clear()
            self._rows = {}
            for index, step in enumerate(steps, 1):
                key = str(step.get("id") or f"s{index}")
                self._rows[key] = key
                table.add_row(
                    Text(str(index), style=theme.FAINT),
                    Text(str(step.get("title", ""))[:70], style=theme.FG_DIM),
                    Text(str(step.get("agent", "")), style=theme.VIOLET),
                    Text("pending", style=theme.MUTED),
                    Text("—", style=theme.FAINT),
                    key=key,
                )
            plan_id = data.get("plan_id", "")
            self._append(f"plan {plan_id} · {len(steps)} step(s)")
            self.query_one("#cowork-stream", Static).update(
                Text(f"plan {plan_id} created · starting workers", style=theme.CYAN)
            )
            self.app.refresh_chrome()
        elif kind == "step_start":
            index = data.get("index") or 0
            total = data.get("total") or 0
            self._append(f"▶ step {index}/{total} [{agent}] {text[:80]}")
            self._update_step(step_id, status="active")
            self.query_one("#cowork-stream", Static).update(
                Text(f"[{agent}] step {index}/{total}: {text}", style=theme.AMBER)
            )
        elif kind == "step_text":
            buffer = (self._streams.get(step_id, "") + text)[-1200:]
            self._streams[step_id] = buffer
            self.query_one("#cowork-stream", Static).update(
                Text(f"[{agent}] {buffer[-900:]}", style=theme.FG_DIM)
            )
        elif kind == "step_tool":
            phase = data.get("phase", "")
            tool = data.get("name", "") or text[:40]
            if phase == "start":
                self._append(f"  ⚙ [{agent}] {tool}")
            elif phase == "end":
                ok = data.get("ok", True)
                self._append(f"  {'✔' if ok else '✖'} [{agent}] {tool} {str(text)[:160]}")
        elif kind == "step_done":
            status = str(data.get("status") or "done")
            elapsed = float(data.get("elapsed") or 0)
            self._update_step(step_id, status=status, elapsed=elapsed)
            self._append(f"{'✔' if status == 'done' else '✖'} step done [{agent}] {text[:140]}")
            self.query_one("#cowork-stream", Static).update(
                Text(f"[{agent}] {status}: {text[:600]}", style=STEP_COLOURS.get(status, theme.MUTED))
            )
        elif kind == "verify":
            self._append(f"verify: {text[:200]}")
            self.query_one("#cowork-stream", Static).update(Text(f"verdict · {text}", style=theme.CYAN))
        elif kind == "done":
            self._append("run finished")
            self.query_one("#cowork-stream", Static).update(Text(text[:900], style=theme.GREEN))
        elif kind == "error":
            self._append(f"error: {text[:240]}")
            self.query_one("#cowork-stream", Static).update(Text(text[:900], style=theme.RED))
        elif kind == "notice":
            self._append(f"notice: {text[:200]}")
        elif kind == "status":
            self.query_one("#cowork-stream", Static).update(Text(text, style=theme.FAINT))

    def _update_step(self, step_id: str, *, status: str = "", elapsed: float = 0.0) -> None:
        if not step_id or step_id not in self._rows:
            return
        table = self.query_one("#cowork-steps", DataTable)
        if status:
            table.update_cell(step_id, "status", Text(status, style=STEP_COLOURS.get(status, theme.MUTED)))
        if elapsed:
            table.update_cell(step_id, "time", Text(f"{elapsed:.1f}s", style=theme.FAINT))

    def finish(self, result: Any) -> None:
        plan = getattr(result, "plan", None)
        summary = Text()
        summary.append("TASK\n", style=f"bold {theme.CYAN}")
        summary.append(str(getattr(plan, "task", "") or "") + "\n\n", style=theme.FG_DIM)
        summary.append("status  ", style=theme.FAINT)
        status = str(getattr(result, "status", ""))
        colour = theme.GREEN if status in ("done", "unverified", "planned") else theme.RED if status == "failed" else theme.AMBER
        summary.append(status, style=f"bold {colour}")
        summary.append("\nsteps   ", style=theme.FAINT)
        summary.append(f"{getattr(plan, 'completed', 0)}/{len(getattr(plan, 'steps', []) or [])} done", style=theme.MUTED)
        summary.append("\ntime    ", style=theme.FAINT)
        summary.append(f"{float(getattr(result, 'elapsed', 0) or 0):.1f}s", style=theme.MUTED)
        verdict = str(getattr(plan, "verdict", "") or "")
        if verdict:
            summary.append("\n\nverdict\n", style=theme.FAINT)
            summary.append(verdict[:600], style=theme.FG_DIM)
        report = str(getattr(result, "report_path", "") or "")
        if report:
            summary.append("\n\nreport\n", style=theme.FAINT)
            summary.append(report, style=theme.CYAN)
        self.query_one("#cowork-summary", Static).update(summary)
        self._append(f"finished · {status}")

    # ── controls ──────────────────────────────────────────────────
    def _submit(self, execute: bool) -> None:
        field = self.query_one("#cowork-task", Input)
        task = field.value.strip()
        if not task:
            self.app.notify_user("Describe the job first", "warn")
            return
        self.app.start_cowork(task, execute=execute)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cowork-run":
            self._submit(True)
        elif event.button.id == "cowork-plan":
            self._submit(False)
        elif event.button.id == "cowork-cancel":
            self.app.cancel_cowork()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "cowork-task":
            self._submit(True)


# ══════════════════════════════════════════════════════════════════════
# Agent Team · live command center
# ══════════════════════════════════════════════════════════════════════
TEAM_STATE_COLOURS = {
    "queued": theme.MUTED,
    "running": theme.AMBER,
    "paused": theme.PLUM,
    "cancelling": theme.RED,
    "done": theme.GREEN,
    "failed": theme.RED,
    "cancelled": theme.FAINT,
    "pending": theme.MUTED,
    "retry": theme.AMBER,
    "skipped": theme.FAINT,
}


class TeamView(BaseView):
    """Everything shown here comes from the live orchestrator, never a mock."""

    def compose(self) -> ComposeResult:
        yield self.head("AGENT TEAM", "supervisor · capability routing · bounded parallel workers")
        with Horizontal(id="team-toolbar"):
            yield Input(placeholder="Goal for the team…", id="team-goal")
            yield Button("Run", id="team-run", classes="-primary")
            yield Button("Plan only", id="team-plan")
            yield Button("Pause", id="team-pause")
            yield Button("Cancel", id="team-cancel")
        with Horizontal(id="team-body"):
            with Vertical(id="team-left"):
                yield Static(id="team-summary", classes="panel")
                yield ProgressBar(total=100, show_eta=False, id="team-progress")
                yield DataTable(id="team-tasks")
            with Vertical(id="team-right"):
                yield DataTable(id="team-agents")
                yield Static(id="team-log", classes="panel")
        with Horizontal(classes="filter-row"):
            yield Button("Model roles", id="team-roles")
            yield Button("Copy plan", id="team-copy")
            yield Button("Open report", id="team-report")

    def on_mount(self) -> None:
        tasks = self.query_one("#team-tasks", DataTable)
        tasks.cursor_type = "row"
        tasks.zebra_stripes = True
        tasks.add_column("ID", width=4, key="id")
        tasks.add_column("TASK", key="title")
        tasks.add_column("ROLE", width=10, key="role")
        tasks.add_column("STATE", width=9, key="state")
        tasks.add_column("TIME", width=7, key="time")
        agents = self.query_one("#team-agents", DataTable)
        agents.cursor_type = "row"
        agents.zebra_stripes = True
        agents.add_column("AGENT", width=8, key="agent")
        agents.add_column("ROLE", width=9, key="role")
        agents.add_column("MODEL", width=22, key="model")
        agents.add_column("STATE", width=8, key="state")
        agents.add_column("TOOLS", width=5, key="tools")
        self._planned: dict[str, Any] = {}
        self._task_rows: set[str] = set()
        self._agent_rows: set[str] = set()
        self._selected_agent = ""
        self._summary_text = "No run yet. Describe a goal and the supervisor will plan it."
        self._last_report = ""
        self._log_frozen = False
        self._render_summary()
        self.set_interval(1.0, self._tick)

    # ── rendering ─────────────────────────────────────────────────
    def _render_summary(self) -> None:
        body = Text()
        team = getattr(self.app, "team", None)
        if team is None or (team.plan is None and not self._planned):
            body.append("SUPERVISOR\n", style=f"bold {theme.CRIMSON}")
            body.append(self._summary_text + "\n\n", style=theme.FG_DIM)
            body.append("roles  ", style=theme.FAINT)
            body.append("architect · researcher · planner · developer · debugger · tester · security · writer\n", style=theme.MUTED)
            body.append("limits ", style=theme.FAINT)
            body.append(
                f"{getattr(self.app.settings, 'team_max_parallel', 2)} parallel · "
                f"{getattr(self.app.settings, 'team_task_timeout', 300)}s timeout · "
                f"{getattr(self.app.settings, 'team_max_retries', 2)} retries",
                style=theme.MUTED,
            )
            self.query_one("#team-summary", Static).update(body)
            return
        plan = team.plan
        if plan is not None:
            body.append("GOAL\n", style=f"bold {theme.CRIMSON}")
            body.append(plan.goal[:600] + "\n\n", style=theme.FG_DIM)
            body.append("planner ", style=theme.FAINT)
            body.append(
                (plan.planner_model or "rule-based fallback") + ("  (fallback)" if plan.fallback else ""),
                style=theme.PLUM if plan.fallback else theme.METAL,
            )
            body.append("   tasks ", style=theme.FAINT)
            body.append(str(len(plan.tasks)), style=theme.FG)
            if plan.notes:
                body.append("\nnotes   ", style=theme.FAINT)
                body.append(plan.notes[:400], style=theme.MUTED)
        if self._last_report:
            body.append("\n\nreport  ", style=theme.FAINT)
            body.append(self._last_report, style=theme.METAL)
        self.query_one("#team-summary", Static).update(body)

    def begin(self, goal: str) -> None:
        self._task_rows.clear()
        self._agent_rows.clear()
        self._planned = {}
        self._selected_agent = ""
        self._last_report = ""
        self._log_frozen = False
        for table_id in ("#team-tasks", "#team-agents"):
            self.query_one(table_id, DataTable).clear()
        self._summary_text = f"planning…\n{goal[:500]}"
        self.query_one("#team-progress", ProgressBar).update(progress=0)
        self.query_one("#team-log", Static).update(Text("waiting for the supervisor…", style=theme.FAINT))
        self._render_summary()

    def apply_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("kind") or "")
        if kind == "team_plan":
            plan = dict(event.get("plan") or {})
            for task in plan.get("tasks") or []:
                self._planned[str(task.get("id") or "")] = task
            self._summary_text = str(event.get("summary") or "")
            self._render_summary()
            self._tick()
        elif kind == "team_task":
            task = dict(event.get("task") or {})
            if task.get("id"):
                self._planned[str(task["id"])] = task
            self._tick()
        elif kind == "team_done":
            self.finish(dict(event.get("result") or {}))
        elif kind == "team_agent_log":
            agent_id = str(event.get("agent_id") or "")
            team = getattr(self.app, "team", None)
            if (
                agent_id
                and agent_id == self._selected_agent
                and not self._log_frozen
                and team is not None
                and team.manager is not None
            ):
                log = team.manager.render_log(agent_id, 40)
                if log:
                    self.query_one("#team-log", Static).update(Text(log[-6000:], style=theme.FG_DIM))
        elif kind == "team_start":
            self.query_one("#team-log", Static).update(
                Text(f"starting {event.get('tasks', 0)} task(s)", style=theme.CYAN)
            )

    def fail(self, message: str) -> None:
        self._summary_text = f"error: {message}"
        self._log_frozen = True
        self._render_summary()
        self.query_one("#team-log", Static).update(Text(message[:3000], style=theme.RED))

    def finish(self, result: Any) -> None:
        if not isinstance(result, dict):
            result = {}
        status = str(result.get("status") or "failed")
        colour = {
            "done": theme.GREEN,
            "partial": theme.AMBER,
            "cancelled": theme.MUTED,
        }.get(status, theme.RED)
        tasks = result.get("tasks") or []
        done = sum(1 for task in tasks if task.get("status") == "done")
        self._summary_text = (
            f"{status.upper()} · {done}/{len(tasks)} tasks done · "
            f"{float(result.get('elapsed') or 0):.1f}s"
        )
        self._last_report = str(result.get("report_path") or "")
        self._render_summary()
        self.query_one("#team-progress", ProgressBar).update(
            progress=100 if status == "done" else (done / len(tasks) * 100 if tasks else 0)
        )
        body = Text()
        body.append(f"{status.upper()}\n", style=f"bold {colour}")
        if result.get("error"):
            body.append(str(result["error"]) + "\n", style=theme.RED)
        verification = str(result.get("verification") or "")
        if verification:
            body.append("\nVERIFICATION\n", style=f"bold {theme.METAL}")
            body.append(verification[:2500] + "\n", style=theme.FG_DIM)
        report = str(result.get("report_path") or "")
        if report:
            body.append("\nreport " + report, style=theme.METAL)
        body.append("\n\nselect an agent row and press enter to view its log", style=theme.FAINT)
        self._log_frozen = True
        self.query_one("#team-log", Static).update(body)
        self.query_one("#team-tasks", DataTable).clear()
        self._task_rows.clear()
        self._tick(force=True)

    def _tick(self, force: bool = False) -> None:
        if not force and not self.visible():
            return
        team = getattr(self.app, "team", None)
        if team is None:
            return
        scheduler = team.scheduler
        tasks = scheduler.all() if scheduler is not None else []
        if tasks:
            table = self.query_one("#team-tasks", DataTable)
            for task in tasks:
                values = (
                    Text(task.id, style=theme.FAINT),
                    Text(task.title[:80], style=theme.FG_DIM if task.status != "done" else theme.FG),
                    Text(task.role, style=theme.PLUM),
                    Text(task.status, style=TEAM_STATE_COLOURS.get(task.status, theme.MUTED)),
                    Text(f"{task.elapsed:.0f}s" if task.started_at else "—", style=theme.FAINT),
                )
                if task.id in self._task_rows:
                    for column, value in zip(("id", "title", "role", "state", "time"), values):
                        try:
                            table.update_cell(task.id, column, value)
                        except Exception:
                            pass
                else:
                    table.add_row(*values, key=task.id)
                    self._task_rows.add(task.id)
            try:
                progress = team.scheduler.progress * 100 if team.scheduler else 0
                self.query_one("#team-progress", ProgressBar).update(progress=progress)
            except Exception:
                pass

        agents = team.manager.all() if team.manager is not None else []
        if agents:
            table = self.query_one("#team-agents", DataTable)
            for agent in agents:
                state = "paused" if agent.state == "paused" else agent.state
                values = (
                    Text(agent.id, style=theme.CRIMSON),
                    Text(agent.role, style=theme.PLUM),
                    Text(agent.model.replace("models/", "")[:22] or "—", style=theme.METAL),
                    Text(state, style=TEAM_STATE_COLOURS.get(state, theme.MUTED)),
                    Text(str(agent.tool_calls), style=theme.FAINT),
                )
                if agent.id in self._agent_rows:
                    for column, value in zip(("agent", "role", "model", "state", "tools"), values):
                        try:
                            table.update_cell(agent.id, column, value)
                        except Exception:
                            pass
                else:
                    table.add_row(*values, key=agent.id)
                    self._agent_rows.add(agent.id)
            if not self._selected_agent and agents:
                self._selected_agent = agents[0].id
            if self._selected_agent and team.manager is not None and not self._log_frozen:
                log = team.manager.render_log(self._selected_agent, 30)
                if log:
                    self.query_one("#team-log", Static).update(Text(log[-6000:], style=theme.FG_DIM))

    def _show_agent_log(self, agent_id: str, *, unlock: bool = False) -> None:
        if unlock:
            self._log_frozen = False
        team = getattr(self.app, "team", None)
        if not agent_id or team is None or team.manager is None:
            return
        log = team.manager.render_log(agent_id, 40)
        if log:
            self.query_one("#team-log", Static).update(Text(log[-6000:], style=theme.FG_DIM))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "team-agents":
            return
        self._selected_agent = str(event.row_key.value or "")
        # After a run, highlighting must not replace the final verification.
        if not self._log_frozen:
            self._show_agent_log(self._selected_agent)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "team-agents":
            return
        self._selected_agent = str(event.row_key.value or "")
        self._show_agent_log(self._selected_agent, unlock=True)

    # ── controls ──────────────────────────────────────────────────
    def _submit(self, execute: bool) -> None:
        field = self.query_one("#team-goal", Input)
        goal = field.value.strip()
        if not goal:
            self.app.notify_user("Describe the goal first", "warn")
            return
        self.app.start_team(goal, execute=execute)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "team-run":
            self._submit(True)
        elif event.button.id == "team-plan":
            self._submit(False)
        elif event.button.id == "team-cancel":
            self.app.cancel_team()
        elif event.button.id == "team-pause":
            self.app.toggle_team_pause()
        elif event.button.id == "team-roles":
            self.app.show_model_roles()
        elif event.button.id == "team-copy":
            team = getattr(self.app, "team", None)
            if team is not None and team.plan is not None:
                self.app.copy_text(team.plan.summary_text, "team plan")
            else:
                self.app.notify_user("No plan yet", "warn")
        elif event.button.id == "team-report":
            team = getattr(self.app, "team", None)
            report = self._last_report or str(
                getattr(getattr(team, "result", None), "report_path", "") or ""
            )
            if report:
                self.app.copy_text(report, "report path")
            else:
                self.app.notify_user("No report yet", "warn")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "team-goal":
            self._submit(True)


# ══════════════════════════════════════════════════════════════════════
# Computer
# ══════════════════════════════════════════════════════════════════════
class ComputerView(BaseView):
    """Screen observer and desktop-task launcher; input stays opt-in."""

    def compose(self) -> ComposeResult:
        yield self.head("COMPUTER", "observe and control the desktop · opt-in, audited")
        with Horizontal(id="computer-toolbar"):
            yield Button("Enable control", id="computer-toggle", classes="-primary")
            yield Button("Screenshot", id="computer-shot")
            yield Button("Refresh", id="computer-refresh")
            yield Input(placeholder="Desktop task, e.g. open Notepad and type hello", id="computer-task")
            yield Button("Run task", id="computer-run")
        yield Static(id="computer-state", classes="panel")
        with Horizontal(id="computer-body"):
            yield DataTable(id="computer-windows")
            yield Static(id="computer-log", classes="panel")

    def on_mount(self) -> None:
        table = self.query_one("#computer-windows", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("", width=2)
        table.add_column("HWND", width=12)
        table.add_column("PID", width=6)
        table.add_column("SIZE", width=11)
        table.add_column("TITLE")
        self._log_lines: list[str] = []
        self._last_log = ""
        self.set_interval(4.0, self.refresh_state)
        self.refresh_state()

    def refresh_state(self) -> None:
        if not self.visible():
            return
        self.run_worker(self._refresh_state_async(), exclusive=True, group="computer-refresh")

    async def _refresh_state_async(self) -> None:
        if not self.visible():
            return
        app = self.app
        enabled = bool(app.settings.computer_enabled)
        try:
            self.query_one("#computer-toggle", Button).label = "Disable control" if enabled else "Enable control"
        except Exception:
            pass
        state = Text()
        state.append("COMPUTER CONTROL\n", style=f"bold {theme.CYAN}")
        for line in app.computer_status_text().splitlines():
            key, _, value = line.partition(" ")
            state.append(f"{key:<12}", style=theme.FAINT)
            state.append(value.strip() + "\n", style=theme.FG_DIM)
        state.append("\ninput ", style=theme.FAINT)
        state.append("asks for approval" if enabled else "disabled until you enable control", style=theme.GREEN if enabled else theme.MUTED)
        self.query_one("#computer-state", Static).update(state)

        table = self.query_one("#computer-windows", DataTable)
        try:
            windows = await asyncio.to_thread(app.computer_backend.list_windows, limit=40)
        except Exception as error:
            table.clear()
            table.add_row("", Text("—", style=theme.FAINT), "", "", Text(str(error)[:100], style=theme.MUTED))
            return
        if not self.is_mounted:
            return
        table.clear()
        for window in windows:
            flag = Text("*" if window.active else " ", style=theme.GREEN)
            title = window.title[:80] + (" [this app]" if window.own else "")
            table.add_row(
                flag,
                Text(f"0x{window.hwnd:08x}", style=theme.CYAN),
                Text(str(window.pid), style=theme.FAINT),
                Text(f"{window.width}x{window.height}", style=theme.MUTED),
                Text(title, style=theme.FG_DIM if not window.active else theme.FG),
            )

    def push_event(self, kind: str, text: str, data: dict[str, Any]) -> None:
        line = f"{time.strftime('%H:%M:%S')} {kind:<10} {str(text)[:220]}"
        if line == self._last_log:
            return
        self._last_log = line
        self._log_lines.append(line)
        self._log_lines = self._log_lines[-80:]
        try:
            self.query_one("#computer-log", Static).update(
                Text("\n".join(self._log_lines)[-5000:], style=theme.FG_DIM)
            )
        except Exception:
            pass

    def _run_task(self) -> None:
        task = self.query_one("#computer-task", Input).value.strip()
        if not task:
            self.app.notify_user("Describe the desktop task first", "warn")
            return
        self.push_event("task", task, {})
        self.app.run_worker(self.app.computer_action("task", task), exclusive=False, group="computer")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button == "computer-toggle":
            self.app.set_computer_enabled(not self.app.settings.computer_enabled)
        elif button == "computer-shot":
            self.app.run_worker(self.app.computer_action("screenshot"), exclusive=False, group="computer")
        elif button == "computer-refresh":
            self.refresh_state()
        elif button == "computer-run":
            self._run_task()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "computer-task":
            self._run_task()
