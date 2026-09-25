from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_center.tools import Toolbox


class ToolboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.toolbox = Toolbox(self.root, self.workspace)

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

    def test_write_file_creates_parent_directories_for_nested_files(self) -> None:
        result = self.toolbox.invoke("write_file", {"path": "project/index.html", "content": "<h1>Hi</h1>"})
        self.assertTrue(result.success, result.output)
        self.assertTrue((self.workspace / "project").is_dir())
        self.assertEqual((self.workspace / "project/index.html").read_text(encoding="utf-8"), "<h1>Hi</h1>")

    def test_empty_write_creates_an_empty_file_not_a_directory(self) -> None:
        result = self.toolbox.invoke("write_file", {"path": "calculator-project", "content": ""})
        self.assertTrue(result.success, result.output)
        self.assertTrue((self.workspace / "calculator-project").is_file())
        self.assertFalse((self.workspace / "calculator-project").is_dir())
        self.assertEqual((self.workspace / "calculator-project").read_bytes(), b"")

    def test_file_parent_returns_typed_parent_path_error(self) -> None:
        self.toolbox.invoke("write_file", {"path": "project", "content": ""})
        result = self.toolbox.invoke("write_file", {"path": "project/index.html", "content": "<h1>Hi</h1>"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_class, "ParentPathIsFile")
        self.assertEqual(result.blocking_path, "project")
        self.assertIn("project", result.output)
        self.assertIn("blocks creation", result.output)
        self.assertTrue((self.workspace / "project").is_file())

    def test_project_files_create_all_parent_directories_implicitly(self) -> None:
        files = {
            "calculator-project/index.html": "<script src=app.js></script>",
            "calculator-project/css/site.css": "body { color: black; }",
            "calculator-project/js/app.js": "console.log('ready');",
        }
        for path, content in files.items():
            result = self.toolbox.invoke("write_file", {"path": path, "content": content})
            self.assertTrue(result.success, result.output)
        for path, content in files.items():
            self.assertEqual((self.workspace / path).read_text(encoding="utf-8"), content)
        self.assertTrue((self.workspace / "calculator-project/css").is_dir())
        self.assertTrue((self.workspace / "calculator-project/js").is_dir())

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
        self.assertFalse(result.success)
        self.assertEqual(result.error_class, "not_applicable")
        self.assertIn("not inside a Git repository", result.output)

    def test_workspace_python_script_runs_without_a_shell(self) -> None:
        self.toolbox.invoke("write_file", {"path": "hello.py", "content": "print('hello')\n"})
        result = self.toolbox.invoke("run_command", {"argv": ["python", "hello.py"]})
        self.assertTrue(result.success, result.output)
        self.assertEqual(result.output.strip(), "hello")

    def test_interactive_python_uses_controlled_stdin(self) -> None:
        self.toolbox.invoke("write_file", {
            "path": "sum.py",
            "content": "a = int(input('A: '))\nb = int(input('B: '))\nprint(a + b)\n",
        })
        result = self.toolbox.invoke("run_command", {
            "argv": ["python", "sum.py"], "stdin": "2\n3\n", "timeout_seconds": 2,
        })
        self.assertTrue(result.success, result.output)
        self.assertTrue(result.output.rstrip().endswith("5"), result.output)

    def test_interactive_python_without_stdin_fails_fast_with_guidance(self) -> None:
        self.toolbox.invoke("write_file", {"path": "wait.py", "content": "input('Value: ')\n"})
        result = self.toolbox.invoke("run_command", {
            "argv": ["python", "wait.py"], "timeout_seconds": 2,
        })
        self.assertFalse(result.success)
        self.assertEqual(result.error_class, "interactive_input_required")
        self.assertIn("INTERACTIVE_INPUT_REQUIRED", result.output)


if __name__ == "__main__":
    unittest.main()
