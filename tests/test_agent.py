from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent import _parse_test_summary, render_report, run_agent
from tools import Toolbox, ToolResult


class AgentLoopTests(unittest.TestCase):
    def test_native_tool_call_loop_records_metrics_and_redacts_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            responses = [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "function": {
                                "name": "write_file",
                                "arguments": {"path": "answer.py", "content": "print('ok')\n"},
                            }
                        }],
                    },
                    "prompt_eval_count": 100,
                    "eval_count": 20,
                    "total_duration": 2_000_000_000,
                    "load_duration": 300_000_000,
                    "prompt_eval_duration": 500_000_000,
                    "eval_duration": 1_000_000_000,
                },
                {
                    "message": {"role": "assistant", "content": "Created the file.", "tool_calls": []},
                    "prompt_eval_count": 120,
                    "eval_count": 10,
                    "total_duration": 1_000_000_000,
                    "load_duration": 0,
                    "prompt_eval_duration": 200_000_000,
                    "eval_duration": 500_000_000,
                },
            ]

            def fake_chat(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                return responses.pop(0)

            class PassingToolbox(Toolbox):
                def invoke(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
                    if name == "run_tests":
                        return ToolResult(name, "6 tests passed", True, 0.01, 0)
                    return super().invoke(name, arguments)

            result = run_agent(
                "Create answer.py",
                model="test-model",
                project_root=root,
                workspace=workspace,
                validate_final=True,
                chat_fn=fake_chat,
                toolbox=PassingToolbox(root, workspace),
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["metrics"]["agent_steps"], 2)
        self.assertEqual(result["metrics"]["prompt_tokens"], 220)
        self.assertEqual(result["metrics"]["generated_tokens"], 30)
        self.assertEqual(result["metrics"]["tool_counts"], {"write_file": 1})
        self.assertEqual(result["metrics"]["tool_calls"], 1)
        self.assertEqual(result["metrics"]["test_runs"], 1)
        self.assertEqual(result["metrics"]["test_cases_run"], 6)
        self.assertEqual(result["metrics"]["test_cases_passed"], 6)
        self.assertEqual(result["metrics"]["files_changed"], 1)
        self.assertEqual(result["metrics"]["files_changed_paths"], ["answer.py"])
        self.assertEqual(result["metrics"]["tool_metrics"]["write_file"]["calls"], 1)
        self.assertEqual(result["metrics"]["tool_metrics"]["run_tests"]["calls"], 1)
        self.assertEqual(result["metrics"]["model_call_details"][0]["tokens_per_second"], 20.0)
        self.assertEqual(result["metrics"]["max_model_call_seconds"], 2.0)
        self.assertEqual(result["transcript"][0]["arguments"]["content"], {"redacted": True, "characters": 12})
        report = render_report(result)
        self.assertIn("INFORME DE EJECUCIÓN", report)
        self.assertIn("Casos detectados: 6 | Pasaron: 6", report)
        self.assertIn("answer.py", report)

    def test_json_action_compatibility_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            responses = [
                {"message": {"role": "assistant", "content": '{"action":"write_file","path":"answer.py","content":"print(42)\\n"}'}, "eval_count": 12},
                {"message": {"role": "assistant", "content": '{"action":"finish","message":"Listo"}'}, "eval_count": 4},
            ]

            def fake_chat(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                return responses.pop(0)

            result = run_agent(
                "Create answer.py",
                model="json-action-model",
                project_root=root,
                workspace=workspace,
                validate_final=False,
                chat_fn=fake_chat,
                toolbox=Toolbox(root, workspace),
            )
            written_source = (workspace / "answer.py").read_text(encoding="utf-8")

        self.assertTrue(result["success"])
        self.assertEqual(result["final_message"], "Listo")
        self.assertEqual(written_source, "print(42)\n")
        self.assertEqual(result["metrics"]["tool_counts"], {"write_file": 1})

    def test_unittest_summary_parser_counts_failures_and_errors(self) -> None:
        summary = _parse_test_summary(
            "Ran 5 tests in 0.02s\n\nFAILED (failures=1, errors=2)", False
        )
        self.assertEqual(summary, {
            "cases": 5,
            "passed": 2,
            "failures": 1,
            "errors": 2,
            "status": "failed",
        })


if __name__ == "__main__":
    unittest.main()
