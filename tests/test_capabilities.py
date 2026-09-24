from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_center.capabilities import CAPABILITY_REGISTRY, CapabilityResolver
from control_center.policy import PolicyEngine, policy_from_legacy, validate_policy
from control_center.worker import PolicyToolbox
from control_center.config import normalize_agent
from control_center.storage import Store


class CapabilityResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.resolver = CapabilityResolver(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def test_filesystem_mappings(self):
        self.assertEqual(self.resolver.resolve("read_file", {"path": "a.py"}), "filesystem.read")
        self.assertEqual(self.resolver.resolve("list_files", {}), "filesystem.list")
        self.assertEqual(self.resolver.resolve("search_code", {"query": "x"}), "filesystem.search")
        self.assertEqual(self.resolver.resolve("edit_file", {"path": "a.py"}), "filesystem.modify")

    def test_write_resolves_create_then_overwrite(self):
        self.assertEqual(self.resolver.resolve("write_file", {"path": "new.py"}), "filesystem.create")
        (self.workspace / "new.py").write_text("x", encoding="utf-8")
        self.assertEqual(self.resolver.resolve("write_file", {"path": "new.py"}), "filesystem.overwrite")

    def test_execution_mappings(self):
        for argv, expected in ((["python", "script.py"], "execution.python_script"),
                               (["python", "-m", "pytest"], "execution.pytest"),
                               (["python", "-m", "unittest", "discover"], "execution.unittest"),
                               (["python", "-m", "py_compile", "a.py"], "execution.py_compile"),
                               (["ruff", "check", "."], "execution.ruff"),
                               (["git", "status", "--short"], "git.status"),
                               (["git", "diff"], "git.diff")):
            self.assertEqual(self.resolver.resolve("run_command", {"argv": argv}), expected)

    def test_unknown_command_and_traversal_fail_closed(self):
        with self.assertRaises(ValueError):
            self.resolver.resolve("run_command", {"argv": ["sh", "-c", "unsafe"]})
        with self.assertRaises(ValueError):
            self.resolver.resolve("write_file", {"path": "../escape.txt"})


class PolicyEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def policy(self, mode="allow", **extra):
        rule = {"mode": mode, **extra}
        return {"capabilities": {"filesystem": {"create": rule, "read": {"mode": "allow"}, "overwrite": rule}}}

    def test_allow_blocked_and_ask_are_distinct(self):
        engine = PolicyEngine(self.policy(), self.workspace)
        self.assertEqual(engine.evaluate("filesystem.create", "src/a.py").outcome, "allow")
        self.assertEqual(PolicyEngine(self.policy("deny"), self.workspace).evaluate("filesystem.create", "src/a.py").outcome, "deny")
        self.assertEqual(PolicyEngine(self.policy("ask"), self.workspace).evaluate("filesystem.create", "src/a.py").outcome, "approval_required")

    def test_paths_traversal_and_absolute_paths(self):
        engine = PolicyEngine(self.policy(paths=["src/**"]), self.workspace)
        self.assertEqual(engine.evaluate("filesystem.create", "src/a.py").outcome, "allow")
        self.assertEqual(engine.evaluate("filesystem.create", "tests/a.py").outcome, "deny")
        self.assertEqual(engine.evaluate("filesystem.create", "../a.py").outcome, "deny")
        self.assertEqual(engine.evaluate("filesystem.create", str(self.workspace / "a.py")).outcome, "deny")

    def test_extensions_case_insensitive_and_max_bytes(self):
        engine = PolicyEngine(self.policy(extensions=[".PY"], max_bytes=4), self.workspace)
        self.assertEqual(engine.evaluate("filesystem.create", "src/a.py", {"size_bytes": 4}).outcome, "allow")
        self.assertEqual(engine.evaluate("filesystem.create", "src/a.txt", {"size_bytes": 1}).outcome, "deny")
        self.assertEqual(engine.evaluate("filesystem.create", "src/a.py", {"size_bytes": 5}).outcome, "deny")

    def test_unknown_and_invalid_policies_deny(self):
        self.assertEqual(PolicyEngine({}, self.workspace).evaluate("filesystem.read", "a.py").outcome, "deny")
        self.assertEqual(PolicyEngine({"capabilities": {"filesystem": {"read": {"mode": "bogus"}}}}, self.workspace).evaluate("filesystem.read", "a.py").outcome, "deny")
        self.assertEqual(PolicyEngine(self.policy(), self.workspace).evaluate("future.delete", "a.py").outcome, "deny")

    def test_policy_schema_is_strict(self):
        with self.assertRaises(ValueError):
            validate_policy({"capabilities": {"filesystem": {"read": {"mode": "allow", "unexpected": 1}}}})
        with self.assertRaises(ValueError):
            validate_policy({"capabilities": {"filesystem": {"read": {"mode": "allow", "paths": ["../x"]}}}})


class EnforcementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = {"permissions": "workspace", "allowed_directories": ["."], "capability_policy": {"capabilities": {
            "filesystem": {name: {"mode": "allow"} for name in ("list", "read", "search", "create", "modify", "overwrite")},
            "git": {"status": {"mode": "allow"}, "diff": {"mode": "allow"}},
            "execution": {name: {"mode": "deny"} for name in ("python_script", "pytest", "unittest", "py_compile", "ruff")},
        }}}
        self.box = PolicyToolbox(self.root, self.workspace, self.config, ["list_files", "read_file", "write_file", "edit_file", "search_code"])

    def tearDown(self):
        self.temp.cleanup()

    def test_allow_executes_and_logs_metadata(self):
        result = self.box.invoke("write_file", {"path": "src/a.py", "content": "x"})
        self.assertTrue(result.success)
        self.assertTrue(result.executed)
        self.assertEqual(result.capability, "filesystem.create")
        self.assertEqual(result.policy_decision, "allow")
        self.assertEqual((self.workspace / "src" / "a.py").read_text(encoding="utf-8"), "x")

    def test_identical_existing_write_skips_overwrite_policy_but_changed_content_does_not(self):
        target = self.workspace / "same.txt"
        target.write_bytes(b"same")
        self.config["capability_policy"]["capabilities"]["filesystem"]["overwrite"] = {"mode": "deny"}
        same_box = PolicyToolbox(self.root, self.workspace, self.config, ["write_file"])
        self.assertEqual(same_box.resolve_action("write_file", {"path": "same.txt", "content": "same"}), "filesystem.create")
        same = same_box.invoke("write_file", {"path": "same.txt", "content": "same"})
        self.assertTrue(same.success)
        self.assertTrue(same.already_satisfied)
        self.assertFalse(same.changed)
        self.assertFalse(same.executed)
        self.assertEqual(same.error_class, "already_satisfied")
        self.assertIn("already_satisfied", same.output)
        self.assertEqual(same.policy_decision, "allow")
        box = PolicyToolbox(self.root, self.workspace, self.config, ["write_file"])
        changed = box.invoke("write_file", {"path": "same.txt", "content": "changed"})
        self.assertFalse(changed.success)
        self.assertEqual(changed.capability, "filesystem.overwrite")
        self.assertEqual(changed.error_class, "policy_denied")
        self.assertEqual(target.read_bytes(), b"same")

    def test_deny_and_ask_never_execute(self):
        self.config["capability_policy"]["capabilities"]["filesystem"]["create"] = {"mode": "deny"}
        denied = PolicyToolbox(self.root, self.workspace, self.config, ["write_file"]).invoke("write_file", {"path": "a.py", "content": "x"})
        self.assertFalse(denied.executed); self.assertFalse((self.workspace / "a.py").exists()); self.assertEqual(denied.policy_decision, "deny")
        self.config["capability_policy"]["capabilities"]["filesystem"]["create"] = {"mode": "ask"}
        asked = PolicyToolbox(self.root, self.workspace, self.config, ["write_file"]).invoke("write_file", {"path": "b.py", "content": "x"})
        self.assertFalse(asked.executed); self.assertEqual(asked.policy_decision, "approval_required"); self.assertFalse((self.workspace / "b.py").exists())

    def test_legacy_policies_respect_disabled_tools(self):
        policy = policy_from_legacy({"permissions": "workspace"}, ["read_file"])
        self.assertEqual(policy["capabilities"]["filesystem"]["read"]["mode"], "allow")
        self.assertEqual(policy["capabilities"]["filesystem"]["create"]["mode"], "deny")
        self.assertEqual(policy["capabilities"]["execution"]["pytest"]["mode"], "deny")

    def test_task_snapshot_contains_effective_legacy_policy(self):
        store = Store(self.root / "state.sqlite3")
        agent = store.create_agent(normalize_agent({"name": "legacy", "tools": ["read_file"]}))
        task = store.create_task(agent["id"], "inspect", str(self.workspace))
        self.assertEqual(task["capability_policy"]["capabilities"]["filesystem"]["read"]["mode"], "allow")
        self.assertEqual(task["capability_policy"]["capabilities"]["filesystem"]["create"]["mode"], "deny")

    def test_windows_separators_are_normalized(self):
        engine = PolicyEngine({"capabilities": {"filesystem": {"create": {"mode": "allow", "paths": ["src\\**"]}}}}, self.workspace)
        self.assertEqual(engine.evaluate("filesystem.create", "src\\main.py").outcome, "allow")


if __name__ == "__main__":
    unittest.main()
