from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gemini_tui.agent.audit import AuditLog
from gemini_tui.tools.registry import ToolContext, ToolError, ToolRegistry, ToolSpec, prop, schema


def settings(**overrides):
    base = dict(
        approval_mode="ask",
        tool_policies={},
        denied_tools=[],
        computer_enabled=False,
        computer_approval="ask",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def spec(name: str, *, category: str = "files", risk: str = "read", dangerous: bool = False, handler=None):
    async def ok(arguments, ctx):
        return f"{name}:{arguments.get('value', '')}"

    return ToolSpec(
        name=name,
        description=f"{name} description",
        parameters=schema({"value": prop("string", "value")}),
        handler=handler or ok,
        dangerous=dangerous,
        category=category,
        risk=risk,
    )


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="registry-test-"))
        self.audit = AuditLog(self.tmp, enabled=False)
        self.registry = ToolRegistry()
        self.called: list[str] = []

        async def write_handler(arguments, ctx):
            self.called.append("write")
            return "written"

        async def shell_handler(arguments, ctx):
            self.called.append("shell")
            return "ran"

        self.registry.add(spec("fs_read"))
        self.registry.add(spec("fs_write", risk="write", dangerous=True, handler=write_handler))
        self.registry.add(spec("shell_run", category="shell", risk="execute", dangerous=True, handler=shell_handler))

    def ctx(self, *, approve=None, **config) -> ToolContext:
        return ToolContext(
            workspace=str(self.tmp),
            cwd=str(self.tmp),
            session_id="sess-1",
            settings=settings(**config),
            audit=self.audit,
            approve=approve,
        )

    async def test_read_tool_runs_freely(self) -> None:
        output = await self.registry.call("fs_read", {"value": "x"}, self.ctx())
        self.assertEqual(output, "fs_read:x")

    async def test_write_requires_approval(self) -> None:
        async def approve(name, args, description):
            return True

        output = await self.registry.call("fs_write", {}, self.ctx(approve=approve))
        self.assertEqual(output, "written")
        self.assertEqual(self.called, ["write"])

        record = self.audit.recent(5)[0]
        self.assertEqual(record.kind, "tool")
        self.assertTrue(record.ok)
        self.assertEqual(record.risk, "write")
        self.assertEqual(record.session, "sess-1")

    async def test_denied_approval_blocks_handler(self) -> None:
        async def approve(name, args, description):
            return False

        with self.assertRaises(ToolError):
            await self.registry.call("fs_write", {}, self.ctx(approve=approve))
        self.assertEqual(self.called, [])

        permission = [row for row in self.audit.recent(10) if row.kind == "permission"]
        self.assertTrue(permission)
        self.assertFalse(permission[0].ok)
        self.assertIn("denied", permission[0].detail.lower())

    async def test_missing_approval_ui_is_an_error(self) -> None:
        with self.assertRaises(ToolError) as caught:
            await self.registry.call("fs_write", {}, self.ctx())
        self.assertIn("approval", str(caught.exception).lower())
        self.assertEqual(self.called, [])

    async def test_readonly_policy_denies_before_handler(self) -> None:
        with self.assertRaises(ToolError):
            await self.registry.call("shell_run", {}, self.ctx(approval_mode="readonly"))
        self.assertEqual(self.called, [])

        denied = [row for row in self.audit.recent(10) if row.kind == "permission"]
        self.assertTrue(denied)
        self.assertFalse(denied[0].ok)
        self.assertIn("read-only", denied[0].detail)

    async def test_settings_override_allows(self) -> None:
        output = await self.registry.call(
            "shell_run", {}, self.ctx(tool_policies={"shell_run": "allow"})
        )
        self.assertEqual(output, "ran")

    async def test_denied_tools_and_unknown_tools(self) -> None:
        with self.assertRaises(ToolError):
            await self.registry.call("fs_read", {}, self.ctx(denied_tools=["fs_read"]))
        with self.assertRaises(ToolError):
            await self.registry.call("nope", {}, self.ctx())

    async def test_disabled_tool_is_refused(self) -> None:
        disabled = spec("off_tool")
        disabled.enabled = False
        self.registry.add(disabled)
        with self.assertRaises(ToolError):
            await self.registry.call("off_tool", {}, self.ctx())

    async def test_non_object_arguments_are_refused(self) -> None:
        with self.assertRaises(ToolError):
            await self.registry.call("fs_read", ["list"], self.ctx())

    async def test_handler_exception_becomes_tool_error(self) -> None:
        async def broken(arguments, ctx):
            raise ValueError("bad input")

        self.registry.add(spec("broken", handler=broken))
        with self.assertRaises(ToolError) as caught:
            await self.registry.call("broken", {}, self.ctx())
        self.assertIn("ValueError", str(caught.exception))

        record = self.audit.recent(5)[0]
        self.assertFalse(record.ok)
        self.assertIn("bad input", record.detail)

    async def test_cancellation_marks_audit_failure(self) -> None:
        import asyncio

        async def cancelled(arguments, ctx):
            raise asyncio.CancelledError

        self.registry.add(spec("slow", handler=cancelled))
        with self.assertRaises(asyncio.CancelledError):
            await self.registry.call("slow", {}, self.ctx())
        self.assertFalse(self.audit.recent(5)[0].ok)


class RegistryShapeTests(unittest.TestCase):
    def test_remove_and_remove_prefix(self) -> None:
        registry = ToolRegistry()
        registry.add(spec("computer_click", category="computer", risk="computer"))
        registry.add(spec("computer_move", category="computer", risk="computer"))
        registry.add(spec("fs_read"))
        self.assertTrue(registry.remove("fs_read"))
        self.assertFalse(registry.remove("fs_read"))
        registry.remove_prefix("computer_")
        self.assertEqual(registry.all(), [])

    def test_dangerous_and_enabled(self) -> None:
        registry = ToolRegistry()
        registry.add(spec("fs_write", risk="write", dangerous=True))
        disabled = spec("off")
        disabled.enabled = False
        registry.add(disabled)
        self.assertEqual(registry.dangerous_names, {"fs_write"})
        self.assertEqual([item.name for item in registry.enabled()], ["fs_write"])
        self.assertEqual(len(registry.all()), 2)

    def test_signature_marks_optional_arguments(self) -> None:
        item = spec("tool")
        self.assertIn("value: string?", item.signature)
        self.assertEqual(item.label, "TOOL")


if __name__ == "__main__":
    unittest.main()
