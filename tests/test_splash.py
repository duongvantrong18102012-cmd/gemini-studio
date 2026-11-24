"""Splash sigil: responsive tiers and an honest ASCII fallback."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gemini_tui.widgets.overlays import (
    SPLASH_GLYPH,
    SPLASH_GLYPH_ASCII,
    SPLASH_GLYPH_COMPACT,
    SPLASH_GLYPH_COMPACT_ASCII,
    SPLASH_GLYPH_MINIMAL,
    SPLASH_GLYPH_MINIMAL_ASCII,
    select_splash_art,
    splash_tier,
    unicode_supported,
)


class TierTests(unittest.TestCase):
    def test_large_terminal_gets_the_full_sigil(self) -> None:
        self.assertEqual(splash_tier(120, 42), "full")
        self.assertEqual(splash_tier(74, 34), "full")

    def test_common_small_terminal_gets_the_compact_sigil(self) -> None:
        # 80x24 is the size the app smoke tests treat as the minimum usable.
        self.assertEqual(splash_tier(80, 24), "compact")
        self.assertEqual(splash_tier(46, 20), "compact")

    def test_very_small_terminal_gets_one_line(self) -> None:
        self.assertEqual(splash_tier(40, 18), "minimal")
        self.assertEqual(splash_tier(20, 10), "minimal")


class ArtTests(unittest.TestCase):
    def test_every_variant_is_non_empty_and_has_no_tabs(self) -> None:
        for variant in (
            SPLASH_GLYPH,
            SPLASH_GLYPH_ASCII,
            SPLASH_GLYPH_COMPACT,
            SPLASH_GLYPH_COMPACT_ASCII,
            SPLASH_GLYPH_MINIMAL,
            SPLASH_GLYPH_MINIMAL_ASCII,
        ):
            self.assertTrue(variant)
            for line in variant:
                self.assertTrue(line.strip())
                self.assertNotIn("\t", line)

    def test_full_sigil_fits_the_full_splash_panel(self) -> None:
        # 80 wide panel minus 3 cells of padding on each side and the border.
        width = max(len(line) for line in SPLASH_GLYPH)
        self.assertLessEqual(width, 72)
        self.assertLessEqual(max(len(line) for line in SPLASH_GLYPH_ASCII), 72)

    def test_compact_and_minimal_fit_narrow_terminals(self) -> None:
        self.assertLessEqual(max(len(line) for line in SPLASH_GLYPH_COMPACT), 56)
        self.assertLessEqual(max(len(line) for line in SPLASH_GLYPH_COMPACT_ASCII), 56)
        self.assertLessEqual(max(len(line) for line in SPLASH_GLYPH_MINIMAL), 44)
        self.assertLessEqual(max(len(line) for line in SPLASH_GLYPH_MINIMAL_ASCII), 44)

    def test_ascii_variant_contains_only_ascii(self) -> None:
        for variant in (
            SPLASH_GLYPH_ASCII,
            SPLASH_GLYPH_COMPACT_ASCII,
            SPLASH_GLYPH_MINIMAL_ASCII,
        ):
            for line in variant:
                line.encode("ascii")

    def test_selection_matches_the_requested_tier_and_encoding(self) -> None:
        self.assertEqual(select_splash_art(120, 42, unicode=True), SPLASH_GLYPH)
        self.assertEqual(select_splash_art(120, 42, unicode=False), SPLASH_GLYPH_ASCII)
        self.assertEqual(select_splash_art(80, 24, unicode=True), SPLASH_GLYPH_COMPACT)
        self.assertEqual(select_splash_art(80, 24, unicode=False), SPLASH_GLYPH_COMPACT_ASCII)
        self.assertEqual(select_splash_art(40, 18, unicode=True), SPLASH_GLYPH_MINIMAL)
        self.assertEqual(select_splash_art(40, 18, unicode=False), SPLASH_GLYPH_MINIMAL_ASCII)

    def test_selection_returns_a_copy(self) -> None:
        art = select_splash_art(120, 42)
        art.append("mutated")
        self.assertNotIn("mutated", SPLASH_GLYPH)


class EncodingTests(unittest.TestCase):
    def test_utf8_terminal_supports_the_sigil(self) -> None:
        self.assertTrue(unicode_supported(SimpleNamespace(encoding="utf-8")))

    def test_cp1252_terminal_falls_back_to_ascii(self) -> None:
        self.assertFalse(unicode_supported(SimpleNamespace(encoding="cp1252")))
        self.assertFalse(unicode_supported(SimpleNamespace(encoding="ascii")))

    def test_env_override_forces_ascii(self) -> None:
        with patch.dict(os.environ, {"GEMINI_TUI_ASCII": "1"}):
            self.assertFalse(unicode_supported(SimpleNamespace(encoding="utf-8")))

    def test_unknown_encoding_falls_back_instead_of_crashing(self) -> None:
        self.assertFalse(unicode_supported(SimpleNamespace(encoding="not-a-codec")))


if __name__ == "__main__":
    unittest.main()
