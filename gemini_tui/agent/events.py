"""Event stream emitted by cowork/computer agents.

The TUI consumes these events to render live progress. Keeping them plain
dataclasses makes the orchestration testable without Textual or the network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentEvent:
    kind: str
    """"plan | step_start | step_text | step_thought | step_tool | step_done |
    verify | done | error | notice | status"."""

    text: str = ""
    agent: str = "lead"
    step_id: str = ""
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "agent": self.agent,
            "step_id": self.step_id,
            "timestamp": self.timestamp,
            "data": self.data,
        }


def is_success_kind(kind: str) -> bool:
    return kind in {"plan", "step_start", "step_text", "step_done", "verify", "done"}
