"""Gemini Computer: a screenshot → decide → act → verify loop.

Each cycle sends the latest screenshot to a multimodal Gemini model together
with the action history, executes the returned tool calls through the normal
registry (approval, policy, audit) and captures a fresh screenshot. Nothing is
faked: if the model is unavailable or an action fails, the run reports it.
"""

from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ...tools.registry import ToolContext, ToolError
from ..permissions import ComputerGuard, PermissionDenied

COMPUTER_TOOLS = (
    "computer_screenshot",
    "computer_click",
    "computer_move",
    "computer_type",
    "computer_key",
    "computer_scroll",
    "computer_drag",
    "computer_open",
    "computer_focus",
    "computer_windows",
    "computer_wait",
)

SYSTEM_PROMPT = """You are Gemini Computer, a careful desktop operator on Windows.

You can see the screen through screenshots and act through mouse and keyboard tools.

Rules:
- Always ground coordinates in the screenshot you received. Never invent UI positions.
- Prefer one action per turn, then look again. State briefly what you see and why you act.
- Click the centre of a target, not its edge. If text is small, zoom by capturing a region.
- Use computer_focus to switch windows instead of clicking taskbar guesses when the title is known.
- Do not type passwords, payment details or other secrets. If a task requires them, stop and ask the human.
- Verify after each action with a screenshot. If the screen did not change, try a different approach.
- When the task is complete, stop calling tools and reply with: DONE: <what you verified>.
- If you are blocked, reply with: BLOCKED: <what the human must do>.
"""


@dataclass
class ComputerAction:
    step: int
    tool: str
    args: dict[str, Any]
    ok: bool
    output: str
    elapsed: float = 0.0

    def render(self) -> str:
        flag = "ok " if self.ok else "ERR"
        detail = self.args.get("text") or self.args.get("keys") or self.args.get("target") or ""
        if not detail and "x" in self.args:
            detail = f"({self.args.get('x')}, {self.args.get('y')})"
        return f"{self.step:>2}. {flag} {self.tool:<18} {str(detail)[:70]}"


@dataclass
class ComputerRunResult:
    task: str
    status: str = "done"              # done | blocked | error | max_steps
    steps: int = 0
    actions: list[ComputerAction] = field(default_factory=list)
    final_text: str = ""
    screenshots: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    error: str = ""

    def render(self, limit: int = 30) -> str:
        lines = [
            f"Gemini Computer · {self.status} · {self.steps} step(s) in {self.elapsed:.1f}s",
            f"Task: {self.task}",
        ]
        if self.actions:
            lines.append("")
            lines.extend(action.render() for action in self.actions[-limit:])
        if self.final_text:
            lines.append("")
            lines.append(self.final_text[:3000])
        if self.error:
            lines.append("")
            lines.append("ERROR: " + self.error[:1200])
        return "\n".join(lines)


def _jpeg_part(path: str, max_width: int) -> Any:
    from PIL import Image
    from google.genai import types

    with Image.open(path) as image:
        if image.width > max_width:
            ratio = max_width / image.width
            image = image.resize((max_width, max(1, int(image.height * ratio))), Image.LANCZOS)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=82, optimize=True)
    return types.Part(inline_data=types.Blob(data=buffer.getvalue(), mime_type="image/jpeg"))


class ComputerDriver:
    def __init__(
        self,
        service: Any,
        registry: Any,
        settings: Any,
        backend: Any,
        guard: ComputerGuard,
        ctx: ToolContext,
        *,
        on_event: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.service = service
        self.registry = registry
        self.settings = settings
        self.backend = backend
        self.guard = guard
        self.ctx = ctx
        self.on_event = on_event or (lambda *_: None)

    # ── helpers ───────────────────────────────────────────────────
    def _capture(self, region: Any = None, scale: float = 1.0) -> str:
        try:
            self.guard.check("screenshot")
        except PermissionDenied as error:
            raise ToolError(str(error)) from error
        shot = self.backend.capture(region=region, scale=scale)
        if getattr(self.ctx, "audit", None) is not None:
            try:
                self.ctx.audit.record(
                    "computer",
                    tool="screenshot",
                    ok=True,
                    risk="screen",
                    args={"region": region, "scale": scale, "path": shot.path},
                    session=getattr(self.ctx, "session_id", ""),
                    agent="computer",
                )
            except Exception:
                pass
        return shot.path

    def _declarations(self) -> list[Any]:
        specs = []
        for name in COMPUTER_TOOLS:
            spec = self.registry.get(name)
            if spec is not None and spec.enabled:
                specs.append(spec)
        # computer_task must not recurse into itself
        return [spec.declaration() for spec in specs if spec.name != "computer_task"]

    def _image_width(self) -> int:
        return max(480, min(1920, int(getattr(self.settings, "computer_vision_width", 1024) or 1024)))

    # ── main loop ─────────────────────────────────────────────────
    async def run(self, task: str, max_steps: int = 10) -> ComputerRunResult:
        from google.genai import types

        started = time.perf_counter()
        result = ComputerRunResult(task=task)
        if not getattr(self.settings, "computer_enabled", False):
            result.status = "error"
            result.error = "Computer control is disabled. Enable it with /computer enable or --computer."
            return result

        model = str(getattr(self.settings, "computer_model", "") or self.settings.model)
        declarations = self._declarations()
        if not declarations:
            result.status = "error"
            result.error = "No computer tools are registered."
            return result

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.15,
            max_output_tokens=1600,
            tools=[types.Tool(function_declarations=declarations)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        action_log: list[str] = []
        last_action_note = ""
        try:
            first_shot = await asyncio.to_thread(self._capture)
            result.screenshots.append(first_shot)
            self.on_event("status", "computer: observing desktop", {"shot": first_shot})
        except ToolError as error:
            result.status = "error"
            result.error = str(error)
            return result

        for step in range(1, max_steps + 1):
            result.steps = step
            state = (
                f"Task: {task}\n\n"
                f"Action history:\n" + ("\n".join(action_log[-12:]) if action_log else "(none yet)") + "\n\n"
                f"Latest action: {last_action_note or '(start)'}\n"
                "Look at the screenshot. Either call the next tool or reply DONE/BLOCKED."
            )
            shot_path = result.screenshots[-1]
            contents = [
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=state),
                        await asyncio.to_thread(_jpeg_part, shot_path, self._image_width()),
                    ],
                )
            ]
            self.on_event("status", f"computer: thinking (step {step}/{max_steps})", {})
            try:
                response = await self.service.client().aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
            except Exception as error:
                result.status = "error"
                result.error = f"Gemini vision call failed: {error}"
                break

            candidate = (getattr(response, "candidates", None) or [None])[0]
            parts = list(getattr(getattr(candidate, "content", None), "parts", None) or [])
            calls = [getattr(part, "function_call", None) for part in parts if getattr(part, "function_call", None)]
            texts = [
                getattr(part, "text", "") or ""
                for part in parts
                if not getattr(part, "function_call", None) and not getattr(part, "thought", False)
            ]
            narrative = " ".join(text.strip() for text in texts if text.strip())
            if narrative:
                self.on_event("text", narrative, {"step": step})
                result.final_text = narrative

            if not calls:
                upper = narrative.upper()
                if "BLOCKED:" in upper:
                    result.status = "blocked"
                elif "DONE:" in upper or narrative:
                    result.status = "done"
                else:
                    result.status = "done"
                break

            for call in calls:
                name = str(getattr(call, "name", ""))
                args = dict(getattr(call, "args", None) or {})
                if name == "computer_task":
                    continue
                action_started = time.perf_counter()
                self.on_event("status", f"computer: {name}", {"args": args})
                try:
                    output = await self.registry.call(name, args, self.ctx)
                    ok = True
                except ToolError as error:
                    output = f"ERROR: {error}"
                    ok = False
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    output = f"ERROR: {type(error).__name__}: {error}"
                    ok = False
                elapsed = time.perf_counter() - action_started
                action = ComputerAction(step, name, args, ok, output, elapsed)
                result.actions.append(action)
                action_log.append(action.render())
                last_action_note = f"{name} -> {'ok' if ok else 'failed'}: {output[:300]}"
                self.on_event(
                    "tool",
                    output[:800],
                    {"tool": name, "ok": ok, "args": args, "step": step},
                )
                # Screenshot after every action so the next decision is grounded.
                try:
                    shot = await asyncio.to_thread(self._capture)
                    result.screenshots.append(shot)
                except ToolError as error:
                    action_log.append(f"screenshot failed: {error}")
                    break

        else:
            result.status = "max_steps"
            result.final_text = result.final_text or f"Stopped after {max_steps} steps without a final answer."

        result.elapsed = time.perf_counter() - started
        if not getattr(self.settings, "computer_keep_screenshots", True):
            for path in result.screenshots:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
        result.screenshots = result.screenshots[-8:]
        return result
