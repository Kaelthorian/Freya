"""Tests for the orchestration-scoped deterministic project context."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from control_center.config import normalize_agent
from control_center.project_state import (
    MAX_HASH_BYTES, ProjectStateManager, query_project_snapshot,
)
from control_center.storage import Store
from control_center.worker import PolicyToolbox
from control_center.orchestrator import Orchestrator


class ProjectStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".project-state-test-", dir=Path(__file__).parent,
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "module.py").write_text(
            "def parse_item(value):\n    return value.strip()\n", encoding="utf-8",
        )
        (self.workspace / "src" / "base.py").write_text(
            "DEFAULT_LIMIT = 10\n", encoding="utf-8",
        )
        self.store_path = self.root / "state.sqlite3"
        self.store = Store(self.store_path)
        self.run = self.store.create_orchestration("Update the project parser")
        self.plan = {
            "tasks": [{
                "id": "writer", "objective": "Update the parser",
                "description": "Maintain the parser artifact.",
                "depends_on": [], "owned_paths": ["src/module.py"],
                "success_criteria": ["src/module.py contains parse_item."],
            }],
            "write_owners": {"src/module.py": "writer"},
        }
        self.manager = ProjectStateManager(self.store)

    def test_manifest_is_bounded_and_state_survives_store_restart(self):
        (self.workspace / "one.txt").write_text("one", encoding="utf-8")
        (self.workspace / "two.txt").write_text("two", encoding="utf-8")
        (self.workspace / "three.txt").write_text("three", encoding="utf-8")
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git" / "private.txt").write_text("ignored", encoding="utf-8")
        manager = ProjectStateManager(self.store, max_manifest_files=2)
        state = manager.initialize(self.run["id"], self.plan, self.workspace)
        self.assertEqual(state["manifest"]["file_count"], 2)
        self.assertTrue(state["manifest"]["truncated"])
        self.assertNotIn(".git/private.txt", {item["path"] for item in state["artifacts"].values()})
        self.assertEqual(state["artifacts"]["src/module.py"]["owner_plan_task_id"], "writer")

        reopened = Store(self.store_path)
        persisted = reopened.get_project_state(self.run["id"])
        self.assertEqual(persisted["revision"], state["revision"])
        self.assertEqual(persisted["artifacts"]["src/module.py"]["sha256"],
                         state["artifacts"]["src/module.py"]["sha256"])

    def test_runtime_context_is_preserved_in_immutable_task_snapshot(self):
        snapshot = {
            "revision": 4, "total_artifact_count": 0,
            "visible_artifact_count": 0, "snapshot_truncated": False,
            "manifest": {"file_count": 0, "truncated": False},
            "artifacts": [], "tasks": [],
        }
        agent = self.store.create_agent(normalize_agent({
            "name": "Generated project worker",
            "config": {"provenance": {
                "generated_by_freya": True, "orchestration_id": self.run["id"],
                "plan_task_id": "writer", "attempt": 1, "factory_version": 1,
                "ephemeral": True,
            }},
        }, allow_provenance=True))
        task = self.store.create_task(
            agent["id"], "Update project artifact", str(self.workspace),
            runtime_context={"project_state_snapshot": snapshot},
        )
        self.assertEqual(task["config"]["runtime_context"]["project_state_snapshot"], snapshot)

    def test_accepted_runtime_candidate_verifies_artifact_symbol_and_dependency(self):
        state = self.manager.initialize(self.run["id"], self.plan, self.workspace)
        module = self.workspace / "src" / "module.py"
        module.write_text(
            "def parse_item(value):\n    return value.strip().lower()\n", encoding="utf-8",
        )
        runtime_task = {
            "id": "runtime-1", "agent_id": "agent-1", "workspace": str(self.workspace),
            "result": {
                "artifacts": [{"path": "src/module.py", "change_type": "overwritten"}],
                "project_context_update": {
                    "artifacts": [{"path": "src/module.py", "purpose": "Normalize parser input."}],
                    "symbols": [{"name": "parse_item", "kind": "function", "signature": "parse_item(value)",
                                 "purpose": "Normalize one parser value.",
                                 "path": "src/module.py"}],
                    "dependencies": [{"path": "src/module.py", "depends_on": "src/base.py",
                                      "relationship": "Reads the shared default limit."}],
                },
            },
        }
        # A worker result remains a candidate until this explicit acceptance call.
        self.assertEqual(self.store.get_project_state(self.run["id"])["revision"], state["revision"])
        accepted = self.manager.accept_task_update(
            self.run["id"], self.plan, self.plan["tasks"][0], runtime_task, "agent-1",
        )
        artifact = accepted["artifacts"]["src/module.py"]
        self.assertEqual(artifact["status"], "verified")
        self.assertEqual(artifact["owner_plan_task_id"], "writer")
        self.assertEqual(artifact["purpose"], "Normalize parser input.")
        self.assertTrue(artifact["symbols"][0]["verified"])
        self.assertEqual(artifact["symbols"][0]["kind"], "function")
        self.assertEqual(artifact["symbols"][0]["status"], "verified")
        self.assertEqual(artifact["dependencies"][0]["path"], "src/base.py")
        self.assertEqual(accepted["tasks"]["writer"]["status"], "accepted")
        events = self.store.get_orchestration(self.run["id"])["events"]
        self.assertIn("project_context.symbol_verified",
                      [json.loads(item["payload_json"]).get("event_type") for item in events])

        accepted_revision = artifact["revision"]
        module.unlink()
        missing, _rendered = self.manager.snapshot_for_dispatch(
            self.run["id"], self.plan, self.plan["tasks"][0], self.workspace,
        )
        missing_artifact = next(item for item in missing["artifacts"]
                                 if item["path"] == "src/module.py")
        self.assertEqual(missing_artifact["status"], "missing")
        self.assertEqual(missing_artifact["revision"], accepted_revision + 1)
        self.assertFalse(missing_artifact["symbols"][0]["verified"])

        verified_content = "def parse_item(value):\n    return value.strip().lower()\n"
        module.write_text(verified_content, encoding="utf-8")
        restored, _rendered = self.manager.snapshot_for_dispatch(
            self.run["id"], self.plan, self.plan["tasks"][0], self.workspace,
        )
        restored_artifact = next(item for item in restored["artifacts"]
                                 if item["path"] == "src/module.py")
        self.assertEqual(restored_artifact["status"], "stale")
        self.assertEqual(restored_artifact["revision"], accepted_revision + 2)
        self.assertFalse(restored_artifact["symbols"][0]["verified"])

        module.write_bytes(b"x" * (MAX_HASH_BYTES + 1))
        oversized, _rendered = self.manager.snapshot_for_dispatch(
            self.run["id"], self.plan, self.plan["tasks"][0], self.workspace,
        )
        oversized_artifact = next(item for item in oversized["artifacts"]
                                  if item["path"] == "src/module.py")
        self.assertEqual(oversized_artifact["status"], "unverified")
        self.assertEqual(oversized_artifact["revision"], accepted_revision + 3)
        self.assertFalse(oversized_artifact["symbols"][0]["verified"])

    def test_unowned_candidate_claim_is_rejected_and_does_not_create_artifact(self):
        self.manager.initialize(self.run["id"], self.plan, self.workspace)
        runtime_task = {
            "id": "runtime-2", "workspace": str(self.workspace),
            "result": {
                "artifacts": [{"path": "src/module.py", "change_type": "modified"}],
                "project_context_update": {
                    "artifacts": [{"path": "src/module.py"}, {"path": "src/foreign.py"}],
                    "symbols": [], "dependencies": [],
                },
            },
        }
        accepted = self.manager.accept_task_update(
            self.run["id"], self.plan, self.plan["tasks"][0], runtime_task,
        )
        self.assertNotIn("src/foreign.py", accepted["artifacts"])
        events = self.store.get_orchestration(self.run["id"])["events"]
        event_types = [json.loads(item["payload_json"]).get("event_type") for item in events]
        self.assertIn("project_context.update_rejected", event_types)

    def test_symbol_signature_must_match_its_declaration_line(self):
        module = self.workspace / "src" / "module.py"
        module.write_text(
            "def parse_item(other):\n    return other\n\n# parse_item(value)\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(module.read_bytes()).hexdigest()
        self.assertFalse(ProjectStateManager._verify_symbol(
            self.workspace, "src/module.py", "parse_item", "parse_item(value)", digest,
        ))

    def test_snapshot_marks_external_change_stale_and_query_never_returns_contents(self):
        self.manager.initialize(self.run["id"], self.plan, self.workspace)
        module = self.workspace / "src" / "module.py"
        module.write_text("def parse_item(value):\n    return 'changed'\n", encoding="utf-8")
        snapshot, _rendered = self.manager.snapshot_for_dispatch(
            self.run["id"], self.plan, self.plan["tasks"][0], self.workspace,
        )
        artifact = next(item for item in snapshot["artifacts"] if item["path"] == "src/module.py")
        self.assertEqual(artifact["status"], "stale")
        self.assertEqual(artifact["revision"], 2)
        self.assertNotIn("return 'changed'", json.dumps(snapshot))
        query = query_project_snapshot(snapshot, {"operation": "artifact", "path": "src/module.py"})
        self.assertEqual(query["operation"], "artifact")
        self.assertNotIn("return 'changed'", query["json"])
        events = self.store.get_orchestration(self.run["id"])["events"]
        event_types = [json.loads(item["payload_json"]).get("event_type") for item in events]
        self.assertIn("artifact.stale", event_types)
        self.assertIn("project_context.snapshot_generated", event_types)

    def test_cross_task_target_revision_is_included_in_requester_snapshot(self):
        (self.workspace / "src" / "foreign.py").write_text(
            "VALUE = 1\n", encoding="utf-8",
        )
        plan = {
            "tasks": [
                {"id": "requester", "objective": "Use the foreign artifact",
                 "description": "Request a change to the owned artifact.",
                 "depends_on": [], "owned_paths": [],
                 "foreign_write_targets": [{
                     "path": "src/foreign.py", "owner_plan_task_id": "owner",
                 }]},
                {"id": "owner", "objective": "Maintain foreign artifact",
                 "description": "Own the shared artifact.", "depends_on": [],
                 "owned_paths": ["src/foreign.py"]},
            ],
            "write_owners": {"src/foreign.py": "owner"},
        }
        self.manager.initialize(self.run["id"], plan, self.workspace)
        snapshot, _rendered = ProjectStateManager(
            self.store, max_snapshot_artifacts=1,
        ).snapshot_for_dispatch(
            self.run["id"], plan, plan["tasks"][0], self.workspace,
        )
        self.assertEqual(len(snapshot["artifacts"]), 1)
        self.assertEqual(snapshot["artifacts"][0]["path"], "src/foreign.py")
        self.assertEqual(snapshot["artifacts"][0]["revision"], 1)


class ProjectContextToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".project-tool-test-", dir=Path(__file__).parent,
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    @staticmethod
    def _snapshot():
        return {
            "revision": 7, "total_artifact_count": 1, "visible_artifact_count": 1,
            "snapshot_truncated": False,
            "manifest": {"file_count": 1, "truncated": False},
            "artifacts": [{"path": "src/module.py", "status": "verified", "revision": 3,
                           "sha256": "a" * 64, "owner_plan_task_id": "writer",
                           "purpose": "Parser module", "symbols": [], "dependencies": []}],
            "tasks": [{"task_id": "writer", "status": "accepted", "objective": "Update parser",
                       "depends_on": []}],
        }

    def test_context_only_agent_has_no_filesystem_tools_or_content(self):
        config = {
            "capability_policy": {"capabilities": {"project": {"read_context": {"mode": "allow"}}}},
            "allowed_directories": ["."],
            "provenance": {"generated_by_freya": True, "plan_task_id": "writer"},
            "runtime_context": {"project_state_snapshot": self._snapshot()},
        }
        box = PolicyToolbox(self.root, self.workspace, config, ["project_context"])
        self.assertEqual([item["function"]["name"] for item in box.schemas], ["project_context"])
        result = box.invoke("project_context", {"operation": "artifact", "path": "src/module.py"})
        self.assertTrue(result.success)
        self.assertEqual(result.capability, "project.read_context")
        self.assertIn("verified", result.output)
        self.assertNotIn("file contents", result.output)
        denied = box.invoke("read_file", {"path": "src/module.py"})
        self.assertFalse(denied.success)
        self.assertEqual(denied.error_class, "tool_unavailable")

    def test_existing_write_requires_read_and_rejects_stale_read(self):
        (self.workspace / "module.py").write_text("before\n", encoding="utf-8")
        config = {
            "capability_policy": {"capabilities": {
                "project": {"read_context": {"mode": "allow"}},
                "filesystem": {
                    "read": {"mode": "allow", "paths": ["module.py"]},
                    "create": {"mode": "allow"},
                    "modify": {"mode": "allow"},
                    "overwrite": {"mode": "allow"},
                },
            }},
            "permissions": "workspace", "allowed_directories": ["."],
            "provenance": {"generated_by_freya": True, "plan_task_id": "writer"},
            "task_owned_paths": ["module.py"], "task_write_owners": {"module.py": "writer"},
            "task_write_scope_enforced": True,
            "runtime_context": {"project_state_snapshot": self._snapshot()},
            "autonomy": {"create_files": "automatic", "modify_files": "automatic"},
        }
        box = PolicyToolbox(self.root, self.workspace, config, ["read_file", "write_file", "edit_file"])
        change = {"path": "module.py", "old": "before", "new": "after"}
        blocked = box.invoke("edit_file", change)
        self.assertEqual(blocked.error_class, "read_before_write_required")
        self.assertEqual((self.workspace / "module.py").read_text(encoding="utf-8"), "before\n")
        overwrite_args = {"path": "module.py", "content": "after\n"}
        box.grant_approval("filesystem.overwrite", overwrite_args, task=True)
        overwrite = box.invoke("write_file", overwrite_args)
        self.assertEqual(overwrite.error_class, "read_before_write_required")

        self.assertTrue(box.invoke("read_file", {"path": "module.py"}).success)
        (self.workspace / "module.py").write_text("external change\n", encoding="utf-8")
        stale = box.invoke("edit_file", change)
        self.assertEqual(stale.error_class, "stale_artifact")
        self.assertEqual((self.workspace / "module.py").read_text(encoding="utf-8"), "external change\n")

        self.assertTrue(box.invoke("read_file", {"path": "module.py"}).success)
        fresh_change = {"path": "module.py", "old": "external change", "new": "after"}
        written = box.invoke("edit_file", fresh_change)
        self.assertTrue(written.success)
        self.assertEqual((self.workspace / "module.py").read_text(encoding="utf-8"), "after\n")

    def test_responsibility_context_shows_direct_later_work(self):
        plan = {"tasks": [
            {"id": "task-1", "objective": "Create calculator files", "depends_on": [],
             "owned_paths": ["calculator.js"]},
            {"id": "task-2", "objective": "Implement calculator logic", "depends_on": ["task-1"],
             "write_targets": ["calculator.js"]},
            {"id": "task-3", "objective": "Unrelated work", "depends_on": []},
        ]}
        rendered = Orchestrator._responsibility_context(plan, plan["tasks"][0])
        self.assertIn("PLAN RESPONSIBILITY CONTEXT", rendered)
        self.assertIn("task-2: Implement calculator logic", rendered)
        self.assertNotIn("Unrelated work", rendered)
        prompt = Orchestrator._execution_prompt("Create a calculator", plan["tasks"][0],
                                                responsibility_context=rendered)
        self.assertIn("task-2: Implement calculator logic", prompt)

    def test_truncated_read_cannot_authorize_existing_file_edit(self):
        (self.workspace / "large.py").write_bytes(b"x" * 21_000)
        config = {
            "capability_policy": {"capabilities": {"filesystem": {
                "read": {"mode": "allow"}, "modify": {"mode": "allow"}}}},
            "permissions": "workspace", "allowed_directories": ["."],
            "provenance": {"generated_by_freya": True, "plan_task_id": "writer"},
            "task_owned_paths": ["large.py"], "task_write_owners": {"large.py": "writer"},
            "task_write_scope_enforced": True,
            "autonomy": {"modify_files": "automatic"},
        }
        box = PolicyToolbox(self.root, self.workspace, config, ["read_file", "edit_file"])
        read = box.invoke("read_file", {"path": "large.py"})
        self.assertTrue(read.success)
        self.assertIn("[output truncated]", read.output)
        blocked = box.invoke("edit_file", {"path": "large.py", "old": "x", "new": "y"})
        self.assertEqual(blocked.error_class, "read_before_write_required")


if __name__ == "__main__":
    unittest.main()
