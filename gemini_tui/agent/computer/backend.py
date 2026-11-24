"""Windows computer control backend (screenshot + mouse + keyboard + windows).

Implemented with the Win32 API through ctypes so it needs no new dependency.
Screenshots use Pillow's ImageGrab. Every method is a real OS call; when the
platform or a call is unavailable the backend raises a clear error instead of
pretending to succeed.

Safety is enforced one layer up (PermissionPolicy + ComputerGuard + approvals).
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ...config import LOGS_DIR

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    from ctypes import wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    user32.GetSystemMetrics.argtypes = [wt.INT]
    user32.GetSystemMetrics.restype = wt.INT
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wt.HWND
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.restype = wt.BOOL
    user32.ShowWindow.argtypes = [wt.HWND, wt.INT]
    user32.ShowWindow.restype = wt.BOOL
    user32.IsIconic.argtypes = [wt.HWND]
    user32.IsIconic.restype = wt.BOOL
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = wt.INT
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, wt.INT]
    user32.GetWindowTextW.restype = wt.INT
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetWindowRect.restype = wt.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    user32.GetCursorPos.restype = wt.BOOL
    user32.SetCursorPos.argtypes = [wt.INT, wt.INT]
    user32.SetCursorPos.restype = wt.BOOL
    user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]
    user32.keybd_event.restype = None
    user32.mouse_event.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
    user32.mouse_event.restype = None

    shell32.ShellExecuteW.argtypes = [
        wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.INT
    ]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
else:  # pragma: no cover - the app targets Windows; keep imports safe elsewhere
    wt = None  # type: ignore[assignment]
    user32 = None  # type: ignore[assignment]
    shell32 = None  # type: ignore[assignment]


class ComputerError(RuntimeError):
    """A real failure from the OS layer."""


# ══════════════════════════════════════════════════════════════════════
# Data
# ══════════════════════════════════════════════════════════════════════
@dataclass
class ScreenInfo:
    width: int
    height: int
    virtual_left: int = 0
    virtual_top: int = 0
    virtual_width: int = 0
    virtual_height: int = 0
    monitors: list[dict[str, Any]] = field(default_factory=list)
    scale_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "virtual": [self.virtual_left, self.virtual_top, self.virtual_width, self.virtual_height],
            "monitors": self.monitors,
            "scale_note": self.scale_note,
        }


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    rect: tuple[int, int, int, int]
    minimized: bool = False
    active: bool = False
    own: bool = False

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    def render(self) -> str:
        flag = "*" if self.active else " "
        own = " [this app]" if self.own else ""
        return (
            f"{flag} 0x{self.hwnd:08x}  pid {self.pid:<6}  {self.width:>4}x{self.height:<4}  "
            f"{self.title[:70]}{own}"
        )


@dataclass
class Shot:
    path: str
    width: int
    height: int
    original_width: int
    original_height: int
    region: tuple[int, int, int, int] | None
    scale: float
    captured_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "original_width": self.original_width,
            "original_height": self.original_height,
            "region": list(self.region) if self.region else None,
            "scale": round(self.scale, 4),
            "captured_at": self.captured_at,
        }


ASCII_RAMP = " .:-=+*#%@"


def ascii_preview(image: Any, width: int = 64, height: int = 0, invert: bool = False) -> str:
    """Render a PIL image as monochrome ASCII (real pixels, no fake output)."""
    if image is None:
        return ""
    width = max(16, min(160, int(width)))
    aspect = image.height / max(1, image.width)
    if height <= 0:
        height = max(6, min(48, int(width * aspect * 0.5)))
    gray = image.convert("L").resize((width, height))
    pixels = list(gray.getdata())
    ramp = ASCII_RAMP[::-1] if invert else ASCII_RAMP
    lines: list[str] = []
    for row in range(height):
        chunk = pixels[row * width : (row + 1) * width]
        lines.append("".join(ramp[min(len(ramp) - 1, value * len(ramp) // 256)] for value in chunk))
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# Key names
# ══════════════════════════════════════════════════════════════════════
VK_NAMES = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "del": 0x2E, "win": 0x5B,
    "meta": 0x5B, "super": 0x5B, "apps": 0x5D, "numlock": 0x90, "scrolllock": 0x91,
    "shiftleft": 0xA0, "shiftright": 0xA1, "ctrlleft": 0xA2, "ctrlright": 0xA3,
    "altleft": 0xA4, "altright": 0xA5,
}
for _index in range(1, 25):
    VK_NAMES[f"f{_index}"] = 0x6F + _index
VK_NAMES.update(
    {
        ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
        "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    }
)

MODIFIERS = {"ctrl", "control", "shift", "alt", "win", "meta", "super"}


def parse_keys(combo: str) -> list[int]:
    """'ctrl+shift+s' -> [VK_CONTROL, VK_SHIFT, ord('S')]."""
    raw = (combo or "").strip().replace(" ", "")
    if not raw:
        raise ComputerError("key combo is empty")
    parts = [part for part in raw.split("+") if part]
    if len(parts) == 1 and len(parts[0]) == 1 and parts[0].isalpha():
        parts = [parts[0]]
    keys: list[int] = []
    for part in parts:
        lookup = part.lower()
        if lookup in VK_NAMES:
            keys.append(VK_NAMES[lookup])
        elif len(part) == 1:
            char = part.upper()
            if char.isalpha() or char.isdigit():
                keys.append(ord(char))
            else:
                raise ComputerError(
                    f"Unsupported key {part!r}. Use letters, digits, F1-F24 or names like enter/tab/left."
                )
        else:
            raise ComputerError(f"Unknown key name {part!r}")
    return keys


# ══════════════════════════════════════════════════════════════════════
# Backend
# ══════════════════════════════════════════════════════════════════════
class BaseBackend:
    name = "unsupported"
    available = False

    def screen_info(self) -> ScreenInfo:
        raise ComputerError("Computer control is not available on this platform")

    def list_windows(self, limit: int = 40) -> list[WindowInfo]:
        raise ComputerError("Window enumeration is not available on this platform")

    def foreground(self) -> WindowInfo | None:
        return None

    def focus_window(self, target: str = "", hwnd: int = 0) -> WindowInfo:
        raise ComputerError("Window focus is not available on this platform")

    def capture(self, region: Any = None, scale: float = 1.0) -> Shot:
        raise ComputerError("Screenshots are not available on this platform")

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> str:
        raise ComputerError("Mouse input is not available on this platform")

    def move(self, x: int, y: int) -> str:
        raise ComputerError("Mouse input is not available on this platform")

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> str:
        raise ComputerError("Mouse input is not available on this platform")

    def scroll(self, clicks: int, x: int | None = None, y: int | None = None) -> str:
        raise ComputerError("Mouse input is not available on this platform")

    def type_text(self, text: str, interval: float = 0.008) -> str:
        raise ComputerError("Keyboard input is not available on this platform")

    def press(self, keys: str) -> str:
        raise ComputerError("Keyboard input is not available on this platform")

    def open_target(self, target: str) -> str:
        raise ComputerError("Opening targets is not available on this platform")


class ScreenshotOnlyBackend(BaseBackend):
    """Cross-platform observation fallback: screenshots yes, input no."""

    name = "screenshot-only"
    available = False

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or (LOGS_DIR / "screens")

    def screen_info(self) -> ScreenInfo:
        try:
            from PIL import ImageGrab

            image = ImageGrab.grab(all_screens=True)
            width, height = image.size
            return ScreenInfo(width=width, height=height, virtual_width=width, virtual_height=height)
        except Exception as error:
            raise ComputerError(f"Screen information unavailable: {error}") from error

    def capture(self, region: Any = None, scale: float = 1.0) -> Shot:
        try:
            from PIL import ImageGrab

            bbox = tuple(region) if region else None
            image = ImageGrab.grab(bbox=bbox, all_screens=True)
        except Exception as error:
            raise ComputerError(f"Screenshot failed: {error}") from error
        return _save_shot(image, region, scale, self.directory)


class WindowsBackend(BaseBackend):
    name = "windows"
    available = True

    # ── setup ─────────────────────────────────────────────────────
    def __init__(self, directory: Path | None = None) -> None:
        if not IS_WINDOWS or user32 is None:
            raise ComputerError("WindowsBackend requires Windows")
        self.directory = directory or (LOGS_DIR / "screens")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.own_pid = os.getpid()
        self._dpi_ready = False
        self._prepare_dpi()
        self._cleanup_old_screens()

    def _prepare_dpi(self) -> None:
        if self._dpi_ready:
            return
        self._dpi_ready = True
        try:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except Exception:
            pass
        try:
            shcore = ctypes.WinDLL("shcore", use_last_error=True)
            shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass

    def _cleanup_old_screens(self, keep_days: float = 3.0) -> None:
        cutoff = time.time() - keep_days * 86400
        try:
            for path in self.directory.glob("shot-*.png"):
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    # ── screen ────────────────────────────────────────────────────
    def screen_info(self) -> ScreenInfo:
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
        virtual = (
            int(user32.GetSystemMetrics(76)),
            int(user32.GetSystemMetrics(77)),
            int(user32.GetSystemMetrics(78)),
            int(user32.GetSystemMetrics(79)),
        )
        monitors = self._monitors()
        note = ""
        try:
            from PIL import ImageGrab

            grabbed = ImageGrab.grab(all_screens=True).size
            if virtual[2] and grabbed[0] != virtual[2]:
                note = f"Pillow captured {grabbed[0]}x{grabbed[1]} px; DPI scaling may differ from Win32 metrics"
        except Exception:
            pass
        return ScreenInfo(
            width=width,
            height=height,
            virtual_left=virtual[0],
            virtual_top=virtual[1],
            virtual_width=virtual[2],
            virtual_height=virtual[3],
            monitors=monitors,
            scale_note=note,
        )

    def _monitors(self) -> list[dict[str, Any]]:
        if wt is None:
            return []
        monitors: list[dict[str, Any]] = []

        class RECT(ctypes.Structure):
            _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wt.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wt.DWORD),
                ("szDevice", wt.WCHAR * 32),
            ]

        monitor_proc = ctypes.WINFUNCTYPE(
            wt.BOOL, wt.HANDLE, wt.HDC, ctypes.POINTER(RECT), wt.LPARAM
        )

        def callback(hmonitor, hdc, rect, lparam):
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                monitors.append(
                    {
                        "device": info.szDevice,
                        "x": info.rcMonitor.left,
                        "y": info.rcMonitor.top,
                        "width": info.rcMonitor.right - info.rcMonitor.left,
                        "height": info.rcMonitor.bottom - info.rcMonitor.top,
                        "primary": bool(info.dwFlags & 1),
                    }
                )
            return True

        try:
            user32.EnumDisplayMonitors(None, None, monitor_proc(callback), 0)
        except Exception:
            pass
        return monitors

    # ── windows ───────────────────────────────────────────────────
    def _window_title(self, hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value.strip()

    def _window_info(self, hwnd: int, active_hwnd: int = 0) -> WindowInfo | None:
        if not user32.IsWindowVisible(hwnd):
            return None
        title = self._window_title(hwnd)
        if not title:
            return None

        class RECT(ctypes.Structure):
            _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]

        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width < 40 or height < 30:
            return None
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return WindowInfo(
            hwnd=int(hwnd),
            title=title,
            pid=int(pid.value),
            rect=(rect.left, rect.top, rect.right, rect.bottom),
            minimized=bool(user32.IsIconic(hwnd)),
            active=int(hwnd) == active_hwnd,
            own=int(pid.value) == self.own_pid,
        )

    def list_windows(self, limit: int = 40) -> list[WindowInfo]:
        if wt is None:
            return []
        active = int(user32.GetForegroundWindow() or 0)
        rows: list[WindowInfo] = []
        proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def callback(hwnd, lparam):
            try:
                info = self._window_info(int(hwnd), active)
            except Exception:
                info = None
            if info is not None:
                rows.append(info)
            return True

        try:
            user32.EnumWindows(proc(callback), 0)
        except Exception as error:
            raise ComputerError(f"EnumWindows failed: {error}") from error
        rows.sort(key=lambda row: (not row.active, row.minimized, row.title.lower()))
        return rows[: max(1, limit)]

    def foreground(self) -> WindowInfo | None:
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        return self._window_info(hwnd, hwnd)

    def focus_window(self, target: str = "", hwnd: int = 0) -> WindowInfo:
        windows = self.list_windows(limit=200)
        chosen: WindowInfo | None = None
        if hwnd:
            chosen = next((row for row in windows if row.hwnd == hwnd), None)
        elif target:
            needle = target.strip().lower()
            chosen = next((row for row in windows if row.title.lower() == needle), None)
            if chosen is None:
                chosen = next((row for row in windows if needle in row.title.lower()), None)
        if chosen is None:
            raise ComputerError(f"No window matched {target or hwnd!r}")

        handle = chosen.hwnd
        if chosen.minimized:
            user32.ShowWindow(handle, 9)  # SW_RESTORE
        else:
            user32.ShowWindow(handle, 5)  # SW_SHOW
        if not user32.SetForegroundWindow(handle):
            # AttachThreadInput is the standard workaround for foreground lock.
            foreground = int(user32.GetForegroundWindow() or 0)
            fg_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
            current = int(ctypes.windll.kernel32.GetCurrentThreadId())
            attached = False
            if fg_thread:
                attached = bool(user32.AttachThreadInput(current, fg_thread, True))
            try:
                user32.BringWindowToTop(handle)
                user32.SetForegroundWindow(handle)
            finally:
                if attached:
                    user32.AttachThreadInput(current, fg_thread, False)
        time.sleep(0.12)
        refreshed = self._window_info(handle, int(user32.GetForegroundWindow() or 0))
        return refreshed or chosen

    # ── capture ───────────────────────────────────────────────────
    def capture(self, region: Any = None, scale: float = 1.0) -> Shot:
        try:
            from PIL import ImageGrab

            bbox = tuple(int(value) for value in region) if region else None
            if bbox and len(bbox) != 4:
                raise ComputerError("region must be [left, top, right, bottom]")
            image = ImageGrab.grab(bbox=bbox, all_screens=True)
        except ComputerError:
            raise
        except Exception as error:
            raise ComputerError(f"Screenshot failed: {error}") from error
        return _save_shot(image, tuple(region) if region else None, scale, self.directory)

    # ── input ─────────────────────────────────────────────────────
    _EXTENDED_VKS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C})

    def _send_key(self, vk: int, up: bool = False) -> None:
        # keybd_event is deprecated but universally available and sufficient
        # for single keys/combo presses. Unicode typing uses SendInput below.
        flags = (0x0001 if vk in self._EXTENDED_VKS else 0) | (0x0002 if up else 0)
        user32.keybd_event(vk, 0, flags, 0)

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> str:
        button = (button or "left").lower()
        down_up = {
            "left": (0x0002, 0x0004),
            "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040),
        }.get(button)
        if down_up is None:
            raise ComputerError("button must be left, right or middle")
        if not user32.SetCursorPos(int(x), int(y)):
            error = ctypes.get_last_error()
            raise ComputerError(f"SetCursorPos({x},{y}) failed (WinError {error})")
        time.sleep(0.03)
        down, up = down_up
        clicks = 2 if double else 1
        for _ in range(clicks):
            user32.mouse_event(down, 0, 0, 0, 0)
            time.sleep(0.01)
            user32.mouse_event(up, 0, 0, 0, 0)
            time.sleep(0.07)
        return f"{'double-' if double else ''}{button} click at ({x}, {y})"

    def move(self, x: int, y: int) -> str:
        if not user32.SetCursorPos(int(x), int(y)):
            raise ComputerError(f"SetCursorPos({x},{y}) failed")
        return f"cursor moved to ({x}, {y})"

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> str:
        down_up = {
            "left": (0x0002, 0x0004),
            "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040),
        }.get((button or "left").lower())
        if down_up is None:
            raise ComputerError("button must be left, right or middle")
        user32.SetCursorPos(int(x1), int(y1))
        time.sleep(0.04)
        user32.mouse_event(down_up[0], 0, 0, 0, 0)
        try:
            steps = 12
            for step in range(1, steps + 1):
                x = int(x1 + (x2 - x1) * step / steps)
                y = int(y1 + (y2 - y1) * step / steps)
                user32.SetCursorPos(x, y)
                time.sleep(0.015)
        finally:
            user32.mouse_event(down_up[1], 0, 0, 0, 0)
        return f"dragged {button} ({x1},{y1}) → ({x2},{y2})"

    def scroll(self, clicks: int, x: int | None = None, y: int | None = None) -> str:
        if x is not None and y is not None:
            user32.SetCursorPos(int(x), int(y))
            time.sleep(0.02)
        delta = int(clicks) * 120
        user32.mouse_event(0x0800, 0, 0, delta, 0)  # MOUSEEVENTF_WHEEL
        return f"scrolled {clicks} notch(es) at ({x if x is not None else 'cursor'}, {y if y is not None else ''})"

    def type_text(self, text: str, interval: float = 0.008) -> str:
        """Type arbitrary Unicode text (Vietnamese included) through SendInput."""
        if not text:
            return "typed nothing"
        interval = max(0.0, min(0.2, float(interval)))
        for char in text:
            if char == "\n":
                self.press("enter")
                time.sleep(interval)
                continue
            if char == "\t":
                self.press("tab")
                time.sleep(interval)
                continue
            encoded = char.encode("utf-16-le")
            for index in range(0, len(encoded), 2):
                unit = int.from_bytes(encoded[index : index + 2], "little")
                self._send_unicode(unit)
                time.sleep(interval)
        return f"typed {len(text)} characters"

    def _send_unicode(self, unit: int) -> None:
        if wt is None:
            raise ComputerError("Win32 input unavailable")
        KEYEVENTF_UNICODE = 0x0004
        KEYEVENTF_KEYUP = 0x0002
        INPUT_KEYBOARD = 1

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wt.WORD),
                ("wScan", wt.WORD),
                ("dwFlags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wt.LONG),
                ("dy", wt.LONG),
                ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]

        def events(flags: int) -> Any:
            item = INPUT()
            item.type = INPUT_KEYBOARD
            item.u.ki = KEYBDINPUT(wVk=0, wScan=unit, dwFlags=flags, time=0, dwExtraInfo=0)
            return item

        down = events(KEYEVENTF_UNICODE)
        up = events(KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
        sent = user32.SendInput(2, (INPUT * 2)(down, up), ctypes.sizeof(INPUT))
        if sent != 2:
            raise ComputerError(f"SendInput failed (sent {sent}/2, WinError {ctypes.get_last_error()})")

    def press(self, keys: str) -> str:
        codes = parse_keys(keys)
        if not codes:
            raise ComputerError("no keys parsed")
        # release in reverse order to keep modifiers sane
        for code in codes:
            self._send_key(code, up=False)
            time.sleep(0.012)
        for code in reversed(codes):
            self._send_key(code, up=True)
            time.sleep(0.012)
        return f"pressed {keys}"

    def open_target(self, target: str) -> str:
        target = (target or "").strip()
        if not target:
            raise ComputerError("target is required")
        result = shell32.ShellExecuteW(None, "open", target, None, None, 1)
        code = int(result or 0)
        if code <= 32:
            raise ComputerError(f"ShellExecute failed for {target!r} (code {code})")
        return f"opened {target}"


def _save_shot(image: Any, region: Any, scale: float, directory: Path) -> Shot:
    from PIL import Image

    directory.mkdir(parents=True, exist_ok=True)
    original_width, original_height = image.size
    scale = max(0.1, min(1.0, float(scale or 1.0)))
    if scale < 1.0:
        image = image.resize(
            (max(1, int(original_width * scale)), max(1, int(original_height * scale))),
            Image.LANCZOS,
        )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    path = directory / f"shot-{stamp}.png"
    image.save(path, "PNG")
    return Shot(
        path=str(path),
        width=image.width,
        height=image.height,
        original_width=original_width,
        original_height=original_height,
        region=tuple(int(v) for v in region) if region else None,
        scale=scale,
    )


def create_backend(directory: Path | None = None) -> BaseBackend:
    if IS_WINDOWS:
        try:
            return WindowsBackend(directory)
        except Exception:
            pass
    try:
        return ScreenshotOnlyBackend(directory)
    except Exception:
        return BaseBackend()
