"""Markdown code fences with language labels, line numbers and copy.

Textual's `MarkdownFence` is already syntax-highlighted, but it has no language
header, no way to copy a single block, and it grows without bound. This widget
keeps the same streaming contract (it is registered in `Markdown.BLOCKS`) and
adds:

* a RED HACKER Pygments palette;
* a header with the language, line count and a clickable COPY control;
* a line-number gutter for normal-sized blocks;
* a capped height with scrolling so a 500-line paste cannot swallow the chat;
* `ctrl+shift+c` / `enter` copy when the fence is focused.

Unknown languages fall back to plain text through Pygments' `ClassNotFound`
handling in `textual.highlight.highlight`.
"""

from __future__ import annotations

from pygments.token import Token
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.highlight import HighlightTheme, highlight
from textual.widgets import Label, Static
from textual.widgets._markdown import Markdown, MarkdownFence

# ══════════════════════════════════════════════════════════════════════
# Palette
# ══════════════════════════════════════════════════════════════════════
class RedHighlightTheme(HighlightTheme):
    """Pygments token styles for the crimson terminal identity."""

    STYLES = {
        Token.Comment: "#6b565c italic",
        Token.Error: "#ff4d5e",
        Token.Generic.Error: "#ff4d5e",
        Token.Generic.Heading: "#ff2e4d bold underline",
        Token.Generic.Subheading: "#ff2e4d bold",
        Token.Generic.Strong: "bold",
        Token.Generic.Emph: "italic",
        Token.Keyword: "bold #ff2e4d",
        Token.Keyword.Constant: "bold #e8b87a",
        Token.Keyword.Namespace: "#c96b7a",
        Token.Keyword.Type: "bold #ffb3c0",
        Token.Literal.Number: "#e8b87a",
        Token.Literal.String: "#d9a0a8",
        Token.Literal.String.Backtick: "#a99398",
        Token.Literal.String.Doc: "#c9a6ad italic",
        Token.Name: "#f2f2f3",
        Token.Name.Attribute: "#ffc2cc",
        Token.Name.Builtin: "#ff6b81",
        Token.Name.Builtin.Pseudo: "#c96b7a italic",
        Token.Name.Class: "#ffb3c0 bold",
        Token.Name.Constant: "#ff8fa0",
        Token.Name.Decorator: "#e8b87a",
        Token.Name.Function: "#ff8fa0",
        Token.Name.Function.Magic: "#ff8fa0 underline",
        Token.Name.Tag: "#ff2e4d bold",
        Token.Name.Variable: "#c4b8bc",
        Token.Number: "#e8b87a",
        Token.Operator: "#c4b8bc",
        Token.Operator.Word: "bold #ff2e4d",
        Token.String: "#d9a0a8",
        Token.Whitespace: "",
    }


MAX_GUTTER_LINES = 400


class StudioFence(MarkdownFence):
    """A code block with a header, optional line numbers and block copy."""

    can_focus = True
    BINDINGS = [
        Binding("ctrl+shift+c", "copy_code", "Copy code", show=False),
        Binding("enter", "copy_code", "Copy code", show=False),
    ]

    @classmethod
    def highlight(
        cls,
        code: str,
        language: str,
        ansi: bool = False,
        dark: bool = False,
    ):
        if ansi:
            return super().highlight(code, language, ansi=True, dark=dark)
        cleaned = (language or "").strip().split(" ")[0]
        return highlight(code, language=cleaned or None, theme=RedHighlightTheme)

    # ── header helpers ────────────────────────────────────────────
    @property
    def language_label(self) -> str:
        cleaned = (self.lexer or "").strip().split(" ")[0]
        return (cleaned or "text").upper()

    @property
    def line_count(self) -> int:
        return max(1, len((self.code or "").splitlines()))

    def _header_lang(self) -> Text:
        line = Text()
        line.append("▍ ", "#ff2e4d")
        line.append(self.language_label, "bold #f2f2f3")
        return line

    def _header_meta(self) -> Text:
        return Text(f"{self.line_count} lines", "#5c4c51")

    def _gutter(self) -> str:
        count = self.line_count
        if count > MAX_GUTTER_LINES:
            return ""
        width = len(str(count))
        return "\n".join(str(index).rjust(width) for index in range(1, count + 1))

    # ── composition ───────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal(classes="fence-head"):
            yield Static(self._header_lang(), classes="fence-lang")
            yield Static(self._header_meta(), classes="fence-meta")
            yield Static("COPY", classes="fence-copy")
        with Horizontal(classes="fence-code-row"):
            gutter = self._gutter()
            gutter_widget = Static(gutter, classes="fence-gutter")
            gutter_widget.display = bool(gutter)
            yield gutter_widget
            yield Label(self._highlighted_code, id="code-content", expand=True)

    def set_content(self, content) -> None:
        super().set_content(content)
        try:
            self.query_one(".fence-lang", Static).update(self._header_lang())
            self.query_one(".fence-meta", Static).update(self._header_meta())
            gutter_widget = self.query_one(".fence-gutter", Static)
            gutter = self._gutter()
            gutter_widget.display = bool(gutter)
            if gutter:
                gutter_widget.update(gutter)
        except Exception:
            pass

    def _refresh_header(self) -> None:
        try:
            self.query_one(".fence-lang", Static).update(self._header_lang())
            self.query_one(".fence-meta", Static).update(self._header_meta())
        except Exception:
            pass

    # ── copy ──────────────────────────────────────────────────────
    def action_copy_code(self) -> None:
        self.copy_code()

    def copy_code(self) -> None:
        app = self.app
        text = self.code or ""
        if not text.strip():
            return
        copy_text = getattr(app, "copy_text", None)
        if callable(copy_text):
            copy_text(text, "code block")
            return
        try:
            app.copy_to_clipboard(text)
            app.notify("Code block copied", timeout=2)
        except Exception:
            pass

    def on_click(self, event) -> None:
        widget = getattr(event, "widget", None)
        classes = getattr(widget, "classes", set()) if widget is not None else set()
        if "fence-copy" in classes or "fence-lang" in classes or widget is self:
            self.copy_code()


class StudioMarkdown(Markdown):
    """Markdown with crimson code fences and click-to-copy blocks."""

    BLOCKS = {**Markdown.BLOCKS, "fence": StudioFence, "code_block": StudioFence}
