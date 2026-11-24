"""Tools exposed to the model for Gemini Code."""

from __future__ import annotations

import asyncio
import fnmatch
import time
from pathlib import Path
from typing import Any

from ...animations import format_duration
from ...tools.builtin import _run_process, _truncate
from ...tools.registry import ToolContext, ToolError, ToolRegistry, ToolSpec, prop, schema
from ..context import estimate_tokens
from .index import CodeStore
from .review import detect_test_command, format_findings, review_diff, review_files


def _spec(
    name: str,
    description: str,
    parameters: dict[str, Any],
    handler: Any,
    *,
    risk: str = "read",
    icon: str = "⌘",
    dangerous: bool = False,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        dangerous=dangerous,
        category="code",
        icon=icon,
        source="agent",
        risk=risk,
    )


def register_code_tools(registry: ToolRegistry, store: CodeStore) -> None:
    workspace = store.workspace

    async def _index(force: bool = False):
        return await asyncio.to_thread(store.ensure, force)

    # ── index ─────────────────────────────────────────────────────
    async def code_index(args: dict[str, Any], ctx: ToolContext) -> str:
        force = bool(args.get("force"))
        started = time.perf_counter()
        index = await _index(force)
        elapsed = time.perf_counter() - started
        return (
            f"{index.summary()}"
            f"Indexed in {format_duration(elapsed)} · root {index.root}\n"
            "Use code_search for symbols/text, code_outline for one file, code_pack to load selected files."
        )

    async def code_search(args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query is required")
        limit = max(1, min(40, int(args.get("limit") or 12)))
        content = bool(args.get("content", True))
        index = await _index(False)
        hits = await asyncio.to_thread(index.search, query, limit, content=content)
        if not hits:
            return f"No index matches for {query!r}. Try a shorter symbol name, or fs_grep for literal text."
        lines = [f"{len(hits)} match(es) for {query!r}:"]
        for hit in hits:
            label = hit.symbol or hit.kind
            lines.append(f"\n{hit.path}:{hit.line}  [{label}]  score {hit.score:.0f}")
            snippet = hit.snippet.strip()
            if snippet:
                lines.append(_truncate(snippet, 700))
        return "\n".join(lines)

    async def code_outline(args: dict[str, Any], ctx: ToolContext) -> str:
        path = str(args.get("path") or "").strip()
        if not path:
            raise ToolError("path is required")
        index = await _index(False)
        return index.outline(path)

    async def code_pack(args: dict[str, Any], ctx: ToolContext) -> str:
        index = await _index(False)
        paths = args.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        selected: list[str] = []
        if paths:
            for item in paths:
                needle = str(item).replace("\\", "/").lstrip("./")
                if needle in index.files:
                    selected.append(needle)
                    continue
                selected.extend([name for name in index.files if name.endswith(needle)])
        glob = str(args.get("glob") or "").strip()
        if glob:
            selected.extend(
                [
                    name
                    for name in index.files
                    if fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(Path(name).name, glob)
                ]
            )
        selected = list(dict.fromkeys(selected))
        if not selected:
            raise ToolError("No indexed files matched. Pass paths or a glob like 'src/**/*.ts'.")
        budget = max(2000, min(60000, int(args.get("max_chars") or 24000)))
        packed = await asyncio.to_thread(index.pack, selected, budget)
        return (
            f"Packed {len(selected)} file(s) · ~{estimate_tokens(packed):,} tokens · "
            f"{len(packed):,} chars (cap {budget:,})\n{packed}"
        )

    # ── review ────────────────────────────────────────────────────
    async def code_review(args: dict[str, Any], ctx: ToolContext) -> str:
        paths = args.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        glob = str(args.get("glob") or "").strip()
        diff = str(args.get("diff") or "")
        limit = max(1, min(200, int(args.get("limit") or 80)))
        if diff.strip():
            findings = await asyncio.to_thread(review_diff, workspace, diff, limit)
            header = "Review of supplied diff"
        else:
            findings = await asyncio.to_thread(
                review_files, workspace, paths or None, glob=glob, limit=limit
            )
            scope = ", ".join(paths) if paths else (glob or "workspace")
            header = f"Review of {scope}"
        command, source = detect_test_command(workspace)
        footer = f"\n\nSuggested test command: {command} ({source})" if command else ""
        return f"{header}\n{format_findings(findings, limit)}{footer}"

    # ── tests ─────────────────────────────────────────────────────
    async def code_test(args: dict[str, Any], ctx: ToolContext) -> str:
        command = str(args.get("command") or "").strip()
        source = "explicit"
        if not command:
            command, source = detect_test_command(workspace)
        if not command:
            raise ToolError(
                "No test runner detected. Pass command explicitly, e.g. "
                "{command: 'python -m pytest -q'}."
            )
        if bool(args.get("dry_run")):
            return f"Detected test command ({source}): {command}\nRun it with dry_run=false."
        timeout = max(5, min(900, int(args.get("timeout") or 300)))
        started = time.perf_counter()
        code, output = await _run_process(command, workspace, timeout)
        elapsed = time.perf_counter() - started
        tail = _truncate(output.strip() or "(no output)", 16000)
        return f"$ {command}\nexit {code} · {format_duration(elapsed)} · {workspace}\n\n{tail}"

    registry.add(
        _spec(
            "code_index",
            "Build or refresh the workspace code index (files, languages, symbols, imports). "
            "Call this once per project before code_search/code_outline.",
            schema(
                {
                    "force": prop("boolean", "Rebuild even if a fresh cache exists"),
                },
                [],
            ),
            code_index,
            icon="⌗",
        )
    )
    registry.add(
        _spec(
            "code_search",
            "Search the code index for symbols and text. Token-efficient orientation: "
            "prefer this over reading whole files. Returns file:line with a short snippet.",
            schema(
                {
                    "query": prop("string", "Symbol name, identifier or text"),
                    "limit": prop("integer", "Max hits", minimum=1, maximum=40),
                    "content": prop("boolean", "Also scan file contents (default true)"),
                },
                ["query"],
            ),
            code_search,
            icon="⌕",
        )
    )
    registry.add(
        _spec(
            "code_outline",
            "Show the symbols, imports and summary of one indexed file without reading its body.",
            schema({"path": prop("string", "File path relative to the workspace")}, ["path"]),
            code_outline,
            icon="≡",
        )
    )
    registry.add(
        _spec(
            "code_pack",
            "Pack selected files (Repomix-style) into one prompt block with a token estimate. "
            "Use sparingly: prefer code_outline/code_search for orientation.",
            schema(
                {
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "File paths relative to the workspace"},
                    "glob": prop("string", "Alternatively, a glob such as 'src/**/*.ts'"),
                    "max_chars": prop("integer", "Character budget", minimum=2000, maximum=60000),
                },
                [],
            ),
            code_pack,
            icon="▤",
        )
    )
    registry.add(
        _spec(
            "code_review",
            "Run deterministic review checks (secrets, eval/exec, bare except, long lines, TODOs) "
            "on files or a unified diff. Findings include file:line.",
            schema(
                {
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "Files or directories"},
                    "glob": prop("string", "Filter by filename glob"),
                    "diff": prop("string", "Unified diff to review instead of files"),
                    "limit": prop("integer", "Max findings", minimum=1, maximum=200),
                },
                [],
            ),
            code_review,
            icon="⚑",
        )
    )
    registry.add(
        _spec(
            "code_test",
            "Detect and run the project's test command (pytest, npm test, cargo test, go test, …). "
            "Requires approval because it executes code.",
            schema(
                {
                    "command": prop("string", "Override the detected command"),
                    "dry_run": prop("boolean", "Only report the detected command"),
                    "timeout": prop("integer", "Seconds", minimum=5, maximum=900),
                },
                [],
            ),
            code_test,
            risk="execute",
            icon="✓",
            dangerous=True,
        )
    )
