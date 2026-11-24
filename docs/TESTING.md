# Testing and verification guide

## How to run

```powershell
python -m unittest discover -s tests -t .
```

Add `-v` for per-test output. No `pytest` install is required; the suite uses
the standard library. Set `PYTHONPATH` to the repository root if it is not
already importable:

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m unittest discover -s tests -t . -v
```

Verbose runs print Textual asyncio “Executing <Task …> took …” notices. They are
slow-callback diagnostics from Textual's message pump, not test failures.

## Isolation

`tests/__init__.py` runs before any `gemini_tui` import and:

- points `GEMINI_TUI_HOME` at a fresh temporary directory, so settings, sessions,
  audit logs, cowork plans, team reports, screenshots and caches never touch the
  real profile;
- removes `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_GENAI_API_KEY` and
  `GEMINI_TUI_API_KEY`;
- sets `GEMINI_TUI_TESTING=1`.

The app smoke tests additionally patch `gemini_tui.app.load_dotenv_files` and
`GeminiService.list_models`, so they cannot read a real key or make a network
call. Clipboard tests patch subprocess/pyperclip boundaries; they do not read or
overwrite the machine's real clipboard.

## Latest result

```
Ran 255 tests in 100.036s
OK
```

Last run: 2026-11-18, Python 3.12.0, Windows. See
[`../TEST_REPORT.md`](../TEST_REPORT.md) for the full evidence, measurements and
caveats.

## Test matrix

| File | Tests | Covers |
| --- | ---: | --- |
| `test_audit.py` | 6 | secret/pattern redaction, JSONL write/read, memory-only mode |
| `test_app_smoke.py` | 9 | real Textual boot, 10 views, consent-gated tools, team refusal without a provider, clipboard routing, composer paste, final team verdict vs agent log, 80×24 layout/sidebar toggle |
| `test_clipboard.py` | 20 | backend order, timeouts, Windows/macOS/Linux helpers, OSC 52 encoding, read/write off the event-loop thread, honest failure results |
| `test_code_index.py` | 10 | index build/cache, symbols, search, outline, pack, review rules, test-command detection |
| `test_codeblock.py` | 8 | language labels, line numbers, long-block gutter cap, copy routing, unknown-language fallback, height cap |
| `test_computer.py` | 10 | ASCII preview, key parsing, real tool handlers with a fake backend, guard/rate limit, backend factory |
| `test_context.py` | 9 | token estimate, trim validity, context budget, project brief, lazy index, lexical/semantic retrieval fallback |
| `test_cowork.py` | 14 | full execute path, plan-only, failed step, worker exception, blocked output, empty plan, cancel, role scoping, computer consent, injected driver, report render |
| `test_manager.py` | 12 | live agent state, per-agent pause/resume, team pause/cancel, cancellation wakeup, log bounds, snapshot |
| `test_memory.py` | 7 | save/get/persist, scopes, search ranking, digest, forget, validation, legacy migration |
| `test_orchestrator.py` | 35 | JSON planning, heuristic fallback, dependency order, parallel slots, retries, model fallback/cooldown, timeouts, empty output, ownership conflicts, MCP discovery, cancellation, pause/resume, verification verdicts, reports and events |
| `test_ownership.py` | 11 | path normalization, leases, conflicts, release, change records, checkpoints, summary |
| `test_performance_contracts.py` | 7 | token batching, 12k chunk cap, thought throttling, final drain, finished ToolCard timer shutdown, filesystem tools off the event loop |
| `test_permissions.py` | 11 | risk classification, read/network free, readonly denial, ask/auto, screen consent, computer modes, overrides, guard limits |
| `test_plan.py` | 9 | JSON fence tolerance, normalisation, step cap, empty rejection, persistence, corrupt file, progress |
| `test_registry.py` | 14 | allow/ask/deny, approval yes/no, missing approval UI, readonly, overrides, denied/unknown/disabled tools, audit records, exceptions, cancellation, remove/prefix, signatures |
| `test_router.py` | 14 | capability requirements, per-role preferences, scoring, cooldowns, fallback candidates, OpenAI-compatible tool limits |
| `test_scheduler.py` | 16 | DAG validation, cycles, ready/slots, attempts, terminal failures, dependent skipping, cancellation, progress, summary |
| `test_splash.py` | 13 | tier thresholds, art sizes, pure-ASCII fallback, encoding detection, env override, copy isolation |
| `test_team_permissions.py` | 20 | role ceilings, readonly hard deny, deny precedence, MCP risk classes, registry approval path, file leases in tools, team context wiring |

## What is actually exercised

- **Real code paths, fake dependencies where determinism matters.** Team and
  Cowork tests inject planner/worker/verifier callables, so the real
  orchestration, DAG, scheduling, retries, ownership, events, report writing and
  failure handling run without a model.
- **Real Textual app.** `test_app_smoke.py` uses `App.run_test`, switches views
  through the real `ContentSwitcher`, checks the no-provider refusal, exercises
  clipboard routing and composer paste, and verifies the 80×24 layout and sidebar
  toggle.
- **Real registry call path.** `test_registry.py` and `test_team_permissions.py`
  verify that policy, ceilings, approval and audit wrap handlers, and that
  denials happen **before** the handler runs.
- **Real scheduler and ownership.** `test_scheduler.py`, `test_ownership.py` and
  `test_orchestrator.py` cover cycles, retries, cancellation, parallel slots,
  lease conflicts and report contents.
- **No live network calls in the suite.** No API key is used. Every model-like
  collaborator is a fake.

## Performance checks

Behavioural contracts live in `test_performance_contracts.py`; wall-clock
measurements are produced by:

```powershell
python scripts/measure_performance.py
```

The script patches dotenv/model listing and makes no network calls. It measures:

- boot through `App.run_test` at 120×42 and 80×24;
- `psutil` calls made by a hidden Dashboard over 3.2 s while chat is visible;
- stream writes for 2,000 fast tokens;
- whether a finished ToolCard stops its timer.

`test_performance_contracts.py` also asserts the filesystem tools perform their
reads and scans on a worker thread, not the event-loop thread. That is a
behavioural check, not a latency measurement: a large scan can still take time,
but it no longer freezes the UI while it runs. `test_clipboard.py` makes the
same assertion for the blocking OS clipboard read and write helpers.

Current numbers on this Windows/Python 3.12 machine are recorded in
[`../TEST_REPORT.md`](../TEST_REPORT.md). They are environment-specific and not a
target. The boot figure includes Textual's test harness and the mount of all ten
views; it is not a promise about interactive terminal startup.

## Live API status

**Not verified in this environment.** No Gemini API key was configured, so no
live model call, tool call, Cowork run or team run is claimed. Gemini and
OpenAI-compatible adapters are exercised with fakes at their boundaries. A live
release check still needs a real key and remains a manual gate.

## Manual UI checklist

Run `python tui.py` (with the splash) and `python tui.py --no-splash`, then check:

- [ ] The splash sigil renders at 120×42; any key skips it.
- [ ] At 80×24 the compact sigil fits; `GEMINI_TUI_ASCII=1` shows the ASCII art.
- [ ] `ctrl+1…0` switches all ten views without remount flicker.
- [ ] `ctrl+b` hides/shows the sidebar; the 80×24 layout remains usable.
- [ ] `ctrl+shift+v` pastes multi-line text from the OS clipboard.
- [ ] `ctrl+shift+c` copies a drag selection and a focused code block.
- [ ] A code fence shows its language, line count and `COPY`; unknown languages
      show `TEXT`.
- [ ] `/team <goal>` refuses honestly when no provider is configured.
- [ ] With a real key, `/team plan <goal>` shows a validated graph.
- [ ] `/team status`, pause, resume and cancel reflect real worker state.
- [ ] The final team verdict stays visible; selecting an agent row and pressing
      `enter` opens that agent's log.
- [ ] `/cowork <task>` streams plan/step events in the Cowork view.
- [ ] `/computer status` reports backend and screen size.
- [ ] `/computer enable` makes control tools available; `/computer disable`
      removes them again.
- [ ] `/audit 20` shows recent tool and approval records.
- [ ] An approval prompt appears for a write tool in `ask` mode.
- [ ] A denied approval blocks the tool and the transcript explains it.
- [ ] A failed copy shows an error notice, not a success toast.
