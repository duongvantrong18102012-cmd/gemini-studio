"""MCP client manager.

Each configured server gets its own asyncio task that owns the stdio session
and processes commands from a queue. Keeping the session inside one task avoids
anyio cancel-scope problems when the TUI starts or stops servers.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..config import CONFIG_DIR, LOGS_DIR, MCP_FILE
from .registry import ToolContext, ToolError, ToolRegistry, ToolSpec, prop, schema

SERVERS_FILE = MCP_FILE

# ══════════════════════════════════════════════════════════════════════
# Config discovery
# ══════════════════════════════════════════════════════════════════════
def _strip_jsonc(text: str) -> str:
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    cleaned = "".join(out)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        try:
            return json.loads(_strip_jsonc(raw))
        except ValueError:
            return {}


def _normalize_command(command: Any) -> tuple[str, list[str]]:
    if isinstance(command, str):
        parts = command.split()
        if not parts:
            return "", []
        command, args = parts[0], parts[1:]
    elif isinstance(command, (list, tuple)) and command:
        command, args = str(command[0]), [str(item) for item in command[1:]]
    else:
        return "", []

    resolved = shutil.which(command) or command
    lowered = resolved.lower()
    if os.name == "nt" and lowered.endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return comspec, ["/c", resolved, *args]
    if os.name == "nt" and lowered.endswith(".ps1"):
        return "powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", resolved, *args]
    return resolved, args


@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"          # stdio | http | sse
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    source: str = "config"
    enabled: bool = True
    timeout: float = 900.0
    trusted: bool = True

    @property
    def label(self) -> str:
        if self.transport == "stdio":
            return f"{self.command} {' '.join(self.args)}".strip()
        return self.url

    @staticmethod
    def _parse_timeout(raw_timeout: Any) -> float:
        try:
            value = float(raw_timeout or 900)
        except (TypeError, ValueError):
            return 900.0
        if value <= 0:
            return 900.0
        return value / 1000.0 if value > 5000 else value

    @classmethod
    def from_raw(
        cls,
        name: str,
        raw: dict[str, Any],
        source: str,
        *,
        trusted: bool = True,
    ) -> "MCPServerConfig | None":
        if not isinstance(raw, dict):
            return None
        transport = str(raw.get("type") or raw.get("transport") or "stdio").lower()
        timeout = cls._parse_timeout(raw.get("timeout"))
        if transport in ("local", "stdio", "process"):
            command, args = _normalize_command(raw.get("command"))
            if not command:
                return None
            env = {
                str(key): str(value)
                for key, value in (raw.get("environment") or raw.get("env") or {}).items()
                if value is not None
            }
            return cls(
                name=name,
                transport="stdio",
                command=command,
                args=args,
                env=env,
                cwd=str(raw.get("cwd") or ""),
                source=source,
                enabled=bool(raw.get("enabled", True)),
                timeout=timeout,
                trusted=trusted,
            )
        if transport in ("remote", "http", "streamable-http", "streamablehttp", "sse"):
            url = str(raw.get("url") or raw.get("serverUrl") or "")
            if not url:
                return None
            headers = {
                str(k): str(v)
                for k, v in (raw.get("headers") or {}).items()
                if v is not None
            }
            return cls(
                name=name,
                transport="sse" if transport == "sse" or url.endswith("/sse") else "http",
                url=url,
                headers=headers,
                source=source,
                enabled=bool(raw.get("enabled", True)),
                timeout=timeout,
                trusted=trusted,
            )
        return None


def _extract_servers(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("servers", "mcpServers"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    mcp = payload.get("mcp")
    if isinstance(mcp, dict):
        for key in ("servers", "mcpServers"):
            if isinstance(mcp.get(key), dict):
                return mcp[key]
        if all(isinstance(value, dict) for value in mcp.values()) and mcp:
            # opencode style: { "mcp": { "name": {...} } }
            if any("command" in value or "url" in value for value in mcp.values()):
                return mcp
    return {}


def discover_configs(workspace: str | None = None, *, trusted: bool = False) -> list[MCPServerConfig]:
    home = Path.home()
    sources: list[tuple[str, Path, bool]] = [
        ("gemini-tui", SERVERS_FILE, True),
        ("project", Path(workspace or os.getcwd()) / "opencode.json", trusted),
        ("project", Path(workspace or os.getcwd()) / "opencode.jsonc", trusted),
        ("project", Path(workspace or os.getcwd()) / ".mcp.json", trusted),
        ("opencode", home / ".config" / "opencode" / "opencode.json", True),
        ("opencode", home / ".config" / "opencode" / "opencode.jsonc", True),
        ("claude", home / ".claude.json", True),
        ("claude", home / ".claude" / "mcp.json", True),
        ("cursor", home / ".cursor" / "mcp.json", True),
        ("vscode", Path(workspace or os.getcwd()) / ".vscode" / "mcp.json", trusted),
    ]
    found: dict[str, MCPServerConfig] = {}
    for source, path, is_trusted in sources:
        if not path.is_file():
            continue
        payload = _read_json(path)
        raw_servers = _extract_servers(payload)
        for name, raw in raw_servers.items():
            if name in found:
                continue
            config = MCPServerConfig.from_raw(str(name), raw, source, trusted=is_trusted)
            if config is not None:
                found[config.name] = config
    return list(found.values())


# ══════════════════════════════════════════════════════════════════════
# Runtime
# ══════════════════════════════════════════════════════════════════════
@dataclass
class MCPToolInfo:
    name: str
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.name.replace("_", " ")


class MCPServerState:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.status = "idle"           # idle | connecting | ready | error | disabled
        self.error = ""
        self.tools: list[MCPToolInfo] = []
        self.started_at = 0.0
        self.last_used = 0.0
        self.calls = 0
        self.task: asyncio.Task | None = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ready = asyncio.Event()
        self.session: Any = None
        self._log: Any = None

    @property
    def name(self) -> str:
        return self.config.name

    def reset(self) -> None:
        self.ready.clear()
        self.queue = asyncio.Queue()
        self.tools = []
        self.error = ""


class MCPManager:
    """Owns server tasks and mirrors their tools into the shared registry."""

    def __init__(self, registry: ToolRegistry, notify=None) -> None:
        self.registry = registry
        self.notify = notify
        self.states: dict[str, MCPServerState] = {}
        self._started = False

    # ── lifecycle ─────────────────────────────────────────────────
    def load(self, workspace: str | None = None, *, trusted: bool = False) -> list[MCPServerConfig]:
        configs = discover_configs(workspace, trusted=trusted)
        for config in configs:
            if config.name not in self.states:
                self.states[config.name] = MCPServerState(config)
        self._register_meta_tools()
        return configs

    def add_raw(self, name: str, raw: dict[str, Any]) -> MCPServerConfig | None:
        config = MCPServerConfig.from_raw(name, raw, "gemini-tui")
        if config is not None:
            self.states[config.name] = MCPServerState(config)
        return config

    async def start(self) -> None:
        self._started = True
        auto = [
            state for state in self.states.values()
            if state.config.enabled and state.config.trusted
        ]
        # Servers are lazy by default; only the ones the user pinned auto-connect.
        for state in auto:
            if state.config.name in _auto_connect_names():
                asyncio.create_task(self.connect(state.name))

    async def stop(self) -> None:
        tasks = []
        for state in list(self.states.values()):
            if state.task and not state.task.done():
                tasks.append(self.disconnect(state.name))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._started = False

    # ── queries ───────────────────────────────────────────────────
    def summary(self) -> list[dict[str, Any]]:
        rows = []
        for state in self.states.values():
            rows.append(
                {
                    "name": state.name,
                    "status": state.status,
                    "tools": len(state.tools),
                    "source": state.config.source,
                    "transport": state.config.transport,
                    "error": state.error,
                    "calls": state.calls,
                    "command": state.config.label,
                }
            )
        rows.sort(key=lambda row: (row["status"] != "ready", row["name"]))
        return rows

    @property
    def ready_count(self) -> int:
        return sum(1 for state in self.states.values() if state.status == "ready")

    @property
    def total_tools(self) -> int:
        return sum(len(state.tools) for state in self.states.values())

    def tool_rows(self, server: str | None = None) -> list[tuple[str, MCPToolInfo]]:
        rows: list[tuple[str, MCPToolInfo]] = []
        for state in self.states.values():
            if server and state.name != server:
                continue
            for tool in state.tools:
                rows.append((state.name, tool))
        return rows

    def config_text(self) -> str:
        payload = {
            "servers": {
                state.name: {
                    "type": "local" if state.config.transport == "stdio" else "remote",
                    "command": (
                        [state.config.command, *state.config.args]
                        if state.config.transport == "stdio"
                        else None
                    ),
                    "url": state.config.url or None,
                    "environment": state.config.env or None,
                    "enabled": state.config.enabled,
                }
                for state in self.states.values()
            }
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)

    # ── connecting ────────────────────────────────────────────────
    async def connect(self, name: str, timeout: float = 60.0) -> tuple[bool, str]:
        state = self.states.get(name)
        if state is None:
            return False, f"Unknown MCP server: {name}"
        if not state.config.trusted:
            return False, f"MCP server {name!r} is from an untrusted workspace config"
        if state.status == "ready" and state.task and not state.task.done():
            return True, f"{name} already connected ({len(state.tools)} tools)"
        if state.task and not state.task.done():
            ready_event = state.ready
            try:
                await asyncio.wait_for(ready_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return False, f"Timed out waiting for {name}"
            if state.status == "ready":
                return True, f"{name} connected ({len(state.tools)} tools)"
            return False, state.error or f"{name} failed to connect"

        state.reset()
        state.status = "connecting"
        ready_event = state.ready
        state.task = asyncio.create_task(self._serve(state), name=f"mcp:{name}")
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self.disconnect(name)
            return False, f"Timed out connecting to {name} after {timeout:.0f}s"
        if state.status == "ready":
            return True, f"{name} connected · {len(state.tools)} tools"
        return False, state.error or f"Could not connect to {name}"

    async def disconnect(self, name: str) -> None:
        state = self.states.get(name)
        if state is None:
            return
        if state.task and not state.task.done():
            try:
                state.queue.put_nowait(None)
            except Exception:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(state.task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                state.task.cancel()
                try:
                    await state.task
                except BaseException:
                    pass
            except BaseException:
                pass
        state.task = None
        state.session = None
        state.status = "idle"
        state.reset()
        self._unregister_tools(state.name)

    # ── the per-server task ───────────────────────────────────────
    @asynccontextmanager
    async def _open(self, state: MCPServerState):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.client.sse import sse_client

        config = state.config
        log_path = LOGS_DIR / f"mcp-{re.sub(r'[^A-Za-z0-9_.-]', '_', config.name)}.log"
        state._log = open(log_path, "a", encoding="utf-8", errors="replace")
        state._log.write(f"\n\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {config.label} ===\n")
        state._log.flush()

        if config.transport == "stdio":
            parameters = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env or None,
                cwd=config.cwd or None,
                encoding_error_handler="replace",
            )

            @asynccontextmanager
            async def run_stdio():
                async with stdio_client(parameters, errlog=state._log) as (read, write):
                    yield read, write

            generator = run_stdio()
        elif config.transport == "sse":
            generator = sse_client(config.url, headers=config.headers)
        else:
            @asynccontextmanager
            async def run_http():
                async with streamablehttp_client(config.url, headers=config.headers) as (read, write, _):
                    yield read, write

            generator = run_http()

        async with generator as (read, write):
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=config.timeout),
            ) as session:
                await session.initialize()
                state.session = session
                yield session

    async def _serve(self, state: MCPServerState) -> None:
        name = state.name
        try:
            async with self._open(state) as session:
                listing = await session.list_tools()
                state.tools = [
                    MCPToolInfo(
                        name=tool.name,
                        description=tool.description or "",
                        schema=dict(tool.inputSchema or {"type": "object", "properties": {}}),
                    )
                    for tool in listing.tools
                ]
                state.status = "ready"
                state.started_at = time.time()
                state.ready.set()
                self._register_tools(state)
                while True:
                    item = await state.queue.get()
                    if item is None:
                        break
                    future, tool_name, arguments = item
                    try:
                        result = await session.call_tool(tool_name, arguments, read_timeout_seconds=timedelta(seconds=state.config.timeout))
                        text = _render_result(result)
                        state.calls += 1
                        state.last_used = time.time()
                        if not future.done():
                            future.set_result(text)
                    except Exception as error:
                        if not future.done():
                            future.set_exception(error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state.status = "error"
            state.error = _short_error(error)
            state.ready.set()
        finally:
            while not state.queue.empty():
                item = state.queue.get_nowait()
                if item and item[0] and not item[0].done():
                    item[0].set_exception(ToolError(f"MCP server {name} disconnected"))
            self._unregister_tools(name)
            state.tools = []
            if state.session is not None:
                state.session = None
            if state._log is not None:
                try:
                    state._log.close()
                except Exception:
                    pass
                state._log = None
            if state.status != "error":
                state.status = "idle"

    # ── calls ─────────────────────────────────────────────────────
    async def call(self, server: str, tool: str, arguments: dict[str, Any] | None) -> str:
        state = self.states.get(server)
        if state is None:
            raise ToolError(f"Unknown MCP server: {server}")
        if not state.config.trusted:
            raise ToolError(f"MCP server {server!r} is from an untrusted workspace config")
        if state.status != "ready" or state.task is None or state.task.done():
            ok, message = await self.connect(server)
            if not ok:
                raise ToolError(message)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await state.queue.put((future, tool, arguments or {}))
        if state.task is None or state.task.done():
            if not future.done():
                future.set_exception(ToolError(state.error or f"MCP server {server} disconnected"))
        try:
            return await asyncio.wait_for(future, timeout=state.config.timeout)
        except asyncio.TimeoutError as error:
            raise ToolError(f"MCP tool {server}/{tool} timed out") from error
        except ToolError:
            raise
        except Exception as error:
            raise ToolError(f"MCP tool {server}/{tool} failed: {error}") from error

    # ── registry mirroring ────────────────────────────────────────
    def _tool_id(self, server: str, tool: str, used: set[str] | None = None) -> str:
        safe = re.sub(r"[^A-Za-z0-9_]", "_", f"mcp_{server}_{tool}").strip("_")
        candidate = safe[:64].strip("_") or "mcp_tool"
        if used is not None and candidate in used:
            import hashlib
            digest = hashlib.sha1(f"{server}:{tool}".encode("utf-8")).hexdigest()[:6]
            candidate = f"{candidate[:57]}_{digest}"
        return candidate

    def _register_tools(self, state: MCPServerState) -> None:
        self._unregister_tools(state.name)
        server = state.name
        used: set[str] = set()
        for tool in state.tools:
            tool_name = tool.name
            tool_id = self._tool_id(server, tool_name, used=used)
            used.add(tool_id)
            spec = ToolSpec(
                name=tool_id,
                description=(tool.description or f"{server} tool {tool_name}")[:900],
                parameters=tool.schema or {"type": "object", "properties": {}},
                handler=self._make_handler(server, tool_name),
                category=f"mcp · {server}",
                icon="⬡",
                source=f"mcp:{server}",
                # MCP servers can do anything; never treat them as a free read.
                risk="write",
            )
            self.registry.add(spec)

    def _make_handler(self, server: str, tool_name: str):
        async def handler(args: dict[str, Any], ctx: ToolContext) -> str:
            return await self.call(server, tool_name, args)

        return handler

    def _unregister_tools(self, server: str) -> None:
        self.registry.remove_source(f"mcp:{server}")

    def _register_meta_tools(self) -> None:
        if self.registry.get("mcp_list_servers"):
            return
        manager = self

        self.registry.add(
            ToolSpec(
                name="mcp_list_servers",
                description=(
                    "List configured MCP servers with connection status, tool counts and sources. "
                    "Use mcp_connect before calling a server's tools."
                ),
                parameters=schema({}, []),
                handler=self._meta_list,
                category="mcp",
                icon="⬡",
                source="mcp-meta",
                risk="read",
            )
        )
        self.registry.add(
            ToolSpec(
                name="mcp_connect",
                description="Connect to an MCP server by name so its tools become callable.",
                parameters=schema(
                    {"server": prop("string", "Server name from mcp_list_servers")},
                    ["server"],
                ),
                handler=self._meta_connect,
                category="mcp",
                icon="⬡",
                source="mcp-meta",
                risk="execute",
            )
        )
        self.registry.add(
            ToolSpec(
                name="mcp_call",
                description=(
                    "Call an MCP tool by server and tool name when the tool is not exposed directly. "
                    "arguments is a JSON object string."
                ),
                parameters=schema(
                    {
                        "server": prop("string", "MCP server name"),
                        "tool": prop("string", "Tool name on that server"),
                        "arguments": prop("string", "JSON object of arguments"),
                    },
                    ["server", "tool"],
                ),
                handler=self._meta_call,
                category="mcp",
                icon="⬡",
                source="mcp-meta",
                risk="write",
            )
        )

    async def _meta_list(self, args: dict[str, Any], ctx: ToolContext) -> str:
        rows = self.summary()
        if not rows:
            return "No MCP servers configured. Add one in mcp.json."
        lines = ["server            status        tools  source"]
        for row in rows:
            lines.append(
                f"{row['name'][:17]:<17} {row['status']:<13} {row['tools']:>5}  {row['source']}"
            )
            if row["error"]:
                lines.append(f"                  └ {row['error'][:120]}")
        return "\n".join(lines)

    async def _meta_connect(self, args: dict[str, Any], ctx: ToolContext) -> str:
        server = str(args.get("server") or "").strip()
        ok, message = await self.connect(server)
        if not ok:
            raise ToolError(message)
        state = self.states[server]
        tools = ", ".join(tool.name for tool in state.tools[:40]) or "(no tools)"
        return f"{message}\nTools: {tools}"

    async def _meta_call(self, args: dict[str, Any], ctx: ToolContext) -> str:
        server = str(args.get("server") or "").strip()
        tool = str(args.get("tool") or "").strip()
        state = self.states.get(server)
        if state is None:
            raise ToolError(f"Unknown MCP server: {server}")
        if state.status != "ready" or state.task is None or state.task.done():
            raise ToolError(f"MCP server {server!r} is not connected. Call mcp_connect first.")
        raw = args.get("arguments")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except ValueError as error:
                raise ToolError(f"arguments is not valid JSON: {error}") from error
        elif isinstance(raw, dict):
            parsed = raw
        else:
            parsed = {}
        return await self.call(server, tool, parsed)


def _render_result(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", "")
        if kind == "text":
            parts.append(getattr(block, "text", ""))
        elif kind == "image":
            parts.append(
                f"[image {getattr(block, 'mimeType', 'image')}, {len(getattr(block, 'data', '') or '')} base64 chars]"
            )
        elif kind == "resource":
            resource = getattr(block, "resource", None)
            text = getattr(resource, "text", None)
            uri = getattr(resource, "uri", "")
            parts.append(text if text else f"[resource {uri}]")
        elif kind == "resource_link":
            parts.append(f"[resource] {getattr(block, 'uri', '')} {getattr(block, 'name', '')}".strip())
        else:
            parts.append(str(block))
    structured = getattr(result, "structuredContent", None)
    if structured:
        try:
            parts.append(json.dumps(structured, indent=2)[:8000])
        except Exception:
            parts.append(str(structured))
    text = "\n".join(part for part in parts if part)
    if getattr(result, "isError", False):
        return "ERROR: " + (text or "tool reported an error")
    return text or "(tool returned no content)"


def _short_error(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        nested = "; ".join(_short_error(item) for item in error.exceptions)
        return nested or str(error)
    text = str(error).strip() or type(error).__name__
    return text[:300]


def _auto_connect_names() -> set[str]:
    payload = _read_json(CONFIG_DIR / "mcp.json")
    names: set[str] = set()
    for key in ("auto_connect", "autoConnect"):
        value = payload.get(key)
        if isinstance(value, list):
            names.update(str(item) for item in value)
    return names
