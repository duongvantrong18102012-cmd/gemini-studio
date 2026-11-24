# Architecture

Gemini Studio is a Textual application over a plain-Python agent layer. The UI,
the Gemini client and the agent runtime are separate: the team orchestrator does
not import Textual, and the computer backend does not import the UI. That is what
makes the core testable without a terminal or a network.

## Layer map

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Textual UI                                                           │
│  app.py · widgets/chat.py · widgets/codeblock.py · widgets/views.py  │
│  widgets/chrome.py · widgets/overlays.py                             │
└───────────────▲────────────────────────────────▲─────────────────────┘
                │ events / tool context          │ commands
┌───────────────┴────────────────────────────────┴─────────────────────┐
│ Agent runtime                                                        │
│  orchestration/  supervisor → DAG → workers → verifier → report      │
│  agent/cowork.py  planner → scoped workers → verifier                │
│  agent/computer/  screenshot → action → verify                       │
│  agent/context.py budget, trimming, retrieval                        │
│  agent/memory.py  scoped notes                                       │
│  agent/audit.py   append-only audit                                  │
│  agent/permissions.py  allow / ask / deny + role ceilings            │
└───────────────▲────────────────────────────────▲─────────────────────┘
                │ declarations / calls           │ results
┌───────────────┴────────────────────────────────┴─────────────────────┐
│ Tool layer                                                           │
│  tools/registry.py  (ToolSpec, ToolContext, call path)               │
│  tools/builtin.py · agent/code/tools.py · agent/computer/tools.py    │
│  tools/mcp_client.py · tools/skills.py · tools/plugins.py            │
└───────────────▲────────────────────────────────▲─────────────────────┘
                │                                │
┌───────────────┴────────────┐      ┌────────────┴─────────────────────┐
│ Gemini (google-genai)      │      │ OS / workspace                   │
│ ai/client.py ai/models.py  │      │ Win32 ctypes · Pillow · git      │
│ clipboard.py native helpers│      │ system clipboard · OSC 52        │
└────────────────────────────┘      └──────────────────────────────────┘
```

The ten views are mounted once inside a `ContentSwitcher`: Chat, Models, Tools,
MCP, Skills, Plugins, Dashboard, Agent Team, Cowork and Computer. Switching a
view is a compositor update, not a remount. `ctrl+b` hides the sidebar; layout
classes (`-compact`, `-narrow`, `-short`, `-stacked`) adapt to terminal size.

## A chat turn

1. `GeminiStudio._handle_submit` starts `_run_turn`.
2. `_run_turn` asks `_ambient_context(prompt)` for a compact context block
   (project brief, memory digest, retrieved code, workspace fingerprint).
3. History is trimmed by `ContextManager.trim`. The trimmer never leaves a
   function response at the head of the history, so the API request stays valid.
4. `GeminiService.run_turn` streams text/thoughts and executes function calls.
5. Every tool call goes through `ToolRegistry.call`, which applies
   `PermissionPolicy`, optional human approval, then runs the handler and writes
   one audit record in `finally`.
6. The assistant message, usage and tool cards are rendered by `ChatView`.
   Ambient context is sent as a `system_suffix` and is **not** persisted into the
   transcript.

`ContextManager` keeps a token estimate, not a tokenizer. It is intentionally
conservative and only used for budgeting. `/context` reports real usage numbers
from the same estimate.

### Streaming and repaint budget

`AssistantMessage.write` appends to `answer_text` and a pending list. A single
flush task is created; it joins pending text, clears it, and writes at most
12,000 characters per stream call, then sleeps 50 ms. A burst of tokens therefore
produces one repaint window, not one per token. Thoughts are throttled to at most
one update per 150 ms unless the accumulated length crosses a boundary.
`stop_stream` performs a final drain and then stops the markdown updater.

`ToolCard` runs a 250 ms spinner timer only while its status is `running`;
`_tick` stops and clears the timer on the first tick after completion. The
Dashboard's two intervals check `BaseView.visible()` before touching `psutil`, so
a hidden Dashboard cannot wake the event loop to sample hardware.

## Clipboard and code blocks

`clipboard.py` exists because Textual's built-in clipboard is an in-app string
plus an OSC 52 write, not the operating-system clipboard. The module tries:

1. `pyperclip` (ctypes on Windows, pbcopy on macOS, xclip/xsel on Linux);
2. platform helpers: PowerShell `Get-Clipboard`/`Set-Clipboard`, `pbpaste` /
   `pbcopy`, `wl-paste`/`wl-copy`, `xclip`, `xsel`.

Every helper runs without a shell and with a 2.5 s timeout. `read_clipboard_async`
and `write_clipboard_async` wrap the blocking calls in `asyncio.to_thread`, so a
slow PowerShell start cannot freeze the UI. The app's copy action runs
`ClipboardManager.copy_async` inside a Textual worker, so a key handler returns
immediately and the toast reports the result when the OS call finishes.
`ClipboardManager` then emits OSC 52 through Textual as an additional
best-effort path. `ClipboardResult` records `ok`, `method` and `error`;
`copy_text` reports those exact values. There is no silent fallback that claims
the OS clipboard was written.

`widgets/codeblock.py` registers `StudioFence` as the `Markdown` fence block. It
keeps MarkdownFence's streaming contract and adds a crimson Pygments theme, a
language label (unknown languages become `TEXT`), line count, optional line
numbers up to 400 lines, a clickable `COPY` control, focus bindings
(`ctrl+shift+c`, `enter`), and a capped height with scrolling. Copying delegates
to `app.copy_text`, so it uses the same real-clipboard path as the rest of the
UI.

The splash in `widgets/overlays.py` is a custom terminal sigil in three tiers
(full, compact, one-line), selected from the real terminal size. Block elements
are used when stdout can encode them; cp1252/cp437 or `GEMINI_TUI_ASCII=1` get a
pure ASCII drawing. Any key dismisses it.

## Cowork

`agent/cowork.py` contains `CoworkOrchestrator`. Its dependencies are injected:

```python
CoworkOrchestrator(
    service=gemini_service,
    registry=tool_registry,
    settings=settings,
    workspace=workspace,
    memory=memory_store,
    retriever=retriever,
    audit=audit_log,
    emit=event_sink,
    planner=None,   # default: model JSON planner
    worker=None,    # default: scoped model worker / computer driver
    verifier=None,  # default: model verifier
)
```

Flow:

1. **Context** — project brief + memory digest + retrieval + index fingerprint.
2. **Plan** — the model returns JSON; `plan_from_model_json` tolerates fences
   and prose, normalises agent names and tool lists, caps the step count and
   rejects unusable plans. `PlanStore` persists every state change.
3. **Scoped execution** — for each step, `worker_registry` intersects the
   tools the planner asked for with the role's allow-list (`ROLE_TOOLS`) and
   removes unsafe/recursive tools (`cowork_run`, `computer_task`, …). A worker
   cannot call a tool outside its scope: `ScopedRegistry.call` rejects it.
4. **Verification** — only when no step failed. The verifier returns
   `ok|issues` plus follow-ups. A failed step skips verification and fails the
   plan.
5. **Report** — a Markdown report is written to `COWORK_DIR` and the result is
   emitted as `done`.

Cancellation is cooperative: `cancel()` sets a flag checked between steps and
inside the worker event loop. A cancelled run is persisted as `cancelled`, not
silently dropped.

Computer steps use `_computer_worker`, which delegates to the vision driver.
The driver still calls tools through the normal registry, so policy, approval,
audit and rate limiting are not bypassed.

## Agent team

`orchestration/` is a separate layer; Cowork remains untouched and keeps its API
and tests. `AgentTeamOrchestrator` takes injected collaborators:

```python
AgentTeamOrchestrator(
    settings=settings,
    router=model_router,
    registry=tool_registry,
    capabilities=capability_registry,
    ownership=file_ownership,
    manager=agent_manager,
    runner=GeminiWorkerRunner(...),
    completer=None,
    emit=event_sink,
    report_dir=TEAM_DIR,
    verify=True,
    max_parallel=2,
    default_timeout=300,
    default_attempts=2,
)
```

Flow:

1. **Plan** — `plan_goal` asks the planner-capable model for a JSON task graph
   (1–8 tasks). `plan_from_json` normalises ids, roles, scopes, dependencies,
   timeouts and attempts; unknown dependencies and cycles are rejected before
   execution. One retry is allowed; on a provider/API error or invalid JSON the
   orchestrator falls back to `heuristic_plan` unless fallback is disabled.
2. **Discover** — `CapabilityRegistry.from_components` reports the tools,
   skills, MCP tools and plugins that actually exist. The planner prompt sees
   this catalogue. Discovery is not permission: calls still pass through the
   registry and `PermissionPolicy`.
3. **Schedule** — `TaskScheduler` holds the DAG, ready/running maps, bounded
   attempts and terminal states. `skip_dependents` marks pending downstream
   tasks `skipped` when a dependency fails. Progress is terminal tasks / total.
4. **Route** — `ModelRouter` scores models by required and preferred
   capabilities, a per-role override, speed, cost and context. A failed model is
   cooled down for 90 s, so a retry can use another capable model. Developer,
   Tester and Debugger tasks require `supports_tools=True`; a model that merely
   advertises the capability is rejected if its adapter cannot execute tools.
5. **Execute** — ready tasks fill free slots up to `max_parallel` (1–4). Each
   worker runs under `asyncio.wait_for(task.timeout_s)` and calls
   `request.checkpoint()` before each model turn, which blocks while paused and
   raises on cancellation. A worker must return non-empty text; empty output is
   a failure, not a success.
6. **Own** — before launch, declared scope paths are pre-claimed. Builtin write
   tools claim a lease and record a `ChangeRecord`; a second task cannot
   overwrite the same normalized path. Leases are released after success,
   failure or cancellation. A completed task writes a checkpoint. Normalized
   keys are case-insensitive on Windows.
7. **Retry** — a worker exception marks the model failed and asks the scheduler
   for a retry while attempts remain. Configuration errors (no capable model,
   missing tool support, ownership conflict) are terminal and do not retry.
8. **Verify** — a verifier model receives task status, acceptance criteria,
   recorded evidence (capped at 9,000 chars) and the change list. `pass` sets
   `verified`; `fail`/`partial` downgrade an otherwise complete run to
   `partial`; a verifier error yields `not run`, never a fake pass. Cancellation
   skips verification.
9. **Report** — Markdown + JSON include tasks, models, attempts, errors, agent
   states, leases, changes, checkpoints and honest limits. Files go to
   `~/.config/gemini-tui/team/`.

The workflow is role-based: Architect/Researcher/Planner read and design,
Developer implements, Tester verifies, Debugger isolates failures, Security and
Writer review or document. It is not a per-file phase state machine.

### Roles and ceilings

| Role | Ceiling | Tool scope |
| --- | --- | --- |
| Architect | `read` | read-only inspection |
| Researcher | `read` | read + web |
| Planner | `read` | read-only inspection |
| Developer | `computer` | read/write/execute + computer tools |
| Debugger | `execute` | read/write/execute |
| Tester | `execute` | read/write/execute |
| Security | `write` | read + shell + limited writes |
| Writer | `write` | read + writes |

Ceilings are ceilings, not grants: a role still needs a policy `allow`/`ask`
decision for the tool it calls.

### Models and providers

`orchestration/models.py` has a provider-independent `ModelSpec` and a
`ModelRegistry`. `build_registry` syncs the Gemini catalogue from the existing
model list. `GeminiProviderAdapter` uses `GeminiService.client()` for text and
JSON completions; it is available only when a key exists.

`OpenAICompatibleAdapter` is registered only when `settings.provider_endpoints`
contains both a base URL and a key (or key env var). Its models declare
`supports_tools=False`, and the UI labels them text/planning-only. The
orchestrator refuses Developer/Tester/Debugger tool tasks on such a model. This
is an honest capability limit, not a silent degradation.

### Live state

`AgentManager` holds each agent's role, model, task id, state, progress, tool
count, elapsed time, summary/error and bounded log. Pause and cancel are
cooperative: they take effect at task boundaries or before the next model call,
never in the middle of a tool write. `pause_all`/`cancel_all` set team-wide
flags; individual pause/cancel intentionally do not. `TeamView` renders exactly
this state plus the scheduler's real task table and event log. The final
verification/error stays in the log pane until the user selects an agent row and
presses `enter`; a passive row highlight cannot replace it.

Team events are plain dicts (`team_plan`, `team_start`, `team_task`,
`team_agent_log`, `team_done`). They are separate from Cowork's `AgentEvent`
stream, and both views keep bounded deques.

## Computer use

`agent/computer/backend.py` is a thin, honest Win32 layer:

- `WindowsBackend` uses `ctypes` (`user32`, `shell32`) and Pillow `ImageGrab`.
- It enables per-monitor DPI awareness when the OS supports it and reports a
  warning when Pillow's captured size differs from Win32 metrics.
- Input methods (`click`, `move`, `drag`, `scroll`, `type_text`, `press`,
  `open_target`) and observation methods (`screen_info`, `list_windows`,
  `focus_window`, `capture`) are separate.
- `ScreenshotOnlyBackend` is the non-Windows fallback: screenshots if Pillow can
  take them, input operations raise.
- `ascii_preview` converts real pixels to a monochrome preview so the model and
  the terminal can inspect an image cheaply.

`agent/computer/driver.py` runs the vision loop:

```text
capture → model sees PNG + action log → returns one call
        → registry.call (policy/approval/audit/guard)
        → capture again → repeat
```

The loop stops on `DONE:`/`BLOCKED:`, a step cap, or an error. It never invents
a screenshot or a successful action.

`ComputerGuard` is a separate safety layer: it checks that control is enabled,
increments a per-minute action counter and enforces a rate limit. The policy
decides **whether** an action is allowed; the guard prevents runaway loops.

## Permissions and risk

`PermissionPolicy.decide(spec, ceiling=None)` returns a
`PermissionVerdict(mode, risk, reason)`. Risk classes:

| Risk | Default | Examples |
| --- | --- | --- |
| `read` | allow | `fs_read`, `git_status`, `code_search`, `computer_status` |
| `network` | allow | `web_search`, `web_fetch` |
| `write` | ask | `fs_write`, `fs_edit`, `code_pack` |
| `execute` | ask | `shell_run`, `code_test` |
| `screen` | allow once computer is enabled | `computer_screenshot` |
| `computer` | ask, deny if disabled | `computer_click`, `computer_type`, `computer_task` |

Resolution order:

1. `settings.denied_tools` → deny.
2. Global `readonly` → deny any risk above `network` (hard deny; overrides
   cannot upgrade it).
3. `settings.tool_policies[name|category|risk]` → explicit allow/ask/deny.
4. Role ceiling: if the decision would be `allow` above the role's ceiling, it
   becomes `ask` with a reason. A ceiling never upgrades a deny.
5. `screen` → deny unless computer control is enabled.
6. `computer` → deny unless enabled; use `settings.computer_approval`.
7. `read`/`network` → allow.
8. `write`/`execute` → `auto` allows, otherwise ask.

MCP mirrored tools are classified `write` (their effects are unknown), while
`mcp_list_servers` is `read`, `mcp_connect` is `execute` and `mcp_call` is
`write`. Approval is handled by `ToolRegistry.call`; the UI attaches
`approve_tool`. Approval decisions are audited separately from tool results.

The team passes `risk_ceiling` through `ToolContext.extras`, along with the
ownership table, lease identity and task/goal metadata. A worker's plan cannot
raise its ceiling.

## Memory and context

`MemoryStore` stores one JSON document under `CONFIG_DIR/memory.json`:

```json
{"entries": {"key": {"value": "...", "scope": "project", "tags": [], "workspace": "...", "hits": 0}}}
```

- `global` notes are always eligible.
- `project` notes match the current workspace.
- `session` notes match the current workspace too, but are intended to be short
  lived; the store does not delete them automatically.
- `search` scores key matches, tag matches, value matches, scope and recency.
- `digest` returns a compact block for ambient context.
- Legacy `cache/memory.json` (flat dictionary) is migrated on first load.

`Retriever` uses embeddings when `GEMINI_API_KEY` is available, but always has a
lexical fallback. `LazyIndex` exposes `CodeStore` to the retriever without
building the index at startup. That is why a large workspace does not delay
boot.

## Events

Cowork/computer agents emit `AgentEvent(kind, text, agent, step_id, data)`:

| Kind | Meaning |
| --- | --- |
| `status` | phase change ("planning", "verifying") |
| `plan` | full plan snapshot |
| `step_start` / `step_done` | step lifecycle |
| `step_text` / `step_thought` | worker output |
| `step_tool` | tool start/end inside a step |
| `verify` | verifier verdict |
| `notice` / `error` | recoverable/terminal problems |
| `done` | final result |

`CoworkView` and `ComputerView` consume that stream. The app keeps bounded
deques (`cowork_events` 500, `computer_events` 250, `team_events` 800) so a long
run cannot grow memory without limit. `TeamView` consumes the separate team
event dictionaries.

## Persistence

| Path | Contents |
| --- | --- |
| `~/.config/gemini-tui/settings.json` | Settings (API key never written) |
| `~/.config/gemini-tui/sessions/` | Chat sessions |
| `~/.config/gemini-tui/cache/` | Model cache, index cache, embedding cache |
| `~/.config/gemini-tui/cowork/` | Plans (`<id>.json`) and reports (`<id>.md`) |
| `~/.config/gemini-tui/team/` | Team reports (`team-*.md`, `team-*.json`) |
| `~/.config/gemini-tui/logs/` | `audit-YYYYMMDD.jsonl`, `screens/shot-*.png` |
| `~/.config/gemini-tui/memory.json` | Scoped memory |
| `~/.config/gemini-tui/plugins/` | Local Python plugins |
| `~/.config/gemini-tui/skills/` | Local skills |

`GEMINI_TUI_HOME` relocates the whole config root; the test suite uses it to
avoid touching real state.

## Error recovery

- Gemini retries 429/5xx with backoff, drops thinking config or tool
  declarations if the model rejects them, and reports a friendly error.
- A tool exception becomes a `ToolError` the model can read; the audit record
  keeps `ok=false` and the error text.
- A worker exception fails one task; retries are bounded and the remaining
  graph continues or is skipped honestly.
- A failed verifier call yields `unverified`, not a fake success.
- A cancelled cowork or team run is persisted and returns a `cancelled` result.
- The computer backend raises `ComputerError` when the OS call is unavailable.
- A clipboard helper failure returns `ok=False` with the backend errors; the UI
  shows an error notice.

## Extension points

- **MCP** — `MCPManager` mirrors server tools as `mcp_<server>_<tool>`.
- **Skills** — `SkillLibrary` reads `SKILL.md` files and exposes `skill_load`.
- **Plugins** — Python files in the config plugin directory can register tools
  and slash commands.
- **New tools** — add a `ToolSpec` (or `registry.register`) with an explicit
  `risk`; the policy, approval and audit path is automatic.
- **New cowork roles** — extend `ROLE_TOOLS`; scoping is enforced centrally.
- **New team roles** — add an `AgentRole` and `ROLE_REQUIREMENTS` entry; the
  router, ceiling and capability discovery pick it up.
- **Other providers** — implement the `ProviderAdapter` protocol and register it;
  declare tool support honestly.

## Why no framework

Gemini CLI, ADK, OpenHands and Browser Use were evaluated (see
[`REPOS.md`](REPOS.md)). Their useful ideas were small: JSON plans, scoped
tools, event streams, observation before action, an audit trail. Adopting a
framework would have added a large dependency, a second agent runtime and a
license surface for little gain. The current layer is standard library plus the
dependencies the TUI already had.
