from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gemini_tui.agent.memory import MemoryStore, tokenize


class MemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="memory-test-"))
        self.path = self.root / "memory.json"
        self.legacy = self.root / "legacy.json"

    def store(self, workspace: str = "/work/a") -> MemoryStore:
        return MemoryStore(workspace=workspace, path=self.path, legacy_path=self.legacy)

    def test_save_get_and_persist(self) -> None:
        store = self.store()
        entry = store.save("test runner", "Use python -m pytest -q", scope="project", tags=["tests"])
        self.assertEqual(entry.scope, "project")
        self.assertEqual(entry.workspace, str(Path("/work/a")))
        self.assertTrue(self.path.exists())

        reloaded = self.store()
        again = reloaded.get("test runner")
        self.assertIsNotNone(again)
        self.assertEqual(again.value, "Use python -m pytest -q")
        self.assertEqual(again.tags, ["tests"])

    def test_invalid_scope_and_empty_values(self) -> None:
        store = self.store()
        with self.assertRaises(ValueError):
            store.save("k", "v", scope="galaxy")
        with self.assertRaises(ValueError):
            store.save("", "v")
        with self.assertRaises(ValueError):
            store.save("k", "   ")

    def test_search_ranks_key_matches(self) -> None:
        store = self.store()
        store.save("deploy target", "staging cluster in eu-west", tags=["ops"])
        store.save("formatting", "use ruff format before committing")
        hits = store.search("deploy")
        self.assertTrue(hits)
        self.assertEqual(hits[0].key, "deploy target")
        self.assertGreaterEqual(hits[0].hits, 1)

    def test_scope_filter_and_digest(self) -> None:
        store = self.store()
        store.save("global note", "always use type hints", scope="global")
        store.save("project note", "this repo uses uv", scope="project")
        store.save("session note", "current branch is main", scope="session")
        self.assertEqual(len(store.entries(scope="project")), 1)
        digest = store.digest("", limit=3)
        self.assertIn("global note", digest)
        self.assertIn("this repo uses uv", digest)

    def test_forget(self) -> None:
        store = self.store()
        store.save("temp", "x")
        self.assertTrue(store.forget("temp"))
        self.assertFalse(store.forget("temp"))
        self.assertIsNone(store.get("temp"))

    def test_legacy_flat_file_is_migrated(self) -> None:
        self.legacy.write_text(
            json.dumps({"old key": {"value": "old value", "updated": 1700000000}}),
            encoding="utf-8",
        )
        store = self.store()
        entry = store.get("old key")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.value, "old value")

    def test_tokenizer_handles_unicode(self) -> None:
        tokens = tokenize("Chạy pytest cho dự án")
        self.assertIn("chạy", tokens)
        self.assertIn("pytest", tokens)


if __name__ == "__main__":
    unittest.main()
