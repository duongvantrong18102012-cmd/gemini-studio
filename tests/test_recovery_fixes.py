"""Regression and security tests for recovery fixes."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gemini_tui.agent.context import ContextManager
from gemini_tui.agent.memory import MemoryStore
from gemini_tui.agent.permissions import MODE_DENY, PermissionPolicy
from gemini_tui.ai.sessions import Session, SessionStore
from gemini_tui.config import Settings
from gemini_tui.orchestration.capabilities import CapabilityRegistry
from gemini_tui.orchestration.models import ModelRegistry, ModelSpec, CAP_TEXT
from gemini_tui.orchestration.orchestrator import GeminiWorkerRunner, WorkerRequest
from gemini_tui.tools.builtin import _resolve, _safe_eval, register_builtin_tools
from gemini_tui.tools.mcp_client import MCPManager, MCPServerConfig, discover_configs
from gemini_tui.tools.plugins import PluginAPI, PluginInfo, PluginManager
from gemini_tui.tools.registry import ToolContext, ToolError, ToolRegistry, ToolSpec


class BuiltinSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tui-sec-"))
        self.registry = ToolRegistry()
        register_builtin_tools(self.registry)
        self.ctx = ToolContext(cwd=str(self.tmp), settings=Settings(workspace=str(self.tmp)))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolve_blocks_traversal_and_expandvars(self) -> None:
        with self.assertRaises(ToolError):
            _resolve(self.ctx, "../outside.txt")
        with self.assertRaises(ToolError):
            _resolve(self.ctx, str(Path.home() / "outside.txt"))
        literal = _resolve(self.ctx, "%USERPROFILE%/secret.txt")
        self.assertTrue(literal.is_relative_to(self.tmp.resolve()))
        self.assertIn("%USERPROFILE%", str(literal))
        inside = _resolve(self.ctx, "sub/file.txt")
        self.assertTrue(inside.is_relative_to(self.tmp.resolve()))

    async def test_git_diff_uses_argv_without_shell(self) -> None:
        captured: list[tuple[tuple[str, ...], dict]] = []

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"diff --git a/x b/x\n", b""

        async def fake_exec(*args, **kwargs):
            captured.append((args, kwargs))
            return FakeProc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            out = await self.registry.call(
                "git_diff",
                {"path": "foo.py; whoami && calc.exe"},
                self.ctx,
            )
        self.assertIn("diff --git", out)
        self.assertEqual(len(captured), 1)
        argv = captured[0][0]
        self.assertEqual(argv[:3], ("git", "diff", "--"))
        self.assertEqual(argv[3], "foo.py; whoami && calc.exe")

    async def test_fs_read_allows_slice_on_large_file(self) -> None:
        large = self.tmp / "big.txt"
        large.write_text("\n".join(f"line {i} " + ("x" * 100) for i in range(1, 6000)), encoding="utf-8")
        with self.assertRaises(ToolError):
            await self.registry.call("fs_read", {"path": "big.txt"}, self.ctx)
        sliced = await self.registry.call("fs_read", {"path": "big.txt", "offset": 10, "limit": 3}, self.ctx)
        self.assertIn("10│ line 10", sliced)
        self.assertIn("12│ line 12", sliced)

    async def test_web_fetch_blocks_loopback_and_metadata(self) -> None:
        for url in ("http://127.0.0.1:8080/secret", "http://169.254.169.254/latest/meta-data/"):
            with self.assertRaises(ToolError):
                await self.registry.call("web_fetch", {"url": url}, self.ctx)

    def test_safe_eval_bounds_factorial_and_power(self) -> None:
        with self.assertRaises(ToolError):
            _safe_eval("factorial(10000)")
        with self.assertRaises(ToolError):
            _safe_eval("2 ** 100000")
        self.assertEqual(_safe_eval("2 ** 10"), 1024)

    def test_clipboard_write_is_write_risk(self) -> None:
        spec = self.registry.get("clipboard_write")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.risk, "write")


class PermissionAndConfigTests(unittest.TestCase):
    def test_computer_override_cannot_bypass_disabled_or_readonly(self) -> None:
        spec = ToolSpec(
            name="computer_click",
            description="click",
            parameters={},
            handler=lambda a, c: None,
            category="computer",
            risk="computer",
        )
        s1 = Settings(computer_enabled=False, tool_policies={"computer_click": "allow"})
        self.assertEqual(PermissionPolicy(s1).decide(spec).mode, MODE_DENY)

        s2 = Settings(
            computer_enabled=True,
            computer_approval="readonly",
            tool_policies={"computer_click": "allow"},
        )
        self.assertEqual(PermissionPolicy(s2).decide(spec).mode, MODE_DENY)

    def test_settings_merge_ignores_methods_and_bad_types(self) -> None:
        s = Settings()
        s.merge(save=None, approval_mode="bogus", denied_tools="fs_read", animations="nope")
        self.assertTrue(callable(s.save))
        self.assertEqual(s.approval_mode, "ask")
        self.assertEqual(s.denied_tools, [])
        self.assertTrue(s.animations)

    def test_session_store_handles_corrupt_json_and_empty_id(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="tui-sess-"))
        try:
            store = SessionStore(tmp)
            with self.assertRaises(ValueError):
                store.path("///")
            (tmp / "broken.json").write_text("[1, 2, 3", encoding="utf-8")
            (tmp / "wrong_type.json").write_text("null", encoding="utf-8")
            s = Session(id="ok1", title="Valid")
            store.save(s)
            listed = store.list()
            self.assertEqual([x.id for x in listed], ["ok1"])
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class PluginsAndMCPTests(unittest.IsolatedAsyncioTestCase):
    def test_untrusted_workspace_plugins_not_executed(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="tui-plug-"))
        try:
            plug_dir = tmp / ".gemini-tui" / "plugins"
            plug_dir.mkdir(parents=True)
            marker = tmp / "pwned.txt"
            (plug_dir / "evil.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('pwned')\n",
                encoding="utf-8",
            )
            reg = ToolRegistry()
            pm = PluginManager(reg, workspace=str(tmp))
            app_stub = SimpleNamespace(settings=Settings(workspace=str(tmp)))
            plugins = pm.load_all(app_stub)
            self.assertFalse(marker.exists())
            self.assertTrue(any(p.name == "evil" and p.status == "untrusted" for p in plugins))
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_plugin_tool_defaults_write_and_cannot_override_builtin(self) -> None:
        reg = ToolRegistry()
        register_builtin_tools(reg)
        api = PluginAPI(PluginInfo(name="demo"), reg, None)
        with self.assertRaises(ValueError):
            api.tool("fs_read", "hijack")(lambda a, c: "x")
        api.tool("demo_tool", "ok")(lambda a, c: "x")
        self.assertEqual(reg.get("demo_tool").risk, "write")

    async def test_mcp_untrusted_workspace_and_meta_call_requires_connect(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="tui-mcp-"))
        try:
            (tmp / ".mcp.json").write_text(
                json.dumps({"mcpServers": {"local_srv": {"command": "echo", "args": ["hi"]}}}),
                encoding="utf-8",
            )
            configs = discover_configs(str(tmp), trusted=False)
            proj = [c for c in configs if c.name == "local_srv"]
            self.assertEqual(len(proj), 1)
            self.assertFalse(proj[0].trusted)

            reg = ToolRegistry()
            mgr = MCPManager(reg)
            mgr.load(str(tmp), trusted=False)
            ok, msg = await mgr.connect("local_srv")
            self.assertFalse(ok)
            self.assertIn("untrusted", msg.lower())

            mgr.add_raw("trusted_idle", {"command": "echo", "args": ["hi"]})
            with self.assertRaises(ToolError):
                await mgr._meta_call({"server": "trusted_idle", "tool": "t"}, ToolContext())

            cfg = MCPServerConfig.from_raw("http_srv", {"type": "http", "url": "https://example.com", "timeout": "bad"}, "gemini-tui")
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg.timeout, 900.0)
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class MemoryContextAndOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def test_memory_empty_query_does_not_match_all(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="tui-mem-"))
        try:
            store = MemoryStore(path=tmp / "mem.json", legacy_path=tmp / "leg.json")
            store.save("alpha", "important architecture note")
            self.assertEqual(store.search("the and or"), [])
            self.assertEqual(store.get("alpha").hits, 0)
            self.assertEqual(len(store.search("architecture")), 1)
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_context_trim_short_overbudget_and_no_leading_fn_resp(self) -> None:
        cm = ContextManager(budget_tokens=2000, reserve_tokens=500)
        msg1 = SimpleNamespace(role="user", parts=[SimpleNamespace(text="x" * 9000)])
        msg2 = SimpleNamespace(role="model", parts=[SimpleNamespace(text="ok")])
        msg3 = SimpleNamespace(
            role="user",
            parts=[SimpleNamespace(function_response=SimpleNamespace(name="fs_read", response={"r": "y"}))],
        )
        kept, report = cm.trim([msg1, msg2, msg3], keep_last=8)
        self.assertGreater(report.dropped, 0)
        if kept:
            self.assertFalse(getattr(kept[0].parts[0], "function_response", None))

    def test_capability_registry_does_not_leak_unmapped_tools(self) -> None:
        reg = ToolRegistry()
        register_builtin_tools(reg)
        reg.add(ToolSpec("cowork_run", "run cowork", {}, lambda a, c: None, category="agent", risk="write"))
        caps = CapabilityRegistry.from_components(reg)
        arch_tools = {c.name for c in caps.for_role("architect", kind="tool")}
        self.assertNotIn("cowork_run", arch_tools)

    async def test_worker_runner_routes_non_gemini_provider(self) -> None:
        mreg = ModelRegistry()
        fake_adapter = SimpleNamespace(
            name="openai_compat",
            available=lambda: True,
            complete=AsyncMock(return_value="openai response"),
        )
        mreg.register_provider(fake_adapter)
        mreg.register(ModelSpec(id="gpt-test", provider="openai_compat", capabilities=frozenset({CAP_TEXT})))

        service = SimpleNamespace(has_key=False)
        runner = GeminiWorkerRunner(service, ToolRegistry(), lambda r: ToolContext(), model_registry=mreg)
        req = WorkerRequest(
            task_id="t1",
            title="Plan",
            goal="Plan",
            role="architect",
            model="gpt-test",
            prompt="hi",
            system="sys",
            allowed_tools=(),
            timeout=30.0,
            checkpoint=AsyncMock(),
            emit=lambda l, lv="info": None,
            provider="openai_compat",
        )
        out = await runner.run(req)
        self.assertEqual(out.text, "openai response")
        fake_adapter.complete.assert_awaited_once()


class TeamViewRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_team_view_finish_does_not_raise_duplicate_key(self) -> None:
        from gemini_tui.app import GeminiStudio
        from gemini_tui.orchestration.scheduler import ScheduledTask, TaskScheduler
        from gemini_tui.widgets.views import TeamView

        app = GeminiStudio(splash=False, audit=False, memory=False)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            view = app.query_one(TeamView)
            task = ScheduledTask(id="t1", title="Check", role="researcher", goal="Check")
            app.team.scheduler = TaskScheduler([task])
            view.begin("Check")
            view.apply_event({"kind": "team_plan", "plan": {"tasks": [task.to_dict()]}, "summary": "plan"})
            task.status = "done"
            view.finish({"status": "done", "tasks": [task.to_dict()], "elapsed": 0.5})
            await pilot.pause()
            self.assertIn("t1", view._task_rows)

    async def test_assistant_message_remains_visible_after_stop_stream(self) -> None:
        from gemini_tui.app import GeminiStudio

        with patch("gemini_tui.app.load_dotenv_files", return_value=[]):
            app = GeminiStudio(splash=False, audit=False, memory=False)
            app.service.list_models = AsyncMock(return_value=[])
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app.chat.add_user("xin chào")
                msg = await app.chat.begin_assistant("gemini-3.1-flash-lite")
                await msg.write("### Xin chào\n\nPhản hồi bình thường.")
                await pilot.pause(0.1)
                await app.chat.end_assistant({"total": 42})
                await pilot.pause(0.1)
                self.assertTrue(msg.display)
                self.assertFalse(msg._closed)
                self.assertGreater(msg.size.height, 0)
