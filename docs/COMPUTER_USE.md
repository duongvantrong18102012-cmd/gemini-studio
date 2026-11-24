# Gemini Computer

Gemini Computer gives the agent real eyes and hands on a Windows desktop. It is
**off by default** and needs explicit consent.

## Consent model

1. Launch with `--computer`, or run `/computer enable` / `/computer on`.
   This sets `settings.computer_enabled = true` and registers the control tools.
2. Observation (`computer_screenshot`) is then allowed automatically: enabling
   control is the consent for looking at the screen.
3. Input (`click`, `type`, `key`, `scroll`, `drag`, `focus`, `open`) is risk
   class `computer` and asks for approval by default.
4. `/computer disable` removes the control tools from the model's declarations
   immediately. `computer_status`, `computer_windows` and `computer_wait` stay
   registered as read-only helpers.

Global `--approval readonly` denies screen and computer risks even when computer
control is enabled. `settings.computer_approval` can be `ask` (default),
`auto` (no per-action prompt), or `readonly` (input denied).

## Requirements

- Windows 10/11. The input backend is Win32-only by design.
- Python dependencies already in `requirements.txt`: Pillow for screenshots.
  No pyautogui, pynput or extra native package.
- A desktop session. Services/session-0 processes cannot interact with the
  logged-in desktop.

On non-Windows platforms the app falls back to a screenshot-only backend if
Pillow can capture; all input operations raise a clear error.

## Tools

| Tool | Risk | What it does |
| --- | --- | --- |
| `computer_status` | read | Backend, screen size, virtual bounds, monitors, foreground window, action counters |
| `computer_windows` | read | Visible top-level windows: HWND, PID, size, title, active flag |
| `computer_screenshot` | screen | Capture the screen or a `[left, top, right, bottom]` region to PNG; returns metadata, foreground window and an ASCII preview |
| `computer_wait` | read | Sleep 0.1–15 s, then report the foreground window |
| `computer_focus` | computer | Bring a window to the foreground by title substring or HWND; restores minimized windows |
| `computer_click` | computer | Move and click at screen coordinates; `double=true` for double-click |
| `computer_move` | computer | Move the cursor without clicking |
| `computer_drag` | computer | Press, move, release between two points |
| `computer_scroll` | computer | Scroll wheel clicks at an optional point |
| `computer_type` | computer | Type Unicode text with `SendInput` |
| `computer_key` | computer | Press a key combo such as `ctrl+shift+s` |
| `computer_open` | computer | Open a URL, file or app target through the shell |
| `computer_task` | computer | Delegate a goal to the screenshot → decide → act → verify loop |

`computer_task` calls the other tools through the normal registry, so each
delegated action still goes through policy, approval, the rate limiter and the
audit log. It is never a bypass.

## Coordinates and DPI

- Input coordinates are Win32 screen pixels, the same space as
  `SetCursorPos`. `computer_status` reports the primary screen and the virtual
  desktop bounds.
- The backend requests per-monitor DPI awareness v2 (`-4`), falling back to
  system DPI awareness. This keeps screenshots and clicks aligned on scaled
  displays.
- If Pillow's captured size differs from Win32 metrics, `computer_status`
  includes a note. In that case prefer region captures and verify with
  `computer_screenshot` before clicking.
- Use the screenshot/preview to ground coordinates. The driver prompt explicitly
  forbids inventing UI positions.

## Safety limits

- **Rate limit** — 90 actions per minute by default
  (`settings.computer_rate_limit`). `ComputerGuard` counts actions and raises a
  clear error when the limit is hit; it never silently drops an action.
- **Approval** — every input tool asks unless the user chose
  `computer_approval=auto`. The approval screen shows the exact arguments.
- **Audit** — every call writes a JSONL record with risk, arguments, result and
  timing. Arguments are redacted for obvious secrets.
- **No secret entry** — the vision driver's system prompt forbids typing
  passwords, payment data or other secrets and tells it to stop and ask a human.
- **Kill switch** — `/computer disable` removes the tools; `esc` cancels the
  active turn; quitting the app stops the loop.
- **Step cap** — `computer_task` is limited to 1–20 steps
  (`computer_max_steps`, default 10).

## Screenshots

PNGs are saved under `~/.config/gemini-tui/logs/screens/` (or
`GEMINI_TUI_HOME/logs/screens/`). The backend cleans old files on startup while
keeping recent shots. `computer_keep_screenshots` exists in settings; disable it
if you do not want screenshots retained (the current backend still writes the
file used for the preview, so review this setting before relying on it as a
retention guarantee).

## Using it from the TUI

```text
/computer status
/computer screenshot
/computer windows
/computer task Open Notepad, type "hello from Gemini", and verify the text appears
```

The Computer view (`ctrl+9`) shows the backend state, counters and a live event
log. The Cowork view (`ctrl+0`) shows `computer` steps when a plan delegates GUI
work. The Agent Team view (`ctrl+8`) is separate: it runs the supervised team
layer and does not replace Cowork.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| “Computer control is disabled” | Run `/computer enable` or launch with `--computer` |
| “no input backend” / “not available on this platform” | Not Windows, or no desktop session |
| Clicks land on the wrong place | DPI scaling mismatch; read the `computer_status` note, capture a region, verify with `computer_screenshot` |
| A window will not focus | Windows foreground lock; `computer_focus` already tries `AttachThreadInput`. Click the window once or run the app at the same integrity level |
| “Rate limit reached” | Wait a minute or raise `computer_rate_limit` |
| UAC / secure desktop prompt | Cannot be automated by design; handle it manually |
| Elevated app ignores input | Windows UIPI blocks lower-integrity processes; run Gemini Studio elevated if the target requires it |
| Screenshot is black | Protected content/DRM, a locked session, or a secure desktop |

## What it does not do

- No OCR or accessibility-tree extraction; the model sees the screenshot and the
  action log.
- No browser DOM control; use Playwright MCP or a dedicated browser tool if you
  need that.
- No macOS/Linux input backend.
- No bypass of UAC, secure desktops, DRM or cross-integrity-level input.
- No parallel GUI workers; computer steps run one at a time.
