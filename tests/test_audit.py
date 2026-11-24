from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gemini_tui.agent.audit import AuditLog, redact_text, redact_value, summarise_args


class RedactionTests(unittest.TestCase):
    def test_patterns_are_redacted(self) -> None:
        text = "key AIza" + "A" * 30 + " and sk-" + "b" * 24
        cleaned = redact_text(text)
        self.assertNotIn("AIza", cleaned)
        self.assertNotIn("sk-", cleaned)
        self.assertIn("[redacted]", cleaned)

    def test_dict_keys_are_redacted(self) -> None:
        payload = {"api_key": "secret", "nested": {"password": "x", "ok": "value"}}
        cleaned = redact_value(payload)
        self.assertEqual(cleaned["api_key"], "[redacted]")
        self.assertEqual(cleaned["nested"]["password"], "[redacted]")
        self.assertEqual(cleaned["nested"]["ok"], "value")

    def test_summary_is_bounded(self) -> None:
        rendered = summarise_args({"text": "x" * 2000}, limit=100)
        self.assertLessEqual(len(rendered), 100)


class AuditLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="audit-test-"))

    def test_records_are_written_and_read(self) -> None:
        log = AuditLog(self.directory)
        log.record("tool", tool="fs_read", ok=True, args={"path": "a.py"}, session="s1")
        log.record("permission", tool="fs_write", ok=False, detail="denied by user")
        rows = log.recent(10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].tool, "fs_write")
        self.assertFalse(rows[0].ok)

        path = log.path_for()
        self.assertTrue(path.exists())
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        payload = json.loads(lines[0])
        self.assertEqual(payload["tool"], "fs_read")

        tail = log.tail_file(10)
        self.assertEqual(len(tail), 2)
        self.assertIn("fs_read", log.render(10))

    def test_secret_in_args_is_redacted_before_disk(self) -> None:
        log = AuditLog(self.directory)
        log.record("tool", tool="shell_run", args={"api_key": "AIza" + "C" * 30})
        text = log.path_for().read_text(encoding="utf-8")
        self.assertNotIn("AIza", text)
        self.assertIn("[redacted]", text)

    def test_disabled_log_keeps_memory_only(self) -> None:
        log = AuditLog(self.directory, enabled=False)
        log.record("tool", tool="calc", ok=True)
        self.assertEqual(len(log.recent(5)), 1)
        self.assertFalse(log.path_for().exists())


if __name__ == "__main__":
    unittest.main()
