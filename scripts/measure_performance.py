"""Reproducible performance measurements for the RED Gemini Studio TUI.

These numbers are environment-specific. The script prints what it actually
measured; nothing here is a target or a guarantee. Run it with:

    python scripts/measure_performance.py

It needs no API key and makes no network calls: the model catalogue and dotenv
loading are patched out, exactly like the smoke tests do.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

from textual.app import App, ComposeResult

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gemini_tui.ai.client import GeminiService  # noqa: E402
from gemini_tui.app import GeminiStudio  # noqa: E402
from gemini_tui.widgets.chat import AssistantMessage, ToolCard  # noqa: E402
from gemini_tui.widgets.views import DashboardView  # noqa: E402


async def _no_models(self):
    return []


class FakeStream:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.stopped = False

    async def write(self, text: str) -> None:
        self.writes.append(text)

    async def stop(self) -> None:
        self.stopped = True


async def measure_boot(size: tuple[int, int], runs: int = 3) -> dict[str, float]:
    timings: list[float] = []
    for _ in range(runs):
        app = GeminiStudio(workspace=str(ROOT), splash=False, audit=False)
        started = time.perf_counter()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            timings.append(time.perf_counter() - started)
    return {
        "median": statistics.median(timings),
        "min": min(timings),
        "max": max(timings),
        "runs": timings,
    }


async def measure_hidden_dashboard(
    size: tuple[int, int], seconds: float = 3.2
) -> dict[str, int]:
    counters = {
        "cpu_percent": 0,
        "virtual_memory": 0,
        "net_io_counters": 0,
        "cpu_count": 0,
        "boot_time": 0,
        "disk_usage": 0,
        "process_iter": 0,
    }

    def counting(name, original):
        def wrapper(*args, **kwargs):
            counters[name] += 1
            return original(*args, **kwargs)

        return wrapper

    patches = []
    for name in counters:
        original = getattr(__import__("psutil"), name)
        patches.append(
            patch(f"gemini_tui.widgets.views.psutil.{name}", counting(name, original))
        )
    for item in patches:
        item.start()
    try:
        app = GeminiStudio(workspace=str(ROOT), splash=False, audit=False)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            dashboard = app.query_one(DashboardView)
            assert not dashboard.visible(), "dashboard should be hidden on boot"
            for name in counters:
                counters[name] = 0
            await asyncio.sleep(seconds)
            await pilot.pause()
    finally:
        for item in reversed(patches):
            item.stop()
    return counters


async def measure_streaming(tokens: int = 2000) -> dict[str, float]:
    message = AssistantMessage("test-model")
    stream = FakeStream()
    message._stream = stream
    started = time.perf_counter()
    for _ in range(tokens):
        await message.write("token ")
    await message._flush_task
    elapsed = time.perf_counter() - started
    return {
        "tokens": tokens,
        "writes": len(stream.writes),
        "bytes": sum(len(chunk) for chunk in stream.writes),
        "elapsed_s": elapsed,
    }


async def measure_tool_timer() -> dict[str, object]:
    class CardApp(App):
        def compose(self) -> ComposeResult:
            yield ToolCard("call-1", "fs_read", {"path": "a.py"}, False)

    app = CardApp()
    app.frame = 0
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        card = app.query_one(ToolCard)
        running = card._timer is not None
        card.finish(True, "read 3 lines", 0.12)
        await asyncio.sleep(0.35)
        stopped = card._timer is None
    return {"timer_before_finish": running, "timer_stopped_after_finish": stopped}


async def main_async() -> None:
    print("RED Gemini Studio - measured performance")
    print(f"Python {sys.version.split()[0]} · {sys.platform}")
    print("No network, no API key. Times are wall clock on this machine only.\n")

    with (
        patch("gemini_tui.app.load_dotenv_files", lambda: None),
        patch.object(GeminiService, "list_models", _no_models),
    ):
        print("boot (start run_test -> first pause):")
        for size in ((120, 42), (80, 24)):
            result = await measure_boot(size)
            print(
                f"  {size[0]}x{size[1]}: median {result['median']:.3f}s "
                f"(min {result['min']:.3f}s, max {result['max']:.3f}s over "
                f"{len(result['runs'])} runs)"
            )

        print("\nhidden dashboard psutil calls while chat is visible (3.2s):")
        for size in ((120, 42), (80, 24)):
            counters = await measure_hidden_dashboard(size)
            total = sum(counters.values())
            print(f"  {size[0]}x{size[1]}: total {total} -> {counters}")

        print("\nstream buffering (2000 tokens fed as fast as the loop allows):")
        stream = await measure_streaming()
        print(
            f"  tokens {int(stream['tokens'])}, stream writes {int(stream['writes'])}, "
            f"{int(stream['bytes'])} chars, {stream['elapsed_s'] * 1000:.1f} ms total"
        )

        print("\ntool card refresh timer:")
        timer = await measure_tool_timer()
        print(f"  {timer}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
