# Architecture — Gemini Studio TUI

*Verified against actual codebase on 2026-11-23.*

## Entry Point & CLI
- [`tui.py`](tui.py): CLI argument parser (`-w/--workspace`, `-m/--model`, `--no-splash`, `--approval`, `--no-animations`, `--computer`, `--no-memory`, `--no-audit`, `--version`), loads `.env` via `load_dotenv_files()`, constructs `GeminiStudio`, and runs `app.run()`.
- [`gemini_tui/config.py`](gemini_tui/config.py): Resolves `CONFIG_DIR` (`GEMINI_TUI_HOME` or `~/.config/gemini-tui`), creates subdirectories (`sessions`, `cache`, `plugins`, `skills`, `logs`, `cowork`, `team`), manages `Settings` (`settings.json`) and API key discovery (`GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_GENAI_API_KEY`, `GEMINI_TUI_API_KEY`).

## Layers
1. **Textual UI (`gemini_tui/app.py`, `gemini_tui/widgets/`)**
   - `GeminiStudio(App)` mounts 10 views inside a `ContentSwitcher`: `ChatView`, `ModelsView`, `ToolsView`, `MCPView`, `SkillsView`, `PluginsView`, `DashboardView`, `TeamView`, `CoworkView`, `ComputerView`.
   - `chrome.py`: `HeaderBar`, `Sidebar`, `StatusBar`.
   - `overlays.py`: `SplashPanel`, `PalettePanel`, `ApprovalPanel`, `HelpPanel`, `ModelPicker`, `SessionPicker`, `TeamRolesPanel`.
   - `chat.py` & `codeblock.py`: `Composer`, `AssistantMessage` (50 ms token batching, 150 ms thought throttling), `ToolCard`, `StudioFence` (custom Pygments fence with copy & line gutter).
   - `clipboard.py`: `ClipboardManager` using `pyperclip` / native OS helpers via `asyncio.to_thread` + best-effort OSC 52.
   - `commands.py`: `CommandRegistry` with slash commands and fuzzy search.

2. **AI & Session Layer (`gemini_tui/ai/`)**
   - `client.py`: `GeminiService` wrapping `google.genai.Client` (`aio`), streaming text/thoughts, executing function-call loops (up to `MAX_TOOL_STEPS = 16`), and retrying on `429`/`5xx`.
   - `models.py`: `ModelInfo` catalogue, fallback models, and cached model list (`cache/models.json`).
   - `sessions.py`: `Session`, `Message`, `Usage`, and `SessionStore` (`sessions/*.json`).

3. **Agent Runtime (`gemini_tui/agent/`)**
   - `context.py`: `ContextManager` (char-heuristic token budgeting and history trimming) + `Retriever` (lexical + optional embedding search over `LazyIndex`).
   - `memory.py`: `MemoryStore` (`memory.json`, scoped `session`/`workspace`/`global`).
   - `permissions.py`: `PermissionPolicy` (`read`/`network`/`write`/`execute`/`screen`/`computer` risks, `allow`/`ask`/`deny`, `readonly` hard boundary, role ceilings, computer rate limits).
   - `audit.py`: `AuditLog` (`logs/audit-*.jsonl` with regex/key secret redaction).
   - `plan.py`: `PlanStore` and `TaskPlan` (`cowork/*.json`).
   - `cowork.py`: `CoworkOrchestrator` (sequential planner -> scoped worker -> verifier -> report).
   - `code/`: `CodeIndex` (AST + regex symbol indexer), `review.py` (deterministic static findings), `tools.py` (`code_*` tools).
   - `computer/`: `WindowsBackend` (`ctypes` Win32 + `PIL.ImageGrab`), `ComputerDriver`, `tools.py` (`computer_*` tools).

4. **Multi-Agent Orchestration (`gemini_tui/orchestration/`)**
   - `orchestrator.py`: `AgentTeamOrchestrator` (planner -> DAG `TaskScheduler` -> parallel workers via `GeminiWorkerRunner` -> `FileOwnership` leases -> verifier -> Markdown report).
   - `models.py`: `ModelRegistry`, `ModelRouter`, `GeminiProviderAdapter`, `OpenAICompatibleAdapter`.
   - `capabilities.py`: `CapabilityRegistry` mapping builtin tools, MCP servers, and skills to team roles.
   - `manager.py`: `AgentManager` tracking live worker state and logs.

5. **Tools, MCP, Skills & Plugins (`gemini_tui/tools/`)**
   - `registry.py`: `ToolRegistry`, `ToolSpec`, `ToolContext`.
   - `builtin.py`: Filesystem (`fs_*`), shell (`shell_run`), git (`git_*`), web (`web_*`), system (`sys_info`, `proc_list`), `calc`, `json_query`, `clipboard_*`, `opencode_run`.
   - `mcp_client.py`: `MCPManager` (`stdio`, `http`, `sse` transports) and meta-tools (`mcp_*`).
   - `skills.py`: `SkillLibrary` (`SKILL.md` frontmatter discovery).
   - `plugins.py`: `PluginManager` (native Python plugins + external opencode/claude detection).

## Known Architectural Gaps (Being Addressed)
- `GeminiWorkerRunner` currently only invokes `GeminiService.run_turn`, even when `ModelRouter` selects an `OpenAICompatibleAdapter` model for non-tool roles.
- Workspace-local plugins, MCP configs, and `.env` files are loaded without a workspace-trust gate.
- Several UI and tool paths still execute blocking OS/disk/psutil calls on the asyncio event loop.
