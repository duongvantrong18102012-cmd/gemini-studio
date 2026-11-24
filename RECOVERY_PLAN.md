# Recovery Plan — Gemini Studio TUI

## Baseline (2026-11-23)
- `pytest -q`: **255 passed in 50.72s** (1 `pytest_asyncio` loop-scope deprecation warning).
- `python tui.py --version` / `--help`: exits 0 (`Gemini Studio 1.0.0`).
- No live `GEMINI_API_KEY` configured in environment; live Gemini/OpenAI-compatible calls are not exercisable without a key, so provider paths are verified with unit/mock tests and static/runtime checks.

## Phases & Priorities

### Phase 1 — P0 Security & Startup Integrity
1. **Eliminate `git_diff` command injection** (`builtin.py`): switch `git_*` and `opencode_run` to `argv`-based `create_subprocess_exec` (`_run_argv`) instead of shell string interpolation / POSIX `shlex.quote` on PowerShell.
2. **Gate untrusted workspace plugins & MCP configs** (`plugins.py`, `mcp_client.py`, `app.py`): require explicit workspace trust (or opt-in) before loading `.gemini-tui/plugins/*.py` or auto-connecting workspace-local MCP servers (`.mcp.json`, `.vscode/mcp.json`, `opencode.json*`); gate `mcp_call` when spawning an unconnected server.
3. **Restrict workspace `.env` auto-loading** (`config.py`): only load allowlisted API key variables from `Path.cwd() / ".env"` so untrusted repos cannot inject `HTTP_PROXY`, `PYTHONSTARTUP`, `PATH`, etc.

### Phase 2 — P1 Core Agent, Permissions, Runtime & TUI Fixes
1. **TUI `TeamView` crashes & dead updates** (`widgets/views.py`): add column keys to `#team-tasks` and `#team-agents`, and clear `#team-tasks` in `TeamView.finish()` before repopulating (prevents `DuplicateKey` crash and silent `ColumnDoesNotExist`).
2. **Streaming & CodeBlock fixes** (`widgets/chat.py`, `widgets/codeblock.py`, `app.py`):
   - Fix `AssistantMessage._flush()` so `_pending` is not discarded when `_markdown` is not yet mounted.
   - Update `.fence-gutter` dynamically in `StudioFence.set_content()`.
3. **Permission & Path Safety** (`agent/permissions.py`, `tools/builtin.py`, `tools/plugins.py`):
   - Enforce `computer_enabled` and `computer_approval == "readonly"` *before* `_override()` in `PermissionPolicy.decide`.
   - Enforce workspace boundary & symlink resolution in `_resolve` (with explicit opt-in/override if needed) and remove `os.path.expandvars`.
   - Default `PluginAPI.tool` to `RISK_WRITE` (unless explicit `risk` is given) and prevent overwriting builtin tools.
4. **Orchestration & Model Routing** (`orchestration/orchestrator.py`, `orchestration/capabilities.py`, `ai/client.py`, `app.py`):
   - Fix `CapabilityRegistry.for_role` so unmapped/unsafe tools (`cowork_run`, `mcp_connect`, `mcp_call`, plugins) are not exposed to every role via empty `roles=frozenset()`.
   - Guard `_register_providers` so unavailable `OpenAICompatibleAdapter`s are not registered, and make `GeminiWorkerRunner` provider-aware (or route text-only tasks via `ModelRegistry.provider(...).complete(...)`).
   - Fix `GeminiService.run_turn` dangling `function_call` in `contents` on `CancelledError` during tool execution; clear `thoughts` across retries.
5. **Process, Memory, Context & Win32 Reliability** (`tools/builtin.py`, `tools/registry.py`, `agent/memory.py`, `agent/context.py`, `agent/computer/backend.py`):
   - Kill process tree and `await process.wait()` on timeout and `CancelledError` in `_run_process`/`_run_argv`; catch `OSError` on invalid `cwd`.
   - Fix `MemoryStore.search` flushing outside `self._lock` and `load()` outside lock; guard empty `query_tokens` in `_score()`.
   - Fix `ContextManager.trim` over-budget short histories, leading `function_response` invariant, and `TrimReport` counts.
   - Add `argtypes`/`restype` to Win32 `ctypes` calls in `WindowsBackend`, wrap `drag` in `try...finally`, fix `KEYEVENTF_EXTENDEDKEY`.

### Phase 3 — P2 Reliability, Event-Loop Offloading, MCP & Hardening
1. **TUI event-loop & UX fixes** (`widgets/chat.py`, `widgets/views.py`, `widgets/chrome.py`, `widgets/overlays.py`, `app.py`):
   - Fix `ChatView` auto-follow scroll detection & streaming scroll-to-end.
   - Offload blocking `psutil` (`DashboardView`), `list_windows` (`ComputerView`), and `plan_store.list` (`Sidebar`) from the event loop.
   - Bind `?` to `action_help` (or align UI hints), fix duplicate `#filter-row` IDs, call `refresh_chrome()` in `ApprovalPanel._resolve()`.
2. **MCP, Cowork & Team lifecycle** (`tools/mcp_client.py`, `agent/cowork.py`, `orchestration/orchestrator.py`):
   - Fix `MCPManager.call()` 900s hang on dying server task and `connect()` concurrent `ready.clear()` race; parse `timeout` for `http`/`sse`.
   - Clean up workers and file leases on `CancelledError` in `AgentTeamOrchestrator.run`; scope `FileOwnership`/`AgentManager` per run.
   - Add `asyncio.wait_for` to `GeminiProviderAdapter.complete` and `CoworkOrchestrator` planner/verifier.
3. **Security & Data Hardening** (`tools/builtin.py`, `agent/audit.py`, `ai/sessions.py`, `config.py`, `agent/code/review.py`, `agent/computer/driver.py`, `agent/computer/tools.py`):
   - Add SSRF validation and streaming size limit in `web_fetch`; bound `calc` (`_safe_eval`).
   - Expand `audit.py` secret redaction patterns/keys and move writes inside `self._lock`.
   - Validate `Settings.load()` fields (dataclass fields + types) and `Session.from_dict()`/`CodeIndex.load()`/`TaskPlan.from_dict()`.
   - Replace `rglob` + `part in SKIP_DIRS` with `os.walk` pruning relative paths; fix `fs_read` slice check on large files; fix `ComputerDriver.run` screenshot cleanup order.

### Phase 4 — P3/P4 Polish, Test Expansion & Documentation Sync
1. Expand unit/integration/security tests for `builtin.py`, `mcp_client.py`, `plugins.py`, `sessions.py`, `config.py`, `TeamView`, `StudioFence`, `ContextManager`, and `MemoryStore`; isolate `GEMINI_TUI_HOME` in `test_app_smoke.py`.
2. Update `docs/ARCHITECTURE.md`, `docs/LIMITATIONS.md`, `ARCHITECTURE.md`, and `TEST_REPORT.md` to reflect actual verified state.
