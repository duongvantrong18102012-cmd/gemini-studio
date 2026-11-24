"""Chat transcript, streaming message blocks and the composer."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll
from textual.widgets import Static, TextArea

from .. import theme
from ..animations import format_tokens, gradient, spinner, truncate
from .codeblock import StudioMarkdown


# ══════════════════════════════════════════════════════════════════════
# Composer
# ══════════════════════════════════════════════════════════════════════
class Composer(TextArea):
    """Multiline prompt box: enter sends, shift+enter adds a line."""

    BINDINGS = [
        Binding("enter", "submit", "Send", priority=True, show=False),
        Binding("shift+enter", "newline", "New line", priority=True, show=False),
        Binding("alt+up", "history_previous", "Previous prompt", priority=True, show=False),
        Binding("alt+down", "history_next", "Next prompt", priority=True, show=False),
        Binding("escape", "escape_app", "Cancel", priority=True, show=False),
        Binding("ctrl+v", "paste_os", "Paste", priority=True, show=False),
        Binding("ctrl+shift+v", "paste_os", "Paste", priority=True, show=False),
        Binding("ctrl+shift+c", "copy_selection", "Copy selection", priority=True, show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            language=None,
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            placeholder="Message Gemini…   / commands   shift+enter for a new line",
            **kwargs,
        )
        self.prompt_history: list[str] = []
        self._history_index = -1
        self._draft = ""

    def on_mount(self) -> None:
        self.styles.height = 3
        self.focus()

    # ── actions ───────────────────────────────────────────────────
    def action_submit(self) -> None:
        text = self.text.strip()
        if text:
            self.app.submit_prompt(text)

    def action_newline(self) -> None:
        self.insert("\n")
        self._grow()

    def action_escape_app(self) -> None:
        self.app.action_escape()

    # ── clipboard ─────────────────────────────────────────────────
    def action_paste(self) -> None:
        """Ctrl+V / Shift+Insert: read the real OS clipboard, not just Textual's."""
        self.action_paste_os()

    def action_paste_os(self) -> None:
        self.run_worker(self._paste_os(), exclusive=False, group="clipboard")

    async def _paste_os(self) -> None:
        manager = getattr(self.app, "clipboard_manager", None)
        if manager is None:
            return
        text, method = await manager.paste_async()
        if not text:
            self.app.notify_user("Clipboard is empty or unavailable", "warn")
            return
        if hasattr(self, "replace"):
            self.replace(text, *self.selection)
        else:
            self.insert(text)
        self._grow()
        self.app.notify_user(f"Pasted {len(text)} chars from {method}", "info", timeout=2)

    def action_copy_selection(self) -> None:
        selected = self.selected_text
        if not selected:
            self.app.notify_user("Select text in the composer first", "warn", timeout=2)
            return
        self.app.copy_text(selected, "composer selection")

    def action_history_previous(self) -> None:
        if not self.prompt_history:
            return
        if self._history_index == -1:
            self._draft = self.text
            self._history_index = len(self.prompt_history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self._set_text(self.prompt_history[self._history_index])

    def action_history_next(self) -> None:
        if self._history_index == -1:
            return
        if self._history_index < len(self.prompt_history) - 1:
            self._history_index += 1
            self._set_text(self.prompt_history[self._history_index])
        else:
            self._history_index = -1
            self._set_text(self._draft)

    def _set_text(self, value: str) -> None:
        self.text = value
        try:
            self.move_cursor(self.document.end)
        except Exception:
            pass
        self._grow()

    def _grow(self) -> None:
        lines = self.text.count("\n") + 1
        self.styles.height = max(3, min(11, lines + 2))

    # ── events ────────────────────────────────────────────────────
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._grow()
        self._history_index = -1
        self.app.update_slash_hints(self.text)

    def remember(self, text: str) -> None:
        if text and (not self.prompt_history or self.prompt_history[-1] != text):
            self.prompt_history.append(text)
            if len(self.prompt_history) > 100:
                self.prompt_history.pop(0)
        self._history_index = -1


# ══════════════════════════════════════════════════════════════════════
# Message blocks
# ══════════════════════════════════════════════════════════════════════
class UserMessage(Static):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def render(self) -> Text:
        body = Text()
        body.append(" YOU ", style=f"bold {theme.BG} on {theme.CYAN}")
        body.append("\n")
        body.append(self.text, style=theme.FG)
        return body


class AssistantMessage(Vertical):
    """One streamed answer segment: meta line, optional thoughts, markdown.

    Tokens are buffered for ~50ms before they reach `MarkdownStream`, so a fast
    stream does not schedule one re-render per token.
    """

    FLUSH_INTERVAL = 0.05

    def __init__(
        self,
        model: str,
        started: float | None = None,
        initial_text: str = "",
        initial_thoughts: str = "",
    ) -> None:
        super().__init__()
        self.model = model
        self.started = started or time.time()
        self.thought_text = initial_thoughts
        self.answer_text = initial_text
        self._initial_text = initial_text
        self._stream = None
        self._markdown: StudioMarkdown | None = None
        self._thought: Static | None = None
        self._footer: Static | None = None
        self._pending: list[str] = []
        self._flush_task: asyncio.Task | None = None
        self._stream_closed = False
        self._thought_last = 0.0

    def compose(self) -> ComposeResult:
        yield Static(self._meta_line(), classes="msg-meta")
        yield Static(
            Text(self.thought_text, style=theme.MUTED) if self.thought_text else "",
            classes="msg-thought",
        )
        yield StudioMarkdown(self._initial_text)
        yield Static("", classes="msg-meta footer -copyable")

    def on_mount(self) -> None:
        self._markdown = self.query_one(StudioMarkdown)
        self._thought = self.query_one(".msg-thought", Static)
        self._footer = self.query_one(".footer", Static)
        if self.thought_text:
            self._thought.add_class("-visible")
        if not self._initial_text:
            self._stream = StudioMarkdown.get_stream(self._markdown)

    def _meta_line(self) -> Text:
        line = Text()
        line.append(" ▍ ", style=f"bold {theme.CRIMSON}")
        line.append("GEMINI", style=f"bold {theme.CRIMSON}")
        line.append(f"  {self.model}", style=theme.FAINT)
        return line

    async def write(self, text: str) -> None:
        if not text:
            return
        self.answer_text += text
        self._pending.append(text)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        try:
            while self._pending and not self._stream_closed:
                if self._stream is None and self._markdown is not None:
                    self._stream = StudioMarkdown.get_stream(self._markdown)
                if self._stream is not None:
                    payload = "".join(self._pending)
                    self._pending.clear()
                    for offset in range(0, len(payload), 12_000):
                        await self._stream.write(payload[offset : offset + 12_000])
                await asyncio.sleep(self.FLUSH_INTERVAL)
            if self._pending and self._stream is not None:
                payload = "".join(self._pending)
                self._pending.clear()
                await self._stream.write(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._flush_task = None

    def add_thought(self, text: str) -> None:
        self.thought_text += text
        if self._thought is None:
            return
        now = time.time()
        # Reasoning arrives token by token; repainting it 30x/s is wasted work.
        if now - self._thought_last < 0.15 and len(self.thought_text) % 32:
            return
        self._thought_last = now
        self._thought.set_class(True, "-visible")
        self._thought.update(
            Text(self.thought_text[-4000:], style=theme.MUTED, no_wrap=False)
        )

    def finish(self, usage: dict[str, Any] | None = None) -> None:
        if self._footer is None:
            return
        line = Text()
        elapsed = max(0.0, time.time() - self.started)
        line.append(f"  {elapsed:.1f}s", style=theme.FAINT)
        if usage:
            total = int(usage.get("total") or 0)
            if total:
                line.append(f"  ·  {format_tokens(total)} tok", style=theme.FAINT)
            thoughts = int(usage.get("thoughts") or 0)
            if thoughts:
                line.append(f"  ·  {format_tokens(thoughts)} thinking", style=theme.VIOLET)
        line.append("   click to copy", style=theme.CRIMSON_DIM)
        self._footer.update(line)

    def copy(self) -> None:
        if self.answer_text.strip():
            self.app.copy_text(self.answer_text, "answer")
        else:
            self.app.notify_user("This answer is empty", "warn", timeout=2)

    def on_click(self, event) -> None:
        widget = getattr(event, "widget", None)
        classes = getattr(widget, "classes", set()) if widget is not None else set()
        if "footer" in classes:
            self.copy()

    async def stop_stream(self) -> None:
        self._stream_closed = True
        if self._flush_task is not None and not self._flush_task.done():
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._flush_task = None
        # Final drain, then stop the markdown updater.
        if self._pending and self._stream is not None:
            try:
                await self._stream.write("".join(self._pending))
            except Exception:
                pass
            self._pending.clear()
        if self._stream is not None:
            try:
                await self._stream.stop()
            except Exception:
                pass
            self._stream = None


class ToolCard(Static):
    """Collapsible tool invocation record."""

    def __init__(self, call_id: str, name: str, args: dict[str, Any], dangerous: bool) -> None:
        super().__init__()
        self.call_id = call_id
        self.tool_name = name
        self.args = args
        self.dangerous = dangerous
        self.status = "running"
        self.output = ""
        self.elapsed = 0.0
        self.expanded = False
        self.started_at = time.time()
        self.add_class("-running")

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.25, self._tick)

    def _tick(self) -> None:
        if self.status == "running":
            self.refresh()
            return
        # A finished card has nothing to animate. Leaving its timer running
        # means every tool call ever made keeps waking the event loop.
        timer = getattr(self, "_timer", None)
        if timer is not None:
            timer.stop()
            self._timer = None

    def finish(self, ok: bool, output: str, elapsed: float) -> None:
        self.status = "ok" if ok else "error"
        self.output = output or ""
        self.elapsed = elapsed
        self.remove_class("-running")
        self.add_class("-ok" if ok else "-error")
        self.refresh()

    def on_click(self) -> None:
        if self.output:
            self.expanded = not self.expanded
            self.refresh()

    def render(self) -> Text:
        app = self.app
        colour = {"running": theme.AMBER, "ok": theme.GREEN, "error": theme.RED}.get(
            self.status, theme.MUTED
        )

        head = Text()
        head.append(" ")
        head.append("⚙", style=f"bold {colour}")
        head.append(f" {self.tool_name}", style=f"bold {theme.FG}")
        if self.dangerous:
            head.append("  write", style=theme.AMBER)
        if self.status == "running":
            head.append(f"  {spinner(app.frame // 2, 'dots')} ", style=theme.AMBER)
            head.append(f"{time.time() - self.started_at:.1f}s", style=theme.FAINT)
        else:
            head.append(f"  {self.status}", style=colour)
            head.append(f"  {self.elapsed * 1000:.0f}ms", style=theme.FAINT)
        digest = self._args_digest()
        if digest:
            head.append("   ")
            head.append(digest, style=theme.MUTED)

        body = Text("\n")
        body.append_text(head)

        if self.output:
            lines = self.output.splitlines()
            preview = lines if self.expanded else lines[:3]
            for line in preview:
                body.append("\n   ", style="")
                body.append("▏ ", style=colour)
                body.append(line[:240], style=theme.FG_DIM if self.status == "ok" else theme.RED)
            remaining = len(lines) - len(preview)
            if remaining > 0 and not self.expanded:
                body.append(f"\n   … {remaining} more lines · click to expand", style=theme.FAINT)
            elif self.expanded and len(lines) > 3:
                body.append("\n   click to collapse", style=theme.FAINT)
        return body

    def _args_digest(self) -> str:
        if not self.args:
            return ""
        pieces = []
        for key, value in list(self.args.items())[:3]:
            rendered = value if isinstance(value, str) else str(value)
            pieces.append(f"{key}={truncate(rendered, 48)}")
        return " · ".join(pieces)[:160]


class Notice(Static):
    def __init__(self, text: str, severity: str = "info") -> None:
        super().__init__()
        self.message = text
        self.severity = severity

    def render(self) -> Text:
        colour = {
            "info": theme.CYAN,
            "warn": theme.AMBER,
            "error": theme.RED,
            "ok": theme.GREEN,
        }.get(self.severity, theme.MUTED)
        body = Text()
        body.append(" ▎", style=colour)
        body.append(f" {self.message}", style=theme.FG_DIM)
        return body


class Welcome(Static):
    def render(self) -> Text:
        body = Text()
        body.append("\n")
        body.append_text(gradient("  ▍ GEMINI STUDIO", theme.GRADIENT, phase=0.1, bold=True))
        body.append("  ")
        body.append("RED HACKER CONSOLE", style=theme.CRIMSON_DIM)
        body.append("\n")
        body.append("  A local-first Gemini command center for real work.\n", style=theme.FG_DIM)
        body.append("\n")
        tips = [
            ("/models", "switch model (default gemini-3.5-flash-lite)"),
            ("/team", "supervisor plans, routes models and runs an agent team"),
            ("/mcp", "connect MCP servers installed on this machine"),
            ("ctrl+shift+v", "paste from the real OS clipboard"),
            ("ctrl+shift+c", "copy selection / focused code block"),
            ("^K", "command palette"),
            ("F1", "keys + every command"),
        ]
        for key, description in tips:
            body.append(f"  {key:<10}", style=f"bold {theme.CYAN}")
            body.append(description + "\n", style=theme.MUTED)
        body.append("\n")
        body.append("  cwd ", style=theme.FAINT)
        body.append(str(self.app.settings.workspace), style=theme.FG_DIM)
        body.append("\n")
        return body


# ══════════════════════════════════════════════════════════════════════
# Chat view
# ══════════════════════════════════════════════════════════════════════
class _TranscriptScroll(VerticalScroll):
    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if hasattr(self.parent, "update_follow"):
            self.parent.update_follow()


class ChatView(Vertical):
    can_focus = False

    def compose(self) -> ComposeResult:
        yield Static(id="chat-head")
        yield _TranscriptScroll(id="transcript")
        yield Static(id="slash-hints")
        with Container(id="composer-wrap"):
            yield Composer(id="composer")
            yield Static(id="composer-hints")

    def on_mount(self) -> None:
        self._transcript = self.query_one("#transcript", VerticalScroll)
        self._head = self.query_one("#chat-head", Static)
        self._hints = self.query_one("#slash-hints", Static)
        self._composer = self.query_one(Composer)
        self._hints_line = self.query_one("#composer-hints", Static)
        self._active: AssistantMessage | None = None
        self._welcome: Welcome | None = None
        self._follow = True
        self.set_interval(1.0, self._refresh_head)
        self._refresh_head()

    # ── header + hints ────────────────────────────────────────────
    def _refresh_head(self) -> None:
        app = self.app
        session = app.session
        line = Text()
        line.append("◆ ", style=f"bold {theme.CYAN}")
        line.append(session.title if session else "New session", style=f"bold {theme.FG}")
        line.append("   ", style="")
        line.append(app.settings.model, style=theme.CYAN)
        line.append(f" · think {app.settings.thinking_level}", style=theme.VIOLET)
        line.append(f" · {format_tokens(session.usage.total if session else 0)} tok", style=theme.MUTED)
        line.append(f" · {session.turns if session else 0} turns", style=theme.FAINT)
        self._head.update(line)

        hints = Text()
        hints.append(" enter ", style=f"{theme.BG} on {theme.CRIMSON}")
        hints.append(" send   ", style=theme.FAINT)
        hints.append(" shift+enter ", style=f"{theme.BG} on {theme.BORDER}")
        hints.append(" newline   ", style=theme.FAINT)
        hints.append(" ctrl+shift+v ", style=f"{theme.BG} on {theme.CRIMSON_DIM}")
        hints.append(" paste   ", style=theme.FAINT)
        hints.append(" / ", style=f"{theme.BG} on {theme.PLUM}")
        hints.append(" commands   ", style=theme.FAINT)
        hints.append(" esc ", style=f"{theme.BG} on {theme.BORDER}")
        hints.append(" cancel", style=theme.FAINT)
        self._hints_line.update(hints)

    # ── scroll helpers ────────────────────────────────────────────
    def update_follow(self) -> None:
        try:
            max_y = self._transcript.max_scroll_y
            self._follow = self._transcript.scroll_offset.y >= max_y - 6
        except Exception:
            self._follow = True

    def on_scroll(self) -> None:
        self.update_follow()

    def _scroll_to_end(self) -> None:
        if self._follow:
            try:
                self._transcript.scroll_end(animate=False)
            except Exception:
                pass

    # ── message lifecycle ─────────────────────────────────────────
    async def clear(self) -> None:
        self._active = None
        self._welcome = None
        try:
            await self._transcript.remove_children()
        except Exception:
            pass
        self._follow = True

    def add_welcome(self) -> None:
        self._welcome = Welcome()
        self._transcript.mount(self._welcome)
        self._scroll_to_end()

    def add_user(self, text: str) -> None:
        if self._welcome is not None:
            try:
                self._welcome.remove()
            except Exception:
                pass
            self._welcome = None
        self._transcript.mount(UserMessage(text))
        self._scroll_to_end()

    def add_notice(self, text: str, severity: str = "info") -> None:
        if self._welcome is not None:
            try:
                self._welcome.remove()
            except Exception:
                pass
            self._welcome = None
        self._transcript.mount(Notice(text, severity))
        self._scroll_to_end()

    async def begin_assistant(self, model: str) -> AssistantMessage:
        message = AssistantMessage(model)
        await self._transcript.mount(message)
        self._active = message
        self._follow = True
        self._scroll_to_end()
        return message

    async def feed_text(self, text: str) -> None:
        if self._active is None:
            await self.begin_assistant(self.app.settings.model)
        assert self._active is not None
        await self._active.write(text)
        self._scroll_to_end()

    def feed_thought(self, text: str) -> None:
        if self._active is not None:
            self._active.add_thought(text)

    async def end_assistant(self, usage: dict[str, Any] | None = None) -> None:
        if self._active is not None:
            await self._active.stop_stream()
            self._active.finish(usage)
            self._active = None

    def add_tool_card(
        self, call_id: str, name: str, args: dict[str, Any], dangerous: bool
    ) -> ToolCard:
        card = ToolCard(call_id, name, args, dangerous)
        self._transcript.mount(card)
        self._follow = True
        self._scroll_to_end()
        return card

    def find_tool_card(self, call_id: str) -> ToolCard | None:
        for card in self._transcript.query(ToolCard):
            if card.call_id == call_id:
                return card
        return None

    async def add_restored(self, message: Any) -> None:
        """Re-render a message loaded from disk."""
        if message.role == "user":
            self.add_user(message.text)
        elif message.role == "notice":
            self.add_notice(message.text, message.meta.get("severity", "info"))
        elif message.role == "assistant":
            block = AssistantMessage(
                message.meta.get("model", self.app.settings.model),
                started=time.time(),
                initial_text=message.text,
                initial_thoughts=message.thoughts,
            )
            await self._transcript.mount(block)
            block.finish(
                {
                    "total": message.usage.total,
                    "thoughts": message.usage.thoughts,
                }
            )
            for tool in message.tools:
                card = ToolCard(
                    tool.get("id", ""),
                    tool.get("name", "?"),
                    tool.get("args", {}),
                    bool(tool.get("dangerous")),
                )
                self._transcript.mount(card)
                card.finish(
                    bool(tool.get("ok")),
                    tool.get("output", ""),
                    float(tool.get("elapsed", 0)),
                )
        self._scroll_to_end()

    # ── slash hints ───────────────────────────────────────────────
    def show_slash_hints(self, query: str, commands: list[Any]) -> None:
        if not commands:
            self._hints.remove_class("-visible")
            return
        lines = Text()
        for index, command in enumerate(commands[:6]):
            if index:
                lines.append("\n")
            lines.append(f" /{command.name}", style=f"bold {theme.CYAN}")
            lines.append(f"  {truncate(command.description, 58)}", style=theme.MUTED)
            lines.append(f"   {command.category}", style=theme.FAINT)
        self._hints.update(lines)
        self._hints.add_class("-visible")

    def hide_slash_hints(self) -> None:
        self._hints.remove_class("-visible")

    def focus_composer(self) -> None:
        try:
            self._composer.focus()
        except Exception:
            pass

    @property
    def composer(self) -> Composer:
        return self._composer

    def clear_composer(self) -> None:
        self._composer.text = ""
        self._composer.styles.height = 3
        self._composer._history_index = -1
        self.hide_slash_hints()
