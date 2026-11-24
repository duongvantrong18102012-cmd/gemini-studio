from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from gemini_tui.agent.computer.backend import (
    BaseBackend,
    ComputerError,
    ScreenInfo,
    Shot,
    ascii_preview,
    create_backend,
    parse_keys,
)
from gemini_tui.agent.computer.tools import register_computer_tools
from gemini_tui.agent.permissions import ComputerGuard
from gemini_tui.tools.registry import ToolContext, ToolError, ToolRegistry


def settings(**overrides):
    base = dict(computer_enabled=True, computer_approval="ask", computer_rate_limit=90)
    base.update(overrides)
    return SimpleNamespace(**base)


class AsciiPreviewTests(unittest.TestCase):
    def test_preview_uses_real_pixels_and_size(self) -> None:
        image = Image.new("L", (64, 32), 0)
        # Paint the top half white so the ramp must produce two distinct rows.
        for x in range(64):
            for y in range(16):
                image.putpixel((x, y), 255)
        text = ascii_preview(image, width=32)
        lines = text.splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(len(line) == 32 for line in lines))
        self.assertNotEqual(lines[0], lines[-1])
        self.assertTrue(set(lines[0]) <= set(" .:-=+*#%@"))

    def test_preview_handles_none_and_invert(self) -> None:
        self.assertEqual(ascii_preview(None), "")
        image = Image.new("L", (16, 16), 0)
        normal = ascii_preview(image, width=16, height=8)
        inverted = ascii_preview(image, width=16, height=8, invert=True)
        self.assertNotEqual(normal, inverted)


class KeyParsingTests(unittest.TestCase):
    def test_combos(self) -> None:
        self.assertEqual(parse_keys("ctrl+shift+s"), [0x11, 0x10, ord("S")])
        self.assertEqual(parse_keys("Enter"), [0x0D])
        self.assertEqual(parse_keys("f5"), [0x74])
        self.assertEqual(parse_keys("a"), [ord("A")])

    def test_unknown_keys_raise(self) -> None:
        with self.assertRaises(ComputerError):
            parse_keys("not-a-key")
        with self.assertRaises(ComputerError):
            parse_keys("")


class FakeBackend(BaseBackend):
    name = "fake"
    available = True

    def __init__(self, image_path: Path) -> None:
        self.image_path = image_path
        self.clicks: list[tuple[int, int]] = []

    def screen_info(self) -> ScreenInfo:
        return ScreenInfo(width=800, height=600, virtual_width=800, virtual_height=600)

    def capture(self, region=None, scale: float = 1.0) -> Shot:
        return Shot(
            path=str(self.image_path),
            width=64,
            height=32,
            original_width=800,
            original_height=600,
            region=None,
            scale=1.0,
        )

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> str:
        self.clicks.append((x, y))
        return f"clicked {x},{y}"

    def move(self, x: int, y: int) -> str:
        return f"moved {x},{y}"


class BackendAndGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="computer-test-"))
        self.image_path = self.tmp / "shot.png"
        Image.new("RGB", (64, 32), (20, 40, 90)).save(self.image_path)
        self.backend = FakeBackend(self.image_path)
        self.registry = ToolRegistry()
        self.guard = ComputerGuard(settings(), backend_available=True)
        register_computer_tools(self.registry, self.backend, self.guard)

    def ctx(self, **config) -> ToolContext:
        return ToolContext(
            settings=settings(**config),
            workspace=str(self.tmp),
            cwd=str(self.tmp),
            computer=self.backend,
        )

    async def test_screenshot_includes_real_ascii_preview(self) -> None:
        spec = self.registry.get("computer_screenshot")
        self.assertIsNotNone(spec)
        output = await spec.handler({"preview": True, "preview_width": 24}, self.ctx())
        self.assertIn("Screenshot saved", output)
        self.assertIn("ASCII preview", output)
        self.assertEqual(self.guard.total_actions, 1)

    async def test_disabled_guard_blocks_screenshot(self) -> None:
        disabled = ComputerGuard(settings(computer_enabled=False), backend_available=True)
        registry = ToolRegistry()
        register_computer_tools(registry, self.backend, disabled)
        spec = registry.get("computer_screenshot")
        with self.assertRaises(ToolError):
            await spec.handler({}, self.ctx(computer_enabled=False))

    async def test_click_goes_through_guard(self) -> None:
        spec = self.registry.get("computer_click")
        output = await spec.handler({"x": 10, "y": 20}, self.ctx())
        self.assertIn("clicked 10,20", output)
        self.assertEqual(self.backend.clicks, [(10, 20)])
        self.assertEqual(self.guard.total_actions, 1)

    async def test_rate_limit_is_enforced(self) -> None:
        limited = ComputerGuard(settings(computer_rate_limit=1), backend_available=True)
        registry = ToolRegistry()
        register_computer_tools(registry, self.backend, limited)
        spec = registry.get("computer_move")
        await spec.handler({"x": 1, "y": 1}, self.ctx())
        with self.assertRaises(ToolError):
            await spec.handler({"x": 2, "y": 2}, self.ctx())

    def test_create_backend_is_explicit(self) -> None:
        backend = create_backend(self.tmp)
        self.assertIn(backend.name, {"windows", "screenshot-only", "unsupported"})
        self.assertIsInstance(backend.available, bool)

    def test_base_backend_refuses_input(self) -> None:
        with self.assertRaises(ComputerError):
            BaseBackend().click(1, 1)


if __name__ == "__main__":
    unittest.main()
