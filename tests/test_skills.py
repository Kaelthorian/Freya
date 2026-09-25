from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from control_center.agent_context import build_agent_context, build_effective_agent
from control_center.api import Application
from control_center.config import normalize_agent
from control_center.skills import (BUILTIN_SKILLS, CORE_WRITE_FILE_INSTRUCTIONS,
                                   MAX_CONTEXT_CHARS, SkillCompatibilityError, normalize_skill,
                                   resolve_agent_skills, skill_compatibility, skill_summary,
                                   skills_context)
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
    def test_freya_core_explains_write_file_creates_files_not_directories(self):
        skill = next(item for item in BUILTIN_SKILLS if item["id"] == "freya-core")
        instructions = "\n".join(skill["instructions"])
        self.assertIn("write_file creates files, not directories", instructions)
        self.assertIn("Never create a directory by calling write_file with empty content", instructions)
        self.assertIn("Parent directories are created automatically", instructions)
        self.assertIn('write_file("calculator-project/index.html", content)', instructions)

    def test_existing_core_skill_gets_write_file_rule_as_a_new_immutable_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = Store(path)
            current = store.get_skill("freya-core")
            old_instructions = [instruction for instruction in current["instructions"]
                                if instruction not in CORE_WRITE_FILE_INSTRUCTIONS]
            edited = store.update_skill("freya-core", {**current, "instructions": old_instructions})
            old_version = edited["version"]

            migrated = Store(path).get_skill("freya-core")
            self.assertEqual(migrated["version"], old_version + 1)
            self.assertEqual(
                migrated["instructions"], [*old_instructions, *CORE_WRITE_FILE_INSTRUCTIONS],
            )
            history = Store(path).skill_version("freya-core", old_version)
            self.assertEqual(history["snapshot"]["instructions"], old_instructions)

    def test_shared_skill_compatibility_uses_required_not_recommended_capabilities(self):
        skill = skill_payload(recommended_capabilities=["execution.python_script"])
        self.assertEqual(skill_compatibility(skill, ["filesystem.read"]), {
            "compatible": True, "missing_required_capabilities": [],
        })
        self.assertEqual(skill_compatibility(skill, ["filesystem.create"]), {
            "compatible": False, "missing_required_capabilities": ["filesystem.read"],
        })
        invalid = skill_payload(required_capabilities=["filesystem.unknown"])
        with self.assertRaises(SkillCompatibilityError) as caught:
            skill_compatibility(invalid, [])
        self.assertEqual(caught.exception.error_type, "InvalidSkillDefinition")

    def test_simple_file_artifact_is_builtin_and_least_privilege(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            skill = next(item for item in store.list_skills()
                         if item["id"] == "simple-file-artifact")
            self.assertEqual(skill["source"], "builtin")
            self.assertEqual(set(skill["required_capabilities"]),
                             {"filesystem.read", "filesystem.create"})
            self.assertNotIn("filesystem.overwrite", skill["required_capabilities"])
            self.assertNotIn("filesystem.overwrite", skill["recommended_capabilities"])

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

    def test_duplicate_skips_archived_ids_and_names(self):
        class Runtime:
            max_workers = 1
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            app = Application(store, Runtime(), Path(directory))
            store.create_skill(skill_payload("archive-source"))
            status, first = app.dispatch("POST", "/api/skills/archive-source/duplicate", {}, {})
            self.assertEqual(status, 201)
            app.dispatch("DELETE", "/api/skills/" + first["id"], {}, {})
            status, second = app.dispatch("POST", "/api/skills/archive-source/duplicate", {}, {})
            self.assertEqual(status, 201)
            self.assertNotEqual(second["id"], first["id"])
            self.assertNotEqual(second["name"].casefold(), first["name"].casefold())

    def test_skill_import_is_atomic_and_rejects_case_insensitive_duplicates(self):
        class Runtime:
            max_workers = 1
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            app = Application(store, Runtime(), Path(directory))
            first = skill_payload("bulk-one", name="Bulk One")
            second = skill_payload("bulk-two", name="Bulk Two")
            status, result = app.dispatch("POST", "/api/skills/import", {}, {"skills": [first, second]})
            self.assertEqual(status, 201)
            self.assertEqual(result["count"], 2)
            self.assertEqual({item["id"] for item in result["skills"]}, {"bulk-one", "bulk-two"})
            status, repeat = app.dispatch("POST", "/api/skills/import", {}, {"version": 1, "skills": [first]})
            self.assertEqual(status, 201)
            self.assertEqual(repeat["count"], 0)
            self.assertEqual(repeat["skipped"], ["bulk-one"])
            with self.assertRaises(ValueError):
                app.dispatch("POST", "/api/skills/import", {}, {"skills": [skill_payload("bulk-three", name="Bulk Three"), skill_payload("bulk-one", name="Another Name")]})
            self.assertNotIn("bulk-three", {item["id"] for item in store.list_skills()})
            with self.assertRaises(ValueError):
                store.create_skill(skill_payload("case-duplicate", name="bulk one"))

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

    def test_immutable_history_auto_versions_and_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            created = store.create_skill(skill_payload("history-skill", version=1))
            self.assertEqual([item["version"] for item in store.skill_versions(created["id"])], [1])
            updated = store.update_skill(created["id"], {"instructions": ["Changed"], "version": 999})
            updated2 = store.update_skill(created["id"], {"description": "Changed again"})
            noop = store.update_skill(created["id"], {"description": "Changed again"})
            self.assertEqual((updated["version"], updated2["version"], noop["version"]), (2, 3, 3))
            versions = store.skill_versions(created["id"])
            self.assertEqual([item["version"] for item in versions], [1, 2, 3])
            self.assertEqual(store.skill_version(created["id"], 1)["snapshot"]["instructions"], ["Use evidence"])
            self.assertEqual(store.skill_version(created["id"], 2)["snapshot"]["instructions"], ["Changed"])
            with store._connection() as connection:
                events = [row[0] for row in connection.execute("SELECT event_type FROM skill_events WHERE skill_id=? ORDER BY id", (created["id"],))]
            self.assertEqual(events, ["skill.created", "skill.updated", "skill.updated"])

    def test_soft_delete_audit_listing_and_history(self):
        class Runtime:
            max_workers = 1
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            app = Application(store, Runtime(), Path(directory))
            created = store.create_skill(skill_payload("archive-skill", version=1))
            status, archived = app.dispatch("DELETE", f"/api/skills/{created['id']}", {}, {})
            self.assertEqual(status, 200)
            self.assertFalse(store.get_skill(created["id"])["enabled"])
            self.assertIsNotNone(store.get_skill(created["id"])["deleted_at"])
            self.assertNotIn(created["id"], {item["id"] for item in store.list_skills()})
            self.assertIn(created["id"], {item["id"] for item in store.list_skills(include_deleted=True)})
            self.assertEqual(store.skill_version(created["id"], 1)["version"], 1)
            with self.assertRaises(ValueError):
                store.create_agent(normalize_agent({"name": "A", "role": "Tester", "skills": [created["id"]]}))
            with store._connection() as connection:
                row = connection.execute("SELECT event_type,version,timestamp,payload_json FROM skill_events WHERE skill_id=? ORDER BY id DESC LIMIT 1", (created["id"],)).fetchone()
                self.assertEqual(row["event_type"], "skill.archived")
                self.assertTrue(row["timestamp"])
                self.assertNotIn("instructions", row["payload_json"])

    def test_priority_is_visible_and_task_precedes_skills(self):
        high = normalize_skill(skill_payload("high-skill", name="High"))
        low = normalize_skill(skill_payload("low-skill", name="Low"))
        assigned = [{**low, "priority": 10}, {**high, "priority": 100}]
        first = resolve_agent_skills({}, assigned, task="stable")
        second = resolve_agent_skills({}, assigned, task="stable")
        self.assertEqual([item["id"] for item in first], ["high-skill", "low-skill"])
        self.assertEqual([item["id"] for item in first], [item["id"] for item in second])
        effective = build_effective_agent({"name":"A","role":"Tester","skills":first,"tools":[],"config":{"capability_policy":{"capabilities":{}}}})
        context = build_agent_context(effective, "Current task", "workspace")
        self.assertIn("Priority: 100", context)
        self.assertIn("Priority: 10", context)
        self.assertLess(context.index("TASK BOUNDARIES"), context.index("SKILLS"))
        self.assertIn("System Policy > Capability Policy > Current User Task > Agent Constraints > Agent Instructions > Skill Priority > Skill Instructions > Skill Procedures", context)

    def test_legacy_database_migrates_skill_history_and_soft_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE skills(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,description TEXT NOT NULL DEFAULT '',instructions TEXT NOT NULL DEFAULT '',required_tools_json TEXT NOT NULL DEFAULT '[]',enabled INTEGER NOT NULL DEFAULT 1)")
            connection.execute("INSERT INTO skills(id,name,description,instructions,required_tools_json,enabled) VALUES(?,?,?,?,?,?)", ("legacy-skill","Legacy","old","Inspect first","[]",1))
            connection.commit(); connection.close()
            store = Store(path)
            legacy = store.get_skill("legacy-skill")
            self.assertEqual(legacy["instructions"], ["Inspect first"])
            self.assertIsNone(legacy["deleted_at"])
            self.assertEqual(store.skill_version("legacy-skill", 1)["snapshot"]["name"], "Legacy")


if __name__ == "__main__":
    unittest.main()
