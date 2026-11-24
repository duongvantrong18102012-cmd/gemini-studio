# Test report

Date: 2026-11-18
Platform: Windows (`win32`), Python 3.12.0
Workspace: `C:\Users\duong\Documents\Visual Studio Code\TUI`

This report records what was actually run and measured. It does not repeat
earlier live-API results, because no API key was available for this verification.

## Suite result

```powershell
python -m unittest discover -s tests -t .
```

```
Ran 255 tests in 100.036s
OK
```

Earlier full runs in this session: 237 tests in 74.573s, 250 tests in 84.586s,
252 tests in 52.519s/53.560s, 253 tests in 96.592s/95.963s/74.816s, and 255
tests in 92.931s. One 253-test run during a workstation stall reported
657.571 s while still passing. These are real runs, but their times are not a
before/after speed comparison: the test set and machine load changed. The count
and the `OK` result are the stable facts.

PowerShell can report a non-zero process exit code because unittest writes its
progress dots to stderr and PowerShell wraps that as `NativeCommandError`. The
`Ran … / OK` lines are the result of record.

| Area | New tests | Result |
| --- | ---: | --- |
| Clipboard backends and manager | 20 | pass |
| Markdown code fences and copy | 8 | pass |
| Scheduler / dependency graph | 16 | pass |
| File ownership / checkpoints | 11 | pass |
| Model registry / router | 14 | pass |
| Agent manager / live state | 12 | pass |
| Team orchestrator | 35 | pass |
| Team permission ceilings / MCP risk / leases | 20 | pass |
| Performance contracts | 7 | pass |
| Splash tiers / ASCII fallback | 13 | pass |
| App smoke additions (team refusal, clipboard, paste, final verdict/log, 80×24) | 5 | pass |

The existing 94 tests from the previous tree were preserved and still pass. No
old assertion was weakened to make the new code pass.

## Real bugs found during verification

1. **`AgentTeamOrchestrator._launch` used an undefined `scheduler` name.**
   `scheduler.mark_running(...)` raised `NameError` on every launch. Fixed to
   `self.scheduler.mark_running(...)`.
2. **The planner did not fall back on provider/API errors.** It caught only
   plan-shape errors, so a dead endpoint aborted the run instead of trying once
   and then using the heuristic plan. The retry loop now catches ordinary
   exceptions; `CancelledError` still propagates.
3. **`AgentManager.resume_all()` iterated dictionary keys instead of agents.**
   It called `self.resume(agent.id)` on `str` objects. Fixed to iterate
   `self._agents.values()`.
4. **The team view had no honest no-provider path in tests.** The app now
   preflights `_team_provider_ready()` and refuses with a real message instead of
   launching a run that cannot work.
5. **`_is_binary` leaked a file handle.** `path.open("rb").read(2048)` left the
   `BufferedReader` open on every binary check; the new offload tests surfaced it
   as a `ResourceWarning`. It now uses a context manager.
6. **The team view's 1 s refresh replaced the final verification with an agent
   log.** A fail-first regression test showed the verdict disappearing as soon
   as rows were rendered. The view now freezes the final/failure log and only
   opens an agent log when the user selects a row and presses `enter`.
7. **Copying blocked the UI thread.** `copy_text` called the synchronous OS
   clipboard helper (up to 2.5 s per backend) from an event handler. It now runs
   through a Textual worker and `ClipboardManager.copy_async`; new tests assert
   the blocking read and write run on a non-event-loop thread.

The tests also confirmed two intended semantics that first appeared to be bugs:
individual pause/cancel is per-agent, while only `pause_all`/`cancel_all` set the
team-wide flags.

## Performance measurements

Command:

```powershell
python scripts/measure_performance.py
```

The script patches dotenv loading and model listing; it makes no network calls.
Results from this machine:

| Measurement | Result |
| --- | --- |
| Boot through `App.run_test`, 120×42 | median **5.106 s** (min 4.592, max 6.918; 3 runs) |
| Boot through `App.run_test`, 80×24 | median **4.504 s** (min 4.427, max 5.002; 3 runs) |
| Hidden Dashboard `psutil` calls, 120×42, 3.2 s | **0** |
| Hidden Dashboard `psutil` calls, 80×24, 3.2 s | **0** |
| 2,000 fast tokens | **1** stream write, 12,000 chars, **52.8 ms** |
| Finished ToolCard timer | stopped (`timer_stopped_after_finish = True`) |

Interpretation and limits:

- The boot number is measured through Textual's `run_test` harness and includes
  mounting all ten views. It is not a terminal wall-clock startup promise, and no
  trustworthy pre-change baseline was captured, so this report does **not** claim
  a startup speedup.
- The hidden-Dashboard result is a real call count: the view's intervals return
  before touching `psutil`, and the counters were reset after mount. The single
  mount-time `net_io_counters()` call is not part of the 3.2 s window.
- The 2,000-token result is a fast-feed contract test, not a model benchmark; no
  network was involved. It proves tokens are batched rather than repainting per
  token.
- The timer result is the behavioural guarantee behind the fix; it is not a CPU
  profile.
- `fs_read`, `fs_list`, `fs_glob`, `fs_grep`, `fs_write` and `fs_edit` now run
  their blocking work through `asyncio.to_thread`. The tests assert that the
  read/scan happens on a non-event-loop thread; they do not claim a latency
  improvement for large scans.

## UI verification

The splash was mounted in a real Textual app at 120×42, 80×24 and 80×24 with
`GEMINI_TUI_ASCII=1`, then inspected through the panel state:

- 120×42: `tier=full`, Unicode art, 12 lines.
- 80×24: `tier=compact`, Unicode art, 6 lines.
- 80×24 with ASCII forced: `tier=compact`, pure ASCII art, 6 lines.

This checks selection and rendering, not pixel quality across terminal emulators.

The app smoke tests boot the real application at 120×42 and 80×24, switch views
through the real `ContentSwitcher`, and verify the sidebar toggle and layout
classes. They do not replace a visual review on a physical terminal.

## What was genuinely exercised

- Real `ToolRegistry.call` path, including permission ceilings and approval
  before handler execution.
- Real DAG scheduling, retries, timeouts, cancellation, ownership leases and
  report writing with fake workers.
- Real planner parsing and fallback logic with fake completions.
- Real clipboard manager routing with patched OS backends (no real clipboard
  content was read or replaced), including the worker-thread read/write path.
- Real Textual widgets for code fences, assistant streaming and ToolCard timers.
- Real splash tier/encoding selection.
- Real builtin filesystem handlers, including the worker-thread offload and the
  policy/ownership wrapper around writes.

## What is not verified

- **No live Gemini or OpenAI-compatible API call.** `get_api_key()` was falsy;
  no key was configured. The Gemini adapter, planner quality and real tool-calling
  loop are not proven by this run.
- **No real team execution on live models.** Team tests use injected workers and
  completers. Concurrency, retries and cancellation are real code paths, but the
  model output is fake.
- **No real desktop input automation.** Computer tests use a fake backend; no
  click or keystroke was sent.
- **No cross-terminal clipboard verification.** Remote SSH, tmux/screen, macOS
  and Linux clipboard helpers were not available on this machine. Their logic is
  unit-tested; their real-world behavior is not.
- **No visual regression/pixel comparison.** Textual rendering differs by
  terminal and font.
- **No pre-change performance baseline.** The current numbers stand alone. The
  prior suite time (96.456 s for 232 tests vs several 253–255 test runs between
  52 and 97 s, with one 657 s stall) is not a valid before/after comparison
  because the test set and machine load changed.
- **Verification is model review, not proof.** The team verifier sees recorded
  evidence, not diffs or raw logs. A `pass` is a second opinion; it cannot replace
  tests.
- **No cost or quota ceiling.** A live team run can consume quota; this was not
  exercised.

## Files added or materially changed

New runtime modules:

- `gemini_tui/clipboard.py`
- `gemini_tui/widgets/codeblock.py`
- `gemini_tui/orchestration/` (`models.py`, `capabilities.py`, `scheduler.py`,
  `manager.py`, `ownership.py`, `orchestrator.py`)
- `gemini_tui/tools/builtin.py` (blocking filesystem work moved to worker threads)
- `gemini_tui/app.py` (team lifecycle/commands, provider setup, clipboard worker)
- `gemini_tui/widgets/views.py` (Agent Team command center and frozen final log)

New tests:

- `tests/test_clipboard.py`, `test_codeblock.py`, `test_scheduler.py`,
  `test_ownership.py`, `test_router.py`, `test_manager.py`,
  `test_orchestrator.py`, `test_team_permissions.py`,
  `test_performance_contracts.py`, `test_splash.py`

New tooling and docs:

- `scripts/measure_performance.py`
- `TEST_REPORT.md`
- updated `README.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`,
  `docs/COMPUTER_USE.md`, `docs/LIMITATIONS.md`

## Conclusion

The suite passes and the new behaviour is covered at the level claimed: real
application boot, real registry policy path, real scheduler/ownership logic,
real widget contracts and honest refusal paths. Live model behavior, real
desktop input and cross-terminal clipboard behavior remain unverified and are
listed as such rather than implied to work.

---

## Recovery Cycles 1–2 (2026-11-23)

- **Command:** `pytest -q`
- **Result:** **272 passed in 61.79s** (255 existing tests + 17 new regression & security tests in `tests/test_recovery_fixes.py`).
- **CLI smoke check:** `python tui.py --version` (`Gemini Studio 1.0.0`) and `python tui.py --help` exited with code `0`.
- **Note:** No live `GEMINI_API_KEY` was configured in this environment, so live Gemini and OpenAI-compatible network calls remain unexercised; provider routing and streaming paths were verified with unit tests and mocks.
