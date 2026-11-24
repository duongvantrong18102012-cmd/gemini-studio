"""Computer-use tools: observe the screen, then act on it.

Observation (status/screenshot/windows) and input (click/type/key/scroll/drag)
both live behind the computer risk class, so they require the user to enable
computer control and to approve actions.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ...tools.registry import ToolContext, ToolError, ToolRegistry, ToolSpec, prop, schema
from ..permissions import ComputerGuard, PermissionDenied
from .backend import BaseBackend, ComputerError, ascii_preview


def _spec(
    name: str,
    description: str,
    parameters: dict[str, Any],
    handler: Any,
    *,
    risk: str = "computer",
    dangerous: bool = False,
    icon: str = "▣",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        dangerous=dangerous,
        category="computer",
        icon=icon,
        source="agent",
        risk=risk,
    )


def _bindings(ctx: ToolContext) -> tuple[BaseBackend, ComputerGuard]:
    backend = getattr(ctx, "computer", None) or ctx.extras.get("computer")
    guard = ctx.extras.get("computer_guard")
    if backend is None or guard is None:
        raise ToolError("Computer backend is not initialised in this session.")
    return backend, guard


def _guard(guard: ComputerGuard, action: str) -> None:
    try:
        guard.check(action)
    except PermissionDenied as error:
        raise ToolError(str(error)) from error


def _foreground_label(backend: BaseBackend) -> str:
    try:
        window = backend.foreground()
    except Exception:
        return ""
    if window is None:
        return ""
    return f"0x{window.hwnd:08x} {window.title[:70]}"


def _preview(path: str, width: int = 64) -> str:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return ascii_preview(image, width=width)
    except Exception:
        return ""


def _capture_output(
    backend: BaseBackend,
    *,
    region: Any = None,
    scale: float = 1.0,
    preview_width: int = 64,
    preview: bool = True,
    note: str = "",
) -> str:
    shot = backend.capture(region=region, scale=scale)
    lines = [
        f"Screenshot saved: {shot.path}",
        f"Image {shot.width}x{shot.height} (screen {shot.original_width}x{shot.original_height}, scale {shot.scale:g})",
    ]
    if shot.region:
        lines.append(f"Region: {list(shot.region)}")
    foreground = _foreground_label(backend)
    if foreground:
        lines.append(f"Foreground: {foreground}")
    if note:
        lines.append(note)
    if preview:
        body = _preview(shot.path, preview_width)
        if body:
            lines.append("")
            lines.append(f"ASCII preview ({preview_width} cols):")
            lines.append(body)
    return "\n".join(lines)


def register_computer_tools(
    registry: ToolRegistry,
    backend: BaseBackend | None = None,
    guard: ComputerGuard | None = None,
) -> None:
    def resolve(ctx: ToolContext) -> tuple[BaseBackend, ComputerGuard]:
        if backend is not None and guard is not None:
            return backend, guard
        return _bindings(ctx)

    # ── observation ───────────────────────────────────────────────
    async def computer_status(args: dict[str, Any], ctx: ToolContext) -> str:
        os_backend, os_guard = resolve(ctx)
        try:
            info = os_backend.screen_info()
        except ComputerError as error:
            raise ToolError(str(error)) from error
        lines = [
            f"backend      {os_backend.name} · {'available' if os_backend.available else 'observation only'}",
            f"control      {'enabled' if os_guard.enabled else 'disabled'} (actions {os_guard.total_actions}, "
            f"{os_guard.actions_this_minute}/min)",
            f"screen       {info.width}x{info.height} px (primary)",
            f"virtual      {info.virtual_width}x{info.virtual_height} at ({info.virtual_left},{info.virtual_top})",
        ]
        for index, monitor in enumerate(info.monitors, 1):
            primary = " primary" if monitor.get("primary") else ""
            lines.append(
                f"monitor {index}    {monitor['width']}x{monitor['height']} at "
                f"({monitor['x']},{monitor['y']}) {monitor.get('device', '')}{primary}"
            )
        foreground = _foreground_label(os_backend)
        if foreground:
            lines.append(f"foreground   {foreground}")
        if info.scale_note:
            lines.append(f"note         {info.scale_note}")
        if not getattr(ctx.settings, "computer_enabled", False):
            lines.append("hint         enable control with /computer enable (or --computer)")
        return "\n".join(lines)

    async def computer_windows(args: dict[str, Any], ctx: ToolContext) -> str:
        os_backend, _ = resolve(ctx)
        limit = max(1, min(100, int(args.get("limit") or 25)))
        try:
            windows = os_backend.list_windows(limit=limit)
        except ComputerError as error:
            raise ToolError(str(error)) from error
        if not windows:
            return "No visible windows matched."
        header = f"{'':2}{'HWND':<12}{'PID':<8}{'SIZE':<12}TITLE"
        return "\n".join([header] + [window.render() for window in windows])

    async def computer_screenshot(args: dict[str, Any], ctx: ToolContext) -> str:
        os_backend, os_guard = resolve(ctx)
        _guard(os_guard, "screenshot")
        region = args.get("region")
        if region is not None and not (isinstance(region, (list, tuple)) and len(region) == 4):
            raise ToolError("region must be [left, top, right, bottom]")
        scale = float(args.get("scale") or 1.0)
        width = max(24, min(140, int(args.get("preview_width") or 64)))
        try:
            return _capture_output(
                os_backend,
                region=region,
                scale=max(0.1, min(1.0, scale)),
                preview_width=width,
                preview=bool(args.get("preview", True)),
            )
        except ComputerError as error:
            raise ToolError(str(error)) from error

    # ── input ─────────────────────────────────────────────────────
    async def computer_focus(args: dict[str, Any], ctx: ToolContext) -> str:
        os_backend, os_guard = resolve(ctx)
        _guard(os_guard, "focus")
        try:
            window = os_backend.focus_window(str(args.get("title") or ""), int(args.get("hwnd") or 0))
        except ComputerError as error:
            raise ToolError(str(error)) from error
        return f"focused 0x{window.hwnd:08x} · {window.title}"

    async def computer_click(args: dict[str, Any], ctx: ToolContext) -> str:
        os_backend, os_guard = resolve(ctx)
        _guard(os_guard, "click")
        x, y = int(args.get("x") or 0), int(args.get("y") or 0)
        try:
            action = os_backend.click(x, y, str(args.get("button") or "left"), bool(args.get("double")))
        except ComputerError as error:
            raise ToolError(str(error)) from error
        foreground = _foreground_label(os_backend)
        return f"{action}\nForeground: {foreground}\nCall computer_screenshot to verify the result."

    async def computer_move(args: dict[str, Any], ctx: ToolContext) -> str:
        os_backend, os_guard = resolve(ctx)
        _guard(os_guard, "move")
        try:
            return os_backend.move(int(args.get("x") or 0), int(args.get("y") or 0))
        except ComputerError as error:
            raise ToolError(str(error)) from error

    async def computer_drag(args: dict[str, Any], ctx: ToolContext) -> str:
        os_backend, os_guard = resolve(ctx)
        _guard(os_guard, "drag")
        try:
            return os_backend.drag(
                int(args.get("x1") or 0),
                int(args.get("y1") or 0),
                int(args.get("x2") or 0),
                int(args.get("y2") or 0),
                str(args.get("button") or "left"),
            )
        except ComputerError as error:
            raise ToolError(str(error)) from error

    async def computer_type(args: dict[str, Any], ctx: ToolContext) -> str:
        os_backend, os_guard = resolve(ctx)
        _guard(os_guard, "type")
        text = str(args.get("text") or "")
        if not text:
            raise ToolError("text is required")
        if len(text) > 20000:
            raise ToolError("text is limited to 20,000 characters per call")
        try:
            action = os_backend.type_text(text, float(args.get("interval") or 0.008))
        except ComputerError as error:
            raise ToolError(str(error)) from error
        return f"{action} (into {_foreground_label(os_backend) or 'the foreground window'})"

    async def computer_key(args: dict[str, Any], ctx: ToolContext) -> str:
        os_backend, os_guard = resolve(ctx)
        _guard(os_guard, "key")
        keys = str(args.get("keys") or "").strip()
        if not keys:
            raise ToolError("keys is required, e.g. 'ctrl+s'")
        try:
            action = os_backend.press(keys)
        except ComputerError as error:
            raise ToolError(str(error)) from error
        return f"{action} (foreground: {_foreground_label(os_backend) or 'unknown'})"

    async def computer_scroll(args: dict[str, Any], ctx: ToolContext) -> str:
        os_backend, os_guard = resolve(ctx)
        _guard(os_guard, "scroll")
        clicks = int(args.get("clicks") or 0)
        if clicks == 0:
            raise ToolError("clicks must be non-zero (positive = up, negative = down)")
        x, y = args.get("x"), args.get("y")
        try:
            return os_backend.scroll(
                clicks,
                int(x) if x is not None else None,
                int(y) if y is not None else None,
            )
        except ComputerError as error:
            raise ToolError(str(error)) from error

    async def computer_open(args: dict[str, Any], ctx: ToolContext) -> str:
        os_backend, os_guard = resolve(ctx)
        _guard(os_guard, "open")
        target = str(args.get("target") or "").strip()
        if not target:
            raise ToolError("target is required (app, file path or URL)")
        try:
            return os_backend.open_target(target)
        except ComputerError as error:
            raise ToolError(str(error)) from error

    async def computer_wait(args: dict[str, Any], ctx: ToolContext) -> str:
        seconds = max(0.1, min(15.0, float(args.get("seconds") or 1.0)))
        await asyncio.sleep(seconds)
        backend_now, _ = resolve(ctx)
        return f"waited {seconds:.1f}s · foreground: {_foreground_label(backend_now) or 'unknown'}"

    # ── vision agent ──────────────────────────────────────────────
    async def computer_task(args: dict[str, Any], ctx: ToolContext) -> str:
        task = str(args.get("task") or "").strip()
        if not task:
            raise ToolError("task is required")
        factory = ctx.extras.get("computer_driver")
        if not callable(factory):
            raise ToolError("The Gemini Computer driver is not available in this session.")
        driver = factory()
        steps = max(1, min(20, int(args.get("max_steps") or 10)))
        result = await driver.run(task, max_steps=steps)
        return result.render()

    # ── registration ──────────────────────────────────────────────
    registry.add(
        _spec(
            "computer_status",
            "Report screen size, monitors, foreground window and whether computer control is enabled. "
            "Start here before acting on the desktop.",
            schema({}, []),
            computer_status,
            risk="read",
            icon="▣",
        )
    )
    registry.add(
        _spec(
            "computer_windows",
            "List visible top-level windows with HWND, PID, size and title. Use it to find a target window.",
            schema({"limit": prop("integer", "Max rows", minimum=1, maximum=100)}, []),
            computer_windows,
            risk="read",
            icon="▤",
        )
    )
    registry.add(
        _spec(
            "computer_screenshot",
            "Capture the screen (or a region) to a PNG, returning metadata, the foreground window and an "
            "ASCII preview. Use it to observe before and after actions.",
            schema(
                {
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "[left, top, right, bottom] in screen pixels",
                    },
                    "scale": prop("number", "Downscale factor 0.1-1.0", minimum=0.1, maximum=1.0),
                    "preview": prop("boolean", "Include an ASCII preview (default true)"),
                    "preview_width": prop("integer", "ASCII columns", minimum=24, maximum=140),
                },
                [],
            ),
            computer_screenshot,
            risk="screen",
            icon="◉",
        )
    )
    registry.add(
        _spec(
            "computer_focus",
            "Bring a window to the foreground by title substring or HWND, restoring it if minimized.",
            schema(
                {
                    "title": prop("string", "Window title or substring"),
                    "hwnd": prop("integer", "Exact window handle (alternative to title)"),
                },
                [],
            ),
            computer_focus,
            dangerous=True,
            icon="⧉",
        )
    )
    registry.add(
        _spec(
            "computer_click",
            "Move the mouse and click at screen coordinates. Set double=true for a double click. "
            "Requires computer control to be enabled.",
            schema(
                {
                    "x": prop("integer", "Screen x"),
                    "y": prop("integer", "Screen y"),
                    "button": prop("string", "left | right | middle", enum=["left", "right", "middle"]),
                    "double": prop("boolean", "Double click"),
                },
                ["x", "y"],
            ),
            computer_click,
            dangerous=True,
            icon="◈",
        )
    )
    registry.add(
        _spec(
            "computer_move",
            "Move the mouse cursor without clicking.",
            schema({"x": prop("integer", "Screen x"), "y": prop("integer", "Screen y")}, ["x", "y"]),
            computer_move,
            dangerous=True,
            icon="↗",
        )
    )
    registry.add(
        _spec(
            "computer_drag",
            "Press, move and release the mouse between two screen points (drag and drop).",
            schema(
                {
                    "x1": prop("integer", "Start x"), "y1": prop("integer", "Start y"),
                    "x2": prop("integer", "End x"), "y2": prop("integer", "End y"),
                    "button": prop("string", "left | right | middle", enum=["left", "right", "middle"]),
                },
                ["x1", "y1", "x2", "y2"],
            ),
            computer_drag,
            dangerous=True,
            icon="⇥",
        )
    )
    registry.add(
        _spec(
            "computer_type",
            "Type Unicode text into the foreground window (supports Vietnamese). "
            "Click the target field first.",
            schema(
                {
                    "text": prop("string", "Text to type; \\n creates a newline"),
                    "interval": prop("number", "Delay between characters in seconds", minimum=0.0, maximum=0.2),
                },
                ["text"],
            ),
            computer_type,
            dangerous=True,
            icon="⌨",
        )
    )
    registry.add(
        _spec(
            "computer_key",
            "Press a key or combination, e.g. 'enter', 'ctrl+s', 'alt+tab', 'win+r', 'f5'.",
            schema({"keys": prop("string", "Key combo")}, ["keys"]),
            computer_key,
            dangerous=True,
            icon="⌥",
        )
    )
    registry.add(
        _spec(
            "computer_scroll",
            "Scroll the mouse wheel. Positive goes up, negative down; optional x/y moves the cursor first.",
            schema(
                {
                    "clicks": prop("integer", "Notches, positive = up"),
                    "x": prop("integer", "Optional screen x"),
                    "y": prop("integer", "Optional screen y"),
                },
                ["clicks"],
            ),
            computer_scroll,
            dangerous=True,
            icon="↕",
        )
    )
    registry.add(
        _spec(
            "computer_open",
            "Open an application, file, folder or URL with the shell (like double-clicking it).",
            schema({"target": prop("string", "Executable, path or URL")}, ["target"]),
            computer_open,
            dangerous=True,
            icon="↗",
        )
    )
    registry.add(
        _spec(
            "computer_wait",
            "Wait briefly for an application to react, then report the foreground window.",
            schema({"seconds": prop("number", "0.1-15", minimum=0.1, maximum=15.0)}, ["seconds"]),
            computer_wait,
            risk="read",
            icon="◷",
        )
    )
    registry.add(
        _spec(
            "computer_task",
            "Delegate a desktop task to the Gemini Computer vision agent: it screenshots, decides, "
            "clicks/types/scrolls, and verifies. Each action still goes through approval.",
            schema(
                {
                    "task": prop("string", "What to accomplish on screen"),
                    "max_steps": prop("integer", "Vision-action steps", minimum=1, maximum=20),
                },
                ["task"],
            ),
            computer_task,
            dangerous=True,
            icon="◆",
        )
    )
