"""Gemini Computer: Windows observation and input, plus the vision driver."""

from __future__ import annotations

from .backend import (
    BaseBackend,
    ComputerError,
    ScreenInfo,
    Shot,
    WindowInfo,
    ascii_preview,
    create_backend,
    parse_keys,
)
from .driver import ComputerAction, ComputerDriver, ComputerRunResult
from .tools import register_computer_tools

__all__ = [
    "BaseBackend",
    "ComputerAction",
    "ComputerDriver",
    "ComputerError",
    "ComputerRunResult",
    "ScreenInfo",
    "Shot",
    "WindowInfo",
    "ascii_preview",
    "create_backend",
    "parse_keys",
    "register_computer_tools",
]
