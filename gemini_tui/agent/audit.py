"""Append-only audit log for tool calls, agent steps and permission decisions.

One JSON Lines file per day under ~/.config/gemini-tui/logs. Secrets are
redacted before anything is written. The log is a first-class product feature:
it makes autonomous runs reviewable after the fact.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..config import LOGS_DIR

SECRET_KEYS = {
    "api_key", "apikey", "key", "token", "access_token", "refresh_token",
    "password", "passwd", "secret", "authorization", "cookie", "set-cookie",
    "private_key", "client_secret",
}
_SECRET_KEY_SUBSTRINGS = (
    "api_key", "apikey", "secret", "password", "passwd",
    "access_token", "refresh_token", "private_key", "client_secret",
)
SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z\-_]{20,}"),
    re.compile(r"sk-[0-9A-Za-z\-_]{16,}"),
    re.compile(r"gh[opsu]_[0-9A-Za-z_]{20,}"),
    re.compile(r"github_pat_[0-9A-Za-z_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"eyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}"),
)


def redact_text(text: str) -> str:
    value = str(text)
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[redacted]", value)
    return value


def redact_value(value: Any, depth: int = 0) -> Any:
    """Recursively redact obvious secrets without destroying useful structure."""
    if depth > 4:
        return "[depth]"
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in SECRET_KEYS or any(sub in lowered for sub in _SECRET_KEY_SUBSTRINGS):
                cleaned[key] = "[redacted]"
            else:
                cleaned[key] = redact_value(item, depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact_value(item, depth + 1) for item in value[:40]]
    if isinstance(value, str):
        return redact_text(value[:4000])
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value)[:500])


def summarise_args(args: Any, limit: int = 700) -> str:
    try:
        rendered = json.dumps(redact_value(args), ensure_ascii=False, default=str)
    except Exception:
        rendered = str(args)
    rendered = redact_text(rendered)
    if len(rendered) > limit:
        rendered = rendered[: limit - 1] + "…"
    return rendered


@dataclass
class AuditRecord:
    ts: float = field(default_factory=time.time)
    kind: str = "tool"              # tool | agent | permission | system
    session: str = ""
    agent: str = ""
    tool: str = ""
    risk: str = ""
    ok: bool = True
    elapsed: float = 0.0
    args: str = ""
    detail: str = ""

    @property
    def time_label(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%H:%M:%S")

    def to_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class AuditLog:
    """Thread-safe JSONL writer with a small in-memory ring for the UI."""

    def __init__(self, directory: Path | None = None, enabled: bool = True, memory_limit: int = 800) -> None:
        self.directory = directory or LOGS_DIR
        self.enabled = enabled
        self.memory_limit = memory_limit
        self._records: list[AuditRecord] = []
        self._lock = threading.Lock()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # ── writing ───────────────────────────────────────────────────
    def path_for(self, ts: float | None = None) -> Path:
        stamp = datetime.fromtimestamp(ts or time.time()).strftime("%Y%m%d")
        return self.directory / f"audit-{stamp}.jsonl"

    def record(
        self,
        kind: str = "tool",
        *,
        tool: str = "",
        ok: bool = True,
        elapsed: float = 0.0,
        args: Any = None,
        detail: str = "",
        risk: str = "",
        session: str = "",
        agent: str = "",
    ) -> AuditRecord:
        entry = AuditRecord(
            kind=kind,
            session=session,
            agent=agent,
            tool=tool,
            risk=risk,
            ok=ok,
            elapsed=round(float(elapsed), 3),
            args=summarise_args(args) if args is not None else "",
            detail=redact_text(str(detail))[:1200],
        )
        with self._lock:
            self._records.append(entry)
            if len(self._records) > self.memory_limit:
                del self._records[: len(self._records) - self.memory_limit]
            if self.enabled:
                try:
                    with self.path_for(entry.ts).open("a", encoding="utf-8") as handle:
                        handle.write(entry.to_line() + "\n")
                except OSError:
                    pass
        return entry

    # ── reading ───────────────────────────────────────────────────
    def recent(self, limit: int = 40, *, kind: str = "", prefix: str = "") -> list[AuditRecord]:
        with self._lock:
            rows = list(reversed(self._records))
        if kind:
            rows = [row for row in rows if row.kind == kind]
        if prefix:
            rows = [row for row in rows if row.tool.startswith(prefix)]
        return rows[: max(1, limit)]

    def tail_file(self, limit: int = 60, path: Path | None = None) -> list[AuditRecord]:
        target = path or self.path_for()
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        except OSError:
            return []
        records: list[AuditRecord] = []
        for line in lines:
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            records.append(
                AuditRecord(
                    ts=float(payload.get("ts", 0) or 0),
                    kind=str(payload.get("kind", "")),
                    session=str(payload.get("session", "")),
                    agent=str(payload.get("agent", "")),
                    tool=str(payload.get("tool", "")),
                    risk=str(payload.get("risk", "")),
                    ok=bool(payload.get("ok", True)),
                    elapsed=float(payload.get("elapsed", 0) or 0),
                    args=str(payload.get("args", "")),
                    detail=str(payload.get("detail", "")),
                )
            )
        return records

    def render(self, limit: int = 20, *, prefix: str = "") -> str:
        rows = self.recent(limit, prefix=prefix)
        if not rows:
            return "No audit entries yet."
        lines = []
        for row in rows:
            flag = "ok " if row.ok else "ERR"
            lines.append(
                f"{row.time_label} {flag} {row.kind:<9} {row.tool or row.agent:<28} {row.args or row.detail}"[:260]
            )
        return "\n".join(lines)

    def clear_memory(self) -> None:
        with self._lock:
            self._records.clear()

    def iter_records(self) -> Iterable[AuditRecord]:
        with self._lock:
            return list(self._records)
