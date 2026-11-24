# Bug Tracker — Gemini Studio TUI

Status values: `OPEN`, `IN_PROGRESS`, `FIXED`, `VERIFIED`.

## P0 — Critical (Startup / Security / Data Loss)

| ID | Component | Summary | Status | Verification |
| --- | --- | --- | --- | --- |
| BUG-001 | `tools/builtin.py` | Command injection in `git_diff` via unquoted `path` passed to shell (`FREE_RISKS`, bypasses `readonly` and `ask`) | FIXED | `test_git_diff_uses_argv_without_shell` |
| BUG-002 | `tools/plugins.py`, `app.py` | Arbitrary Python code execution from untrusted workspace `.gemini-tui/plugins/*.py` on startup | FIXED | `test_untrusted_workspace_plugins_not_executed` |
| BUG-003 | `tools/mcp_client.py`, `app.py` | Arbitrary subprocess execution from untrusted workspace MCP configs + `mcp_call` (`write` risk) auto-connecting `execute`-level servers | FIXED | `test_mcp_untrusted_workspace_and_meta_call_requires_connect` |
| BUG-042 | `widgets/chat.py` | `AssistantMessage` overwrote Textual's internal `MessagePump._closed` flag in `stop_stream()`, causing `Widget.display` to become `False` and making every completed assistant reply vanish from the transcript | FIXED | `test_assistant_message_remains_visible_after_stop_stream` |

## P1 — High (Core Agent / Runtime / TUI Broken)

| ID | Component | Summary | Status | Verification |
| --- | --- | --- | --- | --- |
| BUG-004 | `widgets/views.py` | `TeamView.finish()` clears `_task_rows` without clearing `#team-tasks`, causing `DuplicateKey` crash | FIXED | `test_team_view_finish_does_not_raise_duplicate_key` |
| BUG-005 | `widgets/views.py` | `TeamView` `add_column()` omits `key=`, causing `ColumnDoesNotExist` (silently swallowed) on every `update_cell()` | FIXED | `test_team_view_finish_does_not_raise_duplicate_key` |
| BUG-006 | `widgets/codeblock.py` | `StudioFence` `.fence-gutter` only built in `compose()`, freezing line numbers during streaming | FIXED | `test_codeblock.py` |
| BUG-007 | `widgets/chat.py` | `AssistantMessage._flush()` clears `_pending` before checking `_markdown is not None`, silently dropping chunks | FIXED | `test_app_smoke.py` |
| BUG-008 | `tools/builtin.py` | `_resolve()` allows path traversal, symlink escape, and `os.path.expandvars` outside workspace | FIXED | `test_resolve_blocks_traversal_and_expandvars` |
| BUG-009 | `config.py` | `load_dotenv_files()` injects all keys from untrusted `cwd/.env` into `os.environ` | FIXED | Allowlist in `load_dotenv_files` |
| BUG-010 | `agent/permissions.py` | `tool_policies` `_override()` evaluated before `computer_enabled=False` and `computer_approval="readonly"` | FIXED | `test_computer_override_cannot_bypass_disabled_or_readonly` |
| BUG-011 | `tools/builtin.py` | `opencode_run` uses POSIX `shlex.quote` for Windows PowerShell (`'\''` breaks/injects) | FIXED | Uses `_run_argv` |
| BUG-012 | `tools/builtin.py`, `registry.py` | `_run_process` leaks subprocesses on `TimeoutError`/`CancelledError` and omits `await process.wait()` / invalid `cwd` handling | FIXED | `_kill_process_tree` + `_run_argv` |
| BUG-013 | `agent/computer/backend.py` | Win32 `ctypes` calls lack `argtypes`/`restype` (64-bit handle truncation, false `ShellExecuteW` errors, stuck `drag`, missing extended keys) | FIXED | `test_computer.py` |
| BUG-014 | `agent/memory.py` | `MemoryStore.search()` calls `_flush()` outside `self._lock`; `load()` reads outside lock; empty query matches all entries | FIXED | `test_memory_empty_query_does_not_match_all` |
| BUG-015 | `agent/context.py` | `ContextManager.trim()` skips over-budget short histories, can leave `function_response` at index 0, and under-reports `dropped` | FIXED | `test_context_trim_short_overbudget_and_no_leading_fn_resp` |
| BUG-016 | `orchestration/orchestrator.py` | `GeminiWorkerRunner` sends `OpenAICompatibleAdapter` model IDs to `GeminiService.run_turn` | FIXED | `test_worker_runner_routes_non_gemini_provider` |
| BUG-017 | `app.py`, `orchestration/models.py` | `OpenAICompatibleAdapter` & `ModelSpec`s registered unconditionally even when `base_url`/`api_key` missing | FIXED | Guarded with `adapter.available()` |
| BUG-018 | `orchestration/capabilities.py` | `CapabilityRegistry.for_role()` treats empty `roles=frozenset()` as wildcard, exposing `cowork_run`, `mcp_connect`, `mcp_call`, plugins to all roles | FIXED | `test_capability_registry_does_not_leak_unmapped_tools` |
| BUG-019 | `ai/client.py` | `GeminiService.run_turn()` leaves dangling `function_call` in `contents` on `CancelledError` during tool execution | FIXED | Rollback unanswered `model` content |

## P2 — Medium (Major Features Unreliable)

| ID | Component | Summary | Status | Verification |
| --- | --- | --- | --- | --- |
| BUG-020 | `widgets/chat.py`, `app.py` | `ChatView.on_scroll` never fires on `#transcript`; `_run_turn` bypasses `_scroll_to_end()` | FIXED | `_TranscriptScroll.watch_scroll_y` + `_run_turn` scroll |
| BUG-021 | `app.py`, `widgets/chrome.py` | Advertised `? help` keybinding not implemented (only `f1` bound) | FIXED | Bound `question_mark` -> `help` |
| BUG-022 | `widgets/views.py`, `chrome.py` | Blocking `psutil`, `list_windows`, and `plan_store.list` calls on Textual UI event loop | FIXED | Offloaded via `asyncio.to_thread` + glob count |
| BUG-023 | `widgets/views.py`, `codeblock.py` | Duplicate widget IDs (`#filter-row`, `#code-content`) in same screen | FIXED | Migrated `#filter-row` to `.filter-row` |
| BUG-024 | `orchestration/orchestrator.py` | `AgentTeamOrchestrator.run()` leaks worker tasks and file leases on `CancelledError`; shared `FileOwnership`/`AgentManager` leaks across runs | FIXED | `test_orchestrator.py` + `ownership.clear()` |
| BUG-025 | `tools/mcp_client.py` | `MCPManager.call()` can hang 900s on dying server; concurrent `connect()` races on `ready.clear()` | FIXED | `test_mcp_untrusted_workspace_and_meta_call_requires_connect` |
| BUG-026 | `orchestration/models.py`, `agent/cowork.py` | `GeminiProviderAdapter.complete` ignores `timeout`; `CoworkOrchestrator` planner/verifier lack timeout/retry | FIXED | Added `asyncio.wait_for` |
| BUG-027 | `tools/plugins.py` | `PluginAPI.tool()` defaults to `RISK_READ` and can overwrite builtin tools | FIXED | `test_plugin_tool_defaults_write_and_cannot_override_builtin` |
| BUG-028 | `agent/context.py`, `computer/tools.py`, `builtin.py` | Blocking `LazyIndex.search`, `computer_*` (`time.sleep`/`ImageGrab`), `sys_info`, `proc_list` on asyncio event loop | PARTIAL | `sys_info`/`proc_list` offloaded; `LazyIndex`/`computer_*` pending |
| BUG-029 | `tools/builtin.py`, `agent/code/review.py` | `rglob` traverses `SKIP_DIRS` before filtering and skips entire workspace if ancestor dir is in `SKIP_DIRS` | FIXED | Replaced with `os.walk` pruning `dirnames` |
| BUG-030 | `agent/computer/driver.py` | `ComputerDriver.run` slices `result.screenshots[-8:]` before deleting old screenshots | FIXED | Cleanup moved before slice |
| BUG-031 | `tools/builtin.py` | `fs_read` rejects `offset`/`limit` slices on files > `MAX_READ_BYTES`; `web_fetch` lacks SSRF check and streaming cap; `json_query` uncaught exceptions | FIXED | `test_fs_read_allows_slice_on_large_file`, `test_web_fetch_blocks_loopback_and_metadata` |
| BUG-032 | `config.py`, `code/index.py`, `agent/plan.py`, `ai/sessions.py` | Unvalidated JSON deserialization (`Settings.load` can overwrite methods or corrupt types; `CodeIndex.load`/`PlanStore.list`/`Session.from_dict` crash on bad shapes) | FIXED | `test_settings_merge_ignores_methods_and_bad_types`, `test_session_store_handles_corrupt_json_and_empty_id` |
| BUG-033 | `agent/audit.py`, `tools/mcp_client.py`, `ai/sessions.py` | Secret redaction gaps (`sk-proj-`, `gho_`, `AKIA`, etc.), `AuditLog.record` writes outside lock, `mcp-*.log` unredacted | PARTIAL | `audit.py` fixed; `mcp-*.log` & `sessions.py` redaction pending |

## P3 — Low (UI / Usability / Minor Defects)

| ID | Component | Summary | Status | Verification |
| --- | --- | --- | --- | --- |
| BUG-034 | `app.py` | Composer cleared *after* async slash command completes, wiping user input typed in the meantime | FIXED | Cleared before `run_command` |
| BUG-035 | `widgets/chat.py` | `Composer._paste_os` uses private `_replace_via_keyboard` API | FIXED | Uses public `replace()` |
| BUG-036 | `widgets/overlays.py`, `chrome.py` | `SplashPanel` and `StatusBar` ignore `animations=False` for idle ticks | FIXED | Guarded idle repaints |
| BUG-037 | `widgets/views.py`, `overlays.py`, `app.py` | Swallowed exceptions (`save_session`, `_boot` MCP), missing `refresh_chrome()` in `ApprovalPanel._resolve`, silent `team-copy` | PARTIAL | `ApprovalPanel._resolve` & `team-copy` fixed |
| BUG-038 | `tools/builtin.py` | `clipboard_write` defaults to `RISK_READ`; `_safe_eval` allows CPU/memory DoS (`factorial`/`Pow`/`LShift`) | FIXED | `test_clipboard_write_is_write_risk`, `test_safe_eval_bounds_factorial_and_power` |
| BUG-039 | `tools/skills.py` | Project-local skills cannot override `gemini-tui` skills; `_parse_frontmatter` drops multiline/quoted lists | FIXED | Project override + quote stripping |
| BUG-040 | `tools/mcp_client.py` | `MCPServerConfig.from_raw` ignores `timeout` on `http`/`sse`, crashes on non-numeric `timeout`; `_tool_id` truncates without collision check | OPEN | — |
| BUG-041 | `agent/cowork.py`, `ai/client.py`, `ai/models.py` | Dead `on_event`, stale `EXCLUDED_WORKER_TOOLS`, `thoughts` not cleared on retry, `ModelInfo.family` `IndexError` on `"gemini-"` | OPEN | — |
