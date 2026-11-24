"""Builtin toolset: filesystem, shell, git, web, process and utility tools."""

from __future__ import annotations

import ast
import asyncio
import base64
import fnmatch
import hashlib
import ipaddress
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import psutil

from ..animations import format_bytes, format_duration
from ..config import CACHE_DIR
from .registry import ToolContext, ToolError, ToolRegistry, prop, schema

MAX_READ_BYTES = 512_000
MAX_OUTPUT_CHARS = 24_000
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".next", ".turbo", ".idea", ".vs",
    "target", ".gradle", ".cache", "site-packages",
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip",
    ".gz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib", ".pyc", ".woff",
    ".woff2", ".ttf", ".mp3", ".mp4", ".mov", ".wav", ".ogg", ".webm",
}


# ══════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════
def _claim_write(ctx: ToolContext, path: Path) -> tuple[Any, str, str]:
    """Check the optional team file-ownership lease before a write.

    Returns `(ownership, agent, task)` so the caller can record the change.
    When no ownership table is attached (single-agent chat) this is a no-op.
    """
    extras = getattr(ctx, "extras", {}) or {}
    ownership = extras.get("ownership")
    if ownership is None:
        return None, "", ""
    agent = str(extras.get("ownership_agent") or extras.get("agent") or "agent")
    task = str(extras.get("team_task") or "")
    try:
        ownership.check_write(str(path), agent, task)
    except Exception as error:
        raise ToolError(f"File ownership conflict: {error}") from error
    return ownership, agent, task


def _record_write(ownership: Any, agent: str, task: str, path: Path, detail: str) -> None:
    if ownership is None:
        return
    try:
        ownership.record(str(path), agent, task, detail)
    except Exception:
        pass

def _resolve(ctx: ToolContext, path: str | None, *, allow_outside: bool = False) -> Path:
    raw = (path or ".").strip().strip('"')
    workspace_root = Path(ctx.cwd).resolve()
    expanded = Path(raw).expanduser()
    candidate = expanded if expanded.is_absolute() else (workspace_root / expanded)
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate.absolute()
    if not allow_outside and not getattr(ctx.settings, "allow_outside_workspace", False):
        if not resolved.is_relative_to(workspace_root):
            raise ToolError(f"Path escapes workspace ({workspace_root}): {raw}")
    return resolved


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.65)
    tail = int(limit * 0.25)
    return (
        text[:head]
        + f"\n\n… [{len(text) - head - tail:,} characters elided] …\n\n"
        + text[-tail:]
    )


def _is_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    try:
        with path.open("rb") as handle:
            chunk = handle.read(2048)
    except OSError:
        return True
    return b"\x00" in chunk


def _iter_files(root: Path, pattern: str = "*") -> list[Path]:
    results: list[Path] = []
    if not root.exists():
        return results
    if root.is_file():
        return [root]
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in sorted(dirnames) if d not in SKIP_DIRS]
            for name in sorted(filenames):
                if pattern == "*" or fnmatch.fnmatch(name, pattern):
                    results.append(Path(dirpath) / name)
                    if len(results) > 4000:
                        return results
    except OSError:
        pass
    return results


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess and its children, then reap it."""
    pid = getattr(process, "pid", None)
    if os.name == "nt" and pid:
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=flags,
            )
            await asyncio.wait_for(killer.wait(), timeout=5.0)
        except Exception:
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except Exception:
        pass


async def _run_argv(argv: list[str], cwd: str, timeout: float) -> tuple[int, str]:
    """Run an argument vector directly without invoking a shell."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, NotADirectoryError, OSError) as error:
        raise ToolError(f"Cannot start process {argv[0]!r}: {error}") from error

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _kill_process_tree(process)
        raise ToolError(f"Command timed out after {timeout:.0f}s: {argv[0]}")
    except asyncio.CancelledError:
        await _kill_process_tree(process)
        raise
    text = (stdout or b"").decode("utf-8", errors="replace")
    return process.returncode or 0, text


async def _run_process(command: str, cwd: str, timeout: float) -> tuple[int, str]:
    if os.name == "nt":
        argv = [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command,
        ]
    else:
        argv = ["/bin/bash", "-lc", command]
    return await _run_argv(argv, cwd, timeout)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section"):
            self.parts.append("\n")
        if tag == "a":
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return unescape(parser.text())


def _safe_factorial(n: int) -> int:
    if not isinstance(n, int) or isinstance(n, bool) or n < 0 or n > 1000:
        raise ToolError("factorial argument must be an integer between 0 and 1000")
    return math.factorial(n)


_SAFE_MATH = {
    name: getattr(math, name)
    for name in (
        "sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "log", "log2",
        "log10", "exp", "floor", "ceil", "fabs", "degrees",
        "radians", "hypot", "gcd",
    )
}
_SAFE_MATH.update({"pi": math.pi, "e": math.e, "tau": math.tau, "abs": abs, "round": round, "min": min, "max": max, "factorial": _safe_factorial})


def _safe_eval(expression: str) -> Any:
    tree = ast.parse(expression, mode="eval")
    allowed = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub,
        ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.FloorDiv, ast.USub, ast.UAdd,
        ast.Call, ast.Name, ast.Load, ast.Tuple, ast.List, ast.Compare,
        ast.Gt, ast.Lt, ast.GtE, ast.LtE, ast.Eq, ast.NotEq, ast.BitAnd,
        ast.BitOr, ast.BitXor, ast.LShift, ast.RShift, ast.IfExp, ast.BoolOp,
        ast.And, ast.Or, ast.Not,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ToolError(f"Unsupported expression element: {type(node).__name__}")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            if isinstance(node.right, ast.Constant) and isinstance(node.right.value, (int, float)):
                if abs(node.right.value) > 1000:
                    raise ToolError("Exponent too large (max 1000)")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.LShift):
            if isinstance(node.right, ast.Constant) and isinstance(node.right.value, int):
                if node.right.value < 0 or node.right.value > 256:
                    raise ToolError("Bit shift out of range (0..256)")
        if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
            raise ToolError("Only simple function calls are allowed")
        if isinstance(node, ast.Name) and node.id not in _SAFE_MATH:
            raise ToolError(f"Unknown identifier: {node.id}")
    return eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, _SAFE_MATH)


# ══════════════════════════════════════════════════════════════════════
# registration
# ══════════════════════════════════════════════════════════════════════
def register_builtin_tools(registry: ToolRegistry) -> None:
    # ── filesystem ────────────────────────────────────────────────
    @registry.register(
        "fs_read",
        "Read a UTF-8 text file. Returns numbered lines so you can target edits precisely. "
        "Always call this before editing a file.",
        schema(
            {
                "path": prop("string", "File path (absolute, or relative to the workspace)"),
                "offset": prop("integer", "First line to return (1-based)", minimum=1),
                "limit": prop("integer", "How many lines to return", minimum=1, maximum=2000),
            },
            ["path"],
        ),
        category="files",
        icon="▤",
    )
    async def fs_read(args: dict[str, Any], ctx: ToolContext) -> str:
        path = _resolve(ctx, args.get("path"))
        offset = max(1, int(args.get("offset") or 1))
        limit = min(2000, max(1, int(args.get("limit") or 400)))

        has_slice = "offset" in args or "limit" in args

        def read_sync() -> str:
            if not path.exists():
                raise ToolError(f"File not found: {path}")
            if path.is_dir():
                raise ToolError(f"{path} is a directory; use fs_list")
            if _is_binary(path):
                raise ToolError(
                    f"{path} looks binary ({path.suffix or 'no extension'}); cannot read as text"
                )
            size = path.stat().st_size
            if size > MAX_READ_BYTES and not has_slice:
                raise ToolError(
                    f"{path} is {format_bytes(size)}; read a slice with offset/limit "
                    "or use fs_grep"
                )
            try:
                if size > MAX_READ_BYTES:
                    window: list[str] = []
                    total_lines = 0
                    end_line = offset - 1 + limit
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        for idx, raw_line in enumerate(handle, 1):
                            total_lines = idx
                            if idx >= offset and idx <= end_line:
                                window.append(raw_line.rstrip("\r\n"))
                    body = "\n".join(
                        f"{offset + index:6d}│ {line}" for index, line in enumerate(window)
                    )
                    header = f"{path}  ({total_lines} lines, showing {offset}-{offset + len(window) - 1})"
                    return f"{header}\n{body}" if body else f"{header}\n(empty range)"
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as error:
                raise ToolError(f"Cannot read {path}: {error}") from error
            lines = text.splitlines()
            window = lines[offset - 1 : offset - 1 + limit]
            body = "\n".join(f"{offset + index:6d}│ {line}" for index, line in enumerate(window))
            header = f"{path}  ({len(lines)} lines, showing {offset}-{offset + len(window) - 1})"
            return f"{header}\n{body}" if body else f"{header}\n(empty range)"

        return await asyncio.to_thread(read_sync)

    @registry.register(
        "fs_list",
        "List a directory as a compact tree. Use it to orient yourself in a project.",
        schema(
            {
                "path": prop("string", "Directory to list (default: workspace)"),
                "depth": prop("integer", "Recursion depth", minimum=1, maximum=4),
            },
            [],
        ),
        category="files",
        icon="▦",
    )
    async def fs_list(args: dict[str, Any], ctx: ToolContext) -> str:
        root = _resolve(ctx, args.get("path"))
        depth = max(1, min(4, int(args.get("depth") or 1)))

        def list_sync() -> str:
            if not root.exists():
                raise ToolError(f"Path not found: {root}")
            if not root.is_dir():
                return f"{root}  ({format_bytes(root.stat().st_size)})"
            lines: list[str] = [f"{root}"]
            count = 0

            def walk(directory: Path, prefix: str, level: int) -> None:
                nonlocal count
                if count > 500:
                    return
                try:
                    entries = sorted(
                        directory.iterdir(),
                        key=lambda p: (p.is_file(), p.name.lower()),
                    )
                except OSError:
                    return
                entries = [entry for entry in entries if entry.name not in SKIP_DIRS]
                for index, entry in enumerate(entries):
                    last = index == len(entries) - 1
                    branch = "└─ " if last else "├─ "
                    suffix = "/" if entry.is_dir() else ""
                    size = ""
                    if entry.is_file():
                        try:
                            size = f"  {format_bytes(entry.stat().st_size)}"
                        except OSError:
                            size = ""
                    lines.append(f"{prefix}{branch}{entry.name}{suffix}{size}")
                    count += 1
                    if count > 500:
                        return
                    if entry.is_dir() and level < depth:
                        walk(entry, prefix + ("   " if last else "│  "), level + 1)

            walk(root, "", 1)
            if count > 500:
                lines.append("… listing truncated at 500 entries")
            return "\n".join(lines)

        return await asyncio.to_thread(list_sync)

    @registry.register(
        "fs_glob",
        "Find files by glob pattern (e.g. '**/*.py', 'src/**/*.tsx'). Fast project navigation.",
        schema(
            {
                "pattern": prop("string", "Glob pattern, supports **"),
                "path": prop("string", "Root directory (default: workspace)"),
                "limit": prop("integer", "Max results", minimum=1, maximum=1000),
            },
            ["pattern"],
        ),
        category="files",
        icon="◇",
    )
    async def fs_glob(args: dict[str, Any], ctx: ToolContext) -> str:
        root = _resolve(ctx, args.get("path"))
        pattern = str(args.get("pattern") or "*")
        limit = max(1, min(1000, int(args.get("limit") or 200)))
        matches = await asyncio.to_thread(_iter_files, root, pattern)
        if not matches:
            return f"No files matching {pattern!r} under {root}"
        shown = matches[:limit]
        lines = [str(path.relative_to(root)) if root in path.parents or path != root else str(path) for path in shown]
        suffix = f"\n… {len(matches) - len(shown)} more" if len(matches) > len(shown) else ""
        return f"{len(matches)} match(es):\n" + "\n".join(lines) + suffix

    @registry.register(
        "fs_grep",
        "Search file contents with a regular expression. Returns file:line: text. "
        "Best tool for finding symbols, strings and TODOs.",
        schema(
            {
                "pattern": prop("string", "Python regular expression"),
                "path": prop("string", "Root directory or file (default: workspace)"),
                "glob": prop("string", "Only search matching files, e.g. '*.py'"),
                "ignore_case": prop("boolean", "Case-insensitive search"),
                "limit": prop("integer", "Max matching lines", minimum=1, maximum=500),
            },
            ["pattern"],
        ),
        category="files",
        icon="⌕",
    )
    async def fs_grep(args: dict[str, Any], ctx: ToolContext) -> str:
        root = _resolve(ctx, args.get("path"))
        pattern = str(args.get("pattern") or "")
        if not pattern:
            raise ToolError("pattern is required")
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as error:
            raise ToolError(f"Invalid regex: {error}") from error
        limit = max(1, min(500, int(args.get("limit") or 100)))
        glob_pattern = str(args.get("glob") or "*")
        files = await asyncio.to_thread(_iter_files, root, glob_pattern)

        def scan_sync() -> tuple[list[str], int]:
            hits: list[str] = []
            scanned = 0
            for file in files:
                if _is_binary(file):
                    continue
                try:
                    if file.stat().st_size > MAX_READ_BYTES * 2:
                        continue
                    text = file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                scanned += 1
                display = str(file.relative_to(root)) if root in file.parents else str(file)
                for line_no, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{display}:{line_no}: {line.strip()[:220]}")
                        if len(hits) >= limit:
                            break
                if len(hits) >= limit:
                    break
            return hits, scanned

        hits, scanned = await asyncio.to_thread(scan_sync)
        if not hits:
            return f"No matches for {pattern!r} in {scanned} file(s)"
        suffix = f"\n… stopped at {limit} matches" if len(hits) >= limit else ""
        return "\n".join(hits) + suffix

    @registry.register(
        "fs_write",
        "Create or overwrite a file. Creates parent directories. Prefer fs_edit for existing files. "
        "Requires user approval.",
        schema(
            {
                "path": prop("string", "Target file path"),
                "content": prop("string", "Full file content"),
            },
            ["path", "content"],
        ),
        dangerous=True,
        category="files",
        icon="✎",
    )
    async def fs_write(args: dict[str, Any], ctx: ToolContext) -> str:
        path = _resolve(ctx, args.get("path"))
        content = str(args.get("content") or "")
        ownership, agent, task = _claim_write(ctx, path)

        def write_sync() -> tuple[bool, int]:
            existed = path.exists()
            old_size = path.stat().st_size if existed else 0
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
            except OSError as error:
                raise ToolError(f"Cannot write {path}: {error}") from error
            return existed, old_size

        existed, old_size = await asyncio.to_thread(write_sync)
        _record_write(ownership, agent, task, path, f"write {len(content.splitlines())} lines")
        verb = "Updated" if existed else "Created"
        return f"{verb} {path} ({len(content.splitlines())} lines, {format_bytes(len(content.encode('utf-8')))}; was {format_bytes(old_size)})"

    @registry.register(
        "fs_edit",
        "Replace an exact string inside an existing file. The old string must match exactly "
        "(including indentation). Requires user approval.",
        schema(
            {
                "path": prop("string", "File to edit"),
                "old_string": prop("string", "Exact text to replace"),
                "new_string": prop("string", "Replacement text"),
                "replace_all": prop("boolean", "Replace every occurrence (default: only if unique)"),
            },
            ["path", "old_string", "new_string"],
        ),
        dangerous=True,
        category="files",
        icon="✎",
    )
    async def fs_edit(args: dict[str, Any], ctx: ToolContext) -> str:
        path = _resolve(ctx, args.get("path"))
        old = str(args.get("old_string") or "")
        new = str(args.get("new_string") or "")
        if old == new:
            raise ToolError("old_string and new_string are identical")
        if not old:
            raise ToolError("old_string cannot be empty; use fs_write to create a file")
        ownership, agent, task = _claim_write(ctx, path)

        def edit_sync() -> tuple[int, int, int]:
            if not path.exists():
                raise ToolError(f"File not found: {path}")
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as error:
                raise ToolError(f"Cannot read {path}: {error}") from error
            occurrences = text.count(old)
            if occurrences == 0:
                raise ToolError(
                    "old_string not found. Re-read the file and copy the exact text, including whitespace."
                )
            replace_all = bool(args.get("replace_all"))
            if occurrences > 1 and not replace_all:
                raise ToolError(
                    f"old_string appears {occurrences} times. Add more context to make it unique "
                    "or pass replace_all=true."
                )
            updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            try:
                path.write_text(updated, encoding="utf-8", newline="\n")
            except OSError as error:
                raise ToolError(f"Cannot write {path}: {error}") from error
            replaced = occurrences if replace_all else 1
            return replaced, len(text.splitlines()), len(updated.splitlines())

        replaced, before_lines, after_lines = await asyncio.to_thread(edit_sync)
        _record_write(ownership, agent, task, path, f"edit {replaced} replacement(s)")
        return f"Edited {path}: {replaced} replacement(s), {before_lines} → {after_lines} lines"

    # ── shell / git ───────────────────────────────────────────────
    @registry.register(
        "shell_run",
        "Run a shell command in the workspace (PowerShell on Windows, bash elsewhere). "
        "Returns exit code and combined output. Requires user approval.",
        schema(
            {
                "command": prop("string", "Command line to execute"),
                "cwd": prop("string", "Working directory (default: workspace)"),
                "timeout": prop("integer", "Timeout in seconds", minimum=1, maximum=600),
            },
            ["command"],
        ),
        dangerous=True,
        category="shell",
        icon="⌘",
    )
    async def shell_run(args: dict[str, Any], ctx: ToolContext) -> str:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ToolError("command is required")
        cwd = str(_resolve(ctx, args.get("cwd"))) if args.get("cwd") else ctx.cwd
        timeout = max(1, min(600, int(args.get("timeout") or 120)))
        started = time.perf_counter()
        code, output = await _run_process(command, cwd, timeout)
        elapsed = time.perf_counter() - started
        body = _truncate(output.rstrip()) or "(no output)"
        return f"exit {code} · {format_duration(elapsed)} · {cwd}\n{body}"

    @registry.register(
        "git_status",
        "Show the git working tree status (branch, staged, modified, untracked).",
        schema({"cwd": prop("string", "Repository directory (default: workspace)")}, []),
        category="git",
        icon="⎇",
    )
    async def git_status(args: dict[str, Any], ctx: ToolContext) -> str:
        cwd = str(_resolve(ctx, args.get("cwd"))) if args.get("cwd") else ctx.cwd
        code, output = await _run_argv(
            ["git", "status", "--short", "--branch"], cwd, 30
        )
        if code != 0:
            raise ToolError(output.strip() or "Not a git repository")
        return output.strip() or "Clean working tree"

    @registry.register(
        "git_diff",
        "Show a git diff. Use staged=true for the index, or pass a path.",
        schema(
            {
                "cwd": prop("string", "Repository directory"),
                "path": prop("string", "Limit diff to this path"),
                "staged": prop("boolean", "Diff the index instead of the working tree"),
            },
            [],
        ),
        category="git",
        icon="⎇",
    )
    async def git_diff(args: dict[str, Any], ctx: ToolContext) -> str:
        cwd = str(_resolve(ctx, args.get("cwd"))) if args.get("cwd") else ctx.cwd
        argv = ["git", "diff"]
        if args.get("staged"):
            argv.append("--cached")
        if args.get("path"):
            argv.extend(["--", str(args["path"])])
        code, output = await _run_argv(argv, cwd, 60)
        if code != 0:
            raise ToolError(output.strip() or "git diff failed")
        return _truncate(output.strip() or "(no differences)")

    @registry.register(
        "git_log",
        "Recent commits, one line each.",
        schema(
            {
                "cwd": prop("string", "Repository directory"),
                "limit": prop("integer", "Number of commits", minimum=1, maximum=100),
            },
            [],
        ),
        category="git",
        icon="⎇",
    )
    async def git_log(args: dict[str, Any], ctx: ToolContext) -> str:
        cwd = str(_resolve(ctx, args.get("cwd"))) if args.get("cwd") else ctx.cwd
        limit = max(1, min(100, int(args.get("limit") or 12)))
        code, output = await _run_argv(
            ["git", "log", "-n", str(limit), "--pretty=format:%h  %ad  %an  %s", "--date=short"],
            cwd,
            30,
        )
        if code != 0:
            raise ToolError(output.strip() or "Not a git repository")
        return output.strip() or "(no commits)"

    # ── web ───────────────────────────────────────────────────────
    @registry.register(
        "web_fetch",
        "Fetch an HTTP(S) URL and return readable text (HTML is stripped). "
        "Use it for documentation, changelogs and API references.",
        schema(
            {
                "url": prop("string", "Absolute http(s) URL"),
                "max_chars": prop("integer", "Truncate result", minimum=500, maximum=40000),
            },
            ["url"],
        ),
        category="web",
        icon="↯",
    )
    async def web_fetch(args: dict[str, Any], ctx: ToolContext) -> str:
        from urllib.parse import urljoin, urlparse
        import httpx

        url = str(args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ToolError("url must start with http:// or https://")

        def _check_public_url(target: str) -> None:
            parsed = urlparse(target)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ToolError(f"Invalid URL: {target}")
            host = parsed.hostname
            try:
                infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
            except OSError as error:
                raise ToolError(f"Cannot resolve host {host!r}: {error}") from error
            for info in infos:
                addr_str = info[4][0].split("%")[0]
                try:
                    ip = ipaddress.ip_address(addr_str)
                except ValueError:
                    continue
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                    raise ToolError(f"Blocked non-public address for {host!r} ({addr_str})")

        max_chars = max(500, min(40000, int(args.get("max_chars") or 8000)))
        max_bytes = 1_000_000
        headers = {"User-Agent": "Mozilla/5.0 (compatible; GeminiStudioTUI/1.0)"}
        current_url = url
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=30, headers=headers) as client:
                for _ in range(5):
                    await asyncio.to_thread(_check_public_url, current_url)
                    async with client.stream("GET", current_url) as response:
                        if response.is_redirect and response.headers.get("location"):
                            current_url = urljoin(current_url, response.headers["location"])
                            continue
                        content_type = response.headers.get("content-type", "")
                        if "text" not in content_type and "json" not in content_type and "xml" not in content_type:
                            length = response.headers.get("content-length") or "?"
                            return f"HTTP {response.status_code} · {content_type or 'binary'} · {length} bytes (not text)"
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            chunks.append(chunk)
                            total += len(chunk)
                            if total >= max_bytes:
                                break
                        raw_bytes = b"".join(chunks)[:max_bytes]
                        encoding = response.encoding or "utf-8"
                        body = raw_bytes.decode(encoding, errors="replace")
                        if "html" in content_type:
                            body = _html_to_text(body)
                        body = body.strip()
                        if len(body) > max_chars:
                            body = body[:max_chars] + f"\n… truncated ({len(body):,} chars total)"
                        return f"HTTP {response.status_code} · {current_url}\n\n{body}"
                raise ToolError("Too many redirects")
        except ToolError:
            raise
        except httpx.HTTPError as error:
            raise ToolError(f"Fetch failed: {error}") from error

    @registry.register(
        "web_search",
        "Search the web (DuckDuckGo HTML) and return titles, URLs and snippets.",
        schema(
            {
                "query": prop("string", "Search query"),
                "limit": prop("integer", "Number of results", minimum=1, maximum=15),
            },
            ["query"],
        ),
        category="web",
        icon="⌕",
    )
    async def web_search(args: dict[str, Any], ctx: ToolContext) -> str:
        import httpx

        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query is required")
        limit = max(1, min(15, int(args.get("limit") or 6)))
        headers = {"User-Agent": "Mozilla/5.0 (compatible; GeminiStudioTUI/1.0)"}
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=25, headers=headers) as client:
                response = await client.post(
                    "https://html.duckduckgo.com/html/", data={"q": query}
                )
        except httpx.HTTPError as error:
            raise ToolError(f"Search failed: {error}") from error
        html = response.text
        pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            re.S,
        )
        results: list[str] = []
        for url, title, snippet in pattern.findall(html)[:limit]:
            clean_title = _html_to_text(title)
            clean_snippet = _html_to_text(snippet)
            clean_url = unescape(url)
            if "uddg=" in clean_url:
                match = re.search(r"uddg=([^&]+)", clean_url)
                if match:
                    from urllib.parse import unquote

                    clean_url = unquote(match.group(1))
            results.append(f"{len(results) + 1}. {clean_title}\n   {clean_url}\n   {clean_snippet[:260]}")
        if not results:
            raise ToolError(
                "DuckDuckGo returned no parseable results (it may be rate limiting). "
                "Try web_fetch on a known URL, or use an MCP search server."
            )
        return f"Search: {query}\n\n" + "\n\n".join(results)

    # ── system ────────────────────────────────────────────────────
    @registry.register(
        "sys_info",
        "Report OS, Python, CPU, memory, disk and GPU summary for this machine.",
        schema({}, []),
        category="system",
        icon="▣",
    )
    async def sys_info(args: dict[str, Any], ctx: ToolContext) -> str:
        def _collect() -> str:
            memory = psutil.virtual_memory()
            disk_path = Path(ctx.cwd).anchor or "/"
            try:
                disk = psutil.disk_usage(disk_path)
            except OSError:
                disk = None
            lines = [
                f"os         {platform.system()} {platform.release()} ({platform.machine()})",
                f"python     {sys.version.split()[0]} · {platform.python_implementation()}",
                f"cpu        {psutil.cpu_count(logical=True)} threads · {psutil.cpu_percent(interval=0.15):.0f}% used",
                f"memory     {format_bytes(memory.used)} / {format_bytes(memory.total)} ({memory.percent:.0f}%)",
            ]
            if disk:
                lines.append(f"disk       {format_bytes(disk.used)} / {format_bytes(disk.total)} ({disk.percent:.0f}%)")
            lines.append(f"workspace  {ctx.cwd}")
            try:
                battery = psutil.sensors_battery()
                if battery:
                    lines.append(f"battery    {battery.percent:.0f}% {'charging' if battery.power_plugged else 'on battery'}")
            except Exception:
                pass
            return "\n".join(lines)

        return await asyncio.to_thread(_collect)

    @registry.register(
        "proc_list",
        "List the busiest processes by CPU or memory.",
        schema(
            {
                "limit": prop("integer", "How many rows", minimum=1, maximum=40),
                "sort": prop("string", "Sort by: cpu or memory", enum=["cpu", "memory"]),
            },
            [],
        ),
        category="system",
        icon="▤",
    )
    async def proc_list(args: dict[str, Any], ctx: ToolContext) -> str:
        limit = max(1, min(40, int(args.get("limit") or 10)))
        sort_by = str(args.get("sort") or "cpu")

        def _collect() -> str:
            rows = []
            for process in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
                rows.append(process.info)
            key = "memory_percent" if sort_by == "memory" else "cpu_percent"
            rows.sort(key=lambda item: item.get(key) or 0, reverse=True)
            header = f"{'PID':>7}  {'NAME':<28} {'CPU%':>6} {'MEM%':>6}"
            body = [
                f"{row['pid']:>7}  {(row['name'] or '?')[:28]:<28} {(row['cpu_percent'] or 0):>6.1f} {(row['memory_percent'] or 0):>6.1f}"
                for row in rows[:limit]
            ]
            return "\n".join([header, "-" * len(header), *body])

        return await asyncio.to_thread(_collect)

    @registry.register(
        "net_stats",
        "Network throughput and totals for this machine.",
        schema({}, []),
        category="system",
        icon="⇅",
    )
    async def net_stats(args: dict[str, Any], ctx: ToolContext) -> str:
        first = psutil.net_io_counters()
        await asyncio.sleep(0.5)
        second = psutil.net_io_counters()
        sent = (second.bytes_sent - first.bytes_sent) * 2
        recv = (second.bytes_recv - first.bytes_recv) * 2
        return (
            f"upload     {format_bytes(sent)}/s\n"
            f"download   {format_bytes(recv)}/s\n"
            f"total      ↑ {format_bytes(second.bytes_sent)} · ↓ {format_bytes(second.bytes_recv)}"
        )

    # ── utility ───────────────────────────────────────────────────
    @registry.register(
        "time_now",
        "Current local time, UTC time and unix timestamp.",
        schema({"timezone": prop("string", "IANA timezone, optional")}, []),
        category="utility",
        icon="◷",
    )
    async def time_now(args: dict[str, Any], ctx: ToolContext) -> str:
        local = datetime.now().astimezone()
        utc = datetime.now(timezone.utc)
        line = (
            f"local  {local.strftime('%A, %d/%m/%Y %H:%M:%S %Z')}\n"
            f"utc    {utc.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"unix   {int(time.time())}"
        )
        tz_name = str(args.get("timezone") or "").strip()
        if tz_name:
            try:
                from zoneinfo import ZoneInfo

                other = datetime.now(ZoneInfo(tz_name))
                line += f"\n{tz_name}  {other.strftime('%Y-%m-%d %H:%M:%S %Z')}"
            except Exception as error:
                line += f"\n(timezone error: {error})"
        return line

    @registry.register(
        "calc",
        "Evaluate a mathematical expression safely (sin, sqrt, log, pi, …).",
        schema({"expression": prop("string", "Expression, e.g. 'sqrt(2)*12'")}, ["expression"]),
        category="utility",
        icon="∑",
    )
    async def calc(args: dict[str, Any], ctx: ToolContext) -> str:
        expression = str(args.get("expression") or "").strip()
        if not expression:
            raise ToolError("expression is required")
        result = _safe_eval(expression)
        return f"{expression} = {result}"

    @registry.register(
        "hash_text",
        "Hash text with sha256, sha1 or md5, and optionally base64-encode it.",
        schema(
            {
                "text": prop("string", "Input text"),
                "algorithm": prop("string", "sha256 | sha1 | md5", enum=["sha256", "sha1", "md5"]),
                "base64": prop("boolean", "Also return base64"),
            },
            ["text"],
        ),
        category="utility",
        icon="#",
    )
    async def hash_text(args: dict[str, Any], ctx: ToolContext) -> str:
        text = str(args.get("text") or "")
        algorithm = str(args.get("algorithm") or "sha256")
        if algorithm not in {"sha256", "sha1", "md5"}:
            raise ToolError("algorithm must be sha256, sha1 or md5")
        digest = hashlib.new(algorithm, text.encode("utf-8")).hexdigest()
        output = f"{algorithm}  {digest}"
        if args.get("base64"):
            output += f"\nbase64    {base64.b64encode(text.encode('utf-8')).decode()}"
        return output

    @registry.register(
        "json_query",
        "Parse JSON (from a file or literal string) and return a selected path, pretty-printed.",
        schema(
            {
                "json": prop("string", "JSON text or a file path ending in .json"),
                "path": prop("string", "Dot path, e.g. 'models.0.name' (optional)"),
            },
            ["json"],
        ),
        category="utility",
        icon="{}",
    )
    async def json_query(args: dict[str, Any], ctx: ToolContext) -> str:
        raw = str(args.get("json") or "").strip()
        if not raw:
            raise ToolError("json is required")
        if raw.lower().endswith(".json") and "\n" not in raw:
            candidate = _resolve(ctx, raw)
            if candidate.is_file():
                try:
                    raw = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as error:
                    raise ToolError(f"Cannot read {candidate}: {error}") from error
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise ToolError(f"Invalid JSON: {error}") from error
        path = str(args.get("path") or "").strip()
        if path:
            cursor = data
            for part in path.split("."):
                if not part:
                    continue
                try:
                    if isinstance(cursor, list):
                        cursor = cursor[int(part)]
                    elif isinstance(cursor, dict):
                        cursor = cursor[part]
                    else:
                        raise ToolError(f"Cannot descend into {type(cursor).__name__} at {part!r}")
                except (KeyError, IndexError, ValueError) as error:
                    raise ToolError(f"Path {part!r} not found in JSON: {error}") from error
            data = cursor
        rendered = json.dumps(data, indent=2, ensure_ascii=False)
        return rendered[:20000]

    # ── workspace memory & todos ──────────────────────────────────
    @registry.register(
        "todo_write",
        "Replace the session task list with the given items. Use it for multi-step work so the "
        "user can see progress in the dashboard.",
        schema(
            {
                "items": {
                    "type": "array",
                    "description": "Tasks in order",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "active", "done"]},
                        },
                        "required": ["text"],
                    },
                }
            },
            ["items"],
        ),
        category="workspace",
        icon="☑",
    )
    async def todo_write(args: dict[str, Any], ctx: ToolContext) -> str:
        items = args.get("items") or []
        if not isinstance(items, list):
            raise ToolError("items must be an array")
        cleaned = []
        for item in items[:40]:
            if isinstance(item, str):
                cleaned.append({"text": item, "status": "pending"})
            elif isinstance(item, dict):
                cleaned.append(
                    {
                        "text": str(item.get("text", "")).strip(),
                        "status": str(item.get("status", "pending")),
                    }
                )
        ctx.extras["todos"] = cleaned
        if ctx.app is not None and hasattr(ctx.app, "set_todos"):
            try:
                ctx.app.set_todos(cleaned)
            except Exception:
                pass
        done = sum(1 for item in cleaned if item["status"] == "done")
        return f"Task list updated: {done}/{len(cleaned)} complete\n" + "\n".join(
            f"[{'x' if item['status'] == 'done' else ' '}] {item['text']}" for item in cleaned
        )

    MEMORY_FILE = CACHE_DIR / "memory.json"  # legacy location, migrated by agent.memory

    # ── clipboard ─────────────────────────────────────────────────
    @registry.register(
        "clipboard_read",
        "Read the system clipboard.",
        schema({}, []),
        category="workspace",
        icon="⧉",
    )
    async def clipboard_read(args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            import pyperclip
        except ImportError as error:
            raise ToolError("pyperclip is not installed") from error
        try:
            return await asyncio.to_thread(pyperclip.paste) or "(clipboard empty)"
        except Exception as error:
            raise ToolError(f"Clipboard unavailable: {error}") from error

    @registry.register(
        "clipboard_write",
        "Copy text to the system clipboard.",
        schema({"text": prop("string", "Text to copy")}, ["text"]),
        category="workspace",
        risk="write",
        icon="⧉",
    )
    async def clipboard_write(args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            import pyperclip
        except ImportError as error:
            raise ToolError("pyperclip is not installed") from error
        text = str(args.get("text") or "")
        await asyncio.to_thread(pyperclip.copy, text)
        return f"Copied {len(text)} chars to clipboard"

    # ── opencode bridge ───────────────────────────────────────────
    @registry.register(
        "opencode_run",
        "Delegate a task to the locally installed `opencode` agent CLI, which has access to the "
        "user's full plugin/skill/MCP setup. Returns its printed answer.",
        schema(
            {
                "prompt": prop("string", "Task or question for opencode"),
                "model": prop("string", "Optional model override, e.g. 'opencode/grok-code'"),
                "cwd": prop("string", "Working directory (default: workspace)"),
            },
            ["prompt"],
        ),
        dangerous=True,
        category="bridge",
        icon="⌬",
    )
    async def opencode_run(args: dict[str, Any], ctx: ToolContext) -> str:
        if shutil.which("opencode") is None:
            raise ToolError("The `opencode` CLI is not installed or not on PATH")
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ToolError("prompt is required")
        argv = ["opencode", "run", prompt]
        model = str(args.get("model") or "").strip()
        if model:
            argv.extend(["--model", model])
        cwd = str(_resolve(ctx, args.get("cwd"))) if args.get("cwd") else ctx.cwd
        code, output = await _run_argv(argv, cwd, 600)
        if code != 0:
            raise ToolError(f"opencode exited {code}\n{_truncate(output)}")
        return _truncate(output.strip()) or "(opencode returned no output)"
