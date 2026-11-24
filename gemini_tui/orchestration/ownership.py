"""File ownership and change tracking for parallel agents.

Two agents editing the same file at the same time is the classic way a
multi-agent run corrupts work. This object is a small, synchronous lease table:
an agent claims the paths it intends to write, the tool layer checks the claim
before writing, and the orchestrator releases claims when the task ends.

It is intentionally not a sandbox. It catches accidental overwrite between
cooperating agents; `PermissionPolicy` and the human approval gate remain the
security boundary.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class OwnershipConflict(RuntimeError):
    """Another live agent already owns this path."""


def normalize_path(path: str | os.PathLike[str]) -> str:
    """Stable key for a path, regardless of separators or case on Windows."""
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        resolved = Path(raw).expanduser()
        if not resolved.is_absolute():
            resolved = Path.cwd() / resolved
        value = resolved.resolve(strict=False).as_posix()
    except (OSError, ValueError):
        value = raw.replace("\\", "/")
    return value.lower() if os.name == "nt" else value


@dataclass(frozen=True)
class ChangeRecord:
    path: str
    agent: str
    task: str
    detail: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def time_label(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))


class FileOwnership:
    def __init__(self) -> None:
        self._owners: dict[str, str] = {}
        self._tasks: dict[str, str] = {}
        self._changes: list[ChangeRecord] = []
        self._checkpoints: list[dict[str, Any]] = []

    # ── leases ────────────────────────────────────────────────────
    def owner(self, path: str | os.PathLike[str]) -> str:
        return self._owners.get(normalize_path(path), "")

    def claim(self, path: str | os.PathLike[str], agent: str, task: str = "") -> str:
        """Claim a path for `agent`; returns the normalized key.

        Raises `OwnershipConflict` if a different agent holds an active lease.
        """
        key = normalize_path(path)
        if not key:
            raise ValueError("cannot claim an empty path")
        current = self._owners.get(key, "")
        if current and current != agent:
            raise OwnershipConflict(
                f"{path} is owned by agent {current}; finish or release it before {agent} writes"
            )
        self._owners[key] = agent
        self._tasks[key] = task
        return key

    def check_write(self, path: str | os.PathLike[str], agent: str, task: str = "") -> None:
        """Validate a write, auto-claiming the path when it is free."""
        self.claim(path, agent, task)

    def release(self, path: str | os.PathLike[str], agent: str = "") -> None:
        key = normalize_path(path)
        current = self._owners.get(key)
        if current and (not agent or current == agent):
            self._owners.pop(key, None)
            self._tasks.pop(key, None)

    def release_agent(self, agent: str) -> int:
        keys = [key for key, owner in self._owners.items() if owner == agent]
        for key in keys:
            self._owners.pop(key, None)
            self._tasks.pop(key, None)
        return len(keys)

    def leases(self) -> dict[str, dict[str, str]]:
        return {
            key: {"agent": owner, "task": self._tasks.get(key, "")}
            for key, owner in sorted(self._owners.items())
        }

    def clear_leases(self) -> None:
        self._owners.clear()
        self._tasks.clear()

    def clear(self) -> None:
        self._owners.clear()
        self._tasks.clear()
        self._changes.clear()
        self._checkpoints.clear()

    # ── change log ────────────────────────────────────────────────
    def record(self, path: str, agent: str, task: str = "", detail: str = "") -> ChangeRecord:
        record = ChangeRecord(
            path=normalize_path(path), agent=agent or "?", task=task or "", detail=detail[:500]
        )
        self._changes.append(record)
        if len(self._changes) > 2000:
            self._changes = self._changes[-2000:]
        return record

    def changes(self, limit: int = 40, task: str = "") -> list[ChangeRecord]:
        rows = self._changes if not task else [row for row in self._changes if row.task == task]
        return list(reversed(rows[-max(1, limit) :]))

    # ── checkpoints ───────────────────────────────────────────────
    def checkpoint(self, label: str, files: list[str] | None = None) -> dict[str, Any]:
        """Record a logical checkpoint (path list + timestamp).

        This is metadata, not a VCS commit; it lets the report say which files
        were in scope at each verification point.
        """
        payload = {
            "label": label,
            "files": sorted({normalize_path(path) for path in (files or []) if path}),
            "time": time.time(),
        }
        self._checkpoints.append(payload)
        return payload

    def checkpoints(self) -> list[dict[str, Any]]:
        return list(self._checkpoints)

    def summary(self) -> str:
        return (
            f"{len(self._owners)} active lease(s) · {len(self._changes)} change(s) · "
            f"{len(self._checkpoints)} checkpoint(s)"
        )
