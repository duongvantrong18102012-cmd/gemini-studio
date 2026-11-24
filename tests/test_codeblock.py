"""Code fences: language label, line numbers, copy, unknown-language fallback."""

from __future__ import annotations

import unittest

from textual.app import App, ComposeResult

from gemini_tui.widgets.codeblock import (
    MAX_GUTTER_LINES,
    RedHighlightTheme,
    StudioFence,
    StudioMarkdown,
)


class FenceMetadataTests(unittest.TestCase):
    def fence(self, code: str, language: str = "python") -> StudioFence:
        # Properties only touch plain attributes; no app or DOM is required.
        instance = object.__new__(StudioFence)
        instance.code = code
        instance.lexer = language
        return instance

    def test_language_and_line_count(self) -> None:
        fence = self.fence("a\nb\nc")
        self.assertEqual(fence.language_label, "PYTHON")
        self.assertEqual(fence.line_count, 3)
        self.assertEqual(fence._gutter(), "1\n2\n3")

    def test_no_language_is_labelled_text(self) -> None:
        fence = self.fence("x = 1", "")
        self.assertEqual(fence.language_label, "TEXT")

    def test_language_alias_metadata_is_dropped(self) -> None:
        fence = self.fence("x = 1", "python title=example.py")
        self.assertEqual(fence.language_label, "PYTHON")

    def test_empty_code_still_has_one_line(self) -> None:
        fence = self.fence("", "python")
        self.assertEqual(fence.line_count, 1)
        self.assertEqual(fence._gutter(), "1")

    def test_huge_block_skips_gutter(self) -> None:
        fence = self.fence("\n".join(str(i) for i in range(MAX_GUTTER_LINES + 10)))
        self.assertEqual(fence._gutter(), "")

    def test_highlight_uses_red_theme_and_falls_back(self) -> None:
        themed = StudioFence.highlight("x = 1", "python")
        self.assertEqual(themed.plain, "x = 1")
        unknown = StudioFence.highlight("not really code", "definitely-not-a-language")
        self.assertEqual(unknown.plain, "not really code")
        from pygments.token import Token

        self.assertIn(Token.Keyword, RedHighlightTheme.STYLES)


class FenceWidgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_fence_renders_header_gutter_and_copies(self) -> None:
        class CodeApp(App):
            def __init__(self) -> None:
                super().__init__()
                self.copied: list[tuple[str, str]] = []

            def compose(self) -> ComposeResult:
                yield StudioMarkdown("```python\nprint(1)\nprint(2)\n```")

            def copy_text(self, text: str, label: str = "text") -> None:
                self.copied.append((text, label))

        app = CodeApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            fences = list(app.query(StudioFence))
            self.assertEqual(len(fences), 1)
            fence = fences[0]
            self.assertEqual(fence.language_label, "PYTHON")
            self.assertEqual(fence.line_count, 2)
            self.assertEqual(fence.code, "print(1)\nprint(2)")
            gutter = fence.query_one(".fence-gutter")
            self.assertIn("1", str(gutter.render()))
            fence.copy_code()
            self.assertEqual(app.copied, [("print(1)\nprint(2)", "code block")])

    async def test_unknown_language_renders_without_crashing(self) -> None:
        class CodeApp(App):
            def compose(self) -> ComposeResult:
                yield StudioMarkdown("```not-a-language\nplain text\n```")

        app = CodeApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            fences = list(app.query(StudioFence))
            self.assertEqual(len(fences), 1)
            self.assertEqual(fences[0].language_label, "NOT-A-LANGUAGE")
            self.assertEqual(fences[0].code, "plain text")


if __name__ == "__main__":
    unittest.main()
