"""Live agent state machine.

The command center renders exactly what is here: role, model, task, state,
progress, elapsed time, tool count and the agent's own log. Nothing is
simulated. Pause and cancel are cooperative: they take effect at task
boundaries or before the next model call, never in the middle of a tool write.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .capabilities import AgentRole, role_or_default

STATE_IDLE = "idle"
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_CANCELLING = "cancelling"

ACTIVE_STATES = {STATE_QUEUED, STATE_RUNNING, STATE_PAUSED, STATE_CANCELLING}
TERMINAL_STATES = {STATE_DONE, STATE_FAILED, STATE_CANCELLED}


@dataclass
class AgentState:
    id: str
    role: str
    model: str = ""
    task_id: str = ""
    task_title: str = ""
    state: str = STATE_QUEUED
    progress: float = 0.0
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    tool_calls: int = 0
    error: str = ""
    summary: str = ""
    logs: deque[tuple[float, str, str]] = field(default_factory=lambda: deque(maxlen=400))

    @property
    def role_spec(self) -> AgentRole:
        return role_or_default(self.role)

    @property
    def label(self) -> str:
        return self.role_spec.label

    @property
    def elapsed(self) -> float:
        return max(0.0, (self.ended_at or time.time()) - self.started_at)

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    def log(self, line: str, level: str = "info") -> None:
        self.logs.append((time.time(), level, str(line)[:600]))

    def recent_logs(self, limit: int = 40) -> list[tuple[float, str, str]]:
        return list(self.logs)[-max(1, limit) :]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "label": self.label,
            "model": self.model,
            "task_id": self.task_id,
            "task_title": self.task_title,
            "state": self.state,
            "progress": round(self.progress, 3),
            "elapsed": round(self.elapsed, 2),
            "tool_calls": self.tool_calls,
            "error": self.error,
            "summary": self.summary[:1000],
        }


class AgentManager:
    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self._agents: dict[str, AgentState] = {}
        self._seq = 0
        self._pause_gates: dict[str, asyncio.Event] = {}
        self._cancelled: set[str] = set()
        self._global_paused = False
        self._global_cancel = False

    # ── lifecycle ─────────────────────────────────────────────────
    def create(
        self,
        role: str,
        model: str = "",
        *,
        task_id: str = "",
        task_title: str = "",
        agent_id: str = "",
    ) -> AgentState:
        self._seq += 1
        identifier = agent_id or f"agent-{self._seq:02d}"
        spec = role_or_default(role)
        agent = AgentState(
            id=identifier,
            role=spec.key,
            model=model,
            task_id=task_id,
            task_title=task_title,
            started_at=self.clock(),
        )
        self._agents[identifier] = agent
        gate = asyncio.Event()
        gate.set()
        self._pause_gates[identifier] = gate
        return agent

    def get(self, agent_id: str) -> AgentState | None:
        return self._agents.get(agent_id)

    def all(self) -> list[AgentState]:
        return sorted(self._agents.values(), key=lambda agent: agent.started_at, reverse=True)

    def active(self) -> list[AgentState]:
        return [agent for agent in self._agents.values() if agent.active]

    def for_task(self, task_id: str) -> AgentState | None:
        matches = [agent for agent in self._agents.values() if agent.task_id == task_id]
        return matches[-1] if matches else None

    @property
    def active_count(self) -> int:
        return len(self.active())

    @property
    def globally_paused(self) -> bool:
        return self._global_paused

    @property
    def globally_cancelled(self) -> bool:
        return self._global_cancel

    # ── mutation ──────────────────────────────────────────────────
    def set_state(self, agent_id: str, state: str) -> None:
        agent = self._agents.get(agent_id)
        if agent is None:
            return
        agent.state = state
        if state in TERMINAL_STATES:
            agent.ended_at = self.clock()

    def update(self, agent_id: str, **fields: Any) -> None:
        agent = self._agents.get(agent_id)
        if agent is None:
            return
        for key, value in fields.items():
            if hasattr(agent, key):
                setattr(agent, key, value)

    def log(self, agent_id: str, line: str, level: str = "info") -> None:
        agent = self._agents.get(agent_id)
        if agent is not None:
            agent.log(line, level)

    def progress(self, agent_id: str, value: float) -> None:
        agent = self._agents.get(agent_id)
        if agent is not None:
            agent.progress = max(0.0, min(1.0, float(value)))

    def tool_call(self, agent_id: str, name: str = "", ok: bool = True) -> None:
        agent = self._agents.get(agent_id)
        if agent is None:
            return
        agent.tool_calls += 1
        agent.log(f"{'✔' if ok else '✖'} tool {name}".strip(), "tool" if ok else "error")

    def finish(self, agent_id: str, state: str, *, summary: str = "", error: str = "") -> None:
        agent = self._agents.get(agent_id)
        if agent is None:
            return
        agent.state = state
        agent.summary = summary[:4000]
        agent.error = error[:1000]
        agent.ended_at = self.clock()
        agent.progress = 1.0 if state == STATE_DONE else agent.progress
        if summary:
            agent.log(summary[:300], "done" if state == STATE_DONE else "error")
        self._pause_gates.get(agent_id, asyncio.Event()).set()

    # ── pause / resume / cancel ───────────────────────────────────
    def pause(self, agent_id: str) -> bool:
        agent = self._agents.get(agent_id)
        gate = self._pause_gates.get(agent_id)
        if agent is None or gate is None or agent.state in TERMINAL_STATES:
            return False
        gate.clear()
        if agent.state == STATE_RUNNING:
            agent.state = STATE_PAUSED
        agent.log("paused", "warn")
        return True

    def resume(self, agent_id: str) -> bool:
        gate = self._pause_gates.get(agent_id)
        agent = self._agents.get(agent_id)
        if gate is None or agent is None or agent.state in TERMINAL_STATES:
            return False
        gate.set()
        if agent.state == STATE_PAUSED:
            agent.state = STATE_RUNNING
        agent.log("resumed", "info")
        return True

    def pause_all(self) -> int:
        self._global_paused = True
        return sum(1 for agent in self.active() if self.pause(agent.id))

    def resume_all(self) -> int:
        self._global_paused = False
        return sum(1 for agent in self._agents.values() if self.resume(agent.id))

    def request_cancel(self, agent_id: str) -> bool:
        agent = self._agents.get(agent_id)
        if agent is None or agent.state in TERMINAL_STATES:
            return False
        self._cancelled.add(agent_id)
        if agent.state in (STATE_QUEUED, STATE_RUNNING, STATE_PAUSED):
            agent.state = STATE_CANCELLING
        agent.log("cancellation requested", "warn")
        self._pause_gates.get(agent_id, asyncio.Event()).set()
        return True

    def cancel_all(self) -> int:
        self._global_cancel = True
        return sum(1 for agent in self.active() if self.request_cancel(agent.id))

    def is_cancelled(self, agent_id: str) -> bool:
        return self._global_cancel or agent_id in self._cancelled

    def reset_cancellation(self) -> None:
        self._global_cancel = False
        self._cancelled.clear()

    async def wait_ready(self, agent_id: str, poll: float = 0.08) -> None:
        """Block while paused; raise CancelledError when cancellation is asked."""
        gate = self._pause_gates.get(agent_id)
        while True:
            if self.is_cancelled(agent_id):
                raise asyncio.CancelledError("agent cancelled")
            if gate is None or gate.is_set():
                return
            await asyncio.sleep(poll)

    # ── snapshots ─────────────────────────────────────────────────
    def snapshot(self) -> list[dict[str, Any]]:
        return [agent.to_dict() for agent in self.all()]

    def render_log(self, agent_id: str, limit: int = 40) -> str:
        agent = self._agents.get(agent_id)
        if agent is None:
            return ""
        lines = []
        for stamp, level, line in agent.recent_logs(limit):
            label = time.strftime("%H:%M:%S", time.localtime(stamp))
            lines.append(f"{label} {level:<5} {line}")
        return "\n".join(lines)

    def summary(self) -> str:
        active = self.active_count
        done = sum(1 for agent in self._agents.values() if agent.state == STATE_DONE)
        failed = sum(1 for agent in self._agents.values() if agent.state == STATE_FAILED)
        return f"{len(self._agents)} agents · {active} active · {done} done · {failed} failed"
