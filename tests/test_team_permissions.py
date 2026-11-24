"""Team permission boundaries: ceilings, read-only, MCP risk and file leases.

Planning is not execution authority. These tests prove that a task's role can
lower what a policy allow means, that read-only cannot be overridden, and that
MCP/computer/write actions still go through the existing approval gate.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gemini_tui.agent.permissions import (
    PermissionPolicy,
    RISK_COMPUTER,
    RISK_EXECUTE,
    RISK_READ,
    RISK_WRITE,
)
from gemini_tui.app import GeminiStudio
from gemini_tui.orchestration.capabilities import CapabilityRegistry
from gemini_tui.orchestration.ownership import FileOwnership
from gemini_tui.tools.builtin import register_builtin_tools
from gemini_tui.tools.mcp_client import MCPManager
from gemini_tui.tools.registry import ToolContext, ToolError, ToolRegistry, ToolSpec


def spec(name: str, *, category: str = "files", risk: str = "", dangerous: bool = False):
    return SimpleNamespace(name=name, category=category, risk=risk, dangerous=dangerous)


def settings(**overrides):
    base = dict(
        approval_mode="ask",
        computer_enabled=False,
        computer_approval="ask",
        computer_rate_limit=90,
        tool_policies={},
        denied_tools=[],
        agent_permissions={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class CeilingTests(unittest.TestCase):
    def test_readonly_denies_write_even_with_allow_override(self) -> None:
        policy = PermissionPolicy(
            settings(approval_mode="readonly", tool_policies={"fs_write": "allow"})
        )
        verdict = policy.decide(spec("fs_write", dangerous=True), ceiling="computer")
        self.assertEqual(verdict.mode, "deny")
        self.assertIn("read-only", verdict.reason)

    def test_readonly_still_allows_read_and_network(self) -> None:
        policy = PermissionPolicy(settings(approval_mode="readonly"))
        self.assertEqual(policy.decide(spec("fs_read", category="files")).mode, "allow")
        self.assertEqual(policy.decide(spec("web_search", category="web")).mode, "allow")

    def test_auto_allow_is_downgraded_above_the_role_ceiling(self) -> None:
        policy = PermissionPolicy(settings(approval_mode="auto"))
        verdict = policy.decide(spec("shell_run", category="shell"), ceiling="read")
        self.assertEqual(verdict.mode, "ask")
        self.assertIn("read ceiling", verdict.reason)

    def test_allow_at_or_below_the_ceiling_is_unchanged(self) -> None:
        policy = PermissionPolicy(
            settings(
                approval_mode="auto",
                computer_enabled=True,
                computer_approval="auto",
            )
        )
        self.assertEqual(
            policy.decide(spec("fs_write", dangerous=True), ceiling="write").mode, "allow"
        )
        self.assertEqual(
            policy.decide(spec("shell_run", category="shell"), ceiling="execute").mode,
            "allow",
        )
        self.assertEqual(
            policy.decide(spec("computer_click", category="computer"), ceiling="computer").mode,
            "allow",
        )

    def test_explicit_deny_is_never_upgraded_by_a_ceiling(self) -> None:
        policy = PermissionPolicy(
            settings(tool_policies={"fs_write": "deny"})
        )
        verdict = policy.decide(spec("fs_write", dangerous=True), ceiling="computer")
        self.assertEqual(verdict.mode, "deny")

    def test_allow_override_above_ceiling_still_asks(self) -> None:
        policy = PermissionPolicy(
            settings(approval_mode="ask", tool_policies={"fs_write": "allow"})
        )
        verdict = policy.decide(spec("fs_write", dangerous=True), ceiling="read")
        self.assertEqual(verdict.mode, "ask")
        self.assertIn("above this agent's read ceiling", verdict.reason)

    def test_denied_tools_win_over_everything(self) -> None:
        policy = PermissionPolicy(
            settings(approval_mode="auto", denied_tools=["fs_write"])
        )
        verdict = policy.decide(spec("fs_write"), ceiling="computer")
        self.assertEqual(verdict.mode, "deny")
        self.assertIn("denied_tools", verdict.reason)

    def test_disabled_computer_stays_denied_above_ceiling(self) -> None:
        policy = PermissionPolicy(settings(approval_mode="auto", computer_enabled=False))
        verdict = policy.decide(spec("computer_click", category="computer"))
        self.assertEqual(verdict.mode, "deny")
        self.assertIn("disabled", verdict.reason)

    def test_computer_action_above_read_ceiling_asks_even_when_enabled(self) -> None:
        policy = PermissionPolicy(
            settings(approval_mode="auto", computer_enabled=True, computer_approval="auto")
        )
        verdict = policy.decide(spec("computer_click", category="computer"), ceiling="read")
        self.assertEqual(verdict.mode, "ask")
        self.assertEqual(verdict.risk, RISK_COMPUTER)


class RegistryGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_reads_the_ceiling_from_the_context(self) -> None:
        registry = ToolRegistry()
        executed: list[str] = []

        async def handler(arguments, ctx):
            executed.append("ran")
            return "ran"

        registry.add(
            ToolSpec(
                name="shell_run",
                description="run",
                parameters={"type": "object", "properties": {}},
                handler=handler,
                category="shell",
                risk=RISK_EXECUTE,
            )
        )
        ctx = ToolContext(settings=settings(approval_mode="auto"), extras={"risk_ceiling": "read"})
        with self.assertRaises(ToolError) as caught:
            await registry.call("shell_run", {}, ctx)
        self.assertIn("requires human approval", str(caught.exception))
        self.assertEqual(executed, [])

    async def test_ceiling_ask_reaches_the_approval_callback(self) -> None:
        registry = ToolRegistry()
        approvals: list[str] = []

        async def handler(arguments, ctx):
            return "ran"

        async def approve(name, arguments, description):
            approvals.append(name)
            return True

        registry.add(
            ToolSpec(
                name="shell_run",
                description="run",
                parameters={"type": "object", "properties": {}},
                handler=handler,
                category="shell",
                risk=RISK_EXECUTE,
            )
        )
        ctx = ToolContext(
            settings=settings(approval_mode="auto"),
            approve=approve,
            extras={"risk_ceiling": "read"},
        )
        self.assertEqual(await registry.call("shell_run", {}, ctx), "ran")
        self.assertEqual(approvals, ["shell_run"])

    async def test_readonly_denies_before_asking(self) -> None:
        registry = ToolRegistry()
        asked: list[str] = []

        async def handler(arguments, ctx):
            return "should not run"

        async def approve(name, arguments, description):
            asked.append(name)
            return True

        registry.add(
            ToolSpec(
                name="fs_write",
                description="write",
                parameters={"type": "object", "properties": {}},
                handler=handler,
                category="files",
                risk=RISK_WRITE,
            )
        )
        ctx = ToolContext(settings=settings(approval_mode="readonly"), approve=approve)
        with self.assertRaises(ToolError) as caught:
            await registry.call("fs_write", {}, ctx)
        self.assertIn("read-only", str(caught.exception))
        self.assertEqual(asked, [])

    async def test_mcp_connect_is_gated_as_execute(self) -> None:
        registry = ToolRegistry()
        manager = MCPManager(registry)
        manager._register_meta_tools()
        connect = registry.get("mcp_connect")
        self.assertIsNotNone(connect)
        self.assertEqual(connect.risk, RISK_EXECUTE)

        ctx = ToolContext(settings=settings(approval_mode="auto"), extras={"risk_ceiling": "read"})
        with self.assertRaises(ToolError) as caught:
            await registry.call("mcp_connect", {"server": "demo"}, ctx)
        self.assertIn("requires human approval", str(caught.exception))
        self.assertEqual(manager.states, {})

    async def test_mcp_call_is_classified_as_write(self) -> None:
        registry = ToolRegistry()
        manager = MCPManager(registry)
        manager._register_meta_tools()
        policy = PermissionPolicy(settings(approval_mode="auto"))
        call = registry.get("mcp_call")
        listing = registry.get("mcp_list_servers")
        self.assertEqual(call.risk, RISK_WRITE)
        self.assertEqual(listing.risk, RISK_READ)
        self.assertEqual(policy.decide(call, ceiling="read").mode, "ask")
        self.assertEqual(policy.decide(listing, ceiling="read").mode, "allow")


class OwnershipToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="team-permissions-"))
        self.registry = ToolRegistry()
        register_builtin_tools(self.registry)
        self.ownership = FileOwnership()

    def context(self, agent: str, task: str) -> ToolContext:
        config = settings(approval_mode="auto")
        return ToolContext(
            cwd=str(self.tmp),
            workspace=str(self.tmp),
            settings=config,
            permissions=PermissionPolicy(config),
            extras={
                "ownership": self.ownership,
                "ownership_agent": agent,
                "team_task": task,
                "risk_ceiling": "computer",
            },
        )

    async def test_first_writer_claims_the_file_and_records_the_change(self) -> None:
        output = await self.registry.call(
            "fs_write",
            {"path": "owned.py", "content": "value = 1\n"},
            self.context("task:t1", "t1"),
        )
        self.assertIn("Created", output)
        self.assertEqual((self.tmp / "owned.py").read_text(encoding="utf-8"), "value = 1\n")
        self.assertEqual(self.ownership.owner(self.tmp / "owned.py"), "task:t1")
        change = self.ownership.changes(limit=5)[0]
        self.assertEqual(change.task, "t1")
        self.assertEqual(change.agent, "task:t1")
        self.assertIn("write", change.detail)

    async def test_second_agent_cannot_overwrite_the_same_file(self) -> None:
        await self.registry.call(
            "fs_write",
            {"path": "owned.py", "content": "first\n"},
            self.context("task:t1", "t1"),
        )
        with self.assertRaises(ToolError) as caught:
            await self.registry.call(
                "fs_write",
                {"path": "owned.py", "content": "second\n"},
                self.context("task:t2", "t2"),
            )
        self.assertIn("File ownership conflict", str(caught.exception))
        self.assertIn("task:t1", str(caught.exception))
        self.assertEqual((self.tmp / "owned.py").read_text(encoding="utf-8"), "first\n")

    async def test_editing_the_owned_file_by_its_owner_is_allowed(self) -> None:
        await self.registry.call(
            "fs_write",
            {"path": "owned.py", "content": "value = 1\n"},
            self.context("task:t1", "t1"),
        )
        output = await self.registry.call(
            "fs_edit",
            {"path": "owned.py", "old_string": "value = 1", "new_string": "value = 2"},
            self.context("task:t1", "t1"),
        )
        self.assertIn("Edited", output)
        self.assertEqual(
            (self.tmp / "owned.py").read_text(encoding="utf-8"), "value = 2\n"
        )


class AppContextTests(unittest.TestCase):
    def test_team_tool_context_carries_ceiling_lease_and_goal(self) -> None:
        ownership = FileOwnership()
        app = SimpleNamespace(
            settings=settings(agent_permissions={"architect": "network"}),
            ownership=ownership,
            team=SimpleNamespace(plan=SimpleNamespace(goal="audit the API")),
            tool_context=lambda *, agent, extras: ToolContext(extras=extras),
        )
        ctx = GeminiStudio.team_tool_context(
            app, SimpleNamespace(role="architect", task_id="t7")
        )
        self.assertEqual(ctx.extras["risk_ceiling"], "network")
        self.assertIs(ctx.extras["ownership"], ownership)
        self.assertEqual(ctx.extras["ownership_agent"], "task:t7")
        self.assertEqual(ctx.extras["team_task"], "t7")
        self.assertEqual(ctx.extras["team_goal"], "audit the API")

    def test_team_tool_context_uses_the_role_default_without_an_override(self) -> None:
        app = SimpleNamespace(
            settings=settings(),
            ownership=FileOwnership(),
            team=SimpleNamespace(plan=None),
            tool_context=lambda *, agent, extras: ToolContext(extras=extras),
        )
        ctx = GeminiStudio.team_tool_context(
            app, SimpleNamespace(role="developer", task_id="t8")
        )
        self.assertEqual(ctx.extras["risk_ceiling"], "computer")
        self.assertEqual(ctx.extras["team_goal"], "")


class DiscoveryTests(unittest.TestCase):
    def test_mirrored_mcp_tools_are_write_risk_for_relevant_roles(self) -> None:
        registry = ToolRegistry()
        registry.add(
            ToolSpec(
                name="mcp_demo_query",
                description="query",
                parameters={"type": "object", "properties": {}},
                handler=lambda arguments, ctx: "ok",
                category="mcp",
                source="mcp:demo",
                risk="write",
            )
        )
        capabilities = CapabilityRegistry.from_components(registry)
        item = capabilities.get("mcp_demo_query", kind="tool")
        self.assertIsNotNone(item)
        self.assertEqual(item.risk, "write")
        for role in ("developer", "researcher", "debugger", "tester"):
            self.assertIn(item, capabilities.for_role(role, kind="tool"))
        self.assertNotIn(item, capabilities.for_role("writer", kind="tool"))


if __name__ == "__main__":
    unittest.main()
