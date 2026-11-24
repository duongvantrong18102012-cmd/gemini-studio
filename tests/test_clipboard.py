"""Clipboard behaviour: real backends, honest failures, OSC 52 fallback."""

from __future__ import annotations

import base64
import threading
import unittest
from unittest.mock import AsyncMock, patch

from gemini_tui import clipboard
from gemini_tui.clipboard import (
    ClipboardManager,
    ClipboardResult,
    osc52_likely_supported,
    osc52_sequence,
    read_clipboard,
    write_clipboard,
)


class Osc52Tests(unittest.TestCase):
    def test_sequence_is_base64_and_bel(self) -> None:
        sequence = osc52_sequence("héllo\nworld")
        self.assertTrue(sequence.startswith("\x1b]52;c;"))
        self.assertTrue(sequence.endswith("\a"))
        payload = sequence[len("\x1b]52;c;") : -1]
        self.assertEqual(base64.b64decode(payload).decode("utf-8"), "héllo\nworld")

    def test_empty_text_still_encodes(self) -> None:
        self.assertEqual(osc52_sequence(""), "\x1b]52;c;\a")

    def test_support_guess_never_claims_tmux(self) -> None:
        self.assertFalse(osc52_likely_supported({"TERM": "screen-256color"}))
        self.assertFalse(osc52_likely_supported({"TERM": "tmux-256color"}))
        self.assertFalse(osc52_likely_supported({}))
        self.assertTrue(osc52_likely_supported({"TERM": "xterm-256color"}))
        self.assertTrue(osc52_likely_supported({"WT_SESSION": "1"}))


class WriteTests(unittest.TestCase):
    def test_empty_text_is_rejected(self) -> None:
        result = write_clipboard("")
        self.assertFalse(result.ok)
        self.assertIn("nothing", result.message.lower())

    def test_pyperclip_success_skips_fallbacks(self) -> None:
        with (
            patch.object(clipboard, "_write_pyperclip", return_value=(True, "pyperclip", "")),
            patch.object(clipboard, "_write_windows") as windows,
        ):
            result = write_clipboard("hello")
        self.assertTrue(result.ok)
        self.assertEqual(result.method, "pyperclip")
        windows.assert_not_called()

    def test_native_fallback_after_pyperclip_failure(self) -> None:
        with (
            patch.object(clipboard, "_write_pyperclip", return_value=(False, "", "no clipboard")),
            patch.object(clipboard.os, "name", "posix"),
            patch.object(clipboard.sys, "platform", "darwin"),
            patch.object(clipboard, "_write_macos", return_value=(True, "pbcopy", "")),
        ):
            result = write_clipboard("multi\nline")
        self.assertTrue(result.ok)
        self.assertEqual(result.method, "pbcopy")

    def test_all_backends_fail_reports_both_errors(self) -> None:
        with (
            patch.object(clipboard, "_write_pyperclip", return_value=(False, "", "pyperclip boom")),
            patch.object(clipboard.os, "name", "posix"),
            patch.object(clipboard.sys, "platform", "linux"),
            patch.object(clipboard, "_write_linux", return_value=(False, "", "no tool")),
        ):
            result = write_clipboard("x")
        self.assertFalse(result.ok)
        self.assertIn("pyperclip boom", result.error)
        self.assertIn("no tool", result.error)


class ReadTests(unittest.TestCase):
    def test_pyperclip_success(self) -> None:
        with patch.object(clipboard, "_read_pyperclip", return_value=(True, "text", "")):
            ok, value, error = read_clipboard()
        self.assertTrue(ok)
        self.assertEqual(value, "text")
        self.assertEqual(error, "")

    def test_windows_fallback_normalizes_newlines(self) -> None:
        with patch.object(clipboard, "_powershell", return_value="a\r\nb\r\n"):
            ok, value, _ = clipboard._read_windows()
        self.assertTrue(ok)
        self.assertEqual(value, "a\nb\n")

    def test_fallback_is_used_when_pyperclip_fails(self) -> None:
        with (
            patch.object(clipboard, "_read_pyperclip", return_value=(False, "", "nope")),
            patch.object(clipboard.os, "name", "nt"),
            patch.object(clipboard, "_read_windows", return_value=(True, "text", "")),
        ):
            ok, value, _ = read_clipboard()
        self.assertTrue(ok)
        self.assertEqual(value, "text")

    def test_failure_keeps_both_errors(self) -> None:
        with (
            patch.object(clipboard, "_read_pyperclip", return_value=(False, "", "first")),
            patch.object(clipboard.os, "name", "posix"),
            patch.object(clipboard.sys, "platform", "linux"),
            patch.object(clipboard, "_read_linux", return_value=(False, "", "second")),
        ):
            ok, value, error = read_clipboard()
        self.assertFalse(ok)
        self.assertEqual(value, "")
        self.assertIn("first", error)
        self.assertIn("second", error)


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    def test_copy_uses_system_backend(self) -> None:
        manager = ClipboardManager()
        with patch("gemini_tui.clipboard.write_clipboard", return_value=ClipboardResult(True, "pyperclip")):
            result = manager.copy("code()")
        self.assertTrue(result.ok)
        self.assertEqual(result.method, "pyperclip")
        self.assertIs(manager.last_result, result)

    def test_copy_falls_back_to_osc52_only_when_textual_accepts(self) -> None:
        app = type("App", (), {"copy_to_clipboard": lambda self, text: None})()
        manager = ClipboardManager(app)
        failure = ClipboardResult(False, error="no os clipboard")
        with patch("gemini_tui.clipboard.write_clipboard", return_value=failure):
            result = manager.copy("x")
        self.assertTrue(result.ok)
        self.assertIn("OSC 52", result.method)
        self.assertIn("best effort", result.message)

    def test_copy_reports_failure_without_lying(self) -> None:
        manager = ClipboardManager()
        with patch(
            "gemini_tui.clipboard.write_clipboard",
            return_value=ClipboardResult(False, error="backend unavailable"),
        ):
            result = manager.copy("x")
        self.assertFalse(result.ok)
        self.assertIn("backend unavailable", result.message)

    def test_blank_copy_is_rejected(self) -> None:
        manager = ClipboardManager()
        result = manager.copy("   \n")
        self.assertFalse(result.ok)

    async def test_paste_prefers_system_clipboard(self) -> None:
        manager = ClipboardManager()
        with patch.object(
            clipboard, "read_clipboard_async", new=AsyncMock(return_value=(True, "pasted", ""))
        ):
            text, method = await manager.paste_async()
        self.assertEqual(text, "pasted")
        self.assertEqual(method, "system clipboard")

    async def test_paste_uses_in_app_clipboard_when_system_is_unavailable(self) -> None:
        app = type("App", (), {"clipboard": "in-app text"})()
        manager = ClipboardManager(app)
        with patch.object(
            clipboard, "read_clipboard_async", new=AsyncMock(return_value=(False, "", "no backend"))
        ):
            text, method = await manager.paste_async()
        self.assertEqual(text, "in-app text")
        self.assertEqual(method, "in-app clipboard")

    async def test_paste_reports_empty_system_clipboard(self) -> None:
        manager = ClipboardManager()
        with patch.object(
            clipboard, "read_clipboard_async", new=AsyncMock(return_value=(True, "", ""))
        ):
            text, method = await manager.paste_async()
        self.assertEqual(text, "")
        self.assertIn("empty", method)

    async def test_read_async_runs_off_the_event_loop_thread(self) -> None:
        seen: list[int] = []

        def fake_read() -> tuple[bool, str, str]:
            seen.append(threading.get_ident())
            return True, "from worker", ""

        with patch.object(clipboard, "read_clipboard", fake_read):
            ok, value, _ = await clipboard.read_clipboard_async()
        self.assertTrue(ok)
        self.assertEqual(value, "from worker")
        self.assertTrue(seen)
        self.assertNotEqual(seen[0], threading.get_ident())

    async def test_write_async_runs_off_the_event_loop_thread(self) -> None:
        seen: list[int] = []

        def fake_write(text: str) -> ClipboardResult:
            seen.append(threading.get_ident())
            return ClipboardResult(True, "fake", "")

        with patch.object(clipboard, "write_clipboard", fake_write):
            result = await clipboard.write_clipboard_async("hello")
        self.assertTrue(result.ok)
        self.assertTrue(seen)
        self.assertNotEqual(seen[0], threading.get_ident())


if __name__ == "__main__":
    unittest.main()
