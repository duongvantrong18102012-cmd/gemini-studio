# Gemini Studio

A local-first Gemini agent console for the terminal, wearing a RED HACKER
identity: black and bone-white, crimson accents, restrained motion, and a custom
terminal sigil. It keeps the working agent runtime and adds real clipboard and
code-block ergonomics plus a supervised agent team.

```text
  ▍ GEMINI STUDIO   RED HACKER CONSOLE                    18:42:07  ● READY
    ⚡ gemini-3.5-flash-lite  ·  think low  ·  47 tools  ·  mcp 1/6
```

## Verified status

The current tree was verified on Windows with Python 3.12:

```powershell
python -m unittest discover -s tests -t .    # 255 tests, all passing
```

**No live Gemini API call is claimed in this verification.** No API key was
configured for the run, so the suite exercised the real app, registry,
permission policy, scheduler, ownership, clipboard and UI with injected fake
providers and fake workers. A real Gemini key is still required for real model
output. See [`TEST_REPORT.md`](TEST_REPORT.md) for measurements and the exact
limits of the evidence.

## Install

```powershell
python -m pip install -r requirements.txt
```

Provide a Gemini API key in any of these places (first hit wins):

1. `GEMINI_API_KEY` in the environment
2. `~/.config/gemini-tui/.env`
3. `.env` in the current directory or beside `tui.py`
4. `GEMINI_TUI_API_KEY` in the environment

Get a free key at <https://aistudio.google.com/apikey>, then copy `.env.example`.

## Run

```powershell
python tui.py
python tui.py --workspace . --model gemini-3.5-flash-lite
python tui.py --no-splash --approval readonly --no-animations
python tui.py --computer           # opt in to desktop control (input still asks)
```

| Flag | Meaning |
| --- | --- |
| `-w, --workspace` | Directory the tools operate in (default: cwd) |
| `-m, --model` | Starting model id |
| `--no-splash` | Skip the boot animation |
| `--approval ask\|auto\|readonly` | Write/execute policy |
| `--no-animations` | Turn off ambient motion |
| `--computer` | Enable Gemini Computer for this run |
| `--no-memory` | Do not inject ambient memory into turns |
| `--no-audit` | Keep the audit log in memory only (no JSONL file) |

## Keys

| Key | Action |
| --- | --- |
| `enter` | Send prompt |
| `shift+enter` | New line |
| `esc` | Cancel generation / leave a view / close an overlay |
| `ctrl+k` | Command palette |
| `ctrl+n` / `ctrl+r` | New / resume session |
| `ctrl+m` | Model picker |
| `ctrl+1…0` | Chat, Models, Tools, MCP, Skills, Plugins, Dashboard, Agent Team, Computer, Cowork |
| `ctrl+b` | Show/hide the sidebar (useful on narrow terminals) |
| `ctrl+l` | Clear transcript |
| `ctrl+shift+v` | Paste from the real OS clipboard (composer) |
| `ctrl+shift+c` | Copy selection / focused code block |
| `ctrl+y` | Copy last answer |
| `ctrl+t` / `ctrl+o` | Cycle thinking level / approval mode |
| `f1` | Keyboard + command reference |
| `ctrl+q` | Quit |

`ctrl+v` is also bound to the real clipboard in the composer; `ctrl+c` is left
alone so it keeps its normal interrupt behaviour.

## What it can do

### Chat and clipboard

- Streaming markdown chat with bounded token batching (50 ms), throttled
  thoughts, syntax-highlighted code, and a screenshot-free, low-motion design.
- **Real OS clipboard** through `pyperclip` or native helpers (`PowerShell`,
  `pbpaste`/`pbcopy`, `wl-copy`, `xclip`, `xsel`), with OSC 52 sent as an
  additional best-effort path. Reads and writes run off the UI thread, and
  copies execute in a worker instead of blocking a key handler.
- Copy the last answer, a selection, or one code block. Every copy reports the
  backend used or an honest failure; when only OSC 52 is left it is labelled as
  best-effort, because some terminals ignore it.
- Code fences show a language label, line count and `COPY` control; normal-sized
  blocks get line numbers, unknown languages fall back to plain text, and very
  long blocks are height-capped with scrolling instead of swallowing the chat.

### Gemini Code

- Index the workspace lazily (Python AST + regex symbols for other languages),
  then search, outline and pack only the files a task needs.
- Deterministic review heuristics (secrets, `eval`/`exec`, `shell=True`,
  bare `except`, XSS sinks, TODOs) with file:line findings.
- Detect and run a project's test command (`pytest`, `npm test`, `cargo test`,
  `go test`, `dotnet test`, `make test`, …) through the normal approval path.
- Retrieve code with embeddings when the key allows it, and fall back to
  lexical search instead of failing.

### Supervised agent team

`/team <goal>` runs a separate orchestration layer above Cowork:

1. **Plan** — the supervisor asks a planner-capable model for a JSON task graph
   (1–8 tasks), validates dependencies and cycles, and falls back to a
   conservative heuristic plan if the model cannot produce usable JSON.
2. **Discover** — the capability registry reports the tools, skills, MCP tools and
   plugins that actually exist right now. It does not grant permission.
3. **Route** — each role gets a model by capability: Architect, Researcher,
   Planner, Developer, Debugger, Tester, Security Reviewer and Writer. Per-role
   overrides live in `team_model_roles`; failed models cool down and retries can
   route to a different capable model.
4. **Execute** — independent tasks run in parallel under a fixed worker budget
   (1–4, default 2). Each task has a timeout (default 300 s), bounded attempts
   (default 2) and a declared file scope.
5. **Track** — file leases prevent two workers from overwriting the same path;
   every write is recorded with its agent and task; checkpoints and changes are
   included in the report.
6. **Verify and report** — a verifier model reviews the recorded evidence and
   returns `pass`, `fail` or `partial`. The verdict is explicitly a second
   opinion, **not a substitute for tests**. Markdown + JSON reports are written
   to `~/.config/gemini-tui/team/`.

The dependency graph expresses the coding workflow: read/design (Architect,
Researcher, Planner) → implement (Developer) → test (Tester) → debug
(Debugger), with Security and Writer as review/documentation roles. This is a
role-based workflow, not a per-file phase state machine.

The same run is visible live in the **Agent Team** view (`ctrl+8`): real task
states, assigned models, elapsed time, tool counts, per-agent logs, progress,
and pause/cancel/resume controls. Nothing there is a mock. Select an agent row
and press `enter` to open its log; the final verification verdict stays visible
until you do.

```text
/team Add a --verbose flag, run the tests and update the README
/team plan Add a --verbose flag            # plan only, no workers
/team status                               # live snapshot
/team models                               # capability routing table
/team pause | /team resume | /team cancel
/team auto on|off                          # suggest /team for complex prompts
/solo                                      # hide suggestions; /team still runs
/agents                                    # jump to the live command center
```

**Gemini is fully supported for team workers.** An explicitly configured
OpenAI-compatible endpoint can plan and write text, but it is marked
`supports_tools=False`, so it is refused for Developer/Tester/Debugger tool
tasks rather than pretending to run them.

### Gemini Cowork

- The original planner → scoped workers → verifier flow, unchanged and still
  available at `/cowork`, `/plan` and `ctrl+0`.
- Every step runs through a **scoped registry**: a worker only sees the tools
  its role needs, never the whole catalogue.
- Plans and Markdown reports live in `~/.config/gemini-tui/cowork/`.

### Gemini Computer

- Real Win32 backend through `ctypes`: screen metrics and monitors, window
  enumeration/focus, screenshots with ASCII preview, mouse move/click/drag/
  scroll, typing, key combos and opening files/URLs.
- A screenshot → decide → act → verify loop (`computer_task`) using a
  multimodal model. Every action still passes through policy, approval, the
  rate limiter and the audit log.
- Disabled by default. Observation and input tools are only declared when the
  user enables computer control.
- See [`docs/COMPUTER_USE.md`](docs/COMPUTER_USE.md).

### Agent infrastructure

- **Layered memory** — `global`, `project` and `session` notes with tags,
  lexical scoring and automatic migration of the old flat memory file.
- **Context management** — per-turn trimming that never leaves an orphaned
  function response, plus a budget report in `/context`.
- **Risk-based permissions** — `read`, `network`, `write`, `execute`, `screen`,
  `computer`; per-tool/per-category/per-risk overrides; deny lists.
- **Role risk ceilings** — a team role cannot silently inherit full authority:
  an `allow` above the role ceiling becomes `ask`, and read-only mode denies
  non-free risks before overrides are considered.
- **Audit log** — append-only JSONL under `~/.config/gemini-tui/logs/`, with
  secret redaction. It records tool calls, permission decisions, cowork and team
  runs. `/audit` shows it.
- **MCP, skills and plugins** keep working as before. The app registers 47
  agent tools by default and 57 once computer control is enabled; connected MCP
  servers mirror their own tools dynamically.

## Slash commands

| Group | Commands |
| --- | --- |
| core | `/about` `/help` `/key` `/quit` `/status` |
| session | `/clear` `/compact` `/copy` `/export` `/new` `/rename` `/resume` `/sessions` |
| model | `/model` `/models` `/system` `/temp` `/think` `/tokens` |
| tools | `/cwd` `/init` `/run` |
| agent | `/cowork` `/plan` `/team` `/solo` `/agents` `/computer` `/code` `/context` `/memory` `/audit` |
| mcp | `/mcp list\|connect\|disconnect\|reload` |
| skills | `/skill` `/skills` |
| plugins | `/plugins` |
| media | `/image` `/embed` |
| views | `/chat` `/tools` `/dashboard` |

Useful examples:

```text
/code index                 build/refresh the workspace index
/code search parse_args     search symbols and content
/code review src            heuristic review of a directory
/code test                  detect and run the test command
/plan Add a --verbose flag and run the tests
/team Investigate the flaky test, fix it, run the suite and report
/team plan Refactor the settings loader
/team models                show which model each role gets
/computer status            backend, screen, action counters
/computer screenshot        capture and preview the screen
/computer task Open Notepad and type hello
/context                    budget, memory and index status
/memory save build :: use python -m unittest discover -s tests -t .
/audit 20                   recent tool/permission/agent records
```

## Layout

```text
gemini_tui/
  app.py              App shell, turn loop, agent wiring, team lifecycle, commands
  config.py           Paths, settings, team/provider settings, system prompt
  theme.py            RED palette + global CSS + responsive layout classes
  clipboard.py        OS clipboard backends, async helpers, OSC 52 fallback
  animations.py       Gradients, meters, spinners, shimmer helpers
  agent/
    events.py         Shared event dataclass
    plan.py           Plan/step model, tolerant JSON parser, plan store
    cowork.py         Planner → scoped workers → verifier orchestrator
    permissions.py    Risk policy, role ceilings, computer guard
    audit.py          JSONL audit log with secret redaction
    memory.py         Scoped memory store + memory tools
    context.py        Token budget, trimming, retrieval, lazy index adapter
    tools.py          Cowork/context/audit tools
    code/             Index, review heuristics, code tools
    computer/         Win32 backend, vision driver, computer tools
  orchestration/
    models.py         Model registry, capability router, Gemini/OpenAI adapters
    capabilities.py   Agent roles, role tools, live capability discovery
    scheduler.py      Dependency DAG, attempts, skips, cancellation, progress
    manager.py        Live agent state, logs, team-wide pause/cancel
    ownership.py      File leases, change records, checkpoints
    orchestrator.py   Supervisor plan → execute → verify → report
  ai/                 Gemini streaming/tool loop, model catalogue, sessions
  tools/              Registry, builtin tools, MCP, skills, plugins
  widgets/
    chat.py           Transcript, composer clipboard, buffered streaming
    codeblock.py      Highlighted fences, line numbers, per-block copy
    views.py          Models / Tools / MCP / Skills / Plugins / Dashboard /
                      Agent Team / Cowork / Computer
    overlays.py       Palette, help, approval, responsive splash, toasts
    chrome.py         Header, sidebar, status bar
scripts/
  measure_performance.py   Reproducible local boot/polling/stream measurements
tests/                255 stdlib unittest tests, isolated from real config
TEST_REPORT.md        Actual results, measurements and remaining gaps
docs/
  ARCHITECTURE.md     Module map, flows, data and safety model
  COMPUTER_USE.md     Consent, tools, coordinates, troubleshooting
  REPOS.md            Reference projects, licenses and what was reused
  TESTING.md          Test matrix and how to run it
  LIMITATIONS.md      Honest remaining gaps
```

## Design notes

The ten views are mounted once inside a `ContentSwitcher`; the header, sidebar
and status bar never remount, so switching views is a compositor update rather
than a screen reload — no flicker. Hidden views guard their timers and skip
hardware polling: with chat visible, the Dashboard made **zero** `psutil` calls
over 3.2 s in the measured run. Overlays are `ModalScreen`s layered on top of
that untouched base screen.

The splash is a custom terminal sigil in three responsive tiers. It uses block
elements when the terminal encoding supports them, falls back to pure ASCII on
cp1252/cp437 or with `GEMINI_TUI_ASCII=1`, and any key skips it.

Streaming does not repaint per token: tokens are buffered for 50 ms and written
in chunks capped at 12,000 characters; thoughts are throttled; a finished tool
card stops its refresh timer. Filesystem reads and scans run through
`asyncio.to_thread`, so a large file or directory walk does not block the event
loop. The orchestrator is deliberately plain Python and does not import Textual,
so it is testable without a terminal or a network.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Computer use](docs/COMPUTER_USE.md)
- [Reference repositories and licenses](docs/REPOS.md)
- [Testing guide](docs/TESTING.md)
- [Test report](TEST_REPORT.md)
- [Remaining limitations](docs/LIMITATIONS.md)
