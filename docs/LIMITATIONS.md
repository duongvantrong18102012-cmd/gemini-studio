# Remaining limitations

This is the honest list of what is **not** solved, verified or safe yet. None of
these are hidden behind fake success paths: when a capability is unavailable, the
tool or agent reports the failure.

## Agent team

- **No live verification in this environment.** No API key was configured, so
  team planning, routing and execution are tested with injected fake workers and
  completers. The scheduler, ownership, cancellation and report paths are real;
  model quality is not measured.
- **Planner quality is model quality.** JSON is validated and capped at 8 tasks,
  and the heuristic fallback is conservative, but a bad decomposition produces a
  bad run. The plan is shown while it executes so a human can stop it.
- **Verification is model review.** The verifier sees task status, acceptance
  text, worker reports (capped) and recorded changes — not diffs, failed command
  output or the whole transcript. A `pass` is a second opinion, **not a
  substitute for tests**. A verifier error leaves the run `unverified`.
- **No resume from a checkpoint.** Checkpoints are recorded in the report, but
  there is no “continue this task graph from checkpoint 3” command. A failed or
  cancelled run must be relaunched.
- **Cooperative pause/cancel only.** Cancellation takes effect before the next
  model call or at a task boundary. It does not interrupt a tool already writing
  a file, and it does not kill an in-flight HTTP request.
- **Parallelism is per task, not per file line.** File leases are whole-file
  claims. Two workers cannot edit the same file concurrently; they also cannot
  merge non-overlapping edits to that file. The default budget is 2 workers
  (maximum 4).
- **Ownership is advisory for external tools.** Builtin writes claim leases and
  MCP tools are risk-gated, but an MCP server that writes files outside the
  registry does not consult the ownership table.
- **Role ceilings are guardrails, not a sandbox.** They lower `allow` to `ask`
  and read-only denies non-free risks, but an approved `shell_run` or plugin still
  has the app's OS privileges.
- **No hard cost cap.** A long live run can consume quota; there is no
  per-run budget stop.
- **OpenAI-compatible endpoints are text/planning-only.** They declare
  `supports_tools=False`; Developer/Tester/Debugger tasks are refused rather than
  silently degraded. Only Gemini workers can execute tools.
- **Model capability metadata can be incomplete.** Routing prefers declared
  capabilities; relaxed fallback can select a text model for a role if the
  catalogue lacks planning/reasoning flags. Tool tasks still require
  `supports_tools=True`.

## Cowork

- **Sequential execution.** Cowork steps still run one at a time. The separate
  team layer adds bounded parallelism; Cowork itself does not.
- **No automatic retry.** A failed Cowork step stops the plan and is reported.
  (The team scheduler does retry; Cowork is unchanged.)
- **No resume of an interrupted execution.** Plans are persisted and can be
  inspected, but there is no “continue from step 3” command.
- **Single verifier pass.** Same evidence limits as above.
- **Worker scope is a guardrail.** A worker cannot use tools outside its role,
  but this is not a security sandbox.

## Clipboard and code blocks

- **Remote and multiplexed terminals are not verified.** SSH, tmux/screen, WSL,
  macOS and Linux clipboard helpers were not exercised on this Windows machine.
  Their logic has unit tests, but real-world support depends on the terminal and
  the installed helpers.
- **OSC 52 is best-effort.** The app sends it as a fallback and labels the
  result “sent via OSC 52 (best effort)” when Textual's write does not raise,
  because some terminals ignore or truncate OSC 52. Treat that toast as a
  request, not proof that the terminal applied the copy.
- **A very large copy can exceed terminal limits.** OSC 52 has no standard size
  cap; some terminals drop large payloads. The real backend path does not have
  this limit.
- **Line numbers stop at 400 lines.** Longer fences still scroll and copy
  correctly, but the gutter is omitted to avoid spending width and memory.
- **No per-line selection copy.** Copying is whole-answer, whole-code-block or a
  terminal drag selection, not line-range selection.
- **Click-to-copy depends on mouse support.** Keyboard copy remains available via
  focus + `ctrl+shift+c` / `enter`.

## UI and splash

- **Small terminals are adapted, not infinite.** The layout switches to compact,
  narrow, short and stacked classes, but below roughly 80×24 content is cramped.
  The splash falls back to a one-line mark, but the rest of the app still needs a
  usable terminal height.
- **No visual regression test.** Textual rendering differs by terminal, font and
  Unicode width. The splash tiers and ASCII fallback are unit-tested; pixel
  appearance is not.
- **Font-dependent glyph width.** Block elements and box drawing are “ambiguous
  width” in some East Asian locales. They may occupy two cells and misalign art
  in those terminals; `GEMINI_TUI_ASCII=1` avoids this.
- **Motion is restrained, not free.** Gradients and splash shimmer still repaint
  while visible; use `--no-animations` to remove them.

## Computer use

- **Windows input only.** `click`, `type`, `key`, `scroll`, `drag`, `focus` and
  `open` require Win32. Other platforms get a screenshot-only backend at best.
- **No OCR or accessibility tree.** The model works from pixels and the action
  log. Small text, icon-only UI and unusual layouts are harder than for tools
  that expose the accessibility tree.
- **DPI/multi-monitor edge cases.** Per-monitor DPI awareness is requested and a
  mismatch warning is reported, but mixed-DPI setups can still shift
  coordinates. Verify with a screenshot before clicking.
- **UAC/secure desktop cannot be automated.** Elevation prompts, the lock
  screen, DRM/protected video and some full-screen games are out of reach by
  design.
- **Cross-integrity-level input is blocked.** Windows UIPI prevents a normal
  process from driving an elevated window. Running Gemini Studio elevated is the
  only workaround and increases risk.
- **No browser DOM control.** `computer_open` opens a URL; it does not inspect
  or manipulate page elements. Use a browser-focused MCP server for that.
- **Screenshot retention is not fully configurable yet.** The backend still
  writes the PNG used for the preview. `computer_keep_screenshots` exists but
  retention/cleanup policy needs work before it can be treated as a privacy
  guarantee.
- **No secret handling.** The prompt forbids typing secrets, but a model can
  still make mistakes. Do not run computer tasks on screens containing secrets
  you would not put in a prompt.

## Permissions, audit and safety

- **Guardrails, not a sandbox.** `shell_run`, plugins and MCP servers run with
  the same OS privileges as the app. Approval and policy reduce accidental
  damage; they do not contain a malicious tool or a compromised MCP server.
- **Role ceilings only apply to team tool contexts.** A normal chat turn uses
  session policy without a role ceiling.
- **Audit is local and mutable.** It is append-only by convention, not
  tamper-proof. Anyone with filesystem access can edit or delete it. It is not
  encrypted.
- **Redaction is pattern-based.** Obvious keys/tokens/passwords are redacted from
  audit arguments, but unusual secret formats can slip through.
- **`--no-audit` exists.** A run can be started without a JSONL file (records
  stay in memory). This is useful for sensitive sessions but weakens the review
  trail.
- **Policy overrides are powerful.** `settings.tool_policies` can allow an
  `execute` tool globally. Use it deliberately.
- **Computer input can be set to `auto`.** This removes the per-action prompt.
  The rate limit and audit remain, but a fast model can still do damage quickly.

## Memory and context

- **Token counts are estimates.** `ContextManager` uses a character-based
  heuristic, not the model tokenizer. Budgets are approximate.
- **Session memory does not expire automatically.** `session` scope notes stay
  in the JSON file until forgotten. They are filtered by workspace, not by chat
  session id.
- **Memory search is lexical.** It has no embeddings; semantic recall depends on
  the code retriever, not the memory store.
- **Embeddings require a working key and quota.** On failure the retriever falls
  back to lexical search; results can be less relevant.
- **Ambient context is best-effort.** It is capped and can be truncated. The
  model does not always see the whole project brief.

## Code intelligence

- **Python gets AST symbols; other languages get regex heuristics.** The index is
  useful, not a language server.
- **No cross-file call graph or type resolution.** `code_search` and
  `code_outline` find names and structure; they do not prove an edit is safe.
- **Review rules are heuristics.** They catch common patterns (secrets, `eval`,
  `shell=True`, bare `except`, XSS sinks, TODOs). A clean review is not proof of
  correctness.
- **Index freshness is time-based.** `CodeStore.ensure` rebuilds after an age
  threshold; edits made between rebuilds may not appear until a refresh.
- **Large/binary files are skipped.** Files above the size limit or with binary
  suffixes are excluded by design.

## Performance

- **No trustworthy before/after baseline.** The current measurements (see
  `TEST_REPORT.md`) were taken after the changes. The boot figure includes
  Textual's test harness and mounting all ten views; it is not an interactive
  startup promise.
- **Filesystem tools are off the event loop, not free.** `fs_read`, `fs_list`,
  `fs_glob`, `fs_grep`, `fs_write` and `fs_edit` run their blocking work in
  worker threads. A large scan still consumes CPU and can take time; it no longer
  freezes the UI while it runs. Clipboard reads and copies use the same
  `asyncio.to_thread` path through a Textual worker, so the copy toast arrives
  asynchronously.
- **`shell_run` is bounded by its own timeout.** It can run for up to 600 s and
  consumes a process slot; cancellation is cooperative at the app level.
- **No memory profiler or long-run soak test.** The deques are bounded and code
  fences are capped, but a multi-hour live session was not measured.

## Platform and testing

- **Verified on Windows + Python 3.12.** The suite runs on the development
  machine; CI on other platforms/versions is not configured.
- **No live API call in the automated suite.** No key was configured for this
  verification. Live quota, model availability and latency are external.
- **No real desktop automation test.** Computer input is tested through a fake
  backend and the real guard; a CI machine cannot safely click/type on a desktop.
- **MCP behavior depends on external servers.** A server can be slow, broken or
  change its schema. The MCP manager isolates failures but cannot fix them.

## Product gaps

- No cost/budget hard stop; a long agent run can consume quota.
- No encrypted secret store; keys are read from env/dotenv as before.
- No multi-user or remote-session support.
- No automatic plugin sandboxing.
- The Agent Team and Cowork views are functional command centers, not a full
  trace inspector; the Markdown/JSON reports and audit log are the durable
  records.
