from __future__ import annotations

import json
import unittest
from pathlib import Path

from control_center.agent_selector import AgentSelector
from control_center.config import normalize_agent
from control_center.presets import AGENT_PRESETS, agent_preset_payload


class PipelineAgentPresetTests(unittest.TestCase):
    def test_versioned_agent_exports_mark_the_task_analyst_as_legacy(self):
        export_dir = Path(__file__).resolve().parents[1] / "data" / "agents"
        expected_roles = {
            "programmer": "worker",
            "qa-tester": "qa",
            "code-auditor": "auditor",
        }
        for name, expected_role in expected_roles.items():
            with self.subTest(agent=name):
                payload = json.loads((export_dir / f"{name}.json").read_text(encoding="utf-8"))
                normalized = normalize_agent(payload)
                self.assertEqual(normalized["config"]["orchestration_role"], expected_role)
                self.assertEqual(normalized["config"]["secret_env"], "")
        legacy_analyst = json.loads(
            (export_dir / "task-analyst.json").read_text(encoding="utf-8")
        )
        self.assertIn("legacy compatibility", legacy_analyst["description"].casefold())
        self.assertIn("built-in system task analyst", legacy_analyst["description"].casefold())
        analyst = normalize_agent(legacy_analyst)
        qa = normalize_agent(json.loads(
            (export_dir / "qa-tester.json").read_text(encoding="utf-8")
        ))
        auditor = normalize_agent(json.loads(
            (export_dir / "code-auditor.json").read_text(encoding="utf-8")
        ))
        self.assertEqual(analyst["tools"], [])
        self.assertIn("run_command", qa["tools"])
        self.assertNotIn("write_file", qa["tools"])
        self.assertIn("git_diff", auditor["tools"])
        self.assertNotIn("run_command", auditor["tools"])

    def test_pipeline_presets_have_separated_authority(self):
        self.assertEqual(set(AGENT_PRESETS), {
            "programmer", "qa-tester", "code-auditor",
        })
        with self.assertRaises(ValueError):
            agent_preset_payload("task-analyst")
        qa = agent_preset_payload("qa-tester")
        auditor = agent_preset_payload("code-auditor")
        self.assertEqual(qa["config"]["orchestration_role"], "qa")
        self.assertIn("run_command", qa["tools"])
        self.assertNotIn("write_file", qa["tools"])
        self.assertEqual(auditor["config"]["orchestration_role"], "auditor")
        self.assertNotIn("run_command", auditor["tools"])
        self.assertNotIn("write_file", auditor["tools"])

    def test_interactive_testing_skill_selects_qa_tester(self):
        programmer = agent_preset_payload("programmer")
        qa = agent_preset_payload("qa-tester")
        programmer.update(id="programmer-test", skills=[])
        qa.update(id="qa-test", skills=[])
        selection = AgentSelector().select_agent({
            "id": "qa-interactive-test",
            "objective": "QA-test interactive behavior with controlled input",
            "description": "Run the Python program with bounded stdin.",
            "required_capabilities": ["filesystem.read", "execution.python_script"],
            "preferred_skills": [],
        }, [programmer, qa])
        self.assertEqual(selection["status"], "selected")
        self.assertEqual(selection["selected_agent_id"], qa["id"])
        self.assertNotEqual(selection["selected_agent_id"], programmer["id"])


if __name__ == "__main__":
    unittest.main()
