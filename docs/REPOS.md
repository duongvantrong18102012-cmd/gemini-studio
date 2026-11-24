# Reference repositories, licenses and what was reused

Research was done to choose patterns, not to import projects. **No source code
was copied** from the projects below. Licenses were checked from the repository
metadata during research; licenses can change, so re-check before redistributing
this project or any derivative.

| Project | License (at research time) | What was used | What was not used |
| --- | --- | --- | --- |
| [google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli) | Apache-2.0 | Tool-confirmation model, project context file (`GEMINI.md`), MCP-first integration, explicit tool result handling | Its Node/Ink runtime, sandbox, CLI architecture |
| [google/adk-python](https://github.com/google/adk-python) | Apache-2.0 | Planner/worker separation, agents with restricted tool sets, structured task decomposition | The ADK runtime, session services, deployment stack |
| [All-Hands-AI/OpenHands](https://github.com/All-Hands-AI/OpenHands) | MIT | Event-stream/action-observation model, reviewable agent history, “verify after acting” discipline | Its Docker sandbox, controller runtime, huge dependency tree |
| [sst/opencode](https://github.com/sst/opencode) | MIT | Config discovery locations, model catalogue UX, plugin/skill layout, workspace rebinding | Its server/TUI implementation and provider layer |
| [browser-use/browser-use](https://github.com/browser-use/browser-use) | MIT | Screenshot → decide → act → observe loop, action history in the prompt | DOM automation, browser dependencies |
| [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) | Apache-2.0 | “Snapshot before acting”, compact observation surfaced to the model | Playwright, browser snapshots, accessibility-tree code |
| [anthropics/skills](https://github.com/anthropics/skills) | **No declared license** | Only the open `SKILL.md` folder/format convention (the app already loaded skills) | Any code, text or assets from the repository |
| [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) | MIT | Standard MCP protocol via the `mcp` package already in `requirements.txt` | Vendoring or modifying the SDK |
| [yamadashy/repomix](https://github.com/yamadashy/repomix) | MIT | The compact `<repository>` pack idea used by `code_pack` | Its packer implementation, ignore engine, tokenizer |
| GitNexus | Not reliably identified | Nothing | Everything — the repository could not be identified with confidence, so it was excluded |

## Runtime dependencies (already present before this work)

| Package | License | Used for |
| --- | --- | --- |
| [Textual](https://github.com/Textualize/textual) | MIT | TUI, views, overlays, workers |
| [google-genai](https://github.com/googleapis/python-genai) | Apache-2.0 | Gemini streaming, tools, embeddings, images |
| [Pillow](https://github.com/python-pillow/Pillow) | HPND | Screenshots, ASCII previews |
| [psutil](https://github.com/giampaolo/psutil) | BSD-3-Clause | System dashboard |
| [pyperclip](https://github.com/asweigart/pyperclip) | BSD-3-Clause | Clipboard tools/copy |
| [pywin32](https://github.com/mhammond/pywin32) | PSF | Existing Windows integration; the new computer backend uses `ctypes` instead |
| [mcp](https://github.com/modelcontextprotocol/python-sdk) | MIT | MCP client |

## Deliberate exclusions

- **No agent framework** (LangChain/LangGraph, CrewAI, AutoGen, ADK runtime).
  The needed pieces — planning, scoping, verification, events — are small and
  testable in this codebase. A framework would add a second runtime and a large
  transitive dependency surface.
- **No pyautogui / pynput / keyboard / mouse.** They add native dependencies and
  global hooks. Win32 through `ctypes` plus Pillow covers the same operations
  with the dependencies already installed.
- **No browser automation dependency.** `computer_open` can open a URL in the
  user's browser; browser driving is intentionally out of scope for now.
- **No vendored code.** Patterns were reimplemented against this project's own
  types (`ToolSpec`, `AgentEvent`, `TaskPlan`).

## Attribution policy

If future work copies code from any project, it must:

1. be compatible with this project's distribution license,
2. keep the original copyright and license notice,
3. name the source file and commit in the commit message,
4. be added to this document.
