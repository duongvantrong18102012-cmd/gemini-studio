# Development Changelog — Gemini Studio TUI

## [2026-11-23] Hotfix — Assistant Reply Vanishing After Stream End (`BUG-042`)
- Renamed `AssistantMessage._closed` to `AssistantMessage._stream_closed` in `gemini_tui/widgets/chat.py` so `stop_stream()` no longer collides with Textual's internal `MessagePump._closed` attribute (which caused `Widget.display` to return `False` and hide completed replies).
- Added regression test `test_assistant_message_remains_visible_after_stop_stream` in `tests/test_recovery_fixes.py`.

## [2026-11-23] Cycle 2 — Event-Loop Offloading, Traversal & UI Polish
- Offloaded `DashboardView._update_fast`, `DashboardView._update_processes`, and `ComputerView.refresh_state` blocking `psutil`/Win32 calls to `asyncio.to_thread` (`gemini_tui/widgets/views.py`).
- Replaced `Sidebar.update_nav` synchronous `plan_store.list(50)` JSON reads with lightweight `*.json` glob count (`gemini_tui/widgets/chrome.py`).
- Migrated duplicate `#filter-row` IDs across 5 views to `.filter-row` (`gemini_tui/widgets/views.py`, `gemini_tui/theme.py`).
- Avoided redundant idle repaints in `SplashPanel._tick` and `StatusBar._tick` when `animations=False` (`gemini_tui/widgets/overlays.py`, `gemini_tui/widgets/chrome.py`).
- Replaced `rglob` + ancestor `SKIP_DIRS` check in `review.py:_walk` with `os.walk` pruning `dirnames` (`gemini_tui/agent/code/review.py`).
- Allowed project-local skills to override `gemini-tui` skills and stripped quotes from `allowed-tools` entries (`gemini_tui/tools/skills.py`).

## [2026-11-23] Cycle 1 — P0 Security, P1 Core/TUI & P2 Hardening
- **Security (P0/P1):**
  - Replaced shell string interpolation in `git_status`, `git_diff`, `git_log`, and `opencode_run` with `_run_argv` (`create_subprocess_exec`, no shell); added `_kill_process_tree` on timeout/cancellation (`gemini_tui/tools/builtin.py`).
  - Enforced workspace boundary & symlink resolution in `_resolve` and removed `os.path.expandvars` (`gemini_tui/tools/builtin.py`).
  - Restricted `load_dotenv_files()` on workspace `.env` to API-key allowlist and added `Settings.merge` field/type validation + `trusted_workspaces` (`gemini_tui/config.py`).
  - Gated workspace-local `.gemini-tui/plugins/*.py` and workspace-local MCP configs behind workspace trust; prevented plugin override of builtin tools and defaulted plugin tools to `write` risk (`gemini_tui/tools/plugins.py`, `gemini_tui/tools/mcp_client.py`, `gemini_tui/app.py`).
  - Enforced `computer_enabled` and `computer_approval="readonly"` before `tool_policies` overrides (`gemini_tui/agent/permissions.py`).
  - Added SSRF public-IP validation & streaming byte cap to `web_fetch`, bounded `calc` (`factorial`/`Pow`/`LShift`), marked `clipboard_write` as `write` risk, and added `risk` param to `ToolRegistry.register` (`gemini_tui/tools/builtin.py`, `gemini_tui/tools/registry.py`).
  - Expanded `audit.py` secret redaction patterns/keys and moved JSONL writes inside `self._lock` (`gemini_tui/agent/audit.py`).
- **Core Agent & Orchestration (P1/P2):**
  - Fixed `CapabilityRegistry.for_role` so unmapped tools (`cowork_run`, `mcp_connect`, `mcp_call`) are not exposed to all roles (`gemini_tui/orchestration/capabilities.py`).
  - Guarded `_register_providers` with `adapter.available()` and made `GeminiWorkerRunner` provider-aware for non-Gemini adapters (`gemini_tui/app.py`, `gemini_tui/orchestration/orchestrator.py`).
  - Fixed `GeminiService.run_turn` dangling `function_call` on `CancelledError` and thought duplication across retries (`gemini_tui/ai/client.py`).
  - Fixed `AgentTeamOrchestrator.run` worker/lease cleanup on `CancelledError` and cleared `FileOwnership` per run (`gemini_tui/orchestration/orchestrator.py`, `gemini_tui/orchestration/ownership.py`).
  - Added `asyncio.wait_for` to `GeminiProviderAdapter.complete` and `CoworkOrchestrator._model_plan`; fixed `EXCLUDED_WORKER_TOOLS` and `worker_registry` empty-request handling (`gemini_tui/orchestration/models.py`, `gemini_tui/agent/cowork.py`).
  - Fixed `MemoryStore.search` lock/flush race and empty-query matching (`gemini_tui/agent/memory.py`).
  - Fixed `ContextManager.trim` short over-budget trimming and `function_response` head invariant (`gemini_tui/agent/context.py`).
  - Fixed `MCPManager` `connect` `ready` race, `call` dead-task hang, `http`/`sse` timeout parsing, `_tool_id` collisions, and `_meta_call` requiring connected server (`gemini_tui/tools/mcp_client.py`).
  - Added `argtypes`/`restype` to Win32 `ctypes` calls, `KEYEVENTF_EXTENDEDKEY`, and `drag` `try...finally` (`gemini_tui/agent/computer/backend.py`); fixed `ComputerDriver.run` screenshot cleanup order (`gemini_tui/agent/computer/driver.py`).
  - Hardened `Session.from_dict`, `SessionStore`, and `CodeIndex.load` (`gemini_tui/ai/sessions.py`, `gemini_tui/agent/code/index.py`).
- **TUI & Usability (P1/P2/P3):**
  - Fixed `TeamView` `DataTable` column keys and `finish()` `DuplicateKey` crash (`gemini_tui/widgets/views.py`).
  - Fixed `StudioFence` dynamic `.fence-gutter` updates during streaming (`gemini_tui/widgets/codeblock.py`).
  - Fixed `AssistantMessage._flush` dropping pending chunks when `_markdown` is not yet mounted, `ChatView` reactive scroll-follow, and `Composer._paste_os` public `replace()` (`gemini_tui/widgets/chat.py`).
  - Bound `?` to `action_help`, cleared composer before async slash commands, scrolled transcript during `_run_turn`, and refreshed chrome in `ApprovalPanel._resolve` (`gemini_tui/app.py`, `gemini_tui/widgets/overlays.py`).
- **Tests:**
  - Added `tests/test_recovery_fixes.py` (17 regression & security tests).
- Ran full test suite: `pytest -q` -> **255 passed in 50.72s**.
- Verified CLI entry point: `python tui.py --version` (`Gemini Studio 1.0.0`) and `python tui.py --help`.
- Completed 4-way parallel audit (Architecture/Integration, Interface/TUI, Runtime/Tools, Security/QA).
- Created `RECOVERY_PLAN.md`, `BUG_TRACKER.md`, `ARCHITECTURE.md`, and `CHANGELOG_DEV.md`.
