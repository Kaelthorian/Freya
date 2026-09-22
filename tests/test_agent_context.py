from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from control_center.agent_context import (
    build_agent_context,
    build_effective_agent,
    normalize_result_output,
    skills_for_workspace,
    validate_structured_output,
)
from control_center.config import normalize_agent
from control_center.storage import Store
from control_center.worker import PolicyToolbox, run_task
from control_center.orchestrator import Orchestrator
from control_center.skills import normalize_skill, skills_context


class AgentContextTests(unittest.TestCase):
    def test_structured_identity_behavior_and_contract_reach_context(self):
        agent = normalize_agent({
            "name": "Atlas", "role": "Engineer", "purpose": "Ship reliable changes",
            "responsibilities": ["implement", "test"], "constraints": ["preserve APIs"],
            "instructions": "Use small commits.",
            "config": {"behavior": {"planning": {"mode": "explicit"}},
                       "autonomy": {"implementation_choices": "automatic"},
                       "verification": {"completion_criteria": ["tests pass"]},
                       "output": {"format": "structured"}, "secret_env": "ACC_SECRET_X"},
        })
        effective = build_effective_agent(agent)
        context = build_agent_context(effective, "Fix the bug", "C:/workspace")
        for value in ("Atlas", "Engineer", "Ship reliable changes", "implement", "preserve APIs", "Use small commits.",
                      "Planning: explicit", "implementation_choices: automatic", "tests pass", "Format: structured", "Filesystem"):
            self.assertIn(value, context)
        self.assertNotIn("ACC_SECRET", context)
        self.assertNotIn("coding agent", context.lower())

    def test_legacy_defaults_are_safe_and_output_modes_are_compatible(self):
        agent = normalize_agent({"name": "Legacy", "role": "Reviewer"})
        effective = build_effective_agent(agent)
        self.assertEqual(effective["behavior"]["planning"]["mode"], "adaptive")
        self.assertEqual(effective["autonomy"]["destructive_actions"], "ask")
        self.assertEqual(effective["output"]["format"], "structured")
        self.assertEqual(normalize_result_output("plain", {"format": "text"}), "plain")
        structured = normalize_result_output("plain", {"format": "structured"})
        self.assertEqual(structured["summary"], "plain")
        self.assertEqual(structured["actions"], [])

    def test_task_snapshot_keeps_identity_and_behavior_after_agent_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            agent = store.create_agent(normalize_agent({"name": "Atlas", "role": "Engineer", "purpose": "v1",
                                                        "config": {"behavior": {"planning": {"mode": "adaptive"}}}}))
            task = store.create_task(agent["id"], "task", directory)
            store.update_agent(agent["id"], normalize_agent({"name": "Atlas", "role": "Engineer", "purpose": "v2",
                                                             "config": {"behavior": {"planning": {"mode": "direct"}}}}, store.get_agent(agent["id"])))
            old = store.get_task(task["id"])
            self.assertEqual(old["config"]["identity"]["purpose"], "v1")
            self.assertEqual(old["config"]["behavior"]["planning"]["mode"], "adaptive")

    def test_orchestrator_candidate_contains_semantic_agent_data(self):
        seen = {}
        agent = normalize_agent({"name": "Atlas", "role": "Engineer", "purpose": "Review APIs", "tools": ["read_file"]})
        agent.update(id="a1", enabled=True, status="Idle")
        orchestrator = Orchestrator(None, None, decide=lambda prompt, agents, results: seen.update(agents=agents) or {"action": "respond", "message": "ok"})
        orchestrator._decision("task", [agent], [])
        self.assertEqual(seen["agents"][0]["purpose"], "Review APIs")
        self.assertIn("capabilities_summary", seen["agents"][0])


    def test_structured_output_validator_rejects_unknown_fields(self):
        valid = validate_structured_output({
            "summary": "Done", "actions": [], "artifacts": [],
            "verification": {}, "limitations": [],
        })
        self.assertEqual(valid["summary"], "Done")
        with self.assertRaises(ValueError):
            validate_structured_output({"summary": "Done", "unexpected": True})

    def test_git_inspection_skill_is_filtered_outside_repository(self):
        visible, excluded = skills_for_workspace([
            {"id": "git-inspection", "name": "Git Inspection", "active": True},
            {"id": "python-development", "name": "Python Development", "active": True},
        ], "C:/workspace-that-is-not-a-checkout")
        self.assertEqual(excluded, ["git-inspection"])
        self.assertEqual([skill["id"] for skill in visible], ["python-development"])

    def test_worker_skill_context_hides_recommendations_and_unavailable_operations(self):
        skill = normalize_skill({
            "id": "python-development", "name": "Python Development",
            "description": "Develop and validate Python software.",
            "category": "Software Development", "version": 1,
            "instructions": ["Validate syntax and relevant tests when available."],
            "procedures": [{"name": "Implement", "steps": [
                "Inspect relevant existing files.", "Implement the minimal change.",
                "Validate syntax.", "Run relevant tests when available.",
                "Inspect the resulting diff.",
            ]}],
            "required_capabilities": ["filesystem.read"],
            "recommended_capabilities": ["filesystem.search", "execution.pytest", "git.diff"],
            "tags": ["python"], "enabled": True,
        })
        context = skills_context(
            [skill],
            available_tools={"read_file", "write_file", "run_command"},
            available_capabilities={"filesystem.read", "filesystem.create", "execution.python_script"},
        )
        self.assertIn("Implement the minimal change", context)
        self.assertIn("Execute the program and verify its observable output", context)
        self.assertNotIn("Recommended capabilities", context)
        self.assertNotIn("Missing recommended", context)
        self.assertNotIn("filesystem.search", context)
        self.assertNotIn("execution.pytest", context)
        self.assertNotIn("git.diff", context)
        self.assertNotIn("list_files", context)

class RepeatedFailureTests(unittest.TestCase):
    def test_repeated_nonrecoverable_action_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); workspace = root / "workspace"; workspace.mkdir()
            (workspace / "large.txt").write_bytes(b"x" * 512001)
            config = normalize_agent({"name": "Worker", "role": "Agent", "config": {"max_steps": 6, "max_model_calls": 4,
                "behavior": {"persistence": {"repeated_failure_limit": 2}},
                "capability_policy": {"capabilities": {"filesystem": {"read": {"mode": "allow"}}}}}, "tools": ["read_file"]})["config"]
            events = []
            calls = iter([
                {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "large.txt"}}}]}, "prompt_eval_count": 0, "eval_count": 0},
                {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "large.txt"}}}]}, "prompt_eval_count": 0, "eval_count": 0},
                {"message": {"role": "assistant", "content": "done"}, "prompt_eval_count": 0, "eval_count": 0},
            ])
            result = run_task({"config": config, "tools": ["read_file"], "workspace": str(workspace), "prompt": "inspect"}, root,
                              events.append, lambda: None, transport=lambda *a, **k: next(calls))
            self.assertEqual(result["status"], "Success")
            self.assertIn("REPEATED_ACTION_BLOCKED", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
