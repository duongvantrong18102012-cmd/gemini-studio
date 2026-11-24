from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gemini_tui.agent.plan import (
    PlanError,
    PlanStore,
    TaskPlan,
    extract_json,
    plan_from_model_json,
)


PLAN_JSON = """
```json
{
  "goal": "Ship the change",
  "notes": "keep it small",
  "steps": [
    {"title": "Read code", "goal": "understand", "agent": "code", "tools": "fs_read, code_search", "risk": "read"},
    {"title": "Edit file", "goal": "apply", "agent": "coding", "risk": "write"},
    {"title": "Verify", "goal": "run tests", "agent": "review", "risk": "execute"}
  ]
}
```
"""


class ParsingTests(unittest.TestCase):
    def test_extract_json_tolerates_fences(self) -> None:
        payload = extract_json(PLAN_JSON)
        self.assertEqual(payload["goal"], "Ship the change")

    def test_extract_json_rejects_garbage(self) -> None:
        with self.assertRaises(PlanError):
            extract_json("no json here")
        with self.assertRaises(PlanError):
            extract_json("")

    def test_plan_normalises_agents_and_tools(self) -> None:
        plan = plan_from_model_json(PLAN_JSON, "Do the thing", max_steps=8, model="gemini-x")
        self.assertEqual(len(plan.steps), 3)
        self.assertEqual(plan.steps[0].id, "s1")
        self.assertEqual(plan.steps[0].tools, ["fs_read", "code_search"])
        self.assertEqual(plan.steps[1].agent, "code")  # "coding" alias
        self.assertEqual(plan.model, "gemini-x")
        self.assertEqual(plan.status, "draft")

    def test_plan_limits_steps(self) -> None:
        plan = plan_from_model_json(PLAN_JSON, "Do", max_steps=2)
        self.assertEqual(len(plan.steps), 2)

    def test_plan_rejects_empty(self) -> None:
        with self.assertRaises(PlanError):
            plan_from_model_json('{"steps": []}', "Do")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="plan-test-"))
        self.plan = plan_from_model_json(PLAN_JSON, "Persist me")

    def test_save_load_and_list(self) -> None:
        store = PlanStore(self.directory)
        path = store.save(self.plan)
        self.assertTrue(path.exists())
        loaded = store.load(self.plan.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.task, "Persist me")
        self.assertEqual(len(store.list(5)), 1)

    def test_unknown_plan_returns_none(self) -> None:
        store = PlanStore(self.directory)
        self.assertIsNone(store.load("doesnotexist"))

    def test_progress_helpers(self) -> None:
        plan = TaskPlan(task="x")
        plan.steps = self.plan.steps
        self.assertEqual(plan.completed, 0)
        self.assertEqual(plan.progress, 0.0)
        plan.steps[0].status = "done"
        plan.steps[1].status = "failed"
        self.assertEqual(plan.completed, 1)
        self.assertEqual(plan.failed, 1)
        self.assertAlmostEqual(plan.progress, 2 / 3)
        plan.verdict = "ok: verified"
        self.assertIn("Verdict", plan.summary_text())

    def test_corrupt_file_is_ignored(self) -> None:
        bad = self.directory / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertIsNone(TaskPlan.load(bad))


if __name__ == "__main__":
    unittest.main()
