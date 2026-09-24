from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from control_center.agent_factory import AgentFactory
from control_center.sandbox import SANDBOX_IMAGE, run_in_sandbox
from control_center.skills import CORE_TOOLS, SkillConfigurationError
from control_center.storage import Store
from control_center.tools import Toolbox


class CoreSkillTests(unittest.TestCase):
    def test_only_core_is_advertised_and_every_agent_receives_its_tools(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root) / "state.db")
            self.assertEqual([item["id"] for item in store.list_skills()], ["freya-core"])
            task = {"id": "a", "objective": "Review code", "description": "Read code",
                    "required_capabilities": ["filesystem.read"],
                    "preferred_skills": ["old-or-unknown-skill"], "success_criteria": ["Reviewed"]}
            built = AgentFactory(store).build(task, orchestration_id="test")
            self.assertEqual(built["skill_ids"], ["freya-core"])
            self.assertEqual(set(built["declared_tools"]), set(CORE_TOOLS))
            self.assertEqual(built["effective_tools"], ["read_file"])
            self.assertIn("planner skill preference ignored", built["warnings"][0])

    def test_unknown_core_tool_fails_during_store_initialization(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.db"
            Store(path)
            with closing(sqlite3.connect(path)) as connection:
                with connection:
                    connection.execute("UPDATE skills SET tools_json=? WHERE id='freya-core'",
                                       ('["read_file","fake_tool"]',))
            with self.assertRaisesRegex(SkillConfigurationError, "unknown tool 'fake_tool'"):
                Store(path)


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.box = Toolbox(self.root, self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def test_absent_docker_fails_closed_without_host_execution(self):
        (self.workspace / "script.py").write_text("print('unsafe')", encoding="utf-8")
        with patch("control_center.sandbox.shutil.which", return_value=None):
            result = self.box.invoke("run_command", {"argv": ["python", "script.py"]})
        self.assertFalse(result.success)
        self.assertEqual(result.error_class, "SandboxUnavailable")

    def test_snapshot_is_disposable_and_docker_has_no_host_environment(self):
        (self.workspace / "script.py").write_text("print('hello')", encoding="utf-8")
        outside = self.root / "outside.txt"
        outside.write_text("safe", encoding="utf-8")
        observed = {}

        def fake_run(command, **kwargs):
            observed["command"] = command
            observed["environment"] = kwargs["env"]
            mount = command[command.index("--mount") + 1]
            snapshot = Path(mount.split("source=", 1)[1].split(",target=", 1)[0])
            self.assertEqual((snapshot / "script.py").read_text(encoding="utf-8"), "print('hello')")
            (snapshot / "generated.txt").write_text("temporary", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "hello\n", "")

        with patch("control_center.sandbox.shutil.which", return_value="docker"), \
             patch("control_center.sandbox.subprocess.run", side_effect=fake_run), \
             patch.dict(os.environ, {"FREYA_TEST_SECRET": "do-not-forward"}):
            result = self.box.invoke("run_command", {"argv": ["python", "script.py"]})
        self.assertTrue(result.success, result.output)
        self.assertFalse((self.workspace / "generated.txt").exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "safe")
        args = observed["command"]
        for restriction in ("--network=none", "--cap-drop=ALL", "--read-only", "--pids-limit=64",
                            "--security-opt=no-new-privileges"):
            self.assertIn(restriction, args)
        self.assertEqual(args[args.index("--user=65534:65534") + 1], "--workdir=/workspace")
        self.assertIn(SANDBOX_IMAGE, args)
        self.assertNotIn("do-not-forward", str(args) + str(observed["environment"]))
        self.assertNotIn("USERPROFILE", str(args) + str(observed["environment"]))
        self.assertTrue(self.box.invoke("write_file", {"path": "generated.txt", "content": "persistent"}).success)
        self.assertEqual((self.workspace / "generated.txt").read_text(encoding="utf-8"), "persistent")

    def test_shell_and_mutating_git_are_rejected_before_docker(self):
        for argv in (["powershell", "-Command", "pwd"], ["cmd.exe", "/c", "dir"],
                     ["git", "add", "."], ["git", "reset", "--hard"]):
            with self.subTest(argv=argv), patch("control_center.sandbox.subprocess.run") as run:
                result = self.box.invoke("run_command", {"argv": argv})
                self.assertFalse(result.success)
                run.assert_not_called()

    def test_symlink_escape_is_not_copied(self):
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            (self.workspace / "link.txt").symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable on this account")
        def fake_run(command, **kwargs):
            mount = command[command.index("--mount") + 1]
            snapshot = Path(mount.split("source=", 1)[1].split(",target=", 1)[0])
            self.assertFalse((snapshot / "link.txt").exists())
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch("control_center.sandbox.shutil.which", return_value="docker"), \
             patch("control_center.sandbox.subprocess.run", side_effect=fake_run):
            run_in_sandbox(self.workspace, ["python", "-m", "py_compile", "script.py"], 5)

    def test_real_container_cannot_reach_host_or_network(self):
        try:
            available = subprocess.run(["docker", "image", "inspect", SANDBOX_IMAGE],
                                       capture_output=True, timeout=8).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            available = False
        if not available:
            self.skipTest("local Docker sandbox image is unavailable")
        sentinel = self.root / "outside.txt"
        sentinel.write_text("untouched", encoding="utf-8")
        script = '''import json, os, pathlib, shutil, socket, subprocess
from pathlib import Path
result = {}
Path("generated.txt").write_text("ephemeral")
for label, action in {
    "relative": lambda: open("../outside.txt").read(),
    "absolute": lambda: open("C:/outside.txt").read(),
    "pathlib": lambda: pathlib.Path("../outside.txt").read_text(),
    "remove": lambda: os.remove("../outside.txt"),
    "rmtree": lambda: shutil.rmtree("../outside_dir"),
    "network": lambda: socket.create_connection(("1.1.1.1", 80), 1),
    "child": lambda: subprocess.run(["python", "-c", "open('../outside.txt').read()"], capture_output=True).returncode == 0,
    "system": lambda: os.system("cat ../outside.txt >/tmp/probe 2>/dev/null") == 0,
}.items():
    try:
        result[label] = bool(action())
    except Exception:
        result[label] = False
result["docker_socket"] = pathlib.Path("/var/run/docker.sock").exists()
result["secret"] = "FREYA_TEST_SECRET" in os.environ
result["home"] = os.environ.get("HOME")
result["userprofile"] = "USERPROFILE" in os.environ
print(json.dumps(result))
'''
        (self.workspace / "probe.py").write_text(script, encoding="utf-8")
        with patch.dict(os.environ, {"FREYA_TEST_SECRET": "do-not-forward"}):
            result = self.box.invoke("run_command", {"argv": ["python", "probe.py"]})
        self.assertTrue(result.success, result.output)
        observed = json.loads(result.output)
        for label in ("relative", "absolute", "pathlib", "remove", "rmtree", "network",
                      "child", "system", "docker_socket", "secret", "userprofile"):
            self.assertFalse(observed[label], label)
        self.assertEqual(observed["home"], "/tmp")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")
        self.assertFalse((self.workspace / "generated.txt").exists())
