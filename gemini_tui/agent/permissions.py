"""Execution policy: risk classes, approvals, denies and computer guards.

The registry used to treat every non-dangerous tool as free and every
dangerous tool as `approval_mode`. That is too coarse for computer control, so
each tool now carries a risk class and the policy resolves a verdict:
`allow`, `ask` or `deny`.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

RISK_READ = "read"
RISK_NETWORK = "network"
RISK_WRITE = "write"
RISK_EXECUTE = "execute"
RISK_SCREEN = "screen"
RISK_COMPUTER = "computer"

RISKS = (RISK_READ, RISK_NETWORK, RISK_WRITE, RISK_EXECUTE, RISK_SCREEN, RISK_COMPUTER)
FREE_RISKS = {RISK_READ, RISK_NETWORK}
RISK_RANK = {risk: index for index, risk in enumerate(RISKS)}

MODE_ALLOW = "allow"
MODE_ASK = "ask"
MODE_DENY = "deny"

# Default risk classes by tool category. A tool can override this explicitly.
CATEGORY_RISK = {
    "files": RISK_READ,
    "shell": RISK_EXECUTE,
    "git": RISK_READ,
    "web": RISK_NETWORK,
    "system": RISK_READ,
    "utility": RISK_READ,
    "workspace": RISK_READ,
    "computer": RISK_COMPUTER,
    "code": RISK_READ,
    "agent": RISK_WRITE,
    "skills": RISK_READ,
    "media": RISK_WRITE,
    "bridge": RISK_EXECUTE,
}


class PermissionDenied(Exception):
    """Policy refused an action before it reached the human."""


@dataclass(frozen=True)
class PermissionVerdict:
    mode: str
    risk: str
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.mode != MODE_DENY


class PermissionPolicy:
    """Resolve a tool + settings into allow/ask/deny."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    # ── classification ────────────────────────────────────────────
    def risk_for(self, spec: Any) -> str:
        explicit = getattr(spec, "risk", "") or ""
        if explicit in RISKS:
            return explicit
        category = getattr(spec, "category", "") or ""
        risk = CATEGORY_RISK.get(category, RISK_READ)
        if getattr(spec, "dangerous", False) and risk in FREE_RISKS:
            risk = RISK_WRITE if category in ("files", "workspace") else RISK_EXECUTE
        return risk

    # ── overrides ─────────────────────────────────────────────────
    def _override(self, spec: Any, risk: str) -> str:
        policies = getattr(self.settings, "tool_policies", None) or {}
        if not isinstance(policies, dict):
            return ""
        for key in (getattr(spec, "name", ""), getattr(spec, "category", ""), risk):
            value = str(policies.get(key, "") or "").lower()
            if value in (MODE_ALLOW, MODE_ASK, MODE_DENY):
                return value
        return ""

    def decide(self, spec: Any, *, ceiling: str = "") -> PermissionVerdict:
        name = getattr(spec, "name", "?")
        risk = self.risk_for(spec)
        denied = set(getattr(self.settings, "denied_tools", None) or [])
        if name in denied:
            return PermissionVerdict(MODE_DENY, risk, f"`{name}` is disabled in settings.denied_tools")

        mode = getattr(self.settings, "approval_mode", "ask") or "ask"

        # A read-only session is a hard boundary: an explicit allow override
        # must not be able to turn a write or execute tool back on.
        if mode == "readonly" and risk not in FREE_RISKS:
            return PermissionVerdict(
                MODE_DENY,
                risk,
                f"`{name}` is a {risk} tool but the session is read-only",
            )

        # Computer-use opt-in and computer_approval="readonly" are hard boundaries
        # that policy overrides cannot bypass.
        if risk in (RISK_SCREEN, RISK_COMPUTER) and not getattr(self.settings, "computer_enabled", False):
            return PermissionVerdict(
                MODE_DENY,
                risk,
                "Computer control is disabled. Enable it with /computer enable or --computer.",
            )
        if risk == RISK_COMPUTER and (getattr(self.settings, "computer_approval", "ask") or "ask") == "readonly":
            return PermissionVerdict(
                MODE_DENY, risk, "Computer input is disabled in read-only computer mode"
            )

        override = self._override(spec, risk)
        if override:
            verdict = PermissionVerdict(override, risk, f"policy override for {name}")
            if not verdict.allowed:
                return verdict
            return self._apply_ceiling(verdict, name, risk, ceiling)

        if risk in FREE_RISKS:
            return self._apply_ceiling(
                PermissionVerdict(MODE_ALLOW, risk), name, risk, ceiling
            )

        if risk == RISK_SCREEN:
            if not getattr(self.settings, "computer_enabled", False):
                return PermissionVerdict(
                    MODE_DENY,
                    risk,
                    "Screen observation is disabled. Enable computer control with /computer enable.",
                )
            # Observing is part of the consent granted by enabling computer control.
            return self._apply_ceiling(
                PermissionVerdict(MODE_ALLOW, risk, "screen observation"), name, risk, ceiling
            )

        if risk == RISK_COMPUTER:
            if not getattr(self.settings, "computer_enabled", False):
                return PermissionVerdict(
                    MODE_DENY,
                    risk,
                    "Computer control is disabled. Enable it with /computer enable or --computer.",
                )
            computer_mode = getattr(self.settings, "computer_approval", "ask") or "ask"
            if computer_mode == "readonly":
                return PermissionVerdict(
                    MODE_DENY, risk, "Computer input is disabled in read-only computer mode"
                )
            # Input tools always ask unless the user explicitly opted into auto.
            resolved = MODE_ALLOW if computer_mode == "auto" else computer_mode
            return self._apply_ceiling(
                PermissionVerdict(resolved, risk, "computer action"), name, risk, ceiling
            )

        # write / execute inherit the global approval mode
        resolved = MODE_ALLOW if mode == "auto" else MODE_ASK
        reason = f"{risk} tool" if resolved == MODE_ASK else ""
        return self._apply_ceiling(
            PermissionVerdict(resolved, risk, reason), name, risk, ceiling
        )

    @staticmethod
    def _apply_ceiling(
        verdict: PermissionVerdict, name: str, risk: str, ceiling: str
    ) -> PermissionVerdict:
        """An agent role may never silently exceed its risk ceiling.

        Inside the ceiling the normal verdict stands. Above it, an `allow`
        becomes `ask` so the human still sees the action; `deny` stays `deny`.
        """
        if verdict.mode != MODE_ALLOW or ceiling not in RISK_RANK or risk not in RISK_RANK:
            return verdict
        if RISK_RANK[risk] <= RISK_RANK[ceiling]:
            return verdict
        return PermissionVerdict(
            MODE_ASK,
            risk,
            f"`{name}` is a {risk} action above this agent's {ceiling} ceiling; "
            "it needs explicit human approval",
        )


class ComputerGuard:
    """Rate limit and kill-switch for screen/input actions.

    The guard does not decide whether an action is allowed (the policy does).
    It ensures the backend is enabled and that an agent cannot spin at full
    speed, which is the usual failure mode of computer-use loops.
    """

    def __init__(self, settings: Any, backend_available: bool = True) -> None:
        self.settings = settings
        self.backend_available = backend_available
        self._actions: deque[float] = deque(maxlen=512)
        self.total_actions = 0
        self.blocked_actions = 0

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "computer_enabled", False)) and self.backend_available

    @property
    def actions_this_minute(self) -> int:
        cutoff = time.time() - 60
        return sum(1 for stamp in self._actions if stamp >= cutoff)

    def check(self, action: str) -> None:
        if not self.backend_available:
            self.blocked_actions += 1
            raise PermissionDenied(
                "Computer control is not available on this platform. "
                "Gemini Computer currently requires Windows 10/11."
            )
        if not getattr(self.settings, "computer_enabled", False):
            self.blocked_actions += 1
            raise PermissionDenied(
                "Computer control is disabled. Enable it with /computer enable or launch with --computer."
            )
        limit = max(1, int(getattr(self.settings, "computer_rate_limit", 90) or 90))
        if self.actions_this_minute >= limit:
            self.blocked_actions += 1
            raise PermissionDenied(
                f"Computer rate limit reached ({limit} actions/minute). Wait a minute or raise "
                "settings.computer_rate_limit."
            )
        self._actions.append(time.time())
        self.total_actions += 1

    def state(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self.backend_available,
            "actions": self.total_actions,
            "blocked": self.blocked_actions,
            "per_minute": self.actions_this_minute,
        }
