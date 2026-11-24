"""Persistent chrome: header bar, sidebar navigation and status bar.

These widgets are mounted once for the lifetime of the app. Nothing here is
rebuilt when you change views, which is the main trick behind the flicker-free
navigation.
"""

from __future__ import annotations

import time
from datetime import datetime

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from .. import theme
from ..animations import (
    format_tokens,
    meter,
    mix,
    pulse_dot,
    shimmer_gradient,
    signal_bars,
    spinner,
)


class HeaderBar(Vertical):
    """Brand, model chips, clock and an animated scanline."""

    can_focus = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="header-inner"):
            yield Static(id="brand")
            yield Static(id="header-right")
        yield Static(id="scanline")

    def on_mount(self) -> None:
        self._brand: Static = self.query_one("#brand", Static)
        self._right: Static = self.query_one("#header-right", Static)
        self._scan: Static = self.query_one("#scanline", Static)
        self._phase = 0.0
        self._last_right = ""
        self._render_brand()
        self._render_right(force=True)
        self.set_interval(0.085, self._tick)
        self.set_interval(0.9, self._render_right)

    # ── animation loop ────────────────────────────────────────────
    def _tick(self) -> None:
        if not self.app.settings.animations:
            return
        self._phase = (self._phase + 0.012) % 1.0
        self._render_brand()
        self._render_scan()

    def _render_brand(self) -> None:
        app = self.app
        model = app.settings.model
        thinking = app.settings.thinking_level.upper()
        tools = app.tool_count
        mcp_ready = app.mcp.ready_count
        mcp_total = len(app.mcp.states)

        line1 = Text()
        line1.append("◆ ", style=f"bold {theme.CYAN}")
        line1.append_text(
            shimmer_gradient("GEMINI STUDIO", theme.GRADIENT, self._phase * 1.35, width=0.22)
        )
        line1.append("   ")
        line1.append("local agent console", style=theme.FAINT)

        line2 = Text()
        line2.append("  ⚡ ", style=theme.GREEN)
        line2.append(model, style=f"bold {theme.FG}")
        line2.append("  ·  ", style=theme.FAINT)
        line2.append(f"think {thinking.lower()}", style=theme.VIOLET)
        line2.append("  ·  ", style=theme.FAINT)
        line2.append(f"{tools} tools", style=theme.CYAN_DIM)
        line2.append("  ·  ", style=theme.FAINT)
        mcp_colour = theme.GREEN if mcp_ready else theme.MUTED
        line2.append(f"mcp {mcp_ready}/{mcp_total}", style=mcp_colour)
        if app.is_streaming:
            line2.append("   ")
            line2.append(spinner(app.frame, "orbit"), style=f"bold {theme.AMBER}")
            line2.append(" working", style=theme.AMBER)

        body = Text("\n").join([line1, line2])
        self._brand.update(body)

    def _render_right(self, force: bool = False) -> None:
        app = self.app
        now = datetime.now()
        state = app.state_label
        colour = {
            "READY": theme.GREEN,
            "THINKING": theme.AMBER,
            "STREAMING": theme.CYAN,
            "TOOL": theme.VIOLET,
            "WAITING": theme.CYAN,
            "TEAM": theme.CRIMSON,
            "ERROR": theme.RED,
        }.get(state, theme.MUTED)

        line1 = Text()
        line1.append(now.strftime("%H:%M:%S"), style=f"bold {theme.CYAN}")
        line1.append("  ")
        line1.append_text(pulse_dot(app.frame // 2, colour, theme.BORDER))
        line1.append(f" {state}", style=f"bold {colour}")

        line2 = Text()
        line2.append(now.strftime("%a %d/%m/%Y"), style=theme.MUTED)
        line2.append("  ·  ", style=theme.FAINT)
        elapsed = int(time.time() - app.started_at)
        line2.append(f"up {elapsed // 3600:02d}:{(elapsed % 3600) // 60:02d}", style=theme.FAINT)

        payload = Text("\n").join([line1, line2])
        key = payload.plain
        if force or key != self._last_right:
            self._last_right = key
            self._right.update(payload)

    def _render_scan(self) -> None:
        width = max(20, self.size.width - 2)
        head = (self._phase * 2.2) % 1.0
        text = Text()
        for index in range(width):
            distance = abs((index / max(1, width - 1)) - head)
            if distance < 0.075:
                strength = 1.0 - distance / 0.075
                text.append("━", style=f"bold {mix(theme.CYAN_DIM, theme.CYAN, strength)}")
            elif index % 17 == 0:
                text.append("┼", style=theme.BORDER)
            else:
                text.append("─", style=theme.BORDER_SOFT)
        self._scan.update(text)


class NavItem(Static):
    """A sidebar destination. Click or use ctrl+1..7."""

    can_focus = False

    def __init__(self, view_key: str, icon: str, label: str, hint: str = "") -> None:
        super().__init__()
        self.view_key = view_key
        self.icon = icon
        self.label = label
        self.hint = hint
        self.badge = ""
        self.add_class("nav-item")

    def render(self) -> Text | Table:
        text = Text()
        active = "-active" in self.classes
        accent = theme.CYAN if active else theme.FAINT
        text.append(" ▌" if active else "  ", style=accent)
        text.append(f"{self.icon}  ", style=accent if active else theme.MUTED)
        text.append(self.label, style=f"bold {theme.FG}" if active else theme.FG_DIM)
        if self.badge:
            width = self.content_size.width or 26
            padding = max(1, width - 5 - len(self.label) - len(self.badge))
            text.append(" " * padding, style="")
            text.append(self.badge, style=theme.VIOLET if not active else theme.CYAN)
        return text

    def on_click(self) -> None:
        self.app.switch_view(self.view_key)

    def set_active(self, active: bool) -> None:
        self.set_class(active, "-active")
        self.refresh()

    def set_badge(self, badge: str) -> None:
        if badge != self.badge:
            self.badge = badge
            self.refresh()


class Sidebar(Vertical):
    can_focus = False

    NAV = [
        ("chat", "◆", "Chat", "ctrl+1"),
        ("models", "✦", "Models", "ctrl+2"),
        ("tools", "⌘", "Tools", "ctrl+3"),
        ("mcp", "⬡", "MCP Servers", "ctrl+4"),
        ("skills", "◈", "Skills", "ctrl+5"),
        ("plugins", "⊞", "Plugins", "ctrl+6"),
        ("dashboard", "▣", "Dashboard", "ctrl+7"),
        ("team", "◇", "Agent Team", "ctrl+8"),
        ("cowork", "⇄", "Cowork", "ctrl+0"),
        ("computer", "◉", "Computer", "ctrl+9"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="sidebar-scroll"):
            yield Static("WORKSPACE", classes="side-label")
            for key, icon, label, hint in self.NAV:
                yield NavItem(key, icon, label, hint)
            yield Static("SESSION", classes="side-label")
            yield Static(id="session-card")
            yield Static("SIGNAL", classes="side-label")
            yield Static(id="signal")
            yield Static(id="sidebar-foot")

    def on_mount(self) -> None:
        self._card = self.query_one("#session-card", Static)
        self._signal = self.query_one("#signal", Static)
        self._foot = self.query_one("#sidebar-foot", Static)
        self.set_interval(0.09, self._animate_signal)
        self.set_interval(1.2, self._refresh_card)
        self._refresh_card()

    def _animate_signal(self) -> None:
        app = self.app
        if not app.settings.animations:
            return
        level = 1.0 if app.is_streaming else 0.14
        bars = Text()
        bars.append_text(signal_bars(level, app.frame / 14, count=24))
        self._signal.update(bars)

    def _refresh_card(self) -> None:
        app = self.app
        session = app.session
        settings = app.settings
        used = session.usage.total if session else 0
        context = 1_000_000
        ratio = min(1.0, used / context) if context else 0.0
        colour = theme.GREEN if ratio < 0.5 else theme.AMBER if ratio < 0.8 else theme.RED

        table = Table.grid(expand=True, padding=(0, 0))
        table.add_column(ratio=1)
        table.add_column(justify="right")
        table.add_row(
            Text("model", style=theme.FAINT),
            Text(settings.model.replace("gemini-", ""), style=theme.CYAN, overflow="ellipsis", no_wrap=True),
        )
        table.add_row(
            Text("tokens", style=theme.FAINT),
            Text(format_tokens(used), style=colour),
        )
        table.add_row(
            Text("turns", style=theme.FAINT),
            Text(str(session.turns if session else 0), style=theme.FG_DIM),
        )
        table.add_row(
            Text("mcp", style=theme.FAINT),
            Text(f"{app.mcp.ready_count}/{len(app.mcp.states)}", style=theme.VIOLET),
        )
        table.add_row(
            Text("mode", style=theme.FAINT),
            Text(settings.approval_mode, style=theme.MUTED),
        )
        body = Table.grid(expand=True)
        body.add_column()
        body.add_row(table)
        body.add_row(Text(meter(ratio, 22), style=colour))
        self._card.update(body)

        todos = app.todos
        if todos:
            done = sum(1 for item in todos if item.get("status") == "done")
            footer = Text()
            footer.append(f"☑ {done}/{len(todos)} tasks", style=theme.GREEN if done == len(todos) else theme.MUTED)
            self._foot.update(footer)
        else:
            self._foot.update(Text("^K palette   ? help", style=theme.FAINT))

    def update_nav(self, current: str) -> None:
        for item in self.query(NavItem):
            item.set_active(item.view_key == current)
        for item in self.query(NavItem):
            if item.view_key == "mcp":
                item.set_badge(f"{self.app.mcp.ready_count}/{len(self.app.mcp.states)}")
            elif item.view_key == "tools":
                item.set_badge(str(self.app.tool_count))
            elif item.view_key == "skills":
                item.set_badge(str(len(self.app.skills.skills)))
            elif item.view_key == "plugins":
                item.set_badge(str(len(self.app.plugins.plugins)))
            elif item.view_key == "models":
                item.set_badge(str(len(self.app.models)))
            elif item.view_key == "cowork":
                if self.app.cowork_running:
                    item.set_badge("run")
                else:
                    try:
                        count = sum(1 for _ in self.app.plan_store.directory.glob("*.json"))
                    except OSError:
                        count = 0
                    item.set_badge(str(count))
            elif item.view_key == "team":
                team = getattr(self.app, "team", None)
                if team is not None and team.running:
                    active = self.app.agent_manager.active_count
                    item.set_badge(f"{active} live" if active else "plan")
                else:
                    item.set_badge("idle")
            elif item.view_key == "computer":
                item.set_badge("on" if self.app.settings.computer_enabled else "off")


class StatusBar(Static):
    """One-line contextual status with a live equalizer while generating."""

    can_focus = False

    def on_mount(self) -> None:
        self.set_interval(0.12, self._tick)
        self._tick()

    def _tick(self) -> None:
        app = self.app
        state = app.state_label
        colour = {
            "READY": theme.GREEN,
            "THINKING": theme.AMBER,
            "STREAMING": theme.CYAN,
            "TOOL": theme.VIOLET,
            "WAITING": theme.CYAN,
            "TEAM": theme.CRIMSON,
            "ERROR": theme.RED,
        }.get(state, theme.MUTED)

        text = Text()
        text.append(" ● ", style=colour)
        text.append(f"{state:<9}", style=f"bold {colour}")
        text.append("│ ", style=theme.FAINT)
        text.append(app.settings.model, style=theme.FG_DIM)
        text.append("  │ ", style=theme.FAINT)
        text.append(f"think {app.settings.thinking_level}", style=theme.VIOLET)
        text.append("  │ ", style=theme.FAINT)
        text.append(f"{format_tokens(app.session.usage.total)} tok", style=theme.MUTED)

        if app.last_latency:
            text.append("  │ ", style=theme.FAINT)
            text.append(f"{app.last_latency:.1f}s", style=theme.MUTED)
        team = getattr(app, "team", None)
        if team is not None and team.scheduler is not None and (team.running or team.result is not None):
            scheduler = team.scheduler
            text.append("  │ ", style=theme.FAINT)
            text.append(
                f"team {scheduler.done_count}/{scheduler.total}",
                style=theme.CRIMSON if team.running else theme.MUTED,
            )
            if team.running:
                text.append(f" · {app.agent_manager.active_count} agents", style=theme.PLUM)
        if app.is_streaming:
            text.append("  │ ", style=theme.FAINT)
            text.append(f"{app.stream_elapsed:.1f}s ", style=theme.AMBER)
            text.append_text(signal_bars(1.0, app.frame / 10, count=10))
            text.append("  esc cancel", style=theme.FAINT)
        else:
            text.append("  │ ", style=theme.FAINT)
            text.append("^K commands", style=theme.MUTED)
            text.append("  ", style="")
            text.append("^N new", style=theme.MUTED)
            text.append("  ", style="")
            text.append("^M models", style=theme.MUTED)
            text.append("  ", style="")
            text.append("? help", style=theme.MUTED)
        if not app.is_streaming and not getattr(app.settings, "animations", True):
            plain = text.plain
            if getattr(self, "_last_plain", None) == plain:
                return
            self._last_plain = plain
        else:
            self._last_plain = None
        self.update(text)
