from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gemini_tui.agent.code.index import build_index
from gemini_tui.agent.code.review import detect_test_command, format_findings, review_files


PY_APP = '''"""Sample module."""

import os


class Greeter:
    """Say hello."""

    def greet(self, name: str) -> str:
        return f"hello {name}"


def main() -> None:
    print(Greeter().greet("world"))
'''

BAD_PY = '''import os

password = "hunter2hunter2hunter2"


def run() -> None:
    eval("1 + 1")
    try:
        pass
    except:
        pass
'''


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="code-index-"))
        (self.root / "app.py").write_text(PY_APP, encoding="utf-8")
        (self.root / "bad.py").write_text(BAD_PY, encoding="utf-8")
        (self.root / "notes.md").write_text("# Notes\n\nSome text about greeters.\n", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "util.ts").write_text("export function greet(name: string) { return name; }\n", encoding="utf-8")
        self.index = build_index(str(self.root), max_files=100)

    def test_files_and_symbols(self) -> None:
        self.assertGreaterEqual(self.index.file_count, 4)
        self.assertGreaterEqual(self.index.symbol_count, 2)
        self.assertIn("app.py", self.index.files)
        names = {symbol.name for symbol in self.index.files["app.py"].symbols}
        self.assertIn("Greeter", names)
        self.assertIn("main", names)

    def test_summary_has_languages(self) -> None:
        summary = self.index.summary()
        self.assertIn("python", summary)
        self.assertIn("app.py", summary)

    def test_search(self) -> None:
        hits = self.index.search("greet")
        self.assertTrue(hits)
        self.assertEqual(hits[0].path, "app.py")
        self.assertIn("greet", hits[0].snippet.lower())

    def test_outline(self) -> None:
        outline = self.index.outline("app.py")
        self.assertIn("Greeter", outline)
        self.assertIn("main", outline)
        self.assertIn("imports", outline.lower())

    def test_pack(self) -> None:
        packed = self.index.pack(["app.py"], max_chars=6000)
        self.assertIn("<repository", packed)
        self.assertIn("app.py", packed)
        self.assertIn("class Greeter", packed)

    def test_cache_roundtrip(self) -> None:
        path = self.root / "index.json"
        self.index.save(path)
        from gemini_tui.agent.code.index import CodeIndex

        loaded = CodeIndex.load(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.file_count, self.index.file_count)


class ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="code-review-"))
        (self.root / "bad.py").write_text(BAD_PY, encoding="utf-8")

    def test_findings_include_secret_and_eval(self) -> None:
        findings = review_files(self.root)
        rules = {finding.rule for finding in findings}
        self.assertIn("possible-secret", rules)
        self.assertTrue(any("eval" in rule for rule in rules))

    def test_format_findings(self) -> None:
        text = format_findings(review_files(self.root))
        self.assertIn("finding", text)
        self.assertIn("bad.py", text)

    def test_detect_pytest(self) -> None:
        (self.root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
        command, source = detect_test_command(self.root)
        self.assertIn("pytest", command)
        self.assertEqual(source, "pytest config")

    def test_detect_none(self) -> None:
        empty = Path(tempfile.mkdtemp(prefix="empty-"))
        self.assertEqual(detect_test_command(empty), ("", ""))


if __name__ == "__main__":
    unittest.main()
