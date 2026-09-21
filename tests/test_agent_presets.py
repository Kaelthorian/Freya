from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from control_center.agent_selector import AgentSelector
from control_center.config import normalize_agent
from control_center.presets import AGENT_PRESETS, agent_preset_payload
from control_center.storage import Store


class PipelineAgentPresetTests(unittest.TestCase):
    def test_versioned_agent_exports_are_importable_and_keep_pipeline_roles(self):
        export_dir = Path(__file__).resolve().parents[1] / "data" / "agents"
        expected_roles = {
            "programmer": "worker",
            "task-analyst": "task_analyst",
            "qa-tester": "qa",
            "code-auditor": "auditor",
        }
        for name, expected_role in expected_roles.items():
            with self.subTest(agent=name):
                payload = json.loads((export_dir / f"{name}.json").read_text(encoding="utf-8"))
                normalized = normalize_agent(payload)
                self.assertEqual(normalized["config"]["orchestration_role"], expected_role)
                self.assertEqual(normalized["config"]["secret_env"], "")
        analyst = normalize_agent(json.loads(
            (export_dir / "task-analyst.json").read_text(encoding="utf-8")
        ))
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
            "programmer", "task-analyst", "qa-tester", "code-auditor",
        })
        analyst = agent_preset_payload("task-analyst")
        qa = agent_preset_payload("qa-tester")
        auditor = agent_preset_payload("code-auditor")
        self.assertEqual(analyst["config"]["orchestration_role"], "task_analyst")
        self.assertEqual(analyst["tools"], [])
        self.assertEqual(qa["config"]["orchestration_role"], "qa")
        self.assertIn("run_command", qa["tools"])
        self.assertNotIn("write_file", qa["tools"])
        self.assertEqual(auditor["config"]["orchestration_role"], "auditor")
        self.assertNotIn("run_command", auditor["tools"])
        self.assertNotIn("write_file", auditor["tools"])

    def test_interactive_testing_skill_selects_qa_tester(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "state.sqlite3")
            programmer = store.create_agent(agent_preset_payload("programmer"))
            qa = store.create_agent(agent_preset_payload("qa-tester"))
            selection = AgentSelector().select_agent({
                "id": "qa-interactive-test",
                "objective": "QA-test interactive behavior with controlled input",
                "description": "Run the Python program with bounded stdin.",
                "required_capabilities": ["filesystem.read", "execution.python_script"],
                "preferred_skills": ["interactive-testing", "software-testing"],
            }, store.list_agents())
            self.assertEqual(selection["status"], "selected")
            self.assertEqual(selection["selected_agent_id"], qa["id"])
            self.assertNotEqual(selection["selected_agent_id"], programmer["id"])


if __name__ == "__main__":
    unittest.main()
