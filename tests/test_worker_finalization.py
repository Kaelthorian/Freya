"""One tool-free terminal decision after the existing no-progress detectors."""
from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control_center.config import DEFAULT_CONFIG
from control_center.execution_graph import ExecutionGraph
from control_center.worker import run_task
from control_center.worker_finalization import (
    FORCED_FINALIZATION_FORMAT, ForcedFinalizationInvalidOutput,
    finalization_context, validate_forced_finalization,
)
from tests.test_control_runtime import answer, terminal_answer
from tests.test_execution_graph import execution_plan, planned_task


CONVERTER = '''def celsius_to_fahrenheit(celsius):
    return celsius * 9 / 5 + 32


try:
    value = float(input("Celsius: "))
    print(f"Fahrenheit: {celsius_to_fahrenheit(value):.2f}")
except ValueError:
    print("Invalid temperature")
'''


def structured_answer():
    return answer(json.dumps({"summary": "Execution complete.", "actions": [],
                             "artifacts": [], "verification": {}, "limitations": []}))


def converter_replies(old_content):
    path = "temperature_converter.py"
    return [
        answer(calls=[("read_file", {"path": path})]),
        answer(calls=[("edit_file", {"path": path, "old": old_content, "new": CONVERTER})]),
        *[answer(calls=[("edit_file", {"path": path, "old": old, "new": old})])
          for old in ("celsius_to_fahrenheit", "input", "ValueError")],
        terminal_answer(),
    ]


class ForcedFinalizationContractTests(unittest.TestCase):
    def test_only_catalogued_evidence_is_accepted(self):
        value = json.loads(terminal_answer()["message"]["content"])
        value["evidence_refs"] = ["step-1"]
        self.assertEqual(validate_forced_finalization(json.dumps(value), {"step-1"}), value)
        with self.assertRaises(ForcedFinalizationInvalidOutput):
            validate_forced_finalization(json.dumps(value), set())

    def test_strict_contract_rejects_bad_fields_types_and_duplicates(self):
        valid = json.loads(terminal_answer()["message"]["content"])
        invalid = ["not JSON", "[]", '{"decision":"COMPLETED","decision":"BLOCKED"}']
        for field, bad in (("decision", "completed"), ("decision", None),
                           ("summary", " "), ("reason", []), ("reason", "x" * 2001),
                           ("missing_capability", "execution.python_script"),
                           ("evidence_refs", ["invented"]), ("evidence_refs", "step-1")):
            invalid.append(json.dumps({**valid, field: bad}))
        invalid.extend([json.dumps({**valid, "correct": True}),
                        json.dumps({key: value for key, value in valid.items() if key != "reason"}),
                        json.dumps(valid)[:-1] + ',"reason":"duplicate"}'])
        for value in invalid:
            with self.subTest(value=value[:100]), self.assertRaises(ForcedFinalizationInvalidOutput):
                validate_forced_finalization(value, set())

    def test_context_is_bounded_historical_and_uses_accumulated_data_only(self):
        actions = [{"event_id": f"step-{index}", "success": True, "output": "x" * 5000}
                   for index in range(25)]
        context = finalization_context(
            {"id": "task-2", "prompt": "Finish the converter", "config": {}},
            {"capabilities": {"execution": {"python_script": {"mode": "deny"}}}},
            ["read_file"], actions, {"evidence": [{"event_id": "old-step"}]},
            {"converter.py": (False, "old source", "read-step", "filesystem.read")},
            {"converter.py": None},
            {"workspace_changes": 1, "no_progress_actions": 3,
             "no_progress_trigger": "repeated_noop_edit"},
            {"steps": 25}, "Three no-op edits", ["Converter handles invalid input"],
        )
        self.assertEqual(context["action_count"], 25)
        self.assertEqual(len(context["actions"]), 20)
        self.assertTrue(context["actions_truncated"])
        self.assertTrue(context["actions"][0]["finalization_content_truncated"])
        self.assertTrue(context["observations_are_historical"])
        self.assertFalse(context["last_observable_state"][0]["matches_last_write"])
        self.assertEqual(context["tools_now"], [])
        self.assertEqual(context["capability_modes_during_execution"]["execution.python_script"], "deny")
        self.assertEqual(context["verification"]["evidence"], [{"event_id": "old-step"}])


class WorkerForcedFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "fixture.txt").write_text("hola", encoding="utf-8")

    def run_worker(self, replies, *, config=None, tools=None, checkpoint=None):
        task = {"id": "task-2", "workspace": str(self.workspace),
                "prompt": "Update fixture.txt to meet the supplied design.",
                "tools": tools or ["edit_file", "read_file"],
                "config": {**copy.deepcopy(DEFAULT_CONFIG), "permissions": "workspace",
                           "output": {"format": "structured", "include": [
                               "summary", "actions", "artifacts", "verification", "limitations"]},
                           "verification": {"enabled": False}, **(config or {})}}
        responses = iter(replies)
        self.payloads, self.events = [], []

        def transport(method, url, body, **kwargs):
            self.payloads.append(copy.deepcopy(body))
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response(body) if callable(response) else response

        return run_task(task, self.root, self.events.append, checkpoint or (lambda: None),
                        transport=transport)

    @staticmethod
    def noops(count=3):
        return [answer(calls=[("edit_file", {"path": "fixture.txt", "old": "hola", "new": "hola"})])
                for _ in range(count)]

    def event_types(self):
        return [item.get("event", {}).get("event_type") for item in self.events]

    def assert_one_terminal_call(self, result):
        self.assertEqual(len(self.payloads), 4)
        self.assertEqual(result["model_calls"], 4)
        self.assertEqual(result["tool_calls"], 3)
        self.assertEqual(self.payloads[-1]["tools"], [])
        self.assertEqual(self.payloads[-1]["format"], FORCED_FINALIZATION_FORMAT)
        self.assertEqual(self.event_types().count("worker.forced_finalization.started"), 1)
        self.assertNotIn("model.repair.started", self.event_types())

    def test_normal_completion_has_no_forced_call(self):
        result = self.run_worker([structured_answer()])
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["model_calls"], 1)
        self.assertFalse(result["no_progress_detected"])
        self.assertNotIn("worker.forced_finalization.started", self.event_types())

    def test_completed_is_technical_success_without_more_actions_or_verification(self):
        result = self.run_worker(self.noops() + [terminal_answer()], config={"verification": {
            "enabled": True, "inspect_changes": True, "run_available_tests": True,
            "require_tool_evidence": True, "completion_criteria": ["The intended design is correct."],
        }})
        self.assert_one_terminal_call(result)
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["execution_outcome"], "execution_complete")
        self.assertTrue(result["task_execution_successful"])
        self.assertEqual(result["workspace_changes"], 0)
        self.assertEqual(result["result"]["forced_finalization"]["decision"], "COMPLETED")
        self.assertFalse(result["verification"]["attempted"])
        self.assertFalse(result["verification"]["passed"])
        self.assertEqual(result["verification"]["evidence"], [])
        self.assertNotIn("verification.started", self.event_types())
        self.assertTrue(all(action["already_satisfied"] for action in result["result"]["actions"]))
        self.assertEqual((self.workspace / "fixture.txt").read_text(encoding="utf-8"), "hola")
        context = json.loads(self.payloads[-1]["messages"][1]["content"])
        self.assertEqual(context["trigger"], "repeated_noop_edit")
        self.assertEqual(context["action_count"], 3)
        self.assertEqual(context["success_criteria"], ["The intended design is correct."])

    def test_blocked_preserves_reason_and_missing_capability_without_granting(self):
        reason, capability = "Running the program requires a denied execution capability.", "execution.python_script"
        result = self.run_worker(self.noops() + [terminal_answer("BLOCKED", reason, capability)])
        self.assert_one_terminal_call(result)
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_class"], "operational_blocker")
        self.assertIn(reason, result["error"])
        self.assertIn(capability, result["error"])
        completed = next(item["event"] for item in self.events if
                         item.get("event", {}).get("event_type") == "worker.forced_finalization.completed")
        self.assertEqual(completed["missing_capability"], capability)
        self.assertEqual(completed["reason"], reason)
        context = json.loads(self.payloads[-1]["messages"][1]["content"])
        self.assertEqual(context["capability_modes_during_execution"][capability], "deny")
        self.assertNotIn("approval.requested", self.event_types())

    def test_native_tool_request_in_terminal_output_is_rejected_and_never_dispatched(self):
        response = terminal_answer()
        response["message"]["tool_calls"] = answer(calls=[("write_file", {
            "path": "forbidden.txt", "content": "must never execute"})])["message"]["tool_calls"]
        result = self.run_worker(self.noops() + [response], tools=["edit_file", "write_file"])
        self.assert_one_terminal_call(result)
        self.assertEqual(result["failure_class"], "ForcedFinalizationInvalidOutput")
        self.assertFalse((self.workspace / "forbidden.txt").exists())
        self.assertEqual(self.event_types().count("worker.forced_finalization.failed"), 1)

    def test_invalid_terminal_output_fails_explicitly_with_zero_repairs(self):
        for response in (answer("Finished."), answer("{}"), answer('{"action":"finish"}'),
                         answer(json.dumps({**json.loads(terminal_answer()["message"]["content"]),
                                            "evidence_refs": ["invented-step"]}))):
            with self.subTest(response=response):
                result = self.run_worker(self.noops() + [response])
                self.assert_one_terminal_call(result)
                self.assertEqual(result["status"], "Failed")
                self.assertEqual(result["failure_class"], "ForcedFinalizationInvalidOutput")
                self.assertIn("ForcedFinalizationInvalidOutput", result["error"])

    def test_same_size_byte_change_counts_as_progress(self):
        result = self.run_worker([answer(calls=[("edit_file", {
            "path": "fixture.txt", "old": "hola", "new": "Lkah"})]), structured_answer()])
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["workspace_changes"], 1)
        self.assertTrue(result["result"]["actions"][0]["changed"])
        self.assertFalse(result["no_progress_detected"])
        self.assertEqual((self.workspace / "fixture.txt").read_text(encoding="utf-8"), "Lkah")

    def test_repeated_reads_get_the_same_single_terminal_decision(self):
        reads = [answer(calls=[("read_file", {"path": "fixture.txt"})])] * 3
        result = self.run_worker(reads + [terminal_answer()])
        self.assert_one_terminal_call(result)
        self.assertEqual(result["status"], "Success", result["error"])
        context = json.loads(self.payloads[-1]["messages"][1]["content"])
        self.assertEqual(context["trigger"], "repeated_read")
        self.assertTrue(context["observations_are_historical"])
        self.assertEqual(context["last_observable_state"][0]["output"], "hola")

    def test_alternating_reads_keep_the_existing_early_stop_threshold(self):
        (self.workspace / "second.txt").write_text("other source", encoding="utf-8")
        reads = [answer(calls=[("read_file", {"path": path})]) for path in
                 ("fixture.txt", "second.txt", "fixture.txt", "second.txt", "fixture.txt")]
        result = self.run_worker(reads + [terminal_answer()])
        self.assertEqual(result["status"], "Success", result["error"])
        # The unchanged three-occurrence guard fires before a sixth alternating read.
        self.assertEqual((result["steps"], result["tool_calls"], result["model_calls"]), (5, 5, 6))
        self.assertEqual(self.payloads[-1]["tools"], [])
        self.assertEqual(self.event_types().count("worker.forced_finalization.started"), 1)

    def test_terminal_decision_can_reference_observed_runtime_evidence(self):
        def cite(body):
            context = json.loads(body["messages"][1]["content"])
            response = json.loads(terminal_answer()["message"]["content"])
            response["evidence_refs"] = [context["actions"][-1]["evidence_ref"]]
            return answer(json.dumps(response))

        result = self.run_worker(self.noops() + [cite])
        self.assert_one_terminal_call(result)
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["result"]["forced_finalization"]["evidence_refs"],
                         [result["result"]["actions"][-1]["event_id"]])

    def test_existing_command_evidence_survives_without_additional_verification(self):
        path = self.workspace / "temperature_converter.py"
        path.write_text("# stub\n", encoding="utf-8")
        replies = converter_replies("# stub\n")
        replies.insert(2, answer(calls=[("run_command", {
            "argv": ["python", "temperature_converter.py"], "stdin": "0\n"})]))
        with patch("control_center.tools.run_in_sandbox", return_value=subprocess.CompletedProcess(
                ["python", "temperature_converter.py"], 0, "Fahrenheit: 32.00\n", "")) as execution:
            result = self.run_worker(replies, tools=["read_file", "edit_file", "run_command"], config={
                "permissions": "execute", "verification": {
                    "enabled": True, "inspect_changes": True, "run_available_tests": True,
                    "require_tool_evidence": True,
                    "completion_criteria": ["The converter handles the complete required behavior."],
                }})
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(execution.call_count, 1)
        self.assertEqual((result["tool_calls"], result["model_calls"]), (6, 7))
        self.assertNotIn("verification.started", self.event_types())
        evidence = result["verification"]["evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["type"], "command_execution")
        self.assertIn("Fahrenheit: 32.00", evidence[0]["stdout"])
        context = json.loads(self.payloads[-1]["messages"][1]["content"])
        self.assertEqual(context["verification"]["evidence"], evidence)

    def test_no_terminal_call_if_existing_call_or_token_budget_is_exhausted(self):
        for limit in ({"max_model_calls": 3}, {"max_tokens": 15}):
            with self.subTest(limit=limit):
                result = self.run_worker(self.noops(), config=limit)
                self.assertEqual(len(self.payloads), 3)
                self.assertEqual(result["failure_class"], "ForcedFinalizationBudgetExhausted")
                self.assertEqual(result["status"], "Failed")
                self.assertNotIn("model.repair.started", self.event_types())

    def test_terminal_call_consumes_only_remaining_token_budget(self):
        result = self.run_worker(self.noops() + [terminal_answer()], config={"max_tokens": 16})
        self.assert_one_terminal_call(result)
        self.assertEqual(self.payloads[-1]["options"]["num_predict"], 1)
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_class"], "TaskStopped")
        self.assertIn("cumulative tokens", result["error"])

    def test_terminal_transport_failure_does_not_restart_or_repair(self):
        result = self.run_worker(self.noops() + [TimeoutError("Terminal fixture timeout")])
        self.assert_one_terminal_call(result)
        self.assertEqual(result["status"], "Failed")
        self.assertIn("timeout", result["error"])
        self.assertEqual(self.event_types().count("worker.forced_finalization.failed"), 1)

    def test_time_limit_and_cancellation_still_apply_during_terminal_call(self):
        clock = [0.0]

        def expire(_body):
            clock[0] = 11.0
            return terminal_answer()

        with patch("control_center.worker.time.monotonic", side_effect=lambda: clock[0]):
            result = self.run_worker(self.noops() + [expire], config={"max_seconds": 10})
        self.assert_one_terminal_call(result)
        self.assertEqual(result["status"], "Failed")
        self.assertIn("Maximum execution time", result["error"])

        cancelled = [False]

        def cancel(_body):
            cancelled[0] = True
            return terminal_answer()

        def checkpoint():
            if cancelled[0]:
                raise RuntimeError("Cancelled fixture")

        result = self.run_worker(self.noops() + [cancel], checkpoint=checkpoint)
        self.assert_one_terminal_call(result)
        self.assertEqual(result["status"], "Failed")
        self.assertIn("Cancelled fixture", result["error"])

    def test_step_limit_and_denied_tool_cycles_keep_their_existing_failure_paths(self):
        result = self.run_worker(self.noops(2), config={"max_steps": 2})
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["steps"], 2)
        self.assertNotIn("worker.forced_finalization.started", self.event_types())
        result = self.run_worker([answer(calls=[("request_new_capabilities", {"capability": "shell"})])] * 3)
        self.assertEqual(result["failure_class"], "blocked_action_cycle")
        self.assertNotIn("worker.forced_finalization.started", self.event_types())

    def test_converter_edit_noops_release_qa_without_semantic_acceptance(self):
        path = self.workspace / "temperature_converter.py"
        path.write_text("# stub\n", encoding="utf-8")
        result = self.run_worker(converter_replies("# stub\n"))
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["model_calls"], 6)
        self.assertEqual(result["tool_calls"], 5)
        self.assertEqual(result["workspace_changes"], 1)
        self.assertEqual(path.read_text(encoding="utf-8"), CONVERTER)
        self.assertEqual([row["changed"] for row in result["result"]["actions"]],
                         [None, True, False, False, False])
        graph = ExecutionGraph(execution_plan([
            planned_task("task-1"), planned_task("task-2", ["task-1"]),
            planned_task("task-3", ["task-2"]),
        ]))
        for task_id in ("task-1", "task-2"):
            graph.mark_selected(task_id, "worker", "selection-" + task_id)
            graph.mark_running(task_id, "runtime-" + task_id, "delegation-" + task_id)
            graph.apply_runtime_status(task_id, result["status"])
            graph.refresh_dependencies()
        self.assertEqual(graph.node("task-2")["state"], "runtime_success")
        self.assertIsNone(graph.node("task-2")["evaluation_id"])
        self.assertEqual(graph.node("task-3")["state"], "ready")

        cases = [{"id": "zero", "input": "0\n"}, {"id": "hundred", "input": "100\n"},
                 {"id": "invalid", "input": "invalid\n"}]

        def sandbox(workspace, argv, timeout_seconds, stdin):
            output = {"0\n": "Fahrenheit: 32.00\n", "100\n": "Fahrenheit: 212.00\n",
                      "invalid\n": "Invalid temperature\n"}[stdin]
            return subprocess.CompletedProcess(argv, 0, output, "")

        with patch("control_center.tools.run_in_sandbox", side_effect=sandbox) as execution:
            qa = self.run_worker([answer(calls=[("run_command", {
                "argv": ["python", "temperature_converter.py"]})])], tools=["run_command"], config={
                    "permissions": "execute", "verification_mode": "independent_cases",
                    "verification_cases": cases})
        self.assertEqual(qa["status"], "Success", qa["error"])
        self.assertEqual(execution.call_count, 3)
        self.assertEqual(qa["model_calls"], 1)
        self.assertEqual([row["case_id"] for row in qa["result"]["actions"]],
                         ["zero", "hundred", "invalid"])
        self.assertEqual([row["input"] for row in qa["result"]["actions"]], [case["input"] for case in cases])
        self.assertEqual([row["exit_code"] for row in qa["result"]["actions"]], [0, 0, 0])
        graph.mark_selected("task-3", "worker", "selection-task-3")
        graph.mark_running("task-3", "runtime-task-3", "delegation-task-3")
        graph.apply_runtime_status("task-3", qa["status"])
        self.assertEqual(graph.node("task-3")["state"], "runtime_success")
