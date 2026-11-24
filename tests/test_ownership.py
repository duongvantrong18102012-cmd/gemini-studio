"""File leases and the change log that prevents parallel overwrites."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gemini_tui.orchestration.ownership import (
    FileOwnership,
    OwnershipConflict,
    normalize_path,
)


class LeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ownership-test-"))
        self.ownership = FileOwnership()
        self.file = self.tmp / "app.py"

    def test_claim_auto_assigns_owner(self) -> None:
        self.ownership.check_write(self.file, "task:t1")
        self.assertEqual(self.ownership.owner(self.file), "task:t1")
        self.assertEqual(self.ownership.leases()[normalize_path(self.file)]["task"], "")

    def test_second_agent_is_blocked(self) -> None:
        self.ownership.claim(self.file, "task:t1", "t1")
        with self.assertRaises(OwnershipConflict) as caught:
            self.ownership.check_write(self.file, "task:t2", "t2")
        self.assertIn("task:t1", str(caught.exception))

    def test_same_agent_may_reclaim_and_update_task(self) -> None:
        self.ownership.claim(self.file, "task:t1", "t1")
        self.ownership.claim(self.file, "task:t1", "t9")
        self.assertEqual(self.ownership.leases()[normalize_path(self.file)]["task"], "t9")

    def test_release_requires_the_owner(self) -> None:
        self.ownership.claim(self.file, "task:t1")
        self.ownership.release(self.file, "task:t2")
        self.assertEqual(self.ownership.owner(self.file), "task:t1")
        self.ownership.release(self.file, "task:t1")
        self.assertEqual(self.ownership.owner(self.file), "")

    def test_release_agent_removes_all_its_leases(self) -> None:
        other = self.tmp / "other.py"
        self.ownership.claim(self.file, "task:t1")
        self.ownership.claim(other, "task:t1")
        self.ownership.claim(self.tmp / "third.py", "task:t2")
        self.assertEqual(self.ownership.release_agent("task:t1"), 2)
        self.assertEqual(sorted(self.ownership.leases()), [normalize_path(self.tmp / "third.py")])

    def test_empty_path_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.ownership.claim("", "agent")

    def test_normalize_is_stable_and_case_insensitive_on_windows(self) -> None:
        first = normalize_path(self.file)
        second = normalize_path(str(self.file).replace("/", "\\"))
        self.assertEqual(first, second)
        if __import__("os").name == "nt":
            self.assertEqual(first, normalize_path(str(self.file).upper()))


class ChangeLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ownership = FileOwnership()

    def test_record_and_reverse_order(self) -> None:
        self.ownership.record("a.py", "task:t1", "t1", "write")
        self.ownership.record("b.py", "task:t2", "t2", "edit")
        rows = self.ownership.changes(limit=10)
        self.assertEqual([row.path for row in rows], [
            normalize_path("b.py"), normalize_path("a.py")
        ])
        self.assertEqual(rows[0].detail, "edit")
        self.assertIn(":", rows[0].time_label)

    def test_filter_by_task_and_limit(self) -> None:
        for index in range(5):
            self.ownership.record(f"{index}.py", "task:t1", "t1")
        self.ownership.record("other.py", "task:t2", "t2")
        rows = self.ownership.changes(limit=3, task="t1")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row.task == "t1" for row in rows))

    def test_checkpoint_normalizes_and_deduplicates(self) -> None:
        checkpoint = self.ownership.checkpoint("tests pass", ["a.py", "a.py", ""])
        self.assertEqual(checkpoint["label"], "tests pass")
        self.assertEqual(checkpoint["files"], [normalize_path("a.py")])
        self.assertEqual(len(self.ownership.checkpoints()), 1)

    def test_summary_counts_real_state(self) -> None:
        self.ownership.claim("a.py", "task:t1")
        self.ownership.record("a.py", "task:t1", "t1", "write")
        self.ownership.checkpoint("done", ["a.py"])
        summary = self.ownership.summary()
        self.assertIn("1 active lease", summary)
        self.assertIn("1 change", summary)
        self.assertIn("1 checkpoint", summary)


if __name__ == "__main__":
    unittest.main()
