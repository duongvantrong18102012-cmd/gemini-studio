"""Modal overlays: command palette, help, tool approval and the boot splash.

Overlays are separate screens layered on top of the main dashboard. The base
screen is never remounted, so opening the palette or an approval prompt costs
one composited layer — it does not reload the UI or flash the terminal.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import theme
from ..animations import meter, sample_gradient, shimmer_gradient, spinner, truncate

# A custom sigil: a terminal chip with a lit core, drawn in half blocks. It is
# deliberately not a vendor logo — it is this app's own mark. The ASCII variant
# is used when the terminal encoding cannot carry block elements.
SPLASH_GLYPH = [
    "             ▄▄▄▄▄▄▄▄▄▄▄▄",
    "         ▄▄██████████████████▄▄",
    "      ▄████████▀▀▀▀▀▀▀▀████████▄",
    "    ▄███████▀▀   ▄▄▄▄   ▀▀███████▄",
    "   ███████▀     ██████     ▀███████",
    "  ███████▀      ██████      ▀███████",
    "  ███████▄      ██████      ▄███████",
    "   ███████▄     ▀▀▀▀▀▀     ▄███████",
    "    ▀███████▄▄   ▀▀▀▀   ▄▄███████▀",
    "      ▀████████▄▄▄▄▄▄▄▄████████▀",
    "         ▀▀██████████████████▀▀",
    "             ▀▀▀▀▀▀▀▀▀▀▀▀",
]

SPLASH_GLYPH_ASCII = [
    "             +------------+",
    "         +---+############+---+",
    "      +--+######################+--+",
    "    +-+#########+      +#########+-+",
    "   +##########+          +##########+",
    "  +##########+            +##########+",
    "  +##########+            +##########+",
    "   +##########+          +##########+",
    "    +-+#########+      +#########+-+",
    "      +--+######################+--+",
    "         +---+############+---+",
    "             +------------+",
]

SPLASH_GLYPH_COMPACT = [
    "     ▄▄▄▄▄▄▄▄▄▄▄▄▄▄",
    "  ▄▄██████▀▀▀▀▀▀██████▄▄",
    " ██████▀   ▄▄▄▄   ▀██████",
    " ██████▄   ▀▀▀▀   ▄██████",
    "  ▀▀██████▄▄▄▄▄▄██████▀▀",
    "     ▀▀▀▀▀▀▀▀▀▀▀▀▀▀",
]

SPLASH_GLYPH_COMPACT_ASCII = [
    "     +--------------+",
    "  +--+##############+--+",
    " +#####+          +#####+",
    " +#####+          +#####+",
    "  +--+##############+--+",
    "     +--------------+",
]

SPLASH_GLYPH_MINIMAL = ["▌ GEMINI // TERMINAL"]
SPLASH_GLYPH_MINIMAL_ASCII = ["| GEMINI // TERMINAL"]


def unicode_supported(stream: Any = None) -> bool:
    """True when the terminal can render the block-element glyphs.

    `GEMINI_TUI_ASCII=1` forces the fallback, which is also what a terminal
    with a cp1252/cp437 encoding gets automatically.
    """
    forced = os.environ.get("GEMINI_TUI_ASCII", "").strip().lower()
    if forced in {"1", "true", "yes", "on"}:
        return False
    # `sys.__stdout__` survives Textual's screen redirection; prefer it so the
    # real terminal's encoding decides, not an internal test stream.
    stream = stream or getattr(sys, "__stdout__", None) or sys.stdout
    encoding = getattr(stream, "encoding", "") or ""
    try:
        "▄▀█".encode(encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def splash_tier(width: int, height: int) -> str:
    """Pick art detail from the real terminal size, not a fixed layout."""
    if width >= 74 and height >= 34:
        return "full"
    if width >= 46 and height >= 20:
        return "compact"
    return "minimal"


def select_splash_art(width: int, height: int, *, unicode: bool = True) -> list[str]:
    tier = splash_tier(width, height)
    if tier == "full":
        return list(SPLASH_GLYPH if unicode else SPLASH_GLYPH_ASCII)
    if tier == "compact":
        return list(SPLASH_GLYPH_COMPACT if unicode else SPLASH_GLYPH_COMPACT_ASCII)
    return list(SPLASH_GLYPH_MINIMAL if unicode else SPLASH_GLYPH_MINIMAL_ASCII)



class OverlayPanel(Container):
    """A centered card shown inside a modal screen."""

    can_focus = False

    def on_show(self) -> None:
        """Called once the panel is mounted in its screen."""


# ══════════════════════════════════════════════════════════════════════
# Command palette
# ══════════════════════════════════════════════════════════════════════
class PalettePanel(OverlayPanel):
    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("down", "next_option", "", priority=True, show=False),
        Binding("up", "previous_option", "", priority=True, show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="palette", classes="overlay")
        self._commands: list[Any] = []
        self._input: Input | None = None
        self._list: OptionList | None = None

    def compose(self) -> ComposeResult:
        yield Static("COMMAND PALETTE", classes="overlay-title")
        yield Static("type to filter · enter to run · esc to close", classes="overlay-sub")
        yield Input(placeholder="Search commands…", id="palette-input")
        yield OptionList(id="palette-list")

    def on_mount(self) -> None:
        self._input = self.query_one("#palette-input", Input)
        self._list = self.query_one("#palette-list", OptionList)

    def on_show(self) -> None:
        self._populate("")
        if self._input is not None:
            self._input.value = ""
            self._input.focus()

    def _populate(self, query: str) -> None:
        if self._list is None:
            return
        self._commands = self.app.commands.search(query, limit=60)
        self._list.clear_options()
        for command in self._commands:
            label = Text()
            label.append(f"/{command.name}", style=f"bold {theme.CYAN}")
            label.append(f"   {truncate(command.description, 58)}", style=theme.FG_DIM)
            label.append(f"   {command.category}", style=theme.FAINT)
            self._list.add_option(Option(label, id=command.name))
        if self._commands:
            self._list.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "palette-input":
            self._populate(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._run()

    def action_next_option(self) -> None:
        if self._commands and self._list is not None:
            self._list.action_cursor_down()

    def action_previous_option(self) -> None:
        if self._commands and self._list is not None:
            self._list.action_cursor_up()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._run(event.option_id)

    def _run(self, name: str | None = None) -> None:
        target = name
        if target is None and self._commands and self._list is not None:
            index = self._list.highlighted or 0
            if 0 <= index < len(self._commands):
                target = self._commands[index].name
        app = self.app
        self.screen.dismiss(None)
        if target:
            app.call_after_refresh(lambda: app.run_worker(app.run_command(target), exclusive=False))

    def action_close(self) -> None:
        self.screen.dismiss(None)


# ══════════════════════════════════════════════════════════════════════
# Help
# ══════════════════════════════════════════════════════════════════════
class HelpPanel(OverlayPanel):
    BINDINGS = [Binding("escape", "close", "Close", priority=True)]

    def __init__(self) -> None:
        super().__init__(id="help", classes="overlay")

    def compose(self) -> ComposeResult:
        yield Static("KEYS & COMMANDS", classes="overlay-title")
        yield Static("press esc to return", classes="overlay-sub")
        with VerticalScroll(id="help-scroll"):
            yield Static(id="help-body")

    def on_show(self) -> None:
        self._render_body()
        try:
            self.query_one("#help-scroll", VerticalScroll).scroll_home(animate=False)
        except Exception:
            pass
        try:
            self.query_one("#help-scroll", VerticalScroll).focus()
        except Exception:
            pass

    def _render_body(self) -> None:
        body = Text()
        body.append("KEYBOARD", style=f"bold {theme.CYAN}")
        body.append("\n")
        keys = [
            ("ctrl+k", "command palette"),
            ("ctrl+n", "new session"),
            ("ctrl+r", "resume session"),
            ("ctrl+m", "model picker"),
            ("ctrl+1…0", "switch view"),
            ("ctrl+8", "agent team command center"),
            ("ctrl+0", "cowork plan + workers"),
            ("ctrl+9", "computer control"),
            ("ctrl+b", "toggle sidebar"),
            ("ctrl+l", "clear transcript"),
            ("ctrl+shift+v", "paste the OS clipboard"),
            ("ctrl+shift+c", "copy selection / code block"),
            ("ctrl+y", "copy last answer"),
            ("ctrl+t", "cycle thinking level"),
            ("ctrl+o", "cycle approval mode"),
            ("esc", "cancel generation / close overlay"),
            ("ctrl+q", "quit"),
        ]
        for key, description in keys:
            body.append(f"  {key:<12}", style=f"bold {theme.FG}")
            body.append(description + "\n", style=theme.MUTED)
        body.append("\n")
        body.append("COMMANDS", style=f"bold {theme.CYAN}")
        body.append("\n")
        for category, commands in self.app.commands.categories().items():
            body.append(f"  {category.upper()}\n", style=theme.VIOLET)
            for command in commands:
                body.append(f"    /{command.name:<16}", style=theme.CYAN)
                body.append(truncate(command.description, 64) + "\n", style=theme.MUTED)
        body.append("\n")
        body.append("TOOLS", style=f"bold {theme.CYAN}")
        body.append("\n")
        for tool in self.app.registry.all():
            body.append(f"  {tool.name:<22}", style=theme.FG_DIM)
            body.append(truncate(tool.description, 70) + "\n", style=theme.FAINT)
        self.query_one("#help-body", Static).update(body)

    def action_close(self) -> None:
        self.screen.dismiss(None)


# ══════════════════════════════════════════════════════════════════════
# Approval
# ══════════════════════════════════════════════════════════════════════
class ApprovalPanel(OverlayPanel):
    BINDINGS = [
        Binding("escape", "deny", "Deny", priority=True),
        Binding("n", "deny", "Deny", priority=True, show=False),
        Binding("y", "allow", "Allow", priority=True, show=False),
        Binding("a", "always", "Always", priority=True, show=False),
    ]

    def __init__(self, name: str, args: dict[str, Any], description: str) -> None:
        super().__init__(id="approval", classes="overlay")
        self._name = name
        self._args = args
        self._description = description
        self.result: bool = False

    def compose(self) -> ComposeResult:
        yield Static("PERMISSION REQUIRED", classes="overlay-title")
        yield Static("the model wants to run a write tool", classes="overlay-sub")
        with VerticalScroll(id="approval-body"):
            yield Static(id="approval-text")
        with Horizontal(id="approval-actions"):
            yield Button("Deny  esc", id="approval-deny")
            yield Button("Allow  y", id="approval-allow", classes="-primary")
            yield Button("Always  a", id="approval-always")

    def on_show(self) -> None:
        self._render_body()
        try:
            self.query_one("#approval-allow", Button).focus()
        except Exception:
            pass

    def _render_body(self) -> None:
        body = Text()
        body.append("tool  ", style=theme.FAINT)
        body.append(self._name, style=f"bold {theme.CYAN}")
        body.append("\n\n")
        body.append(self._description, style=theme.FG_DIM)
        body.append("\n\n")
        body.append("arguments", style=theme.FAINT)
        body.append("\n")
        try:
            rendered = json.dumps(self._args, indent=2, ensure_ascii=False)
        except Exception:
            rendered = str(self._args)
        for line in rendered.splitlines()[:26]:
            body.append(line[:220] + "\n", style=theme.MUTED)
        body.append("\n")
        body.append("y", style=f"bold {theme.GREEN}")
        body.append(" allow   ", style=theme.MUTED)
        body.append("a", style=f"bold {theme.CYAN}")
        body.append(" always allow writes   ", style=theme.MUTED)
        body.append("n", style=f"bold {theme.RED}")
        body.append("/esc deny", style=theme.MUTED)
        self.query_one("#approval-text", Static).update(body)

    def _resolve(self, allowed: bool, always: bool = False) -> None:
        self.result = allowed
        app = self.app
        if always and allowed:
            app.settings.approval_mode = "auto"
            app.settings.save()
            if hasattr(app, "refresh_chrome"):
                app.refresh_chrome()
            app.notify_user("Approval mode is now auto", "warn")
        self.screen.dismiss(allowed)

    def action_allow(self) -> None:
        self._resolve(True)

    def action_always(self) -> None:
        self._resolve(True, always=True)

    def action_deny(self) -> None:
        self._resolve(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approval-allow":
            self.action_allow()
        elif event.button.id == "approval-always":
            self.action_always()
        elif event.button.id == "approval-deny":
            self.action_deny()


# ══════════════════════════════════════════════════════════════════════
# Splash
# ══════════════════════════════════════════════════════════════════════
class SplashPanel(OverlayPanel):
    def __init__(self) -> None:
        super().__init__(id="splash")
        self._started = 0.0
        self._phase = 0.0
        self._lines: list[tuple[str, str]] = []
        self._shown = 0
        self._done = False
        self._unicode = True
        self._tier = "full"
        self._art: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static(id="splash-art")
        yield Static(id="splash-body")

    def on_show(self) -> None:
        self._started = time.time()
        self._phase = 0.0
        self._shown = 0
        app = self.app
        self._unicode = unicode_supported()
        width, height = app.size
        self._tier = splash_tier(width, height)
        self._art = select_splash_art(width, height, unicode=self._unicode)
        self.remove_class("-compact", "-minimal", "-tight")
        if self._tier == "compact":
            self.add_class("-compact")
        elif self._tier == "minimal":
            self.add_class("-minimal")
        if height < 28:
            self.add_class("-tight")

        key_state = "configured" if app.service.has_key else "missing · set GEMINI_API_KEY"
        self._lines = [
            ("terminal", "truecolor · alt-screen · animated"),
            ("gemini api", key_state),
            ("models", f"{len(app.models)} available · default {app.settings.model}"),
            ("mcp", f"{len(app.mcp.states)} servers discovered"),
            ("skills", f"{len(app.skills.skills)} installed"),
            ("plugins", f"{app.plugins.loaded_count} native · {app.plugins.external_count} external"),
            ("tools", f"{app.tool_count} registered"),
            ("workspace", str(app.settings.workspace)),
        ]
        if self._tier == "compact":
            self._lines = self._lines[:6]
        elif self._tier == "minimal":
            self._lines = self._lines[:2]
        self.set_interval(1 / 20, self._tick)

    def _tick(self) -> None:
        if self._done:
            return
        elapsed = time.time() - self._started
        animate = bool(self.app.settings.animations)
        if animate:
            self._phase = (self._phase + 0.03) % 1.0
        if not animate:
            if getattr(self, "_static_drawn", False):
                if elapsed > 1.35:
                    self._finish()
                return
            self._static_drawn = True
            self._shown = len(self._lines)
        else:
            self._shown = min(len(self._lines), int(elapsed / 0.1))

        # A single bright scan row travels down the sigil. One moving style
        # change per frame is cheap; a per-character animation would not be.
        sweep = int((self._phase * len(self._art)) / 1.0) % max(1, len(self._art))
        art = Text()
        for row, line in enumerate(self._art):
            colour = sample_gradient(theme.GRADIENT, row / max(1, len(self._art) - 1) + self._phase * 0.4)
            if animate and row == sweep:
                colour = theme.BONE
            art.append(line, style=f"bold {colour}")
            art.append("\n")
        self.query_one("#splash-art", Static).update(art)

        body = Text()
        body.append("  ")
        if animate:
            body.append_text(shimmer_gradient("GEMINI STUDIO", theme.GRADIENT, self._phase, width=0.3))
        else:
            body.append("GEMINI STUDIO", style=f"bold {theme.CRIMSON}")
        cursor_on = not animate or int(elapsed * 4) % 2 == 0
        if cursor_on:
            body.append("▌" if self._unicode else "_", style=f"bold {theme.BONE}")
        body.append("  ")
        body.append("v1.0 · local agent console", style=theme.FAINT)
        body.append("\n\n")
        for index, (label, value) in enumerate(self._lines):
            if index >= self._shown:
                break
            ok = "missing" not in value
            mark = ("✔" if self._unicode else "+") if ok else "!"
            body.append(f"  {mark} ", style=theme.GREEN if ok else theme.AMBER)
            body.append(f"{label:<11}", style=theme.FG_DIM)
            body.append(truncate(value, 46) + "\n", style=theme.MUTED)
        progress = self._shown / max(1, len(self._lines))
        body.append("\n  ")
        if self._shown >= len(self._lines):
            spin = spinner(int(elapsed * 12), "braille") if self._unicode else "|/-\\"[int(elapsed * 8) % 4]
            body.append(spin + " ready", style=theme.CYAN)
        else:
            if self._unicode:
                body.append(meter(progress, 28), style=theme.CYAN)
            else:
                body.append(meter(progress, 28, full="#", empty="-"), style=theme.CYAN)
            body.append(f"  {progress * 100:3.0f}%", style=theme.MUTED)
        body.append("\n\n  ")
        body.append("press any key to skip", style=theme.FAINT)
        self.query_one("#splash-body", Static).update(body)

        if elapsed > 1.35:
            self._finish()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            self.screen.dismiss(None)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# Screens
# ══════════════════════════════════════════════════════════════════════
class PanelScreen(ModalScreen[Any]):
    """Modal screen hosting a single centered overlay panel."""

    def __init__(self, panel: OverlayPanel) -> None:
        super().__init__()
        self.panel = panel

    def compose(self) -> ComposeResult:
        yield self.panel

    def on_mount(self) -> None:
        self.panel.on_show()


class PaletteScreen(PanelScreen):
    """Command palette."""

    def __init__(self) -> None:
        super().__init__(PalettePanel())


class HelpScreen(PanelScreen):
    """Keyboard + command reference."""

    def __init__(self) -> None:
        super().__init__(HelpPanel())


class ApprovalScreen(PanelScreen):
    """Ask the human before running a write tool."""

    def __init__(self, name: str, args: dict[str, Any], description: str) -> None:
        super().__init__(ApprovalPanel(name, args, description))


class SplashScreen(PanelScreen):
    """Boot animation; any key skips it."""

    def __init__(self) -> None:
        super().__init__(SplashPanel())

    def on_key(self, event) -> None:  # type: ignore[override]
        event.stop()
        self.panel._finish()


# ══════════════════════════════════════════════════════════════════════
# Toasts (rendered on the main screen, above the composer)
# ══════════════════════════════════════════════════════════════════════
class Toast(Static):
    def __init__(self, message: str, severity: str = "info") -> None:
        super().__init__()
        self.message = message
        self.severity = severity
        self.add_class(f"-{severity if severity in ('ok', 'warn', 'error') else 'info'}")

    def render(self) -> Text:
        colour = {
            "ok": theme.GREEN,
            "warn": theme.AMBER,
            "error": theme.RED,
            "info": theme.CYAN,
        }.get(self.severity, theme.CYAN)
        icon = {"ok": "✔", "warn": "!", "error": "✖", "info": "◆"}.get(self.severity, "◆")
        body = Text()
        body.append(f" {icon} ", style=f"bold {colour}")
        body.append(truncate(self.message, 160), style=theme.FG_DIM)
        return body


class ToastHost(Container):
    can_focus = False

    def compose(self) -> ComposeResult:
        yield from ()

    def push(self, message: str, severity: str = "info", timeout: float = 4.0) -> None:
        toast = Toast(message, severity)
        self.mount(toast)
        self.set_timer(timeout, lambda: self._dismiss(toast))

    async def _dismiss(self, toast: Toast) -> None:
        try:
            await toast.animate("styles.opacity", 0.0, duration=0.25)
        except Exception:
            pass
        try:
            toast.remove()
        except Exception:
            pass
