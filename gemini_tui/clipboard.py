"""Real operating-system clipboard access with bounded fallbacks.

Textual's built-in clipboard only writes an OSC 52 escape sequence and keeps a
private in-app string; `TextArea.action_paste` reads that private string. That
is why pasting from the OS into the composer did nothing. This module talks to
the real system clipboard and always reports which backend worked, or an
honest error when none did.

Backends, in order:

* `pyperclip` (ctypes on Windows, pbcopy on macOS, xclip/xsel on Linux),
* native commands: PowerShell `Get-Clipboard`/`Set-Clipboard`, `pbpaste`/
  `pbcopy`, `wl-paste`/`wl-copy`, `xclip`, `xsel`.

OSC 52 is still emitted by the app as an additional best-effort path for
terminals that support it (many do not, and macOS Terminal does not). A real
backend reports a normal success; if only Textual's OSC 52 write succeeds, the
result is labelled best-effort because the terminal may ignore it.

Limits: remote SSH sessions and some terminals cannot reach the local system
clipboard. In that case `ClipboardResult.ok` is False and the UI says so
instead of pretending the copy worked.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Sequence

COPY_TIMEOUT = 2.5
PASTE_TIMEOUT = 2.5


@dataclass(frozen=True)
class ClipboardResult:
    ok: bool
    method: str = ""
    error: str = ""

    @property
    def message(self) -> str:
        if self.ok:
            if self.method.startswith("OSC 52"):
                return "sent via OSC 52 (best effort; the terminal may ignore it)"
            return f"copied via {self.method}"
        return self.error or "clipboard unavailable"


def _run(
    command: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float = COPY_TIMEOUT,
) -> str | None:
    """Run a clipboard helper without a shell; return stdout or None."""
    try:
        completed = subprocess.run(
            list(command),
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


def _powershell(script: str, *, input_text: str | None = None) -> str | None:
    return _run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        input_text=input_text,
        timeout=COPY_TIMEOUT,
    )


# ══════════════════════════════════════════════════════════════════════
# Read
# ══════════════════════════════════════════════════════════════════════
def _read_pyperclip() -> tuple[bool, str, str]:
    try:
        import pyperclip
    except Exception as error:  # pragma: no cover - dependency missing
        return False, "", f"pyperclip import failed: {error}"
    try:
        value = pyperclip.paste()
    except Exception as error:
        return False, "", str(error)
    if value is None:
        return False, "", "pyperclip returned no text"
    return True, str(value), ""


def _read_windows() -> tuple[bool, str, str]:
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Get-Clipboard -Raw"
    )
    value = _powershell(script)
    if value is None:
        return False, "", "PowerShell Get-Clipboard failed"
    return True, value.replace("\r\n", "\n"), ""


def _read_macos() -> tuple[bool, str, str]:
    value = _run(["pbpaste"])
    if value is None:
        return False, "", "pbpaste failed"
    return True, value, ""


def _read_linux() -> tuple[bool, str, str]:
    errors = []
    for command in (
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ):
        if shutil.which(command[0]) is None:
            continue
        value = _run(command)
        if value is not None:
            return True, value, ""
        errors.append(command[0])
    return False, "", "no Linux clipboard tool worked" + (f" ({', '.join(errors)})" if errors else "")


def read_clipboard() -> tuple[bool, str, str]:
    """Read the OS clipboard. Returns `(ok, text, error)`."""
    ok, value, error = _read_pyperclip()
    if ok:
        return True, value, ""
    helper_error = error
    if os.name == "nt":
        ok, value, error = _read_windows()
    elif sys.platform == "darwin":
        ok, value, error = _read_macos()
    else:
        ok, value, error = _read_linux()
    if ok:
        return True, value, ""
    return False, "", f"{helper_error or 'pyperclip failed'}; {error}"


# ══════════════════════════════════════════════════════════════════════
# Write
# ══════════════════════════════════════════════════════════════════════
def _write_pyperclip(text: str) -> tuple[bool, str, str]:
    try:
        import pyperclip
    except Exception as error:  # pragma: no cover - dependency missing
        return False, "", f"pyperclip import failed: {error}"
    try:
        pyperclip.copy(text)
    except Exception as error:
        return False, "", str(error)
    return True, "pyperclip", ""


def _write_windows(text: str) -> tuple[bool, str, str]:
    script = (
        "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
        "$value = [Console]::In.ReadToEnd(); Set-Clipboard -Value $value"
    )
    if _powershell(script, input_text=text) is None:
        return False, "", "PowerShell Set-Clipboard failed"
    return True, "powershell", ""


def _write_macos(text: str) -> tuple[bool, str, str]:
    if _run(["pbcopy"], input_text=text) is None:
        return False, "", "pbcopy failed"
    return True, "pbcopy", ""


def _write_linux(text: str) -> tuple[bool, str, str]:
    errors = []
    for command in (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ):
        if shutil.which(command[0]) is None:
            continue
        if _run(command, input_text=text) is not None:
            return True, command[0], ""
        errors.append(command[0])
    return False, "", "no Linux clipboard tool worked" + (f" ({', '.join(errors)})" if errors else "")


def write_clipboard(text: str) -> ClipboardResult:
    """Write text to the real OS clipboard."""
    text = text if isinstance(text, str) else str(text or "")
    if not text:
        return ClipboardResult(False, error="nothing to copy")
    ok, method, error = _write_pyperclip(text)
    if ok:
        return ClipboardResult(True, method)
    first_error = error
    if os.name == "nt":
        ok, method, error = _write_windows(text)
    elif sys.platform == "darwin":
        ok, method, error = _write_macos(text)
    else:
        ok, method, error = _write_linux(text)
    if ok:
        return ClipboardResult(True, method)
    return ClipboardResult(False, error=f"{first_error or 'pyperclip failed'}; {error}")


# ══════════════════════════════════════════════════════════════════════
# OSC 52 fallback
# ══════════════════════════════════════════════════════════════════════
def osc52_sequence(text: str, *, selection: str = "c") -> str:
    """Encode text as an OSC 52 clipboard escape sequence."""
    payload = base64.b64encode((text or "").encode("utf-8")).decode("ascii")
    return f"\x1b]52;{selection};{payload}\a"


def osc52_likely_supported(environ: dict[str, str] | None = None) -> bool:
    """A conservative guess; OSC 52 is still sent as a best-effort fallback."""
    env = environ if environ is not None else os.environ
    term = (env.get("TERM", "") or "").lower()
    if "screen" in term or "tmux" in term:
        # tmux supports it only when `set-clipboard on`; we cannot know here.
        return False
    return bool(env.get("TERM") or env.get("WT_SESSION") or env.get("TERM_PROGRAM"))


# ══════════════════════════════════════════════════════════════════════
# Async helpers
# ══════════════════════════════════════════════════════════════════════
async def read_clipboard_async() -> tuple[bool, str, str]:
    """Read the clipboard off the UI thread (PowerShell can take ~0.5s)."""
    return await asyncio.to_thread(read_clipboard)


async def write_clipboard_async(text: str) -> ClipboardResult:
    return await asyncio.to_thread(write_clipboard, text)


# ══════════════════════════════════════════════════════════════════════
# App-facing manager
# ══════════════════════════════════════════════════════════════════════
class ClipboardManager:
    """Copy/paste used by the UI: real OS clipboard + in-app + OSC 52.

    `app` is the Textual App. It is optional so the manager can be unit tested
    without a terminal.
    """

    def __init__(self, app: Any = None) -> None:
        self.app = app
        self.last_result: ClipboardResult | None = None

    def copy(self, text: str) -> ClipboardResult:
        text = text if isinstance(text, str) else str(text or "")
        if not text.strip():
            result = ClipboardResult(False, error="nothing to copy")
            self.last_result = result
            return result
        result = write_clipboard(text)
        # OSC 52 + Textual's internal clipboard, best effort.
        if self.app is not None:
            try:
                self.app.copy_to_clipboard(text)
                if not result.ok:
                    result = ClipboardResult(True, "OSC 52 (terminal)")
            except Exception:
                pass
        self.last_result = result
        return result

    async def copy_async(self, text: str) -> ClipboardResult:
        result = await write_clipboard_async(text)
        if self.app is not None:
            try:
                self.app.copy_to_clipboard(text)
                if not result.ok:
                    result = ClipboardResult(True, "OSC 52 (terminal)")
            except Exception:
                pass
        self.last_result = result
        return result

    async def paste_async(self) -> tuple[str, str]:
        """Return `(text, method)`. Falls back to Textual's in-app clipboard."""
        ok, value, error = await read_clipboard_async()
        if ok and value:
            return value, "system clipboard"
        if ok and not value and self.app is not None:
            in_app = str(getattr(self.app, "clipboard", "") or "")
            if in_app:
                return in_app, "in-app clipboard"
            return "", "system clipboard (empty)"
        if ok and not value:
            return "", "system clipboard (empty)"
        if self.app is not None:
            in_app = str(getattr(self.app, "clipboard", "") or "")
            if in_app:
                return in_app, "in-app clipboard"
        return "", error or "clipboard unavailable"
