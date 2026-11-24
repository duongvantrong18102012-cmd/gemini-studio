from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gemini_tui.agent.context import (
    ContextManager,
    LazyIndex,
    Retriever,
    build_context_block,
    estimate_tokens,
    load_project_brief,
)


class FakePart:
    def __init__(self, text="", *, function_response=None) -> None:
        self.text = text
        self.function_response = function_response


class FakeContent:
    def __init__(self, *parts) -> None:
        self.parts = list(parts)


class FakeIndex:
    def __init__(self) -> None:
        self.files = {"a.py": None}

    def search(self, query, limit=20, *, content=True):
        return [
            SimpleNamespace(path="a.py", line=3, snippet="def target(): pass", score=10, kind="symbol")
        ]


class ContextTests(unittest.TestCase):
    def test_estimate_tokens(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)
        self.assertGreater(estimate_tokens("x" * 100), 20)

    def test_usage_and_trim(self) -> None:
        manager = ContextManager(budget_tokens=1000, reserve_tokens=100)
        filler = "x" * 600
        contents = [FakeContent(FakePart(f"turn {index} " + filler)) for index in range(8)]
        usage = manager.usage(contents)
        self.assertGreater(usage["percent"], 0)

        trimmed, report = manager.trim(contents, keep_last=3)
        self.assertLessEqual(len(trimmed), len(contents))
        self.assertGreaterEqual(report.dropped, 0)
        if report.dropped:
            self.assertTrue(report.note)

    def test_trim_never_starts_with_function_response(self) -> None:
        manager = ContextManager(budget_tokens=2000, reserve_tokens=1900)
        response = FakeContent(
            FakePart("", function_response=SimpleNamespace(name="fs_read", response={"result": "x" * 400}))
        )
        tail = [FakeContent(FakePart("later turn " + "y" * 300)) for _ in range(3)]
        trimmed, _ = manager.trim([response] + tail, keep_last=3)
        self.assertTrue(trimmed)
        self.assertFalse(any(content.parts and content.parts[0].function_response for content in [trimmed[0]]))

    def test_build_context_block_respects_budget(self) -> None:
        block = build_context_block(
            brief="brief " * 2000,
            memory="memory " * 500,
            retrieval="code " * 500,
            max_chars=1200,
        )
        self.assertLessEqual(len(block), 1400)
        self.assertIn("<memory>", block)

    def test_project_brief_prefers_gemini_md(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="brief-test-"))
        (root / "GEMINI.md").write_text("project rules", encoding="utf-8")
        (root / "README.md").write_text("readme", encoding="utf-8")
        brief = load_project_brief(str(root))
        self.assertIn("GEMINI.md", brief)
        self.assertIn("project rules", brief)

    def test_lazy_index_builds_only_on_search(self) -> None:
        calls = []

        class Store:
            @property
            def index(self):
                return None

            def ensure(self, force=False):
                calls.append(force)
                return FakeIndex()

        lazy = LazyIndex(Store())
        self.assertEqual(lazy.files, {})
        self.assertEqual(calls, [])
        hits = lazy.search("target")
        self.assertEqual(len(hits), 1)
        self.assertEqual(calls, [False])


class RetrieverTests(unittest.IsolatedAsyncioTestCase):
    async def test_lexical_retrieval(self) -> None:
        retriever = Retriever(FakeIndex(), ".", embed=None, cache_path=Path(tempfile.mkdtemp()) / "e.json")
        result = await retriever.retrieve("target", limit=3)
        self.assertFalse(result.empty)
        self.assertEqual(result.mode, "lexical")
        self.assertIn("a.py:3", result.block)

    async def test_empty_query(self) -> None:
        retriever = Retriever(FakeIndex(), ".", embed=None, cache_path=Path(tempfile.mkdtemp()) / "e.json")
        result = await retriever.retrieve("  ")
        self.assertTrue(result.empty)
        self.assertEqual(result.mode, "none")

    async def test_embedding_failure_falls_back(self) -> None:
        async def broken(texts):
            raise RuntimeError("no embeddings")

        retriever = Retriever(
            FakeIndex(), ".", embed=broken, cache_path=Path(tempfile.mkdtemp()) / "e.json"
        )
        result = await retriever.retrieve("target")
        self.assertFalse(result.empty)
        self.assertEqual(result.mode, "lexical")


if __name__ == "__main__":
    unittest.main()
