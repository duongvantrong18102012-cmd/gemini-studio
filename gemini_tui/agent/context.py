"""Context management: token budgets, history trimming, retrieval.

Design goals:
- keep the conversation valid (never start history with a function response),
- spend tokens on the current task, not the whole repository,
- retrieve code on demand through an index instead of dumping files,
- degrade honestly from embeddings to lexical search when embeddings are
  unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from ..config import CACHE_DIR


def estimate_tokens(text: str) -> int:
    """Rough tokenizer: ~4 characters per token for prose, denser for code.

    Deliberately conservative; it is used for budgeting, not for billing.
    """
    value = text or ""
    if not value:
        return 0
    return max(1, math.ceil(len(value) / 3.6))


def _part_text(part: Any) -> str:
    text = getattr(part, "text", None)
    if text:
        return str(text)
    call = getattr(part, "function_call", None)
    if call is not None:
        return f"{getattr(call, 'name', '')} {getattr(call, 'args', '')}"
    response = getattr(part, "function_response", None)
    if response is not None:
        return f"{getattr(response, 'name', '')} {getattr(response, 'response', '')}"
    inline = getattr(part, "inline_data", None)
    if inline is not None:
        # A vision image is roughly 260 tokens at 1024px; use a fixed estimate.
        return " " * 1000
    return ""


def content_tokens(content: Any) -> int:
    total = 0
    for part in getattr(content, "parts", None) or []:
        total += estimate_tokens(_part_text(part))
    return total + 4


def contents_tokens(contents: Iterable[Any]) -> int:
    return sum(content_tokens(item) for item in contents)


def has_function_response(content: Any) -> bool:
    return any(getattr(part, "function_response", None) for part in getattr(content, "parts", None) or [])


def _content_summary(content: Any, limit: int = 240) -> str:
    for part in getattr(content, "parts", None) or []:
        text = getattr(part, "text", None)
        if text:
            cleaned = " ".join(str(text).split())
            return cleaned[:limit]
    return ""


@dataclass
class TrimReport:
    kept: int
    dropped: int
    before: int
    after: int
    note: str = ""


class ContextManager:
    """Budget a Gemini content list while preserving tool call/response pairs."""

    def __init__(self, budget_tokens: int = 120_000, reserve_tokens: int = 8_000) -> None:
        self.budget = max(2_000, int(budget_tokens))
        self.reserve = max(500, int(reserve_tokens))

    def usage(self, contents: Iterable[Any], system_tokens: int = 0) -> dict[str, int]:
        used = contents_tokens(contents)
        return {
            "tokens": used + system_tokens,
            "budget": self.budget,
            "remaining": max(0, self.budget - used - system_tokens),
            "percent": min(100, int(100 * (used + system_tokens) / max(1, self.budget))),
        }

    def trim(self, contents: list[Any], *, keep_last: int = 8) -> tuple[list[Any], TrimReport]:
        """Return (contents, report). The first retained item never starts with a
        function response, so the conversation remains valid for the API."""
        before = contents_tokens(contents)
        limit = self.budget - self.reserve
        if before <= limit:
            return contents, TrimReport(len(contents), 0, before, before)

        start = max(0, len(contents) - keep_last)
        while start < len(contents) and has_function_response(contents[start]):
            start += 1
        # If the tail consists only of function responses, step backward to find
        # a valid non-function-response turn boundary rather than leaving a
        # function_response at index 0.
        if start >= len(contents):
            start = max(0, len(contents) - keep_last)
            while start > 0 and has_function_response(contents[start]):
                start -= 1
            if start < len(contents) and has_function_response(contents[start]):
                return [], TrimReport(0, len(contents), before, 0)

        dropped_items = list(contents[:start])
        kept = list(contents[start:])
        while kept and contents_tokens(kept) > limit and len(kept) > 1:
            dropped_items.append(kept.pop(0))
            while kept and has_function_response(kept[0]):
                dropped_items.append(kept.pop(0))

        summary_bits = [bit for bit in (_content_summary(item) for item in dropped_items) if bit]
        note = ""
        if summary_bits:
            note = "Earlier context condensed: " + " | ".join(summary_bits[-8:])[:900]
        after = contents_tokens(kept)
        return kept, TrimReport(len(kept), len(dropped_items), before, after, note)


# ══════════════════════════════════════════════════════════════════════
# Project brief
# ══════════════════════════════════════════════════════════════════════
BRIEF_NAMES = ("GEMINI.md", "AGENTS.md", "CLAUDE.md", ".cursorrules", "README.md")


def load_project_brief(workspace: str, max_chars: int = 5000) -> str:
    root = Path(workspace)
    for name in BRIEF_NAMES:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > max_chars:
            text = text[:max_chars] + "\n… brief truncated"
        return f"Project brief ({name}):\n{text}"
    return ""


# ══════════════════════════════════════════════════════════════════════
# Retrieval
# ══════════════════════════════════════════════════════════════════════
@dataclass
class RetrievalHit:
    path: str
    line: int
    snippet: str
    score: float
    kind: str = "text"

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"[{location}] {self.snippet.strip()[:600]}"


@dataclass
class RetrievalResult:
    mode: str
    block: str
    hits: list[RetrievalHit] = field(default_factory=list)
    tokens: int = 0
    note: str = ""

    @property
    def empty(self) -> bool:
        return not self.hits


class LazyIndex:
    """Expose a CodeStore through the tiny interface Retriever needs.

    The index is only built the first time a query needs it, which keeps app
    startup instant even in large workspaces.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    @property
    def files(self) -> dict[str, Any]:
        index = getattr(self.store, "index", None)
        return getattr(index, "files", {}) or {}

    def search(self, query: str, limit: int = 20, *, content: bool = True) -> list[Any]:
        index = self.store.ensure(False)
        return index.search(query, limit, content=content)

    def summary(self, limit: int = 1600) -> str:
        index = getattr(self.store, "index", None)
        if index is None:
            return "Workspace index is not built yet (call code_index)."
        return index.summary(limit)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class Retriever:
    """Hybrid code retrieval: embeddings when available, lexical always."""

    def __init__(
        self,
        index: Any,
        workspace: str,
        *,
        embed: Callable[[list[str]], Awaitable[list[list[float]]]] | None = None,
        embedding_model: str = "gemini-embedding-002",
        cache_path: Path | None = None,
        max_files: int = 400,
    ) -> None:
        self.index = index
        self.workspace = str(Path(workspace))
        self.embed = embed
        self.embedding_model = embedding_model
        self.cache_path = cache_path or (CACHE_DIR / "embedding-index.json")
        self.max_files = max_files
        self._vectors: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # ── embedding cache ───────────────────────────────────────────
    def _load_cache(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if payload.get("root") != self.workspace or payload.get("model") != self.embedding_model:
            return
        entries = payload.get("entries")
        if isinstance(entries, dict):
            self._vectors = entries

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(
                    {
                        "root": self.workspace,
                        "model": self.embedding_model,
                        "entries": self._vectors,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    @staticmethod
    def _file_digest(path: Path) -> str:
        try:
            stat = path.stat()
            raw = f"{stat.st_mtime_ns}:{stat.st_size}".encode()
        except OSError:
            return ""
        return hashlib.sha1(raw).hexdigest()

    def _file_text(self, relative: str, limit: int = 1400) -> str:
        path = Path(self.workspace) / relative
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:
            return ""

    async def _semantic_hits(self, query: str, limit: int) -> list[RetrievalHit] | None:
        if self.embed is None:
            return None
        files = getattr(self.index, "files", {}) or {}
        if not files:
            return None
        self._load_cache()
        chosen = list(files.values())[: self.max_files]
        missing = [item.path for item in chosen if self._vectors.get(item.path, {}).get("hash") != self._file_digest(Path(self.workspace) / item.path)]
        try:
            if missing:
                texts = []
                for relative in missing[:120]:
                    text = self._file_text(relative, 1200)
                    texts.append(f"{relative}\n{text}")
                vectors = await self.embed(texts)
                for relative, vector in zip(missing, vectors):
                    self._vectors[relative] = {
                        "hash": self._file_digest(Path(self.workspace) / relative),
                        "vector": vector,
                    }
                self._save_cache()
            query_vectors = await self.embed([query])
            if not query_vectors:
                return None
            query_vector = query_vectors[0]
            scored: list[tuple[float, str]] = []
            for item in chosen:
                entry = self._vectors.get(item.path)
                if not entry:
                    continue
                scored.append((_cosine(query_vector, entry.get("vector") or []), item.path))
            scored.sort(reverse=True)
            hits: list[RetrievalHit] = []
            for score, relative in scored[: max(1, limit)]:
                snippet = self._best_snippet(relative, query)
                hits.append(RetrievalHit(relative, 0, snippet, score, "semantic"))
            return [hit for hit in hits if hit.snippet]
        except Exception:
            return None

    def _best_snippet(self, relative: str, query: str, max_lines: int = 8) -> str:
        path = Path(self.workspace) / relative
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        terms = {token.lower() for token in re.findall(r"[\w\-]{3,}", query)}
        best_index, best_score = 0, -1
        for index in range(len(lines)):
            window = " ".join(lines[index : index + 12]).lower()
            score = sum(window.count(term) for term in terms)
            if score > best_score:
                best_index, best_score = index, score
        start = max(0, best_index - 1)
        snippet = "\n".join(lines[start : start + 14])
        return snippet[:900]

    # ── public API ────────────────────────────────────────────────
    async def retrieve(self, query: str, limit: int = 6, max_chars: int = 5000) -> RetrievalResult:
        query = (query or "").strip()
        if not query:
            return RetrievalResult(mode="none", block="")
        hits: list[RetrievalHit] | None = None
        mode = "lexical"
        note = ""
        if self.embed is not None:
            try:
                hits = await self._semantic_hits(query, limit)
                if hits:
                    mode = "embedding"
            except Exception:
                hits = None
        if not hits:
            hits = self._lexical_hits(query, limit)
        if not hits:
            return RetrievalResult(mode=mode, block="", note="no relevant code found")

        lines = [f"Retrieved context ({mode} search, {len(hits)} hits):"]
        used = 0
        kept: list[RetrievalHit] = []
        for hit in hits:
            rendered = hit.render()
            if used + len(rendered) > max_chars:
                break
            lines.append(rendered)
            lines.append("")
            used += len(rendered) + 1
            kept.append(hit)
        block = "\n".join(lines)
        return RetrievalResult(mode=mode, block=block, hits=kept, tokens=estimate_tokens(block), note=note)

    def _lexical_hits(self, query: str, limit: int) -> list[RetrievalHit]:
        search = getattr(self.index, "search", None)
        if callable(search):
            try:
                raw = search(query, limit=limit)
            except Exception:
                raw = []
            hits: list[RetrievalHit] = []
            for item in raw:
                path = getattr(item, "path", "")
                line = int(getattr(item, "line", 0) or 0)
                snippet = getattr(item, "snippet", "") or self._best_snippet(path, query)
                if snippet:
                    hits.append(
                        RetrievalHit(path, line, snippet, float(getattr(item, "score", 0)), getattr(item, "kind", "text"))
                    )
            if hits:
                return hits
        return []


def build_context_block(
    *,
    brief: str = "",
    memory: str = "",
    skills: str = "",
    retrieval: str = "",
    fingerprint: str = "",
    max_chars: int = 7000,
) -> str:
    """Assemble ambient context with a hard character budget.

    Order matters: identity/instructions, then memory, then retrieved code.
    """
    sections = [
        ("WORKSPACE", fingerprint),
        ("MEMORY", memory),
        ("SKILLS", skills),
        ("PROJECT", brief),
        ("RETRIEVED CODE", retrieval),
    ]
    out: list[str] = []
    used = 0
    for title, body in sections:
        body = (body or "").strip()
        if not body:
            continue
        block = f"<{title.lower()}>\n{body}\n</{title.lower()}>"
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining < 400:
                break
            block = block[:remaining] + "\n… truncated"
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


async def gather_retrieval(retriever: Retriever | None, query: str, limit: int = 5) -> RetrievalResult:
    if retriever is None:
        return RetrievalResult(mode="none", block="")
    return await retriever.retrieve(query, limit=limit)
