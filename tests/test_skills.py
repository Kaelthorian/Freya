from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_center.agent_context import build_agent_context, build_effective_agent
from control_center.api import Application
from control_center.config import normalize_agent
from control_center.skills import MAX_CONTEXT_CHARS, normalize_skill, resolve_agent_skills, skill_summary, skills_context
from control_center.storage import Store
from control_center.orchestrator import Orchestrator


def skill_payload(skill_id="demo-skill", **changes):
    value = {
        "id": skill_id, "name": "Demo Skill", "description": "Reusable guidance", "category": "Testing",
        "version": 2, "instructions": ["Use evidence"],
        "procedures": [{"name": "Demo procedure", "steps": ["Inspect", "Validate"]}],
        "required_capabilities": ["filesystem.read"], "recommended_capabilities": ["execution.pytest"],
        "tags": ["demo", "testing"], "enabled": True,
    }
    value.update(changes)
    return value


class SkillRegistryTests(unittest.TestCase):
    def test_api_crud_and_agent_assignment(self):
        class Runtime:
            max_workers = 1
            def resume(self, *_args):
                return None

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            app = Application(store, Runtime(), Path(directory))
            status, created = app.dispatch("POST", "/api/skills", {}, skill_payload("api-skill"))
            self.assertEqual(status, 201)
            status, listed = app.dispatch("GET", "/api/skills", {"q": "demo"}, {})
            self.assertEqual(status, 200)
            self.assertTrue(any(item["id"] == "api-skill" for item in listed))
            status, listed = app.dispatch("GET", "/api/skills", {"q": "Testing"}, {})
            self.assertEqual(status, 200)
            self.assertTrue(any(item["id"] == "api-skill" for item in listed))
            status, duplicate = app.dispatch("POST", "/api/skills/api-skill/duplicate", {}, {})
            self.assertEqual(status, 201)
            self.assertEqual(duplicate["source"], "user")
            status, duplicate_again = app.dispatch("POST", "/api/skills/api-skill/duplicate", {}, {})
            self.assertEqual(status, 201)
            self.assertNotEqual(duplicate_again["id"], duplicate["id"])
            status, agent = app.dispatch("POST", "/api/agents", {}, {"name": "A", "role": "Tester", "skills": [{"id": "api-skill", "priority": 25}]})
            self.assertEqual(status, 201)
            self.assertEqual(agent["skills"][0]["id"], "api-skill")
            status, summaries = app.dispatch("GET", f"/api/agents/{agent['id']}/skills", {}, {})
            self.assertEqual(status, 200)
            self.assertTrue(summaries[0]["operational"])

    def test_validation_and_stable_id(self):
        skill = normalize_skill(skill_payload())
        self.assertEqual(skill["id"], "demo-skill")
        self.assertEqual(skill["version"], 2)
        with self.assertRaises(ValueError):
            normalize_skill(skill_payload("Not Safe"))
        with self.assertRaises(ValueError):
            normalize_skill(skill_payload(procedures=[{"name": "bad", "steps": [{"command": "rm"}]}]))
        with self.assertRaises(ValueError):
            normalize_skill(skill_payload(metadata={"permissions": "allow_everything"}))

    def test_registry_persistence_crud_and_disable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            created = store.create_skill(skill_payload())
            self.assertEqual(store.get_skill(created["id"])["category"], "Testing")
            with self.assertRaises(ValueError):
                store.create_skill(skill_payload())
            changed = store.update_skill("demo-skill", {"name": "Renamed", "version": 3})
            self.assertEqual(changed["name"], "Renamed")
            agent = store.create_agent(normalize_agent({"name": "A", "role": "Tester", "skills": [{"id": "demo-skill", "priority": 50}], "config": {"capability_policy": {"capabilities": {"filesystem": {"read": {"mode": "allow"}}}}}}))
            self.assertEqual(store.get_agent(agent["id"])["skills"][0]["priority"], 50)
            disabled = store.delete_skill("demo-skill")
            self.assertFalse(disabled["enabled"])
            store.update_agent(agent["id"], normalize_agent({"name": "A", "role": "Tester", "skills": []}, store.get_agent(agent["id"])))
            self.assertEqual(store.get_agent(agent["id"])["skills"], [])

    def test_ask_policy_and_disabled_skill_are_non_operational_or_inactive(self):
        skill = normalize_skill(skill_payload())
        ask = resolve_agent_skills({}, [skill], policy={"capabilities": {"filesystem": {"read": {"mode": "ask"}}}})
        self.assertFalse(ask[0]["operational"])
        disabled = resolve_agent_skills({}, [{**skill, "enabled": False}], policy={"capabilities": {"filesystem": {"read": {"mode": "allow"}}}})
        self.assertFalse(disabled[0]["active"])

    def test_multiple_skills_have_stable_priority_order_and_context(self):
        first = normalize_skill(skill_payload("first-skill", name="First", tags=["shared"]))
        second = normalize_skill(skill_payload("second-skill", name="Second", tags=["shared"]))
        resolved = resolve_agent_skills({}, [{**first, "priority": 10}, {**second, "priority": 10}], task="shared")
        self.assertEqual([item["id"] for item in resolved], ["first-skill", "second-skill"])
        effective = build_effective_agent({"name": "A", "role": "Tester", "skills": resolved, "tools": [], "config": {"capability_policy": {"capabilities": {"filesystem": {"read": {"mode": "allow"}}}}}})
        context = build_agent_context(effective, "shared", "workspace")
        self.assertIn("First", context)
        self.assertIn("Second", context)

    def test_skill_context_has_a_global_budget(self):
        skill = normalize_skill(skill_payload(instructions=["x" * 16000]))
        self.assertLessEqual(len(skills_context([skill] * 100)), MAX_CONTEXT_CHARS)

    def test_orchestrator_receives_skill_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            skill = store.create_skill(skill_payload("orchestrator-skill", tags=["python", "api"]))
            agent = store.create_agent(normalize_agent({"name": "Atlas", "role": "Engineer", "skills": [{"id": skill["id"], "priority": 20}], "config": {"capability_policy": {"capabilities": {"filesystem": {"read": {"mode": "allow"}}}}}}))
            seen = {}
            Orchestrator(store, None, decide=lambda prompt, agents, results: seen.update(agents=agents) or {"action": "respond", "message": "ok"})._decision("python api", [agent], [])
            self.assertEqual(seen["agents"][0]["skills"][0]["id"], "orchestrator-skill")
            self.assertIn("python", seen["agents"][0]["skills"][0]["tags"])

    def test_default_orchestrator_selection_uses_skill_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            react = store.create_skill(skill_payload("react-ui", name="React UI", tags=["react", "frontend"]))
            python = store.create_skill(skill_payload("python-api", name="Python API", tags=["python", "api"]))
            store.create_agent(normalize_agent({"name": "Frontend", "role": "Developer", "skills": [{"id": react["id"]}]}))
            store.create_agent(normalize_agent({"name": "Backend", "role": "Developer", "skills": [{"id": python["id"]}]}))
            decision = Orchestrator(store, None)._decision("modify the Python API", store.list_agents(), [])
            self.assertEqual(decision["tasks"][0]["agent_id"], store.list_agents()[1]["id"])

    def test_required_and_recommended_compatibility_never_grants_access(self):
        skill = normalize_skill(skill_payload())
        resolved = resolve_agent_skills({}, [skill], policy={"capabilities": {"filesystem": {"read": {"mode": "deny"}}}})
        self.assertFalse(resolved[0]["operational"])
        self.assertEqual(resolved[0]["missing_required_capabilities"], ["filesystem.read"])
        self.assertIn("execution.pytest", resolved[0]["missing_recommended_capabilities"])
        self.assertEqual(skill_summary(resolved[0])["operational"], False)

    def test_priority_and_relevance_separate_assigned_from_active(self):
        skills = [normalize_skill(skill_payload("python-development", name="Python Development", tags=["python"])),
                  normalize_skill(skill_payload("network-troubleshooting", name="Network Troubleshooting", tags=["network"]))]
        resolved = resolve_agent_skills({}, [{**item, "priority": 0} for item in skills], task="Review Python code")
        self.assertEqual(resolved[0]["id"], "python-development")  # relevance is separate from assignment priority
        self.assertTrue(all("active" in item for item in resolved))

    def test_context_contains_skill_instructions_and_procedures(self):
        skill = normalize_skill(skill_payload())
        effective = build_effective_agent({"name": "A", "role": "Tester", "instructions": "Extra", "skills": [{**skill, "priority": 10}], "tools": [], "config": {"capability_policy": {"capabilities": {"filesystem": {"read": {"mode": "allow"}}}}}})
        context = build_agent_context(effective, "Validate", "workspace")
        self.assertIn("Demo Skill", context)
        self.assertIn("Use evidence", context)
        self.assertIn("Inspect", context)
        self.assertIn("Use skill procedures as guidance", context)

    def test_task_snapshot_keeps_skill_version_after_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            store.create_skill(skill_payload())
            agent = store.create_agent(normalize_agent({"name": "A", "role": "Tester", "skills": [{"id": "demo-skill", "priority": 5}], "config": {"capability_policy": {"capabilities": {"filesystem": {"read": {"mode": "allow"}}}}}}))
            task = store.create_task(agent["id"], "Validate", directory)
            store.update_skill("demo-skill", {"version": 9})
            self.assertEqual(store.get_task(task["id"])["skills"][0]["version"], 2)


if __name__ == "__main__":
    unittest.main()
