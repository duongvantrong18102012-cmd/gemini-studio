"""Chat transcripts, token accounting and on-disk session storage."""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import SESSIONS_DIR


def _now() -> float:
    return time.time()


@dataclass
class Usage:
    prompt: int = 0
    output: int = 0
    thoughts: int = 0
    total: int = 0
    cached: int = 0
    calls: int = 0
    latency: float = 0.0

    def add(self, meta: Any) -> None:
        if meta is None:
            return
        self.prompt += int(getattr(meta, "prompt_token_count", 0) or 0)
        self.output += int(getattr(meta, "candidates_token_count", 0) or 0)
        self.thoughts += int(getattr(meta, "thoughts_token_count", 0) or 0)
        self.total += int(getattr(meta, "total_token_count", 0) or 0)
        self.cached += int(getattr(meta, "cached_content_token_count", 0) or 0)
        self.calls += 1

    @property
    def output_with_thoughts(self) -> int:
        return self.output + self.thoughts

    def merge(self, data: dict[str, Any] | None, calls: int = 1) -> None:
        data = data or {}
        self.prompt += int(data.get("prompt") or 0)
        self.output += int(data.get("output") or 0)
        self.thoughts += int(data.get("thoughts") or 0)
        self.total += int(data.get("total") or 0)
        self.cached += int(data.get("cached") or 0)
        self.calls += calls

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Usage":
        usage = cls()
        for key, value in (data or {}).items():
            if hasattr(usage, key) and isinstance(value, (int, float)):
                setattr(usage, key, value)
        return usage


@dataclass
class Message:
    role: str                      # user | assistant | notice | tool
    text: str = ""
    thoughts: str = ""
    ts: float = field(default_factory=_now)
    usage: Usage = field(default_factory=Usage)
    tools: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["usage"] = self.usage.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            role=str(data.get("role", "assistant")),
            text=str(data.get("text", "")),
            thoughts=str(data.get("thoughts", "")),
            ts=float(data.get("ts", _now())),
            usage=Usage.from_dict(data.get("usage")),
            tools=list(data.get("tools", [])),
            meta=dict(data.get("meta", {})),
        )


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = "New session"
    model: str = ""
    workspace: str = ""
    created: float = field(default_factory=_now)
    updated: float = field(default_factory=_now)
    messages: list[Message] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    contents: list[Any] = field(default_factory=list)   # google.genai.types.Content
    pinned: bool = False

    # ── display helpers ───────────────────────────────────────────
    @property
    def turns(self) -> int:
        return sum(1 for message in self.messages if message.role == "user")

    @property
    def preview(self) -> str:
        for message in self.messages:
            if message.role == "user" and message.text.strip():
                return " ".join(message.text.split())[:72]
        return "Empty session"

    @property
    def age_label(self) -> str:
        delta = max(0.0, _now() - self.updated)
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{delta / 60:.0f}m ago"
        if delta < 86400:
            return f"{delta / 3600:.1f}h ago"
        return time.strftime("%d/%m", time.localtime(self.updated))

    def touch(self) -> None:
        self.updated = _now()

    def auto_title(self) -> None:
        if self.title and self.title != "New session":
            return
        for message in self.messages:
            if message.role == "user" and message.text.strip():
                cleaned = " ".join(message.text.split())
                self.title = cleaned[:60] + ("…" if len(cleaned) > 60 else "")
                return

    # ── serialisation ─────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        contents: list[Any] = []
        for content in self.contents:
            try:
                contents.append(content.model_dump(mode="json", exclude_none=True))
            except AttributeError:
                contents.append(content)
            except Exception:
                continue
        return {
            "id": self.id,
            "title": self.title,
            "model": self.model,
            "workspace": self.workspace,
            "created": self.created,
            "updated": self.updated,
            "messages": [message.to_dict() for message in self.messages],
            "usage": self.usage.to_dict(),
            "contents": contents,
            "pinned": self.pinned,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        if not isinstance(data, dict):
            raise ValueError("session payload must be a JSON object")
        contents: list[Any] = []
        raw_contents = data.get("contents") or []
        if isinstance(raw_contents, list) and raw_contents:
            try:
                from google.genai import types

                for item in raw_contents:
                    try:
                        contents.append(types.Content.model_validate(item))
                    except Exception:
                        continue
            except Exception:
                contents = []
        raw_messages = data.get("messages")
        messages = [
            Message.from_dict(item)
            for item in (raw_messages if isinstance(raw_messages, list) else [])
            if isinstance(item, dict)
        ]
        try:
            created = float(data.get("created", _now()))
        except (TypeError, ValueError):
            created = _now()
        try:
            updated = float(data.get("updated", _now()))
        except (TypeError, ValueError):
            updated = _now()
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            title=str(data.get("title", "Session")),
            model=str(data.get("model", "")),
            workspace=str(data.get("workspace", "")),
            created=created,
            updated=updated,
            messages=messages,
            usage=Usage.from_dict(data.get("usage") if isinstance(data.get("usage"), dict) else None),
            contents=contents,
            pinned=bool(data.get("pinned")),
        )


class SessionStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or SESSIONS_DIR
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, session_id: str) -> Path:
        safe = "".join(ch for ch in str(session_id or "") if ch.isalnum() or ch in "-_")
        if not safe:
            raise ValueError(f"Invalid session id: {session_id!r}")
        return self.directory / f"{safe}.json"

    def save(self, session: Session) -> None:
        session.touch()
        session.auto_title()
        try:
            target = self.path(session.id)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=".session-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(json.dumps(session.to_dict(), ensure_ascii=False, indent=1))
                tmp_path = Path(handle.name)
            tmp_path.replace(target)
        except (OSError, ValueError):
            pass

    def load(self, session_id: str) -> Session | None:
        try:
            payload = json.loads(self.path(session_id).read_text(encoding="utf-8"))
            return Session.from_dict(payload)
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def delete(self, session_id: str) -> bool:
        try:
            self.path(session_id).unlink()
            return True
        except (OSError, ValueError):
            return False

    def list(self, limit: int = 100) -> list[Session]:
        sessions: list[Session] = []
        for file in self.directory.glob("*.json"):
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
                sessions.append(Session.from_dict(payload))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        sessions.sort(key=lambda item: (item.pinned, item.updated), reverse=True)
        return sessions[:limit]
