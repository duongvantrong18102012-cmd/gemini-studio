from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from textual.containers import Horizontal
from textual.widgets import ContentSwitcher, Static

from gemini_tui.ai.client import GeminiService
from gemini_tui.app import VIEW_KEYS, GeminiStudio
from gemini_tui.clipboard import ClipboardResult
from gemini_tui.widgets.chat import Composer
from gemini_tui.widgets.chrome import Sidebar
from gemini_tui.widgets.views import ComputerView, CoworkView, TeamView


async def _no_models(self):
    return []


class AppSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="app-test-"))
        self.patches = [
            patch("gemini_tui.app.load_dotenv_files", lambda: None),
            patch.object(GeminiService, "list_models", _no_models),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()

    async def test_boot_registers_views_tools_and_commands(self) -> None:
        app = GeminiStudio(workspace=str(self.tmp), splash=False, audit=False)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()

            self.assertGreater(app.tool_count, 0)
            switcher = app.query_one("#views", ContentSwitcher)
            for key in VIEW_KEYS:
                app.switch_view(key)
                await pilot.pause()
                self.assertEqual(app.current_view, key)
                self.assertEqual(switcher.current, key)

            self.assertIsInstance(app.query_one(CoworkView), CoworkView)
            self.assertIsInstance(app.query_one(ComputerView), ComputerView)
            self.assertIsInstance(app.query_one(TeamView), TeamView)
            self.assertIsNotNone(app.cowork.computer_driver_factory)

            self.assertTrue(app.cowork_status_text().strip())
            self.assertIn("backend", app.computer_status_text().lower())

    async def test_computer_consent_controls_tool_declarations(self) -> None:
        app = GeminiStudio(workspace=str(self.tmp), splash=False, audit=False)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()

            self.assertIsNotNone(app.registry.get("computer_status"))
            self.assertIsNone(app.registry.get("computer_click"))

            app.set_computer_enabled(True)
            await pilot.pause()
            self.assertIsNotNone(app.registry.get("computer_click"))
            self.assertTrue(app.settings.computer_enabled)

            app.set_computer_enabled(False)
            await pilot.pause()
            self.assertIsNone(app.registry.get("computer_click"))
            self.assertIsNotNone(app.registry.get("computer_status"))

    async def test_infrastructure_commands_do_not_crash(self) -> None:
        app = GeminiStudio(workspace=str(self.tmp), splash=False, audit=False)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            for name, argument in (
                ("context", ""),
                ("memory", "list"),
                ("audit", ""),
                ("code", "status"),
                ("computer", "status"),
                ("plan", ""),
            ):
                await app.run_command(name, argument)
                await pilot.pause()

            app.rebind_workspace()
            await pilot.pause()
            self.assertEqual(app.cowork.workspace, app.settings.workspace)

    async def test_tool_context_is_fully_wired(self) -> None:
        app = GeminiStudio(workspace=str(self.tmp), splash=False, audit=False)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            ctx = app.tool_context(agent="test")
            self.assertEqual(ctx.workspace, app.settings.workspace)
            self.assertIs(ctx.permissions, app.permissions)
            self.assertIs(ctx.audit, app.audit)
            self.assertIs(ctx.memory, app.memory)
            self.assertIsNotNone(ctx.approve)
            self.assertEqual(ctx.extras.get("agent"), "test")
            for key in ("retriever", "computer_guard", "run_cowork", "context_usage"):
                self.assertIn(key, ctx.extras)

    async def test_team_refuses_without_a_configured_provider(self) -> None:
        app = GeminiStudio(workspace=str(self.tmp), splash=False, audit=False)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            # No provider adapter is configured. Make that explicit so a local
            # user config cannot make this test pass or fail by accident.
            app.model_registry._providers.clear()
            self.assertFalse(app._team_provider_ready())

            app.start_team("fix the failing tests")
            await pilot.pause()

            team = app.query_one(TeamView)
            self.assertEqual(app.current_view, "team")
            self.assertIn("No model provider is configured", team._summary_text)
            self.assertIsNone(app._team_worker)
            self.assertIsNone(app.team.scheduler)
            self.assertEqual(app.agent_manager.snapshot(), [])
            self.assertEqual(app.state_label, "READY")

    async def test_copy_text_routes_through_the_clipboard_manager(self) -> None:
        app = GeminiStudio(workspace=str(self.tmp), splash=False, audit=False)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            result = ClipboardResult(True, "pyperclip")
            with (
                patch.object(
                    app.clipboard_manager, "copy_async", new=AsyncMock(return_value=result)
                ) as copy,
                patch.object(type(app), "notify_user") as notify,
            ):
                worker = app.copy_text("print(1)\nprint(2)", "code block")
                self.assertIsNotNone(worker)
                await worker.wait()
            copy.assert_awaited_once_with("print(1)\nprint(2)")
            notify.assert_called_once()
            message, severity = notify.call_args[0][:2]
            self.assertIn("code block", message)
            self.assertIn("copied via pyperclip", message)
            self.assertEqual(severity, "ok")

    async def test_composer_paste_reads_the_system_clipboard(self) -> None:
        app = GeminiStudio(workspace=str(self.tmp), splash=False, audit=False)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            composer = app.query_one(Composer)
            keys = {binding.key for binding in Composer.BINDINGS}
            self.assertIn("ctrl+shift+v", keys)
            self.assertIn("ctrl+shift+c", keys)
            with (
                patch.object(
                    app.clipboard_manager,
                    "paste_async",
                    new=AsyncMock(return_value=("line one\nline two", "system clipboard")),
                ),
                patch.object(type(app), "notify_user") as notify,
            ):
                await composer._paste_os()
            self.assertEqual(composer.text, "line one\nline two")
            notify.assert_called_once()
            self.assertIn("system clipboard", notify.call_args[0][0])

    async def test_team_view_keeps_the_final_verification_until_an_agent_is_picked(self) -> None:
        app = GeminiStudio(workspace=str(self.tmp), splash=False, audit=False)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            agent = app.agent_manager.create("developer", "gemini-test", task_id="t1")
            agent.log("implemented the widget")
            team = app.query_one(TeamView)

            team.finish(
                {
                    "status": "done",
                    "tasks": [{"id": "t1", "status": "done"}],
                    "verification": "pass · 12 tests passed",
                    "elapsed": 1.2,
                }
            )
            await pilot.pause()
            log = team.query_one("#team-log", Static)
            self.assertIn("12 tests passed", str(log.content))

            # The 1 s refresh must not replace the verdict with an agent log.
            team._tick(force=True)
            self.assertIn("12 tests passed", str(log.content))

            # Opening an agent is the explicit request to see that agent's log.
            team.on_data_table_row_selected(
                SimpleNamespace(
                    data_table=SimpleNamespace(id="team-agents"),
                    row_key=SimpleNamespace(value=agent.id),
                )
            )
            self.assertIn("implemented the widget", str(log.content))

    async def test_small_terminal_layout_and_sidebar_toggle(self) -> None:
        app = GeminiStudio(workspace=str(self.tmp), splash=False, audit=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            sidebar = app.query_one(Sidebar)
            body = app.query_one("#body", Horizontal)
            self.assertTrue(sidebar.has_class("-compact"))
            self.assertTrue(sidebar.has_class("-narrow"))
            self.assertTrue(body.has_class("-short"))

            await pilot.resize_terminal(120, 42)
            await pilot.pause()
            self.assertFalse(sidebar.has_class("-compact"))
            self.assertFalse(sidebar.has_class("-narrow"))
            self.assertFalse(body.has_class("-short"))

            app.action_toggle_sidebar()
            await pilot.pause()
            self.assertTrue(sidebar.has_class("-hidden"))


if __name__ == "__main__":
    unittest.main()
