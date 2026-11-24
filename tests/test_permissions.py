from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gemini_tui.agent.permissions import (
    ComputerGuard,
    PermissionDenied,
    PermissionPolicy,
)


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
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class PolicyTests(unittest.TestCase):
    def test_read_and_network_are_free(self) -> None:
        policy = PermissionPolicy(settings(approval_mode="readonly"))
        self.assertEqual(policy.decide(spec("fs_read")).mode, "allow")
        self.assertEqual(policy.decide(spec("web_search", category="web")).mode, "allow")

    def test_readonly_denies_write_and_execute(self) -> None:
        policy = PermissionPolicy(settings(approval_mode="readonly"))
        write = policy.decide(spec("fs_write", dangerous=True))
        shell = policy.decide(spec("shell_run", category="shell", dangerous=True))
        self.assertFalse(write.allowed)
        self.assertFalse(shell.allowed)
        self.assertEqual(write.risk, "write")
        self.assertEqual(shell.risk, "execute")

    def test_ask_and_auto(self) -> None:
        ask = PermissionPolicy(settings(approval_mode="ask"))
        auto = PermissionPolicy(settings(approval_mode="auto"))
        self.assertEqual(ask.decide(spec("fs_edit", dangerous=True)).mode, "ask")
        self.assertEqual(auto.decide(spec("fs_edit", dangerous=True)).mode, "allow")

    def test_explicit_risk_wins(self) -> None:
        policy = PermissionPolicy(settings())
        verdict = policy.decide(spec("code_test", category="code", risk="execute", dangerous=True))
        self.assertEqual(verdict.risk, "execute")
        self.assertEqual(verdict.mode, "ask")

    def test_screen_requires_consent(self) -> None:
        disabled = PermissionPolicy(settings())
        self.assertFalse(disabled.decide(spec("computer_screenshot", category="computer", risk="screen")).allowed)
        enabled = PermissionPolicy(settings(computer_enabled=True))
        self.assertEqual(
            enabled.decide(spec("computer_screenshot", category="computer", risk="screen")).mode, "allow"
        )

    def test_computer_input_modes(self) -> None:
        ask = PermissionPolicy(settings(computer_enabled=True, computer_approval="ask"))
        auto = PermissionPolicy(settings(computer_enabled=True, computer_approval="auto"))
        readonly = PermissionPolicy(settings(computer_enabled=True, computer_approval="readonly"))
        click = spec("computer_click", category="computer", risk="computer", dangerous=True)
        self.assertEqual(ask.decide(click).mode, "ask")
        self.assertEqual(auto.decide(click).mode, "allow")
        self.assertFalse(readonly.decide(click).allowed)

    def test_disabled_computer_denies_input(self) -> None:
        policy = PermissionPolicy(settings(computer_enabled=False))
        verdict = policy.decide(spec("computer_click", category="computer", risk="computer"))
        self.assertFalse(verdict.allowed)
        self.assertIn("disabled", verdict.reason.lower())

    def test_overrides_and_denied_tools(self) -> None:
        policy = PermissionPolicy(
            settings(
                tool_policies={"fs_edit": "deny", "shell_run": "allow"},
                denied_tools=["web_fetch"],
            )
        )
        self.assertFalse(policy.decide(spec("fs_edit", dangerous=True)).allowed)
        self.assertEqual(policy.decide(spec("shell_run", category="shell", dangerous=True)).mode, "allow")
        self.assertFalse(policy.decide(spec("web_fetch", category="web")).allowed)


class GuardTests(unittest.TestCase):
    def test_disabled_guard_blocks(self) -> None:
        guard = ComputerGuard(settings(), backend_available=True)
        with self.assertRaises(PermissionDenied):
            guard.check("click")
        self.assertEqual(guard.blocked_actions, 1)

    def test_guard_counts_and_rate_limits(self) -> None:
        config = settings(computer_enabled=True, computer_rate_limit=2)
        guard = ComputerGuard(config, backend_available=True)
        guard.check("move")
        guard.check("move")
        self.assertEqual(guard.total_actions, 2)
        with self.assertRaises(PermissionDenied):
            guard.check("move")
        self.assertEqual(guard.blocked_actions, 1)
        state = guard.state()
        self.assertTrue(state["enabled"])
        self.assertEqual(state["actions"], 2)

    def test_unavailable_backend_blocks(self) -> None:
        guard = ComputerGuard(settings(computer_enabled=True), backend_available=False)
        with self.assertRaises(PermissionDenied):
            guard.check("screenshot")


if __name__ == "__main__":
    unittest.main()
