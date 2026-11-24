"""A small dependency-aware task scheduler.

Pure state: no I/O, no Textual, no model calls. The orchestrator owns execution
and asks this object what may run next. That separation makes dependency cycles,
retries and cancellation testable without a network or a terminal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_FAILED = "failed"
TASK_SKIPPED = "skipped"
TASK_CANCELLED = "cancelled"

TERMINAL_STATES = {TASK_DONE, TASK_FAILED, TASK_SKIPPED, TASK_CANCELLED}
SUCCESS_STATES = {TASK_DONE}
FAILURE_STATES = {TASK_FAILED, TASK_SKIPPED, TASK_CANCELLED}


class ScheduleError(ValueError):
    """The task graph is invalid (duplicate id, unknown dependency, cycle)."""


@dataclass
class ScheduledTask:
    id: str
    title: str
    role: str = "developer"
    goal: str = ""
    depends_on: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    acceptance: str = ""
    status: str = TASK_PENDING
    attempts: int = 0
    max_attempts: int = 0
    timeout_s: float = 0.0
    agent_id: str = ""
    model: str = ""
    result: str = ""
    error: str = ""
    artifacts: list[str] = field(default_factory=list)
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return max(0.0, (self.ended_at or time.time()) - self.started_at)

    @property
    def retryable(self) -> bool:
        return self.attempts < max(1, self.max_attempts)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "role": self.role,
            "goal": self.goal,
            "depends_on": list(self.depends_on),
            "scope": list(self.scope),
            "acceptance": self.acceptance,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "timeout_s": self.timeout_s,
            "agent_id": self.agent_id,
            "model": self.model,
            "result": self.result[:4000],
            "error": self.error,
            "artifacts": list(self.artifacts),
            "elapsed": round(self.elapsed, 3),
        }


class TaskScheduler:
    def __init__(
        self,
        tasks: Iterable[ScheduledTask] = (),
        *,
        max_parallel: int = 2,
        default_timeout: float = 300.0,
        default_attempts: int = 2,
    ) -> None:
        self.max_parallel = max(1, int(max_parallel))
        self.default_timeout = max(1.0, float(default_timeout))
        self.default_attempts = max(1, int(default_attempts))
        self._tasks: dict[str, ScheduledTask] = {}
        for task in tasks:
            self.add(task)
        self.validate()

    # ── graph ─────────────────────────────────────────────────────
    def add(self, task: ScheduledTask) -> ScheduledTask:
        if not task.id:
            raise ScheduleError("every task needs an id")
        if task.id in self._tasks:
            raise ScheduleError(f"duplicate task id: {task.id}")
        if not task.timeout_s:
            task.timeout_s = self.default_timeout
        if not task.max_attempts:
            task.max_attempts = self.default_attempts
        self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    def all(self) -> list[ScheduledTask]:
        return list(self._tasks.values())

    def __len__(self) -> int:
        return len(self._tasks)

    def validate(self) -> None:
        ids = set(self._tasks)
        for task in self._tasks.values():
            if task.id in task.depends_on:
                raise ScheduleError(f"{task.id} depends on itself")
            missing = [dep for dep in task.depends_on if dep not in ids]
            if missing:
                raise ScheduleError(f"{task.id} depends on unknown task(s): {', '.join(missing)}")
        # Kahn's algorithm; any leftovers are a cycle.
        remaining = {task.id: set(task.depends_on) for task in self._tasks.values()}
        resolved: set[str] = set()
        while True:
            ready = {task_id for task_id, deps in remaining.items() if deps <= resolved}
            if not ready:
                break
            for task_id in ready:
                resolved.add(task_id)
                remaining.pop(task_id, None)
        if remaining:
            raise ScheduleError("dependency cycle: " + ", ".join(sorted(remaining)))

    def topological_layers(self) -> list[list[str]]:
        """Independent tasks grouped by depth; useful for tests and the UI."""
        depths: dict[str, int] = {}

        def depth(task_id: str, seen: set[str] | None = None) -> int:
            if task_id in depths:
                return depths[task_id]
            seen = set(seen or ())
            if task_id in seen:
                raise ScheduleError("dependency cycle")
            seen.add(task_id)
            task = self._tasks[task_id]
            value = 0 if not task.depends_on else 1 + max(depth(dep, seen) for dep in task.depends_on)
            depths[task_id] = value
            return value

        for task_id in self._tasks:
            depth(task_id)
        layers: dict[int, list[str]] = {}
        for task_id, level in depths.items():
            layers.setdefault(level, []).append(task_id)
        return [sorted(layers[level]) for level in sorted(layers)]

    # ── status ────────────────────────────────────────────────────
    def ready(self) -> list[ScheduledTask]:
        ready = []
        for task in self._tasks.values():
            if task.status != TASK_PENDING:
                continue
            if all(self._tasks[dep].status == TASK_DONE for dep in task.depends_on):
                ready.append(task)
        return sorted(ready, key=lambda task: (len(task.depends_on), task.id))

    def running(self) -> list[ScheduledTask]:
        return [task for task in self._tasks.values() if task.status == TASK_RUNNING]

    @property
    def running_count(self) -> int:
        return len(self.running())

    @property
    def slots(self) -> int:
        return max(0, self.max_parallel - self.running_count)

    def mark_running(self, task_id: str, agent_id: str = "", model: str = "") -> ScheduledTask:
        task = self._require(task_id)
        if task.status not in (TASK_PENDING, TASK_RUNNING):
            raise ScheduleError(f"{task_id} cannot start from status {task.status}")
        task.status = TASK_RUNNING
        task.attempts += 1
        task.agent_id = agent_id or task.agent_id
        task.model = model or task.model
        task.started_at = task.started_at or time.time()
        task.ended_at = 0.0
        task.error = ""
        return task

    def mark_done(self, task_id: str, result: str = "", artifacts: list[str] | None = None) -> None:
        task = self._require(task_id)
        task.status = TASK_DONE
        task.result = result
        task.error = ""
        task.ended_at = time.time()
        if artifacts:
            for item in artifacts:
                if item and item not in task.artifacts:
                    task.artifacts.append(item)

    def mark_failed(self, task_id: str, error: str) -> str:
        """Record a failure; return `retry` or the final status."""
        task = self._require(task_id)
        task.error = str(error)[:1000]
        task.ended_at = time.time()
        if task.retryable:
            task.status = TASK_PENDING
            return "retry"
        task.status = TASK_FAILED
        self.skip_dependents(task_id, reason=f"dependency {task_id} failed")
        return TASK_FAILED

    def fail_terminal(self, task_id: str, error: str) -> None:
        """Fail without retrying (configuration or ownership errors)."""
        task = self._require(task_id)
        task.error = str(error)[:1000]
        task.status = TASK_FAILED
        task.ended_at = time.time()
        self.skip_dependents(task_id, reason=f"dependency {task_id} failed")

    def mark_skipped(self, task_id: str, reason: str = "") -> None:
        task = self._require(task_id)
        task.status = TASK_SKIPPED
        task.error = reason or task.error
        task.ended_at = time.time()

    def skip_dependents(self, task_id: str, reason: str = "") -> None:
        relevant = {task_id}
        changed = True
        while changed:
            changed = False
            for task in self._tasks.values():
                if task.status == TASK_PENDING and relevant & set(task.depends_on):
                    task.status = TASK_SKIPPED
                    task.error = reason or f"dependency {task_id} failed"
                    task.ended_at = time.time()
                    relevant.add(task.id)
                    changed = True

    def cancel_pending(self, reason: str = "cancelled") -> None:
        for task in self._tasks.values():
            if task.status in (TASK_PENDING, TASK_RUNNING):
                task.status = TASK_CANCELLED
                task.error = task.error or reason
                task.ended_at = time.time()

    # ── aggregates ────────────────────────────────────────────────
    @property
    def total(self) -> int:
        return len(self._tasks)

    @property
    def done_count(self) -> int:
        return sum(1 for task in self._tasks.values() if task.status == TASK_DONE)

    @property
    def failed_count(self) -> int:
        return sum(1 for task in self._tasks.values() if task.status == TASK_FAILED)

    @property
    def skipped_count(self) -> int:
        return sum(1 for task in self._tasks.values() if task.status == TASK_SKIPPED)

    @property
    def progress(self) -> float:
        if not self._tasks:
            return 0.0
        terminal = sum(1 for task in self._tasks.values() if task.status in TERMINAL_STATES)
        return terminal / len(self._tasks)

    def complete(self) -> bool:
        return all(task.status in TERMINAL_STATES for task in self._tasks.values())

    def deadlocked(self) -> bool:
        return (
            not self.complete()
            and not self.ready()
            and not self.running()
        )

    def failed(self, task_id: str) -> ScheduledTask:
        task = self._require(task_id)
        if task.status not in SUCCESS_STATES:
            raise ScheduleError(f"{task_id} is not done (status {task.status})")
        return task

    def _require(self, task_id: str) -> ScheduledTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise ScheduleError(f"unknown task: {task_id}")
        return task

    # ── rendering ─────────────────────────────────────────────────
    def summary_text(self) -> str:
        failed = self.failed_count + self.skipped_count
        return (
            f"{self.done_count}/{self.total} done · {self.running_count} running · "
            f"{failed} failed/skipped · {self.progress * 100:.0f}%"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "max_parallel": self.max_parallel,
            "progress": round(self.progress, 3),
            "tasks": [task.to_dict() for task in self.all()],
        }
