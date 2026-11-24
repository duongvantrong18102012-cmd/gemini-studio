"""Layered memory: global, project and session notes with lexical retrieval.

The old implementation was a flat JSON dictionary. This store adds scopes,
tags, hit counts and a scored search, while still reading the legacy file the
first time so existing notes are not lost.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..config import CACHE_DIR, CONFIG_DIR
from ..tools.registry import ToolContext, ToolError, ToolRegistry, prop, schema

SCOPES = ("global", "project", "session")
TOKEN_RE = re.compile(r"[\w\-À-ỹ]+", re.UNICODE)
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "when", "what",
    "how", "why", "use", "using", "file", "files", "code", "task", "step", "theo",
    "cua", "cho", "voi", "nhung", "mot", "cac", "trong", "khi", "lam",
}


def _now() -> float:
    return time.time()


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "") if len(token) > 1]


def _content_tokens(text: str) -> set[str]:
    return {token for token in tokenize(text) if token not in STOPWORDS}


@dataclass
class MemoryEntry:
    key: str
    value: str
    scope: str = "global"
    tags: list[str] = field(default_factory=list)
    workspace: str = ""
    created: float = field(default_factory=_now)
    updated: float = field(default_factory=_now)
    hits: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, key: str, data: Any) -> "MemoryEntry":
        if not isinstance(data, dict):
            return cls(key=key, value=str(data))
        scope = str(data.get("scope", "global") or "global").lower()
        if scope not in SCOPES:
            scope = "global"
        return cls(
            key=str(data.get("key") or key),
            value=str(data.get("value", "") or ""),
            scope=scope,
            tags=[str(tag)[:40] for tag in data.get("tags", []) or []][:12],
            workspace=str(data.get("workspace", "") or ""),
            created=float(data.get("created", data.get("updated", _now())) or _now()),
            updated=float(data.get("updated", _now()) or _now()),
            hits=int(data.get("hits", 0) or 0),
        )

    @property
    def age_days(self) -> float:
        return max(0.0, (_now() - self.updated) / 86400)

    def summary(self, limit: int = 220) -> str:
        value = " ".join(self.value.split())
        if len(value) > limit:
            value = value[: limit - 1] + "…"
        return value


class MemoryStore:
    """JSON-backed memory shared by chat, cowork workers and the TUI."""

    def __init__(
        self,
        workspace: str = "",
        path: Path | None = None,
        legacy_path: Path | None = None,
    ) -> None:
        self.workspace = str(Path(workspace).expanduser()) if workspace else ""
        self.path = path or (CONFIG_DIR / "memory.json")
        self.legacy_path = legacy_path or (CACHE_DIR / "memory.json")
        self._lock = threading.Lock()
        self._entries: dict[str, MemoryEntry] = {}
        self.load()

    # ── persistence ───────────────────────────────────────────────
    def load(self) -> None:
        with self._lock:
            payload: dict[str, Any] = {}
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            raw_entries = payload.get("entries") if isinstance(payload, dict) else None
            if not isinstance(raw_entries, dict):
                # legacy flat dictionary: {"key": {"value": ..., "updated": ...}}
                raw_entries = payload if isinstance(payload, dict) else {}
            entries: dict[str, MemoryEntry] = {}
            for key, value in raw_entries.items():
                if not isinstance(key, str):
                    continue
                entries[key] = MemoryEntry.from_dict(key, value)
            if not entries and self.legacy_path.exists():
                try:
                    legacy = json.loads(self.legacy_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    legacy = {}
                if isinstance(legacy, dict):
                    for key, value in legacy.items():
                        if isinstance(value, dict) and "value" in value:
                            entries[str(key)] = MemoryEntry(
                                key=str(key),
                                value=str(value.get("value", "")),
                                updated=float(value.get("updated", _now()) or _now()),
                            )
            self._entries = entries

    def _flush(self) -> None:
        payload = {
            "version": 2,
            "entries": {key: entry.to_dict() for key, entry in self._entries.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".memory-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(json.dumps(payload, indent=1, ensure_ascii=False))
                tmp_path = Path(handle.name)
            tmp_path.replace(self.path)
        except OSError:
            pass

    # ── writes ────────────────────────────────────────────────────
    def save(
        self,
        key: str,
        value: str,
        scope: str = "global",
        tags: Iterable[str] | None = None,
        workspace: str | None = None,
    ) -> MemoryEntry:
        name = " ".join(str(key or "").split())[:120]
        if not name:
            raise ValueError("memory key is required")
        scope = (scope or "global").lower()
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        body = str(value or "").strip()
        if not body:
            raise ValueError("memory value is empty")
        with self._lock:
            existing = self._entries.get(name)
            entry = MemoryEntry(
                key=name,
                value=body[:20000],
                scope=scope,
                tags=[str(tag).strip()[:40] for tag in (tags or []) if str(tag).strip()][:12],
                workspace=(workspace if workspace is not None else self.workspace) if scope == "project" else "",
                created=existing.created if existing else _now(),
                updated=_now(),
                hits=existing.hits if existing else 0,
            )
            self._entries[name] = entry
            self._flush()
        return entry

    def forget(self, key: str) -> bool:
        with self._lock:
            removed = self._entries.pop(str(key), None)
            if removed is not None:
                self._flush()
            return removed is not None

    def get(self, key: str) -> MemoryEntry | None:
        with self._lock:
            return self._entries.get(str(key))

    # ── reads ─────────────────────────────────────────────────────
    def entries(self, scope: str = "", notice_limit: int = 500) -> list[MemoryEntry]:
        with self._lock:
            rows = list(self._entries.values())
        if scope:
            rows = [entry for entry in rows if entry.scope == scope]
        rows.sort(key=lambda entry: entry.updated, reverse=True)
        return rows[:notice_limit]

    def _score(self, query: str, entry: MemoryEntry) -> float:
        needle = query.strip().lower()
        if not needle:
            return 0.0
        query_tokens = _content_tokens(query)
        haystack = f"{entry.key} {entry.value} {' '.join(entry.tags)}".lower()
        if not query_tokens:
            return 12.0 if needle in haystack else 0.0
        hay_tokens = _content_tokens(haystack)
        overlap = len(query_tokens & hay_tokens)
        score = overlap * 18.0
        if needle in entry.key.lower():
            score += 45
        if needle in haystack:
            score += 12
        if entry.key.lower() == needle:
            score += 60
        if score <= 0:
            return 0.0
        if entry.scope == "project" and self.workspace and entry.workspace == self.workspace:
            score += 10
        if entry.scope == "global":
            score += 3
        score += max(0.0, 8.0 - entry.age_days * 0.25)
        score += min(5.0, entry.hits * 0.2)
        return score

    def search(self, query: str, limit: int = 8, scope: str = "") -> list[MemoryEntry]:
        with self._lock:
            rows = list(self._entries.values())
        if scope:
            rows = [entry for entry in rows if entry.scope == scope]
        scored = [(self._score(query, entry), entry) for entry in rows]
        scored = [(score, entry) for score, entry in scored if score > 0]
        scored.sort(key=lambda pair: (-pair[0], -pair[1].updated))
        hits = [entry for _, entry in scored[: max(1, limit)]]
        if hits:
            with self._lock:
                for entry in hits:
                    entry.hits += 1
                self._flush()
        return hits

    def digest(self, query: str = "", limit: int = 6, max_chars: int = 1600) -> str:
        rows = self.search(query, limit=limit) if query.strip() else self.entries(notice_limit=limit)
        if not rows:
            return ""
        lines: list[str] = []
        used = 0
        for entry in rows:
            tag = f" [{', '.join(entry.tags)}]" if entry.tags else ""
            line = f"- ({entry.scope}) {entry.key}{tag}: {entry.summary(200)}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# Tools
# ══════════════════════════════════════════════════════════════════════
def register_memory_tools(registry: ToolRegistry, store: MemoryStore) -> None:
    """Register the memory tools once. Memory is local agent state, not a
    workspace write, so these tools are auto-allowed; every call is audited."""

    async def memory_save(args: dict[str, Any], ctx: ToolContext) -> str:
        key = str(args.get("key") or "").strip()
        value = str(args.get("value") or "").strip()
        scope = str(args.get("scope") or "global").strip().lower()
        tags = args.get("tags") or []
        if isinstance(tags, str):
            tags = [item.strip() for item in tags.split(",") if item.strip()]
        if not key:
            raise ToolError("key is required")
        if not value:
            raise ToolError("value is required")
        try:
            entry = store.save(key, value, scope=scope, tags=tags, workspace=ctx.workspace)
        except ValueError as error:
            raise ToolError(str(error)) from error
        return f"Remembered {entry.key!r} in {entry.scope} scope ({len(entry.value)} chars)"

    async def memory_search(args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query is required")
        limit = max(1, min(30, int(args.get("limit") or 6)))
        rows = store.search(query, limit=limit, scope=str(args.get("scope") or ""))
        if not rows:
            return "No matching memory."
        return "\n\n".join(
            f"[{entry.scope}] {entry.key}\n{entry.value[:1200]}" for entry in rows
        )

    async def memory_list(args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or "").strip().lower()
        rows = store.entries(scope=str(args.get("scope") or ""))
        if query:
            rows = [entry for entry in rows if query in entry.key.lower() or query in entry.value.lower()]
        if not rows:
            return "No remembered notes yet."
        return "\n".join(
            f"{entry.key:<32} [{entry.scope:<7}] {entry.summary(120)}" for entry in rows[:60]
        )

    async def memory_forget(args: dict[str, Any], ctx: ToolContext) -> str:
        key = str(args.get("key") or "").strip()
        if not key:
            raise ToolError("key is required")
        return f"Forgetting {key!r}" if store.forget(key) else f"No memory named {key!r}"

    specs = (
        (
            "memory_save",
            "Persist a note across sessions. scope=global for machine-wide facts, project for this "
            "workspace, session for the current job. Tags improve retrieval.",
            schema(
                {
                    "key": prop("string", "Short identifier"),
                    "value": prop("string", "What to remember"),
                    "scope": prop("string", "global | project | session", enum=list(SCOPES)),
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags"},
                },
                ["key", "value"],
            ),
            memory_save,
        ),
        (
            "memory_search",
            "Search remembered notes by meaning-ish lexical relevance (key, body, tags).",
            schema(
                {
                    "query": prop("string", "What to recall"),
                    "limit": prop("integer", "Max results", minimum=1, maximum=30),
                    "scope": prop("string", "Filter by scope", enum=list(SCOPES)),
                },
                ["query"],
            ),
            memory_search,
        ),
        (
            "memory_list",
            "List remembered notes, newest first, optionally filtered by substring.",
            schema(
                {
                    "query": prop("string", "Substring filter"),
                    "scope": prop("string", "Filter by scope", enum=list(SCOPES)),
                },
                [],
            ),
            memory_list,
        ),
        (
            "memory_forget",
            "Delete a remembered note permanently.",
            schema({"key": prop("string", "Exact key")}, ["key"]),
            memory_forget,
        ),
    )
    for name, description, parameters, handler in specs:
        registry.add(
            _make_spec(name, description, parameters, handler)
        )


def _make_spec(name: str, description: str, parameters: dict[str, Any], handler: Any):
    from ..tools.registry import ToolSpec

    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        dangerous=False,
        category="memory",
        icon="◉",
        source="agent",
        risk="read",
    )
