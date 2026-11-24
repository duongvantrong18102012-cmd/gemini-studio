"""Small, dependency-free animation and formatting helpers.

These render Rich renderables; the widgets only swap the renderable when it
actually changed, which keeps the terminal from flickering.
"""

from __future__ import annotations

import math
import random
import time
from typing import Iterable, Sequence

from rich.text import Text

# ══════════════════════════════════════════════════════════════════════
# Colour maths
# ══════════════════════════════════════════════════════════════════════
def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def rgb_to_hex(rgb: Sequence[float]) -> str:
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def mix(color_a: str, color_b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    a = hex_to_rgb(color_a)
    b = hex_to_rgb(color_b)
    return rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def sample_gradient(colors: Sequence[str], t: float) -> str:
    """Sample a multi-stop gradient, `t` in [0, 1]."""
    if not colors:
        return "#ffffff"
    if len(colors) == 1:
        return colors[0]
    t = t % 1.0
    scaled = t * (len(colors) - 1)
    index = min(int(scaled), len(colors) - 2)
    return mix(colors[index], colors[index + 1], scaled - index)


# ══════════════════════════════════════════════════════════════════════
# Text effects
# ══════════════════════════════════════════════════════════════════════
def gradient(
    text: str,
    colors: Sequence[str],
    phase: float = 0.0,
    *,
    bold: bool = False,
    dim: bool = False,
    spacing: float = 0.055,
) -> Text:
    """Colour every character along a moving gradient."""
    if not text:
        return Text("")
    if len(text) > 1:
        step = min(0.12, spacing * 2)
    else:
        step = 0.0
    result = Text()
    span = max(1, len(text) - 1)
    for index, char in enumerate(text):
        t = phase + (index / span) * step * (span / 2)
        result.append(
            char,
            style=f"bold {sample_gradient(colors, t)}" if bold else sample_gradient(colors, t),
        )
    if dim:
        result.stylize("dim")
    return result


def shimmer(
    text: str,
    base: str,
    highlight: str,
    phase: float,
    *,
    width: int = 4,
    bold: bool = False,
) -> Text:
    """A highlight sweeping across static text (banner logos, titles)."""
    result = Text()
    length = max(1, len(text))
    head = (phase % 1.0) * (length + width * 2) - width
    for index, char in enumerate(text):
        distance = abs(index - head)
        if distance <= width:
            strength = 1.0 - (distance / (width + 1)) ** 1.5
            colour = mix(base, highlight, strength)
        else:
            colour = base
        result.append(char, style=f"bold {colour}" if bold else colour)
    return result


def shimmer_gradient(
    text: str,
    colors: Sequence[str],
    phase: float,
    *,
    width: float = 0.16,
    bold: bool = True,
) -> Text:
    """Moving gradient with a brighter head — used for the wordmark."""
    result = Text()
    if not text:
        return result
    span = max(1, len(text) - 1)
    head = (phase % 1.0)
    for index, char in enumerate(text):
        position = index / span if span else 0.0
        t = (position + phase) % 1.0
        colour = sample_gradient(colors, t)
        distance = abs(((position - head + 0.5) % 1.0) - 0.5)
        if distance < width:
            boost = 1.0 - distance / width
            colour = mix(colour, "#ffffff", 0.55 * boost)
        result.append(char, style=f"bold {colour}" if bold else colour)
    return result


SPINNERS: dict[str, str] = {
    "braille": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    "orbit": "⣾⣽⣻⢿⡿⣟⣯⣷",
    "dots": "⓰⓱⓲⓳⓴⓵⓶⓷⓸",
    "line": "⠂⠒⠲⠴⠦⠖⠒⠐",
    "pulse": "●◐◓◑◒",
    "arrow": "←↖↑↗→↘↓↙",
    "globe": "🌍🌎🌏",
}


def spinner(frame: int, style: str = "braille") -> str:
    glyphs = SPINNERS.get(style, SPINNERS["braille"])
    return glyphs[frame % len(glyphs)]


BAR_GLYPHS = " ▁▂▃▄▅▆▇█"


def signal_bars(
    level: float,
    frame: float,
    count: int = 12,
    *,
    idle: float = 0.12,
) -> Text:
    """Animated equalizer. `level` 0..1 scales the amplitude."""
    result = Text()
    level = max(0.0, min(1.0, level))
    for index in range(count):
        wave = (
            math.sin((index * 0.9) + frame * 2.1) * 0.5
            + math.sin((index * 0.37) - frame * 1.3) * 0.5
        )
        height = idle + abs(wave) * (0.25 + level * 0.75)
        height = max(0.0, min(1.0, height))
        glyph_index = int(height * (len(BAR_GLYPHS) - 1))
        colour = sample_gradient(("#0f7f92", "#2ae8ff", "#a273ff"), (index / count + frame * 0.15) % 1.0)
        result.append(BAR_GLYPHS[glyph_index], style=colour)
    return result


def meter(value: float, width: int = 10, *, full: str = "█", empty: str = "░") -> str:
    value = max(0.0, min(1.0, value))
    filled = int(round(value * width))
    return full * filled + empty * (width - filled)


def pulse_dot(frame: int, on: str = "#3dffa8", off: str = "#1c4a36") -> Text:
    """Two-frame breathing dot, cheap enough to run continuously."""
    strength = 0.5 + 0.5 * math.sin(frame * 0.25)
    return Text("●", style=mix(off, on, strength))


def typewriter(text: str, progress: float, cursor: str = "▌") -> Text:
    cut = int(len(text) * max(0.0, min(1.0, progress)))
    body = Text(text[:cut])
    if cut < len(text):
        body.append(cursor, style="bold #2ae8ff")
    return body


def sparkline(values: Iterable[float], width: int = 24) -> str:
    data = [float(v) for v in values][-width:]
    if not data:
        return ""
    peak = max(data) or 1.0
    return "".join(BAR_GLYPHS[max(0, min(8, int((v / peak) * 8)))] for v in data)


# ══════════════════════════════════════════════════════════════════════
# Formatting
# ══════════════════════════════════════════════════════════════════════
def format_tokens(count: float | int | None) -> str:
    if not count:
        return "0"
    count = float(count)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(int(count))


def format_bytes(count: float | int | None) -> str:
    if not count:
        return "0 B"
    count = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(count) < 1024 or unit == "TB":
            return f"{count:.1f} {unit}" if unit != "B" else f"{int(count)} B"
        count /= 1024
    return f"{count:.1f} TB"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def truncate(text: str, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def age(timestamp: float | None) -> str:
    if not timestamp:
        return "—"
    delta = max(0.0, time.time() - timestamp)
    if delta < 60:
        return f"{delta:.0f}s"
    if delta < 3600:
        return f"{delta / 60:.0f}m"
    if delta < 86400:
        return f"{delta / 3600:.1f}h"
    return f"{delta / 86400:.0f}d"


def random_state(seed: int) -> random.Random:
    return random.Random(seed)
