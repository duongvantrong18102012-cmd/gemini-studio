"""Task plans: the data model shared by the planner, workers and the TUI.

A plan is deliberately plain data. It is produced by the model as JSON, parsed
tolerantly, validated here, persisted to disk and replayed by the orchestrator.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

AGENTS = ("code", "computer", "research", "review", "general")
STEP_STATUSES = ("pending", "active", "done", "failed", "skipped")
RISKS = ("read", "write", "execute", "computer")

AGENT_LABELS = {
    "code": "Gemini Code",
    "computer": "Gemini Computer",
    "research": "Research",
    "review": "Review",
    "general": "General",
}


class PlanError(ValueError):
    """The model returned a plan that cannot be executed."""


def _now() -> float:
    return time.time()


def _clean_title(value: str, fallback: str = "Step") -> str:
    text = " ".join(str(value or "").split())
    return (text or fallback)[:120]


@dataclass
class PlanStep:
    id: str
    title: str
    goal: str = ""
    agent: str = "code"
    tools: list[str] = field(default_factory=list)
    risk: str = "read"
    status: str = "pending"          # pending | active | done | failed | skipped
    attempts: int = 0
    result: str = ""
    error: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.ended_at or _now()
        return max(0.0, end - self.started_at)

    @property
    def agent_label(self) -> str:
        return AGENT_LABELS.get(self.agent, self.agent.title())

    @property
    def summary(self) -> str:
        text = self.result or self.error or self.goal
        return " ".join(text.split())[:400]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "agent": self.agent,
            "tools": list(self.tools),
            "risk": self.risk,
            "status": self.status,
            "attempts": self.attempts,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanStep":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:6]),
            title=_clean_title(data.get("title", "")),
            goal=str(data.get("goal", "") or ""),
            agent=str(data.get("agent", "code") or "code").lower(),
            tools=[str(item) for item in data.get("tools", []) or []],
            risk=str(data.get("risk", "read") or "read").lower(),
            status=str(data.get("status", "pending") or "pending").lower(),
            attempts=int(data.get("attempts", 0) or 0),
            result=str(data.get("result", "") or ""),
            error=str(data.get("error", "") or ""),
            started_at=float(data.get("started_at", 0) or 0),
            ended_at=float(data.get("ended_at", 0) or 0),
        )


@dataclass
class TaskPlan:
    task: str
    steps: list[PlanStep] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "draft"             # draft | running | done | failed | cancelled
    model: str = ""
    created: float = field(default_factory=_now)
    updated: float = field(default_factory=_now)
    context_notes: str = ""
    verdict: str = ""
    error: str = ""

    # ── state helpers ─────────────────────────────────────────────
    @property
    def completed(self) -> int:
        return sum(1 for step in self.steps if step.status == "done")

    @property
    def failed(self) -> int:
        return sum(1 for step in self.steps if step.status == "failed")

    @property
    def progress(self) -> float:
        if not self.steps:
            return 0.0
        finished = sum(1 for step in self.steps if step.status in ("done", "failed", "skipped"))
        return finished / len(self.steps)

    @property
    def active_step(self) -> PlanStep | None:
        return next((step for step in self.steps if step.status == "active"), None)

    def get_step(self, step_id: str) -> PlanStep | None:
        return next((step for step in self.steps if step.id == step_id), None)

    def touch(self) -> None:
        self.updated = _now()

    def next_pending(self) -> PlanStep | None:
        return next((step for step in self.steps if step.status == "pending"), None)

    def summary_text(self, limit: int = 2200) -> str:
        lines = [f"Task: {self.task}", f"Plan: {self.id} · {self.completed}/{len(self.steps)} steps done"]
        for index, step in enumerate(self.steps, 1):
            glyph = {
                "done": "✔", "failed": "✖", "active": "●", "skipped": "→", "pending": "○",
            }.get(step.status, "○")
            lines.append(f"{glyph} {index}. {step.title} [{step.agent_label}]")
            if step.error:
                lines.append(f"   error: {step.error[:300]}")
        if self.verdict:
            lines.append("")
            lines.append("Verdict: " + self.verdict)
        return "\n".join(lines)[:limit]

    # ── serialisation ─────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "steps": [step.to_dict() for step in self.steps],
            "status": self.status,
            "model": self.model,
            "created": self.created,
            "updated": self.updated,
            "context_notes": self.context_notes,
            "verdict": self.verdict,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskPlan":
        return cls(
            task=str(data.get("task", "")),
            steps=[PlanStep.from_dict(item) for item in data.get("steps", [])],
            id=str(data.get("id") or uuid.uuid4().hex[:10]),
            status=str(data.get("status", "draft")),
            model=str(data.get("model", "")),
            created=float(data.get("created", _now())),
            updated=float(data.get("updated", _now())),
            context_notes=str(data.get("context_notes", "")),
            verdict=str(data.get("verdict", "")),
            error=str(data.get("error", "")),
        )

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{self.id}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: Path) -> "TaskPlan | None":
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None


# ══════════════════════════════════════════════════════════════════════
# Parsing model output
# ══════════════════════════════════════════════════════════════════════
def extract_json(text: str) -> dict[str, Any]:
    """Find the first JSON object in a model response, tolerating fences/prose."""
    raw = (text or "").strip()
    if not raw:
        raise PlanError("The planner returned an empty response")
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise PlanError(f"No JSON object found in planner response: {raw[:240]}")
    try:
        payload = json.loads(raw[start : end + 1])
    except ValueError as error:
        raise PlanError(f"Planner JSON is invalid: {error}") from error
    if not isinstance(payload, dict):
        raise PlanError("Planner JSON must be an object")
    return payload


def _normalise_tools(value: Any) -> list[str]:
    if isinstance(value, str):
        items = re.split(r"[,\s]+", value)
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        items = []
    cleaned: list[str] = []
    for item in items:
        name = item.strip().strip("`")
        if name and name not in cleaned:
            cleaned.append(name[:60])
    return cleaned[:16]


def plan_from_model_json(text: str, task: str, *, max_steps: int = 8, model: str = "") -> TaskPlan:
    payload = extract_json(text)
    raw_steps = payload.get("steps") or payload.get("plan") or []
    if isinstance(raw_steps, dict):
        raw_steps = list(raw_steps.values())
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanError("The plan contains no steps")
    steps: list[PlanStep] = []
    for index, item in enumerate(raw_steps[:max_steps], 1):
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("name") or item.get("step") or f"Step {index}"
        goal = item.get("goal") or item.get("detail") or item.get("description") or title
        agent = str(item.get("agent", "code") or "code").strip().lower()
        if agent not in AGENTS:
            agent = {"coding": "code", "browser": "computer", "desktop": "computer", "write": "general"}.get(agent, "code")
        risk = str(item.get("risk", "read") or "read").strip().lower()
        if risk not in RISKS:
            risk = "read"
        steps.append(
            PlanStep(
                id=f"s{index}",
                title=_clean_title(title, f"Step {index}"),
                goal=str(goal)[:2000],
                agent=agent,
                tools=_normalise_tools(item.get("tools") or item.get("tool")),
                risk=risk,
            )
        )
    if not steps:
        raise PlanError("Every step in the plan was unusable")
    return TaskPlan(
        task=task.strip(),
        steps=steps,
        model=model,
        context_notes=str(payload.get("notes", "") or "")[:2000],
        status="draft",
    )


class PlanStore:
    """Persist plans so a long cowork run survives a restart."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, plan: TaskPlan) -> Path:
        plan.touch()
        return plan.save(self.directory)

    def load(self, plan_id: str) -> TaskPlan | None:
        safe = "".join(ch for ch in plan_id if ch.isalnum() or ch in "-_")
        return TaskPlan.load(self.directory / f"{safe}.json")

    def list(self, limit: int = 30) -> list[TaskPlan]:
        plans: list[TaskPlan] = []
        for path in self.directory.glob("*.json"):
            plan = TaskPlan.load(path)
            if plan is not None:
                plans.append(plan)
        plans.sort(key=lambda item: item.updated, reverse=True)
        return plans[:limit]
