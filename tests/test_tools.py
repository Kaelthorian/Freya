from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import Toolbox


class ToolboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.toolbox = Toolbox(self.root, self.workspace, self.root / "evaluator")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_path_traversal_is_rejected(self) -> None:
        result = self.toolbox.invoke("read_file", {"path": "../outside.txt"})
        self.assertFalse(result.success)
        self.assertIn("outside the task workspace", result.output)

    def test_absolute_path_is_rejected(self) -> None:
        result = self.toolbox.invoke("read_file", {"path": str(self.root / "outside.txt")})
        self.assertFalse(result.success)
        self.assertIn("Absolute paths", result.output)

    def test_write_read_and_exact_edit(self) -> None:
        written = self.toolbox.invoke("write_file", {"path": "src/calc.py", "content": "VALUE = 1\n"})
        self.assertTrue(written.success)
        read = self.toolbox.invoke("read_file", {"path": "src/calc.py"})
        self.assertEqual(read.output, "VALUE = 1\n")
        edited = self.toolbox.invoke("edit_file", {"path": "src/calc.py", "old": "VALUE = 1", "new": "VALUE = 2"})
        self.assertTrue(edited.success)
        self.assertEqual((self.workspace / "src/calc.py").read_text(encoding="utf-8"), "VALUE = 2\n")

    def test_edit_refuses_ambiguous_match(self) -> None:
        self.toolbox.invoke("write_file", {"path": "same.py", "content": "x = 1\nx = 1\n"})
        result = self.toolbox.invoke("edit_file", {"path": "same.py", "old": "x = 1", "new": "x = 2"})
        self.assertFalse(result.success)
        self.assertEqual((self.workspace / "same.py").read_text(encoding="utf-8"), "x = 1\nx = 1\n")

    def test_search_is_case_insensitive_and_skips_venv(self) -> None:
        self.toolbox.invoke("write_file", {"path": "src/app.py", "content": "def ValidateToken(): pass\n"})
        ignored = self.workspace / ".venv"
        ignored.mkdir()
        (ignored / "noise.py").write_text("ValidateToken\n", encoding="utf-8")
        result = self.toolbox.invoke("search_code", {"query": "validatetoken"})
        self.assertTrue(result.success)
        self.assertIn("src/app.py:1", result.output)
        self.assertNotIn("noise.py", result.output)

    def test_run_command_rejects_unlisted_executable(self) -> None:
        result = self.toolbox.invoke("run_command", {"argv": ["cmd.exe", "/c", "echo unsafe"]})
        self.assertFalse(result.success)
        self.assertIn("Command blocked", result.output)

    def test_git_diff_reports_when_project_has_no_repository(self) -> None:
        result = self.toolbox.invoke("git_diff")
        self.assertTrue(result.success)
        self.assertIn("not inside a Git repository", result.output)

    def test_workspace_python_script_runs_without_a_shell(self) -> None:
        self.toolbox.invoke("write_file", {"path": "hello.py", "content": "print('hello')\n"})
        result = self.toolbox.invoke("run_command", {"argv": ["python", "hello.py"]})
        self.assertTrue(result.success, result.output)
        self.assertEqual(result.output.strip(), "hello")


if __name__ == "__main__":
    unittest.main()
