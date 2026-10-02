"""Real scheduler/process tests plus deterministic tool and budget regressions."""

import json
import os
import tempfile
import threading
import time
import unittest
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from control_center.agent_factory import AgentFactory
from control_center.config import DEFAULT_CONFIG, normalize_agent
from control_center.evaluator import Evaluator
from control_center.final_state import build_final_state
from control_center.execution_graph import ExecutionGraph
from control_center.runtime import Runtime
from control_center.storage import Store
from control_center.task_spec import render_task_spec, validate_task_spec
from control_center.transport import TransportError, request_json
from control_center.worker import (
    PolicyToolbox, _command_evidence_criteria, _readback_evidence_criteria, run_task,
)
from control_center.tools import ToolResult


ROOT = Path(__file__).resolve().parents[1]


def answer(content="Finished.", calls=None, **metrics):
    result = {"message": {"role": "assistant", "content": content}, "prompt_eval_count": 3, "eval_count": 2}
    if calls:
        result["message"]["tool_calls"] = [{"function": {"name": name, "arguments": args}} for name, args in calls]
    result.update(metrics)
    return result


def terminal_answer(decision="COMPLETED", reason="No further operational action is needed.",
                    missing_capability=None):
    return answer(json.dumps({"decision": decision, "summary": "Worker execution stopped.",
                             "reason": reason, "evidence_refs": [],
                             "missing_capability": missing_capability}))


class FakeOllama:
    def __init__(self, responses=None):
        owner = self
        self.responses = deque(responses or [])
        self.requests = []
        self.lock = threading.Lock()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.redirect = ""
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                with owner.lock:
                    owner.requests.append(body)
                    response = owner.responses.popleft() if owner.responses else answer()
                owner.entered.set()
                owner.release.wait(8)
                if owner.redirect:
                    self.send_response(302)
                    self.send_header("Location", owner.redirect)
                    self.end_headers()
                    return
                payload = json.dumps(response).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:{}".format(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.events = []

    def run_worker(self, responses, *, config=None, tools=None, toolbox=None, token="",
                   task_characteristics=None, prompt="Create hello.py"):
        config = {**DEFAULT_CONFIG, **(config or {})}
        task = {"config": config, "tools": tools if tools is not None else ["write_file", "read_file"],
                "workspace": str(self.workspace), "prompt": prompt}
        if task_characteristics is not None:
            task["task_characteristics"] = dict(task_characteristics)
        replies = iter(responses)
        self.payloads = []
        def transport(method, url, body, **kwargs):
            self.payloads.append(body)
            return next(replies)
        return run_task(task, self.root, self.events.append, lambda: None,
                        transport=transport, toolbox=toolbox, token=token)

    def evaluate_result(self, evaluator, *, planned_task, runtime_task, **kwargs):
        # Mirrors the parent preparation boundary; Evaluator never reads the workspace.
        task = {**runtime_task, "final_state": build_final_state(
            planned_task, [runtime_task], str(self.workspace))}
        return evaluator.evaluate(planned_task=planned_task, runtime_task=task, **kwargs)

    @staticmethod
    def _structured_output():
        return {"format": "structured", "include": [
            "summary", "actions", "artifacts", "verification", "limitations",
        ]}

    def test_noop_edit_is_observable_without_workspace_mutation_or_empty_diff(self):
        target = self.workspace / "calculator.html"
        target.write_text("<button>7</button>", encoding="utf-8")
        result = self.run_worker([
            answer(calls=[("edit_file", {"path": "calculator.html", "old": "7", "new": "7"})]),
            answer("The requested content is already present."),
        ], tools=["edit_file"], config={"permissions": "workspace", "verification": {"enabled": False},
                                       "output": self._structured_output()},
            prompt="Inspect calculator.html")
        action = result["result"]["actions"][0]
        self.assertTrue(action["success"])
        self.assertFalse(action["changed"])
        self.assertTrue(action["already_satisfied"])
        self.assertIn("ALREADY_SATISFIED", action["output"])
        self.assertEqual(result["workspace_changes"], 0)
        self.assertEqual(result["result"]["artifacts"], [])
        self.assertNotIn("workspace_diffs", result["result"])
        event_types = [item.get("event", {}).get("event_type") for item in self.events]
        self.assertNotIn("workspace.diff", event_types)
        satisfied = next(item["event"] for item in self.events
                         if item.get("event", {}).get("event_type") == "worker.write_already_satisfied")
        self.assertEqual(satisfied["tool"], "edit_file")
        self.assertFalse(satisfied["changed"])
        self.assertTrue(satisfied["already_satisfied"])
        self.assertEqual(target.read_text(encoding="utf-8"), "<button>7</button>")

    def test_different_noop_edits_on_same_file_stop_before_step_limit(self):
        target = self.workspace / "calculator.html"
        target.write_text("<button>7</button><button>8</button><button>9</button>", encoding="utf-8")
        calls = [("edit_file", {"path": "calculator.html", "old": str(value), "new": str(value)})
                 for value in (7, 8, 9, 7, 8)]
        result = self.run_worker([answer(calls=[call]) for call in calls[:3]] + [terminal_answer()],
                                 tools=["edit_file"],
                                 config={"permissions": "workspace", "verification": {"enabled": False},
                                         "output": self._structured_output(), "max_steps": 20},
                                 prompt="Update calculator.html")
        self.assertEqual(result["status"], "Success")
        self.assertEqual(result["failure_class"], "")
        self.assertTrue(result["no_progress_detected"])
        self.assertLess(result["steps"], 20)
        self.assertEqual(result["workspace_changes"], 0)
        self.assertEqual(len(result["result"]["actions"]), 3)
        self.assertTrue(all(action["already_satisfied"] and not action["changed"]
                            for action in result["result"]["actions"]))
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "<button>7</button><button>8</button><button>9</button>")
        self.assertTrue(any(item.get("event", {}).get("event_type") == "task.no_progress"
                            and item["event"].get("output", {}).get("pattern") == "repeated_noop_edit"
                            for item in self.events))

    def test_noop_edit_then_new_read_evidence_then_real_edit_progresses(self):
        target = self.workspace / "calculator.html"
        target.write_text("<button>7</button>", encoding="utf-8")
        result = self.run_worker([
            answer(calls=[("edit_file", {"path": "calculator.html", "old": "7", "new": "7"})]),
            answer(calls=[("read_file", {"path": "calculator.html"})]),
            answer(calls=[("edit_file", {"path": "calculator.html", "old": "7", "new": "8"})]),
            answer("Updated calculator.html."),
        ], tools=["edit_file", "read_file"],
            config={"permissions": "workspace", "verification": {"enabled": False},
                    "output": self._structured_output()},
            prompt="Update calculator.html")
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertFalse(result["no_progress_detected"])
        self.assertEqual(result["workspace_changes"], 1)
        self.assertEqual([action["changed"] for action in result["result"]["actions"]],
                         [False, None, True])
        self.assertEqual(len(result["result"]["artifacts"]), 1)
        self.assertEqual(len(result["result"]["workspace_diffs"]), 1)
        self.assertNotEqual(result["result"]["workspace_diffs"][0]["diff"], "(no textual difference)")
        self.assertEqual(target.read_text(encoding="utf-8"), "<button>8</button>")

    def test_read_then_noop_edit_is_only_a_candidate_for_evaluator(self):
        target = self.workspace / "calculator.html"
        target.write_text("<button>7</button>", encoding="utf-8")
        result = self.run_worker([
            answer(calls=[("read_file", {"path": "calculator.html"})]),
            answer(calls=[("edit_file", {"path": "calculator.html", "old": "7", "new": "7"})]),
            answer("The requested content is already present."),
        ], tools=["read_file", "edit_file"],
            config={"permissions": "workspace", "verification": {"enabled": False},
                    "output": self._structured_output()},
            task_characteristics={"requires_filesystem_write": True},
            prompt="Update calculator.html")
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertTrue(result["already_satisfied_candidate"])
        self.assertEqual(result["workspace_changes"], 0)
        self.assertEqual(result["result"]["artifacts"], [])
        self.assertEqual(result["result"]["already_satisfied_candidate"]["artifact_observations"][0]["path"],
                         "calculator.html")

    def _assigned_worker_context(self, source_task_id):
        from control_center.agent_factory import AgentFactory

        return {
            "permissions": "read_only",
            "capability_policy": AgentFactory.capability_policy(["filesystem.read"]),
            "provenance": {
                "generated_by_freya": True, "ephemeral": True,
                "orchestration_id": "orchestration-1", "plan_task_id": "task-2",
                "attempt": 1, "factory_version": 1,
            },
            "worker_assignment": {
                "worker_id": "worker-1", "task_ids": ["task-1", "task-2"],
                "generation": 1,
            },
            "task_foreign_write_targets": [
                {"path": "calculator.html", "owner_plan_task_id": "task-1"},
            ],
            "task_planned_write_targets": [
                {"path": "calculator.html", "owner_plan_task_id": "task-1"},
            ],
            "task_write_owners": {"calculator.html": "task-1"},
            "task_write_scope_enforced": True,
            "active_task_capabilities": ["filesystem.read"],
            "active_task_tools": ["read_file"],
            "runtime_context": {
                "active_task_context": {
                    "worker_id": "worker-1",
                    "previous_completed_task_ids": ["task-1"],
                    "current_task": {"task_id": "task-2"},
                },
                "project_state_snapshot": {
                    "artifacts": [{
                        "path": "calculator.html", "revision": 3,
                        "last_modified_by_task": source_task_id,
                    }],
                },
            },
            "verification": {
                "enabled": False, "inspect_changes": False,
                "run_available_tests": False, "require_tool_evidence": False,
                "completion_criteria": ["calculator.html implements the requested calculator."],
            },
            "output": self._structured_output(),
        }

    def test_anticipated_artifact_can_satisfy_only_the_same_worker_assignment(self):
        (self.workspace / "calculator.html").write_text(
            "<main>Calculator with addition and subtraction</main>", encoding="utf-8",
        )
        result = self.run_worker([
            answer(calls=[("read_file", {"path": "calculator.html"})]),
            answer('{"summary":"The calculator is already complete.","actions":[],"artifacts":[],"verification":{},"limitations":[]}'),
        ], tools=["read_file"], config=self._assigned_worker_context("task-1"),
            task_characteristics={"requires_filesystem_write": True},
            prompt="Complete the future calculator task")
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertTrue(result["already_satisfied_candidate"])
        observation = result["result"]["already_satisfied_candidate"]["artifact_observations"][0]
        self.assertEqual(observation["source_task_id"], "task-1")
        self.assertEqual(result["workspace_changes"], 0)
        evaluation = self.evaluate_result(Evaluator(offline=True),
            planned_task={
                "id": "task-2", "objective": "Confirm the existing calculator file.",
                "description": "Read calculator.html and confirm it exists.",
                "success_criteria": ["calculator.html exists"],
                "owned_paths": [], "write_targets": ["calculator.html"],
                "required_capabilities": ["filesystem.read"], "preferred_skills": [],
            },
            runtime_task=result,
            execution_node={"selected_agent_id": "worker", "runtime_task_id": "runtime", "attempt": 1},
        )
        self.assertEqual(evaluation["status"], "accepted")
        self.assertTrue(evaluation["context_snapshot"]["final_state"]["files"][0]["exists"])
        self.assertNotIn("runtime_task", evaluation["context_snapshot"])

    def test_anticipated_artifact_from_another_worker_does_not_bypass_mutation_contract(self):
        (self.workspace / "calculator.html").write_text(
            "<main>Calculator with addition and subtraction</main>", encoding="utf-8",
        )
        result = self.run_worker([
            answer(calls=[("read_file", {"path": "calculator.html"})]),
            answer('{"summary":"The calculator is already complete.","actions":[],"artifacts":[],"verification":{},"limitations":[]}'),
        ], tools=["read_file"], config=self._assigned_worker_context("task-3"),
            task_characteristics={"requires_filesystem_write": True},
            prompt="Complete the future calculator task")
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_class"], "ExpectedWorkspaceMutationNotObserved")
        self.assertFalse(result["already_satisfied_candidate"])
        self.assertEqual(result["workspace_changes"], 0)

    def test_repeated_reads_of_existing_presence_only_target_complete_for_evaluator(self):
        (self.workspace / "a.js").write_text("// existing scaffold\n", encoding="utf-8")
        result = self.run_worker([
            answer(calls=[("read_file", {"path": "a.js"})]) for _ in range(3)
        ], tools=["read_file"], config={
            "permissions": "workspace", "task_owned_paths": ["a.js"],
            "output": self._structured_output(),
            "verification": {"enabled": True, "completion_criteria": ["a.js exists"]},
        }, task_characteristics={"requires_filesystem_write": True},
            prompt="Create a.js if absent")
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertTrue(result["already_satisfied_candidate"])
        self.assertFalse(result["no_progress_detected"])
        self.assertEqual(result["model_calls"], 3)
        self.assertTrue(any(item.get("event", {}).get("event_type") == "task.auto_completed"
                            for item in self.events))
        evaluation = self.evaluate_result(Evaluator(offline=True),
            planned_task={
                "id": "task-2", "objective": "Create a.js if absent",
                "description": "Ensure a.js exists", "success_criteria": ["a.js exists"],
                "owned_paths": ["a.js"], "write_targets": ["a.js"],
                "required_capabilities": ["filesystem.read"], "preferred_skills": [],
            },
            runtime_task=result,
            execution_node={"selected_agent_id": "worker", "runtime_task_id": "runtime", "attempt": 1},
        )
        self.assertEqual(evaluation["status"], "accepted", evaluation)

    def test_native_tools_metrics_and_no_private_reasoning(self):
        response = answer("<think>PRIVATE_INTERNAL</think>", [("write_file", {"path": "hello.py", "content": "print('hello')"})])
        response["message"]["thinking"] = "PRIVATE_INTERNAL"
        result = self.run_worker([response, answer("<think>PRIVATE_INTERNAL</think>Created hello.py")])
        self.assertEqual(result["status"], "Success")
        self.assertEqual((self.workspace / "hello.py").read_text(), "print('hello')")
        self.assertEqual(result["total_tokens"], 10)
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["tool_calls"], 2)
        self.assertTrue(result["verification"]["attempted"])
        self.assertTrue(result["verification"]["passed"])
        self.assertFalse(result["verification"]["unavailable"])
        self.assertIn("filesystem:read_file:hello.py", [item["check"] for item in result["verification"]["evidence"]])
        self.assertNotIn("PRIVATE_INTERNAL", json.dumps(self.events) + json.dumps(result))
        self.assertNotIn("thinking", self.payloads[-1]["messages"][2])

    def test_successful_file_write_emits_workspace_code_diff(self):
        result = self.run_worker([
            answer(calls=[("write_file", {"path": "calculator.bat", "content": "@echo off\necho %1"})]),
            answer("Created calculator.bat."),
        ])
        self.assertEqual(result["status"], "Success")
        diffs = [event["event"] for event in self.events
                 if event.get("event", {}).get("event_type") == "workspace.diff"]
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["output"]["path"], "calculator.bat")
        self.assertEqual(diffs[0]["output"]["change_type"], "created")
        self.assertIn("+@echo off", diffs[0]["output"]["diff"])
        self.assertEqual(diffs[0]["output"]["source"], "workspace_diff")
        self.assertEqual(diffs[0]["output"]["tool"], "write_file")
        self.assertEqual(diffs[0]["output"]["capability"], "filesystem.create")
        self.assertTrue(diffs[0]["output"]["event_id"])

    def test_cross_task_retry_supplies_contract_and_enters_approval(self):
        (self.workspace / "owner.txt").write_text("before", encoding="utf-8")
        config = {
            "capability_policy": AgentFactory.capability_policy([
                "filesystem.read", "filesystem.modify",
            ]),
            "permissions": "workspace", "allowed_directories": ["."],
            "provenance": {"generated_by_freya": True,
                           "orchestration_id": "orchestration-1",
                           "plan_task_id": "requester"},
            "task_owned_paths": [],
            "task_foreign_write_targets": [
                {"path": "owner.txt", "owner_plan_task_id": "owner"}],
            "task_planned_write_targets": [],
            "task_write_owners": {"owner.txt": "owner"},
            "task_write_scope_enforced": True,
        }
        incomplete = {
            "path": "owner.txt", "old": "before", "new": "after",
            "reason": "Prevent stale cache entries from persisting", "blocking": True,
        }
        corrected = {
            "path": "owner.txt", "old": "before", "new": "after",
            "requested_change": "Add an explicit cache expiry setting",
            "reason": "Prevent stale cache entries from persisting", "blocking": True,
        }
        result = self.run_worker([
            answer(calls=[("read_file", {"path": "owner.txt"}),
                          ("edit_file", incomplete)]),
            answer(calls=[("edit_file", corrected)]),
        ], config=config, tools=["read_file", "edit_file"], prompt="Update the owned cache file")
        self.assertEqual(result["status"], "WaitingForApproval")
        request = next(item["event"] for item in self.events
                       if item.get("event", {}).get("event_type") == "cross_task_modification.requested")
        self.assertEqual(request["requested_change"], "Add an explicit cache expiry setting")
        self.assertEqual(request["needed_for"],
                         "Completing plan task requester requires this declared file change.")
        retry_prompt = next(item["content"] for item in self.payloads[1]["messages"]
                            if item.get("role") == "user"
                            and "Fields needing correction" in item.get("content", ""))
        self.assertIn("requested_change", retry_prompt)
        self.assertIn("needed_for", retry_prompt)
        self.assertEqual((self.workspace / "owner.txt").read_text(encoding="utf-8"), "before")

    def test_identical_incomplete_cross_task_action_stops_as_no_progress(self):
        (self.workspace / "owner.txt").write_text("before", encoding="utf-8")
        config = {
            "capability_policy": AgentFactory.capability_policy([
                "filesystem.read", "filesystem.modify",
            ]),
            "permissions": "workspace", "allowed_directories": ["."],
            "provenance": {"generated_by_freya": True,
                           "orchestration_id": "orchestration-1",
                           "plan_task_id": "requester"},
            "task_owned_paths": [],
            "task_foreign_write_targets": [
                {"path": "owner.txt", "owner_plan_task_id": "owner"}],
            "task_planned_write_targets": [],
            "task_write_owners": {"owner.txt": "owner"},
            "task_write_scope_enforced": True,
        }
        incomplete = {
            "path": "owner.txt", "old": "before", "new": "after",
            "reason": "Prevent stale cache entries from persisting", "blocking": True,
        }
        result = self.run_worker([
            answer(calls=[("read_file", {"path": "owner.txt"}),
                          ("edit_file", incomplete)]),
            answer(calls=[("edit_file", incomplete)]),
            terminal_answer("BLOCKED", "The required cross-task request details are incomplete."),
        ], config=config, tools=["read_file", "edit_file"], prompt="Update the owned cache file")
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_class"], "operational_blocker")
        self.assertTrue(result["no_progress_detected"])
        self.assertLess(result["steps"], config.get("max_steps", 20))
        events = [item["event"] for item in self.events
                  if item.get("event", {}).get("event_type") == "task.no_progress"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["output"]["repeat_count"], 2)
        self.assertEqual((self.workspace / "owner.txt").read_text(encoding="utf-8"), "before")

    def test_successful_command_output_becomes_acceptance_evidence(self):
        result = self.run_worker([
            answer(calls=[("write_file", {"path": "hello.py", "content": "print('Hello World')"})]),
            answer(calls=[("read_file", {"path": "hello.py"})]),
            answer(calls=[("run_command", {"argv": ["python", "hello.py"]})]),
            answer("Created and verified hello.py."),
        ], tools=["write_file", "read_file", "run_command"], config={
            "permissions": "execute",
            "output": {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
            "verification": {
                "enabled": True, "inspect_changes": False, "run_available_tests": False,
                "require_tool_evidence": True,
                "completion_criteria": ["The program outputs 'Hello World' when executed"],
            },
        })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertTrue(result["verification"]["attempted"])
        self.assertTrue(result["verification"]["passed"])
        self.assertFalse(result["verification"]["unavailable"])
        evidence = result["verification"]["evidence"]
        command = next(item for item in evidence if item.get("type") == "command_execution")
        self.assertEqual(command["exit_code"], 0)
        self.assertIn("Hello World", command["output"])
        self.assertIn("The program outputs 'Hello World' when executed",
                      command["supports_acceptance_criteria"])
        self.assertTrue(any(item.get("check") == "filesystem:read_file:hello.py"
                            for item in evidence))
        self.assertTrue(any(item["tool"] == "run_command" for item in result["result"]["actions"]))

    def test_file_creation_stops_after_matching_readback_evidence(self):
        criterion = "calculator.py exists in the selected workspace and can be read."
        content = "print('ready')\n"
        result = self.run_worker([
            answer(calls=[("write_file", {"path": "calculator.py", "content": content})]),
            answer(calls=[("read_file", {"path": "calculator.py"})]),
        ], tools=["write_file", "read_file"], config={
            "output": {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
            "verification": {
                "enabled": True, "inspect_changes": False, "run_available_tests": False,
                "require_tool_evidence": True, "completion_criteria": [criterion],
            },
        })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["tool_calls"], 2)
        self.assertTrue(result["verification"]["passed"])
        self.assertTrue(any(item.get("check") == "filesystem:read_file:calculator.py"
                            for item in result["verification"]["evidence"]))
        self.assertTrue(any(event.get("event", {}).get("event_type") == "task.auto_completed"
                            for event in self.events))

    def test_single_case_qa_stops_after_exact_command_evidence(self):
        (self.workspace / "calculator.py").write_text(
            "first = float(input())\nsecond = float(input())\n"
            "print(f'The sum is: {first + second:.0f}')\n",
            encoding="utf-8",
        )
        criterion = "The bounded command outputs '8' and exits successfully."
        result = self.run_worker([
            answer(calls=[("run_command", {
                "argv": ["python", "calculator.py"], "stdin": "3\n5",
            })]),
        ], tools=["run_command"], config={
            "permissions": "execute",
            "output": {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
            "verification": {
                "enabled": True, "inspect_changes": False, "run_available_tests": False,
                "require_tool_evidence": True, "completion_criteria": [criterion],
                "stop_after_acceptance_evidence": True,
            },
        })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["tool_calls"], 1)
        self.assertTrue(result["verification"]["passed"])
        self.assertIn("The sum is: 8", result["verification"]["evidence"][0]["output"])
        self.assertTrue(any(event.get("event", {}).get("event_type") == "task.auto_completed"
                            for event in self.events))

    def test_command_evidence_requires_exact_numeric_output_and_all_assertions(self):
        criterion = "The bounded command outputs '8' and exits successfully."
        argv = ["python", "calculator.py"]
        self.assertEqual(_command_evidence_criteria(
            [criterion], argv, ToolResult("run_command", "The sum is: 8.0", True, 0, exit_code=0),
        ), [])
        self.assertEqual(_command_evidence_criteria(
            [criterion], argv, ToolResult("run_command", "The sum is: 8", True, 0, exit_code=0),
        ), [criterion])
        self.assertEqual(_command_evidence_criteria(
            [criterion], argv, ToolResult("run_command", "The sum is: 8", True, 0, exit_code=1),
        ), [])
        self.assertEqual(_readback_evidence_criteria(
            ["calculator.py exists and can be read."], "calculator.py", "source", "source",
        ), ["calculator.py exists and can be read."])
        self.assertEqual(_readback_evidence_criteria(
            ["calculator.py does not exist and can be read."], "calculator.py", "source", "source",
        ), [])

    def test_calculator_create_run_and_identical_write_finishes_from_evidence(self):
        content = "first = float(input())\nsecond = float(input())\nprint(f'The sum is: {first + second:.1f}')\n"
        result = self.run_worker([
            answer(calls=[("write_file", {"path": "calculator.py", "content": content})]),
            answer(calls=[("run_command", {"argv": ["python", "calculator.py"], "stdin": "3\n5"})]),
            answer(calls=[("write_file", {"path": "calculator.py", "content": content})]),
        ], tools=["write_file", "read_file", "run_command"], config={
            "permissions": "execute",
            "output": {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
            "verification": {
                "enabled": True, "inspect_changes": False, "run_available_tests": False,
                "require_tool_evidence": True,
                "completion_criteria": ["The program outputs 'The sum is: 8.0' when executed"],
            },
        })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertNotIn("BlockedActionCycle", result["error"])
        self.assertEqual(result["workspace_changes"], 1)
        self.assertTrue(result["verification"]["requested"])
        self.assertTrue(result["verification"]["attempted"])
        self.assertTrue(result["verification"]["passed"])
        evidence = next(item for item in result["verification"]["evidence"]
                        if item.get("type") == "command_execution")
        self.assertEqual(evidence["exit_code"], 0)
        self.assertIn("The sum is: 8.0", evidence["output"])
        duplicate = next(item for item in result["result"]["actions"] if item.get("already_satisfied"))
        self.assertFalse(duplicate["changed"])
        self.assertEqual(duplicate["error_class"], "already_satisfied")
        diffs = [event["event"] for event in self.events
                 if event.get("event", {}).get("event_type") == "workspace.diff"]
        self.assertEqual(len(diffs), 1)

    def test_parent_path_error_stops_sibling_writes_before_strategy_change(self):
        result = self.run_worker([
            answer(calls=[
                ("write_file", {"path": "calculator-project", "content": ""}),
                ("write_file", {"path": "calculator-project/index.html", "content": "old"}),
                ("write_file", {"path": "calculator-project/style.css", "content": "body {}"}),
            ]),
            answer(calls=[
                ("write_file", {"path": "site/index.html", "content": "<h1>Calculator</h1>"}),
                ("write_file", {"path": "site/style.css", "content": "body {}"}),
            ]),
            answer("Created site with its HTML and CSS files."),
        ], tools=["write_file"], config={
            "verification": {"enabled": False, "inspect_changes": False, "run_available_tests": False,
                            "require_tool_evidence": False, "completion_criteria": []},
        })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertTrue((self.workspace / "calculator-project").is_file())
        self.assertTrue((self.workspace / "site/index.html").is_file())
        self.assertTrue((self.workspace / "site/style.css").is_file())
        first_batch = next(message for message in self.payloads[1]["messages"]
                           if message.get("role") == "assistant" and message.get("tool_calls"))
        attempted_paths = [call["function"]["arguments"]["path"] for call in first_batch["tool_calls"]]
        self.assertEqual(attempted_paths, ["calculator-project", "calculator-project/index.html"])
        structural_errors = [event["event"] for event in self.events
                             if event.get("event", {}).get("error_class") == "ParentPathIsFile"]
        self.assertEqual(len(structural_errors), 1)

    def test_parent_path_error_rejects_repeated_child_write_without_execution(self):
        result = self.run_worker([
            answer(calls=[
                ("write_file", {"path": "project", "content": ""}),
                ("write_file", {"path": "project/index.html", "content": "<h1>Hi</h1>"}),
            ]),
            answer(calls=[("write_file", {"path": "project/style.css", "content": "body {}"})]),
        ], tools=["write_file"], config={
            "verification": {"enabled": False, "inspect_changes": False, "run_available_tests": False,
                            "require_tool_evidence": False, "completion_criteria": []},
        })
        self.assertEqual(result["status"], "Failed")
        self.assertIn("ParentPathIsFile", result["error"])
        self.assertEqual(result["tool_calls"], 2)
        finished = [event["event"] for event in self.events
                    if event.get("event", {}).get("event_type") == "step.finished"]
        self.assertEqual(len(finished), 2)
        self.assertEqual(finished[-1]["error_class"], "ParentPathIsFile")
        self.assertFalse((self.workspace / "project/style.css").exists())

    def test_changed_arguments_after_policy_denial_allow_a_new_strategy(self):
        existing = self.workspace / "existing.py"
        existing.write_text("old", encoding="utf-8")
        policy = {
            "capabilities": {
                "filesystem": {"create": {"mode": "allow"}, "read": {"mode": "allow"},
                               "modify": {"mode": "deny"}, "overwrite": {"mode": "deny"}},
                "execution": {}, "git": {},
            }
        }
        result = self.run_worker([
            answer(calls=[("write_file", {"path": "existing.py", "content": "new"})]),
            answer(calls=[("write_file", {"path": "alternate.py", "content": "new"})]),
            answer("Created alternate.py."),
        ], tools=["write_file"], config={"capability_policy": policy})
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(existing.read_text(encoding="utf-8"), "old")
        self.assertEqual((self.workspace / "alternate.py").read_text(encoding="utf-8"), "new")
        steps = [event["event"] for event in self.events
                 if event.get("event", {}).get("event_type") == "step.finished"]
        self.assertEqual([item["error_class"] for item in steps[:2]], ["policy_denied", ""])
        self.assertEqual(result["tool_calls"], 3)  # includes the bounded read-back verifier

    def test_worker_prompt_describes_only_the_effective_toolbox(self):
        self.run_worker([answer("Finished.")], tools=["read_file", "write_file", "run_command"],
                         config={"permissions": "execute"})
        prompt = self.payloads[0]["messages"][0]["content"]
        for name in ("read_file", "write_file", "run_command"):
            self.assertIn(name, prompt)
        for name in ("list_files", "search_code", "git_diff", "edit_file"):
            self.assertNotIn(name, prompt)
        self.assertNotIn("filesystem.list", prompt)
        self.assertNotIn("filesystem.search", prompt)
        self.assertNotIn("request_missing_capabilities", prompt)
        self.assertNotIn("request_new_capabilities", prompt)
        self.assertIn("Do not invent or", prompt)
        self.assertIn("read a prerequisite brief file", prompt)
        self.assertIn("Do not repeat", prompt)
        self.assertIn("same missing-file read", prompt)

    def test_unavailable_and_unknown_actions_do_not_request_capabilities_or_cycle(self):
        result = self.run_worker([
            answer(calls=[("list_files", {"path": "."})]),
            answer(calls=[("request_missing_capabilities", {"capability": "filesystem.list"})]),
            answer(calls=[("write_file", {"path": "hello.py", "content": "print('Hello World')\n"})]),
            answer("Created hello.py."),
        ], tools=["read_file", "write_file", "run_command"])
        self.assertEqual(result["status"], "Success", result["error"])
        steps = [event["event"] for event in self.events
                 if event.get("event", {}).get("event_type") == "step.finished"]
        self.assertEqual([item["error_class"] for item in steps[:2]], ["tool_unavailable", "unknown_tool"])
        self.assertEqual(sum(event.get("event", {}).get("event_type") == "capability.requested"
                             for event in self.events), 0)
        self.assertFalse(any(event.get("event", {}).get("event_type") == "task.blocked"
                             for event in self.events))
        self.assertEqual((self.workspace / "hello.py").read_text(), "print('Hello World')\n")

    def test_policy_denial_is_not_retried_even_when_read_retries_are_configured(self):
        policy = {
            "capabilities": {
                "filesystem": {"read": {"mode": "allow", "paths": ["allowed/**"]}},
                "execution": {}, "git": {},
            }
        }
        result = self.run_worker([
            answer(calls=[("read_file", {"path": "missing.txt"})]),
            answer("The file is unavailable."),
        ], tools=["read_file"], config={"retries": 2, "capability_policy": policy})
        self.assertEqual(result["status"], "Success")
        self.assertEqual(result["tool_calls"], 1)
        steps = [event["event"] for event in self.events
                 if event.get("event", {}).get("event_type") == "step.finished"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["error_class"], "policy_denied")

    def test_simple_file_is_verified_by_readback_without_git_or_tests(self):
        result = self.run_worker([
            answer(calls=[("write_file", {"path": "hola_mundo.txt", "content": "hola mundo"})]),
            answer(calls=[("read_file", {"path": "hola_mundo.txt"})]),
            answer("Created and verified hola_mundo.txt."),
        ], tools=["write_file", "read_file"], config={
            "output": {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
            "verification": {
                "enabled": True, "inspect_changes": False, "run_available_tests": False,
                "require_tool_evidence": True, "completion_criteria": ["The file contains 'hola mundo'"],
            },
        })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertTrue(result["verification"]["attempted"])
        self.assertTrue(result["verification"]["passed"])
        self.assertFalse(result["verification"]["unavailable"])
        readback = next(item for item in result["verification"]["evidence"]
                        if item.get("check") == "filesystem:read_file:hola_mundo.txt")
        self.assertEqual(readback["type"], "file_readback")
        self.assertEqual(readback["source"], "runtime_verification")
        self.assertEqual(readback["tool"], "read_file")
        self.assertEqual(readback["capability"], "filesystem.read")
        self.assertTrue(readback["event_id"])
        self.assertEqual(readback["output"], "hola mundo")

    def test_duplicate_write_after_verified_read_is_completed_without_overwrite(self):
        result = self.run_worker([
            answer(calls=[("write_file", {"path": "hello.py", "content": "print('Hello World')"})]),
            answer(calls=[("read_file", {"path": "hello.py"})]),
            answer(calls=[("write_file", {"path": "hello.py", "content": "print('Hello World')"})]),
        ], config={
            "output": {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
        })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual((self.workspace / "hello.py").read_text(), "print('Hello World')")
        self.assertTrue(result["verification"]["passed"])
        self.assertEqual(result["result"]["actions"][-1]["error_class"], "already_satisfied")
        self.assertTrue(any(
            event.get("event", {}).get("event_type") == "task.auto_completed"
            and "duplicate write" in event.get("event", {}).get("reason", "")
            for event in self.events
        ))

    def test_duplicate_write_after_mutation_stops_without_semantic_criteria_evidence(self):
        content = 'print("hola")\n'
        criterion = "The calculator supports addition, subtraction, multiplication and division."
        result = self.run_worker([
            answer(calls=[("write_file", {"path": "calculator.py", "content": content})]),
            answer(calls=[("write_file", {"path": "calculator.py", "content": content})]),
        ], tools=["write_file", "read_file"], prompt="Create calculator.py",
            task_characteristics={"requires_filesystem_write": True}, config={
                "output": {"format": "structured", "include": [
                    "summary", "actions", "artifacts", "verification", "limitations",
                ]},
                "verification": {
                    "enabled": True, "inspect_changes": False, "run_available_tests": False,
                    "require_tool_evidence": True, "completion_criteria": [criterion],
                },
            })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(len(self.payloads), 2)
        actions = result["result"]["actions"]
        self.assertEqual(len(actions), 2)
        self.assertTrue(actions[0]["success"])
        self.assertTrue(actions[0]["changed"])
        self.assertTrue(actions[1]["already_satisfied"])
        self.assertFalse(actions[1]["changed"])
        self.assertFalse(any(action.get("policy_decision") == "deny" for action in actions))
        self.assertTrue(any(event.get("event", {}).get("event_type") == "worker.write_already_satisfied"
                            for event in self.events))
        content_match = next(item for item in result["verification"]["evidence"]
                             if item.get("type") == "file_content_match")
        self.assertEqual(content_match["path"], "calculator.py")
        self.assertEqual(content_match["source"], "runtime_write_result")
        self.assertEqual(content_match["tool"], "write_file")
        self.assertEqual(content_match["capability"], "filesystem.create")
        self.assertTrue(content_match["event_id"])
        self.assertTrue(content_match["match"])
        self.assertEqual(content_match["status"], "passed")
        self.assertEqual(
            result["result"]["summary"],
            "Execution completed; requested workspace state is already satisfied.",
        )
        self.assertEqual(result["workspace_changes"], 1)
        self.assertEqual([item["path"] for item in result["result"]["artifacts"]], ["calculator.py"])
        self.assertTrue(result["result"]["workspace_diffs"])
        self.assertFalse(any(
            criterion in item.get("supports_acceptance_criteria", [])
            for item in result["verification"]["evidence"]
        ))
        self.assertTrue(any(
            event.get("event", {}).get("event_type") == "task.auto_completed"
            and "duplicate write" in event.get("event", {}).get("reason", "")
            for event in self.events
        ))

        evaluator_input = {}

        def reject_incomplete_calculator(_prompt, context):
            evaluator_input.update(context)
            return {"criteria": [{
                "criterion": criterion, "status": "partial",
                "reason": "The source only prints a greeting and does not implement the four operations.",
                "evidence": ["workspace diff for calculator.py"], "confidence": 1.0,
            }]}

        planned_task = {
            "id": "T-1", "objective": "Create a calculator",
            "description": "Implement the four arithmetic operations.",
            "success_criteria": [criterion], "required_capabilities": [], "preferred_skills": [],
        }
        evaluation = self.evaluate_result(Evaluator(model=reject_incomplete_calculator),
            planned_task=planned_task,
            runtime_task=result,
            execution_node={"selected_agent_id": "calculator-worker",
                            "runtime_task_id": "calculator-runtime", "attempt": 1},
        )
        self.assertEqual(evaluation["status"], "needs_revision")
        final_files = evaluator_input["final_state"]["files"]
        self.assertTrue(any(item["path"] == "calculator.py" and item.get("content") for item in final_files))
        self.assertNotIn("workspace_diff", json.dumps(evaluator_input))
        self.assertEqual(len(result["result"]["actions"]), 2)
        self.assertEqual(len(result["result"]["artifacts"]), 1)

    def test_created_javascript_duplicate_write_evaluates_and_unlocks_dependent_task(self):
        source = "export const add = (a, b) => a + b;\n"
        criterion = "calculator.js exists."
        result = self.run_worker([
            answer(calls=[("write_file", {"path": "calculator.js", "content": source})]),
            answer(calls=[("write_file", {"path": "calculator.js", "content": source})]),
        ], prompt="Create calculator.js", config={
            "output": {"format": "structured", "include": [
                "summary", "actions", "artifacts", "verification", "limitations",
            ]},
            "verification": {"enabled": True, "inspect_changes": False,
                             "run_available_tests": False, "require_tool_evidence": True,
                             "completion_criteria": [criterion]},
        })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["workspace_changes"], 1)
        actions = result["result"]["actions"]
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]["capability"], "filesystem.create")
        self.assertTrue(actions[0]["changed"])
        self.assertTrue(actions[1]["already_satisfied"])
        self.assertFalse(actions[1]["changed"])
        self.assertFalse(any(item["policy_decision"] == "deny" for item in actions))
        plan_task = {
            "id": "task-3", "objective": "Create JavaScript logic",
            "description": "Create the JavaScript file", "depends_on": [],
            "required_capabilities": ["filesystem.create"], "preferred_skills": [],
            "success_criteria": [criterion], "owned_paths": ["calculator.js"],
            "write_targets": ["calculator.js"], "semantic_operations": ["create_file"],
        }
        outcome = self.evaluate_result(Evaluator(lambda *_: self.fail("LLM must not be called")),
            planned_task=plan_task,
            runtime_task={"status": result["status"], "result": result["result"],
                          "verification": result["verification"], "error": result["error"]},
            execution_node={"selected_agent_id": "agent", "runtime_task_id": "runtime",
                            "attempt": 1},
        )
        self.assertEqual(outcome["status"], "accepted")
        self.assertEqual(outcome["metrics"]["model_calls"], 0)
        self.assertEqual(outcome["metrics"]["decision_source"], "deterministic_success")
        next_task = {**plan_task, "id": "task-4", "depends_on": ["task-3"],
                     "objective": "Continue implementation", "write_targets": [],
                     "owned_paths": [], "required_capabilities": []}
        graph = ExecutionGraph({"goal": "Continue implementation", "summary": "Two tasks",
                                "complexity": "multi_step", "tasks": [plan_task, next_task],
                                "success_criteria": ["Tasks complete"]})
        graph.mark_selected("task-3", "agent", "selection")
        graph.mark_running("task-3", "runtime", "delegation")
        graph.apply_runtime_status("task-3", "Success", result=result["result"])
        graph.apply_evaluation("task-3", "evaluation", outcome["status"], outcome["summary"])
        self.assertEqual(graph.refresh_dependencies(), [
            {"task_id": "task-4", "from": "pending", "to": "ready"},
        ])

    def test_initial_already_satisfied_write_finishes_without_artifact_and_evaluator_reviews(self):
        content = 'print("hola")\n'
        criterion = "The calculator supports addition, subtraction, multiplication and division."
        (self.workspace / "calculator.py").write_bytes(content.encode("utf-8"))
        result = self.run_worker([
            answer(calls=[("read_file", {"path": "calculator.py"})]),
            answer(calls=[("write_file", {"path": "calculator.py", "content": content})]),
        ], tools=["write_file", "read_file"], prompt="Create calculator.py",
            task_characteristics={"requires_filesystem_write": True}, config={
                "output": {"format": "structured", "include": [
                    "summary", "actions", "artifacts", "verification", "limitations",
                ]},
                "verification": {
                    "enabled": True, "inspect_changes": False, "run_available_tests": False,
                    "require_tool_evidence": True, "completion_criteria": [criterion],
                },
            })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["model_calls"], 2)
        action = result["result"]["actions"][1]
        self.assertTrue(action["success"])
        self.assertTrue(action["already_satisfied"])
        self.assertFalse(action["changed"])
        self.assertEqual(result["result"]["artifacts"], [])
        self.assertNotIn("workspace_diffs", result["result"])
        self.assertTrue(result["verification"]["attempted"])
        self.assertFalse(result["verification"]["passed"])

        evaluator = Evaluator(offline=True)
        evaluation = self.evaluate_result(evaluator,
            planned_task={
                "id": "T-1", "objective": "Create a calculator", "description": "Create calculator.py",
                "success_criteria": [criterion], "required_capabilities": [], "preferred_skills": [],
            },
            runtime_task=result,
            execution_node={"selected_agent_id": "calculator-worker",
                            "runtime_task_id": "calculator-runtime", "attempt": 1},
        )
        self.assertEqual(evaluation["status"], "blocked")
        self.assertEqual(evaluation["criteria"][0]["status"], "unknown")
        self.assertNotIn("runtime_task", evaluator.last_context)
        self.assertTrue(evaluator.last_context["final_state"]["files"][0]["exists"])

    def test_repeated_identical_already_satisfied_write_stops_on_second_request(self):
        content = "print('hola')\n"
        write = ("write_file", {"path": "calculator.py", "content": content})
        (self.workspace / "calculator.py").write_bytes(content.encode("utf-8"))
        result = self.run_worker([
            answer(calls=[("read_file", {"path": "calculator.py"}), write,
                          ("read_file", {"path": "calculator.py"})]),
            answer(calls=[write]),
            answer("The worker should not request this third model call."),
        ], tools=["write_file", "read_file"], prompt="Create calculator.py",
            task_characteristics={"requires_filesystem_write": True}, config={
                "output": {"format": "structured", "include": [
                    "summary", "actions", "artifacts", "verification", "limitations",
                ]},
            })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(len(self.payloads), 2)
        actions = result["result"]["actions"]
        self.assertTrue(actions[1]["already_satisfied"])
        self.assertEqual(actions[2]["tool"], "read_file")
        self.assertTrue(actions[3]["already_satisfied"])
        self.assertFalse(actions[3]["changed"])
        self.assertEqual(result["result"]["artifacts"], [])
        self.assertTrue(any(
            event.get("event", {}).get("event_type") == "task.auto_completed"
            and "same already-satisfied write repeated" in event.get("event", {}).get("reason", "")
            for event in self.events
        ))

    def test_failed_first_write_does_not_complete_from_already_satisfied_path(self):
        config = {
            **DEFAULT_CONFIG,
            "output": {"format": "structured", "include": [
                "summary", "actions", "artifacts", "verification", "limitations",
            ]},
            "verification": {
                "enabled": False, "inspect_changes": False, "run_available_tests": False,
                "require_tool_evidence": False, "completion_criteria": [],
            },
        }
        toolbox = PolicyToolbox(self.root, self.workspace, config, ["write_file"])
        failed_write = ToolResult(
            "write_file", "The write was not executed.", False, 0,
            capability="filesystem.create", policy_decision="invalid_request",
            executed=False, error_class="invalid_request",
        )
        final = json.dumps({
            "summary": "The requested write failed.", "actions": [], "artifacts": [],
            "verification": {}, "limitations": ["The write was not executed."],
        })
        with patch.object(toolbox, "invoke", return_value=failed_write):
            result = self.run_worker([
                answer(calls=[("write_file", {"path": "calculator.py", "content": "print(1 + 1)\n"})]),
                answer(final),
            ], tools=["write_file"], prompt="Create calculator.py",
                task_characteristics={"requires_filesystem_write": True}, config=config,
                toolbox=toolbox)
        self.assertEqual(result["status"], "Failed")
        self.assertFalse(result["result"]["actions"][0]["success"])
        self.assertFalse(result["result"]["actions"][0]["already_satisfied"])
        self.assertEqual(result["workspace_changes"], 0)
        self.assertFalse(any(
            event.get("event", {}).get("event_type") == "task.auto_completed"
            for event in self.events
        ))

    def test_identical_policy_denial_is_intercepted_before_second_tool_execution(self):
        existing = self.workspace / "existing.py"
        existing.write_text("print('old')", encoding="utf-8")
        policy = {
            "capabilities": {
                "filesystem": {
                    "create": {"mode": "allow"}, "read": {"mode": "allow"},
                    "modify": {"mode": "deny"}, "overwrite": {"mode": "deny"},
                    "list": {"mode": "deny"}, "search": {"mode": "deny"},
                },
                "execution": {"python_script": {"mode": "deny"}, "pytest": {"mode": "deny"},
                               "unittest": {"mode": "deny"}, "py_compile": {"mode": "deny"},
                               "ruff": {"mode": "deny"}},
                "git": {"status": {"mode": "deny"}, "diff": {"mode": "deny"}},
            }
        }
        config = {
            **DEFAULT_CONFIG,
            "capability_policy": policy,
            "output": {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
        }
        box = PolicyToolbox(self.root, self.workspace, config, ["write_file"])
        with patch.object(box, "invoke", wraps=box.invoke) as invoke:
            result = self.run_worker([
                answer(calls=[("write_file", {"path": "existing.py", "content": "print('new')"})]),
                answer(calls=[("write_file", {"path": "existing.py", "content": "print('new')"})]),
                answer(calls=[("write_file", {"path": "existing.py", "content": "print('new')"})]),
            ], tools=["write_file"], config=config, toolbox=box)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result["status"], "Failed")
        self.assertIn("BlockedActionCycle", result["error"])
        writes = [event["event"] for event in self.events
                  if event.get("event", {}).get("event_type") == "step.finished"]
        self.assertEqual(len(writes), 3)
        self.assertEqual(writes[0]["error_class"], "policy_denied")
        self.assertEqual(writes[1]["error_class"], "repeated_policy_denied")
        self.assertEqual(writes[2]["error_class"], "blocked_action_cycle")
        self.assertIn("ACTION_BLOCKED_PERMANENTLY_FOR_CURRENT_STATE", writes[1]["output"])
        self.assertEqual(result["tool_calls"], 1)
        self.assertTrue(result["result"]["actions"])
        self.assertEqual(result["result"]["actions"][0]["error_class"], "policy_denied")
        self.assertIn("blocked_action_cycle", result["result"]["limitations"][0])
        self.assertTrue(any(event.get("event", {}).get("event_type") == "task.result_contract"
                            and event["event"]["output"].get("deterministic_runtime_result")
                            for event in self.events))
        self.assertFalse(any(event.get("event", {}).get("event_type") == "model.repair.started"
                             for event in self.events))

    def test_runtime_actions_and_artifacts_survive_malformed_structured_output(self):
        result = self.run_worker([
            answer(calls=[("write_file", {"path": "observed.py", "content": "print(1)"})]),
            answer("The model returned prose instead of the structured contract."),
            answer(json.dumps({
                "summary": "Created observed.py.",
                "actions": [], "artifacts": [],
                "verification": "The file was created.",
                "limitations": [],
            })),
        ], tools=["write_file"], config={
            "output": {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
            "verification": {"enabled": False, "inspect_changes": False, "run_available_tests": False,
                            "require_tool_evidence": False, "completion_criteria": []},
        })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["result"]["actions"][0]["tool"], "write_file")
        self.assertEqual(result["result"]["artifacts"][0]["path"], "observed.py")
        self.assertFalse(any("structured output" in item.casefold()
                             for item in result["result"]["limitations"]))
        contract_event = next(
            item["event"] for item in self.events
            if item.get("event", {}).get("event_type") == "task.result_contract"
        )
        self.assertFalse(contract_event["output"]["model_response_valid"])
        self.assertTrue(contract_event["output"]["repair_attempted"])
        self.assertTrue(contract_event["output"]["repair_succeeded"])
        self.assertFalse(contract_event["output"]["fallback_normalization_used"])
        self.assertIn("prose", contract_event["output"]["original_response_preview"])
        repair_event = next(
            item["event"] for item in self.events
            if item.get("event", {}).get("event_type") == "model.repair.finished"
        )
        self.assertIn("transport", repair_event["output"])

    def test_failed_structured_repair_is_logged_without_becoming_task_failure(self):
        result = self.run_worker([
            answer(calls=[("write_file", {"path": "fallback.py", "content": "print(1)"})]),
            answer("The model returned prose instead of the structured contract."),
            answer("The repair response was also prose."),
        ], tools=["write_file"], config={
            "output": {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
            "verification": {"enabled": False, "inspect_changes": False, "run_available_tests": False,
                            "require_tool_evidence": False, "completion_criteria": []},
        })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["result"]["summary"],
                         "The model returned prose instead of the structured contract.")
        self.assertFalse(any("structured output" in item.casefold()
                             for item in result["result"]["limitations"]))
        contract_event = next(
            item["event"] for item in self.events
            if item.get("event", {}).get("event_type") == "task.result_contract"
        )
        self.assertTrue(contract_event["output"]["fallback_normalization_used"])
        self.assertTrue(contract_event["output"]["normalized_result_valid"])
        self.assertFalse(contract_event["output"]["repair_succeeded"])

    def test_textual_registered_tool_call_executes_once_before_final_result(self):
        contract = {
            "summary": "Created output.txt.",
            "actions": [{"tool": "fabricated_tool"}],
            "artifacts": [{"path": "fabricated.txt"}],
            "verification": {}, "limitations": [],
        }
        tool_call = json.dumps({
            "name": "write_file",
            "arguments": {"path": "output.txt", "content": "real write"},
        })
        result = self.run_worker([
            answer(tool_call),
            answer(json.dumps(contract)),
        ], tools=["write_file"], task_characteristics={"requires_filesystem_write": True},
            config={
                "output": {"format": "structured",
                           "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
                "verification": {"enabled": False, "inspect_changes": False,
                                 "run_available_tests": False, "require_tool_evidence": False,
                                 "completion_criteria": []},
            })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual((self.workspace / "output.txt").read_text(), "real write")
        self.assertEqual([item["tool"] for item in result["result"]["actions"]], ["write_file"])
        self.assertEqual([item["path"] for item in result["result"]["artifacts"]], ["output.txt"])
        self.assertEqual(result["workspace_changes"], 1)
        finished = [event["event"] for event in self.events
                    if event.get("event", {}).get("event_type") == "step.finished"]
        self.assertEqual([event["tool"] for event in finished], ["write_file"])
        event_types = [item.get("event", {}).get("event_type") for item in self.events]
        self.assertLess(event_types.index("step.finished"),
                        event_types.index("task.result_contract"))

    def test_textual_unregistered_tool_is_not_executed_or_reported_as_success(self):
        tool_call = json.dumps({
            "name": "write_file",
            "arguments": {"path": "blocked.txt", "content": "bad"},
        })
        result = self.run_worker([answer(tool_call)], tools=["read_file"],
                                 task_characteristics={"requires_filesystem_write": True},
                                 config={"verification": {"enabled": False, "inspect_changes": False,
                                                          "run_available_tests": False,
                                                          "require_tool_evidence": False,
                                                          "completion_criteria": []}})
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_class"], "ExpectedWorkspaceMutationNotObserved")
        self.assertEqual(result["tool_calls"], 0)
        self.assertEqual(result["workspace_changes"], 0)
        self.assertFalse((self.workspace / "blocked.txt").exists())
        self.assertFalse(any(item.get("event", {}).get("event_type") == "step.finished"
                             for item in self.events))

    def test_textual_tool_call_requires_object_arguments(self):
        malformed_call = json.dumps({
            "name": "write_file",
            "arguments": json.dumps({"path": "bad.txt", "content": "bad"}),
        })
        result = self.run_worker(
            [answer(malformed_call)],
            tools=["write_file"],
            task_characteristics={"requires_filesystem_write": True},
            config={"verification": {"enabled": False, "inspect_changes": False,
                                     "run_available_tests": False, "require_tool_evidence": False,
                                     "completion_criteria": []}},
        )
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_class"], "ExpectedWorkspaceMutationNotObserved")
        self.assertEqual(result["tool_calls"], 0)
        self.assertFalse((self.workspace / "bad.txt").exists())

    def test_final_success_claim_without_runtime_write_fails_and_discards_claimed_ledger(self):
        claimed = {
            "summary": "Created claimed.txt.",
            "actions": [{
                "name": "write_file",
                "arguments": {"path": "claimed.txt", "content": "invented"},
                "success": True,
            }],
            "artifacts": [{"path": "claimed.txt"}],
            "verification": {"passed": True}, "limitations": [],
        }
        result = self.run_worker([answer(json.dumps(claimed))],
                                 task_characteristics={"requires_filesystem_write": True},
                                 config={
                                     "output": {"format": "structured",
                                                "include": ["summary", "actions", "artifacts",
                                                            "verification", "limitations"]},
                                     "verification": {"enabled": False, "inspect_changes": False,
                                                      "run_available_tests": False,
                                                      "require_tool_evidence": False,
                                                      "completion_criteria": []},
                                 })
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_class"], "ExpectedWorkspaceMutationNotObserved")
        self.assertEqual(result["task_execution_successful"], False)
        self.assertEqual(result["workspace_changes"], 0)
        self.assertEqual(result["result"]["actions"], [])
        self.assertEqual(result["result"]["artifacts"], [])
        self.assertIn("ExpectedWorkspaceMutationNotObserved", result["result"]["limitations"][0])
        contract_event = next(item["event"] for item in self.events
                              if item.get("event", {}).get("event_type") == "task.result_contract")
        self.assertTrue(contract_event["output"]["result_contract_valid"])
        self.assertFalse(contract_event["output"]["task_execution_successful"])

    def test_existing_source_read_allows_no_write_candidate_for_evaluator(self):
        source = "def add(a, b):\n    return a + b\n"
        (self.workspace / "calculator.py").write_text(source, encoding="utf-8")
        result = self.run_worker([
            answer(calls=[("read_file", {"path": "calculator.py"})]),
            answer('{"summary":"add is already implemented","actions":[],"artifacts":[],"verification":{},"limitations":[]}'),
        ], tools=["read_file", "edit_file"], prompt="Implement add()", config={
            "permissions": "workspace",
            "provenance": {"generated_by_freya": True, "plan_task_id": "writer"},
            "task_owned_paths": ["calculator.py"],
            "task_write_owners": {"calculator.py": "writer"},
            "task_write_scope_enforced": True,
            "output": {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
            "verification": {"enabled": False},
        }, task_characteristics={"requires_filesystem_write": True})
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertTrue(result["already_satisfied_candidate"])
        self.assertEqual(result["result"]["already_satisfied_candidate"]["artifact_observations"][0]["path"],
                         "calculator.py")
        self.assertEqual(result["result"]["artifacts"], [])
        self.assertTrue(any(item.get("event", {}).get("event_type") == "task.already_satisfied_candidate"
                            for item in self.events))
        self.assertTrue(any(item.get("event", {}).get("event_type") == "artifact.read_observed"
                            for item in self.events))
        criterion = "add(a, b) returns the sum."
        planned = {"id": "writer", "objective": "Implement add()",
                   "success_criteria": [criterion], "required_capabilities": ["filesystem.modify"]}
        def decision(_prompt, context):
            evidence = context["final_state"]["files"]
            self.assertTrue(any(source.strip() in item.get("content", "").replace("\r\n", "\n") for item in evidence))
            return {"criteria": [{"criterion": criterion, "status": "satisfied",
                                  "reason": "The read source contains the implementation.",
                                  "evidence": ["read_file calculator.py"], "confidence": 1.0}]}
        evaluation = self.evaluate_result(Evaluator(model=decision),
            planned_task=planned, runtime_task=result,
            execution_node={"selected_agent_id": "agent", "runtime_task_id": "runtime", "attempt": 1})
        self.assertEqual(evaluation["status"], "accepted")
        without_semantic_review = self.evaluate_result(Evaluator(offline=True),
            planned_task=planned, runtime_task=result,
            execution_node={"selected_agent_id": "agent", "runtime_task_id": "runtime", "attempt": 1})
        self.assertEqual(without_semantic_review["status"], "blocked")

    def test_read_before_edit_is_recoverable_and_emits_event(self):
        (self.workspace / "calculator.py").write_bytes(b"before\n")
        config = {
            "permissions": "workspace",
            "capability_policy": {"capabilities": {"filesystem": {
                "read": {"mode": "allow"}, "modify": {"mode": "allow"}}}},
            "provenance": {"generated_by_freya": True, "plan_task_id": "writer"},
            "task_owned_paths": ["calculator.py"],
            "task_write_owners": {"calculator.py": "writer"},
            "task_write_scope_enforced": True,
            "verification": {"enabled": False},
        }
        result = self.run_worker([
            answer(calls=[("edit_file", {"path": "calculator.py", "old": "before", "new": "after"}),
                          ("read_file", {"path": "calculator.py"}),
                          ("edit_file", {"path": "calculator.py", "old": "before", "new": "after"})]),
            answer("Updated calculator.py."),
        ], tools=["edit_file", "read_file"], config=config,
            task_characteristics={"requires_filesystem_write": True})
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual((self.workspace / "calculator.py").read_text(encoding="utf-8"), "after\n")
        events = [item.get("event", {}).get("event_type") for item in self.events]
        self.assertIn("artifact.read_before_write_required", events)
        self.assertIn("artifact.read_observed", events)

    def test_generated_file_task_can_report_symbols_after_readback(self):
        source = "def add(a, b):\n    return a + b\n"
        reported = {"summary": "Created add().", "actions": [], "artifacts": [],
                    "verification": {}, "limitations": [],
                    "project_context_update": {"artifacts": [],
                        "symbols": [{"name": "add", "kind": "function", "path": "calc.py",
                                     "signature": "add(a, b)", "purpose": "Return the sum."}],
                        "dependencies": []}}
        result = self.run_worker([
            answer(calls=[("write_file", {"path": "calc.py", "content": source})]),
            answer(calls=[("read_file", {"path": "calc.py"})]),
            answer(json.dumps(reported)),
        ], tools=["write_file", "read_file"], config={
            "permissions": "workspace",
            "capability_policy": {"capabilities": {"filesystem": {
                "create": {"mode": "allow"}, "read": {"mode": "allow"}},
                "project": {"read_context": {"mode": "allow"}}}},
            "provenance": {"generated_by_freya": True, "plan_task_id": "writer"},
            "task_owned_paths": ["calc.py"], "task_write_owners": {"calc.py": "writer"},
            "task_write_scope_enforced": True,
            "runtime_context": {"project_state_snapshot": {"revision": 0, "artifacts": [], "tasks": []}},
            "output": {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
            "verification": {"enabled": True, "inspect_changes": False,
                             "run_available_tests": False, "require_tool_evidence": True,
                             "completion_criteria": ["calc.py exists and can be read."]},
        }, task_characteristics={"requires_filesystem_write": True})
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(result["result"]["project_context_update"]["symbols"][0]["name"], "add")

    def test_successful_format_repair_cannot_fabricate_a_missing_write(self):
        repaired_claim = {
            "summary": "Created repaired.txt.",
            "actions": [{"tool": "write_file", "success": True}],
            "artifacts": [{"path": "repaired.txt"}],
            "verification": {}, "limitations": [],
        }
        result = self.run_worker([
            answer("The file was created successfully."),
            answer(json.dumps(repaired_claim)),
        ], task_characteristics={"requires_filesystem_write": True},
            config={
                "max_model_calls": 2,
                "output": {"format": "structured",
                           "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
                "verification": {"enabled": False, "inspect_changes": False,
                                 "run_available_tests": False, "require_tool_evidence": False,
                                 "completion_criteria": []},
            })
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_class"], "ExpectedWorkspaceMutationNotObserved")
        self.assertEqual(result["result"]["actions"], [])
        self.assertEqual(result["result"]["artifacts"], [])
        contract_event = next(item["event"] for item in self.events
                              if item.get("event", {}).get("event_type") == "task.result_contract")
        self.assertTrue(contract_event["output"]["repair_succeeded"])
        self.assertFalse(contract_event["output"]["fallback_normalization_used"])
        self.assertFalse(contract_event["output"]["task_execution_successful"])

    def test_format_normalization_does_not_turn_missing_write_into_success(self):
        result = self.run_worker([
            answer("The file was created successfully."),
            answer("This repair is still not valid JSON."),
        ], task_characteristics={"requires_filesystem_write": True},
            config={
                "max_model_calls": 2,
                "output": {"format": "structured",
                           "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
                "verification": {"enabled": False, "inspect_changes": False,
                                 "run_available_tests": False, "require_tool_evidence": False,
                                 "completion_criteria": []},
            })
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_class"], "ExpectedWorkspaceMutationNotObserved")
        self.assertEqual(result["result"]["actions"], [])
        self.assertEqual(result["result"]["artifacts"], [])
        contract_event = next(item["event"] for item in self.events
                              if item.get("event", {}).get("event_type") == "task.result_contract")
        self.assertFalse(contract_event["output"]["model_response_valid"])
        self.assertTrue(contract_event["output"]["repair_attempted"])
        self.assertFalse(contract_event["output"]["repair_succeeded"])
        self.assertTrue(contract_event["output"]["fallback_normalization_used"])
        self.assertTrue(contract_event["output"]["result_contract_valid"])
        self.assertTrue(contract_event["output"]["normalized_result_valid"])
        self.assertFalse(contract_event["output"]["task_execution_successful"])

    def test_read_only_task_succeeds_without_workspace_changes(self):
        result = self.run_worker([answer("Read-only analysis completed.")], tools=["read_file"],
                                 task_characteristics={"requires_filesystem_write": False},
                                 config={"verification": {"enabled": False, "inspect_changes": False,
                                                          "run_available_tests": False,
                                                          "require_tool_evidence": False,
                                                          "completion_criteria": []}})
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertTrue(result["task_execution_successful"])
        self.assertEqual(result["execution_outcome"], "execution_complete")
        self.assertEqual(result["workspace_changes"], 0)
        completion = next(item["event"] for item in self.events
                          if item.get("event", {}).get("event_type") == "worker.execution.completed")
        self.assertEqual(completion["status"], "ExecutionComplete")
        self.assertNotIn("accepted", completion)

    def test_web_calculator_worker_to_evaluator_uses_real_write_and_readback(self):
        source_prompt = (
            "Create a web calculator supporting addition, subtraction, multiplication, and division."
        )
        criterion = "calculator/index.html exists and can be read."
        spec = validate_task_spec({
            "status": "READY_FOR_PLANNING",
            "source_prompt": source_prompt,
            "objective": "Create a web calculator with four arithmetic operations.",
            "user_intent": source_prompt,
            "deliverables": [{"description": "A usable web calculator.", "source": "explicit"}],
            "requirements": [
                {"description": "Support addition.", "source": "explicit"},
                {"description": "Support subtraction.", "source": "explicit"},
                {"description": "Support multiplication.", "source": "explicit"},
                {"description": "Support division.", "source": "explicit"},
            ],
            "constraints": [],
            "user_decisions": {},
            "assumptions": [{
                "description": "Use plain HTML, CSS, and JavaScript.",
                "reason": "The browser platform is sufficient for the requested calculator.",
            }],
            "validation_expectations": [criterion],
            "context": {},
            "clarification_questions": [],
            "readiness_reason": "The requested interface and operations are specified.",
        })
        planned_task = {
            "id": "calculator",
            "objective": spec["objective"],
            "description": "Implement the requested web calculator.",
            "success_criteria": [criterion],
            "required_capabilities": ["filesystem.create"],
        }
        html = """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Calculator</title>
<label>First number <input id="left" type="number"></label>
<select id="operation">
<option value="add">Addition</option><option value="subtract">Subtraction</option>
<option value="multiply">Multiplication</option><option value="divide">Division</option>
</select>
<label>Second number <input id="right" type="number"></label>
<button id="calculate">Calculate</button><output id="result"></output>
<script>
document.querySelector("#calculate").addEventListener("click", () => {
  const left = Number(document.querySelector("#left").value);
  const right = Number(document.querySelector("#right").value);
  const operation = document.querySelector("#operation").value;
  const result = operation === "add" ? left + right
    : operation === "subtract" ? left - right
    : operation === "multiply" ? left * right
    : right === 0 ? "Cannot divide by zero" : left / right;
  document.querySelector("#result").textContent = String(result);
});
</script></html>"""
        write_call = json.dumps({
            "name": "write_file",
            "arguments": {"path": "calculator/index.html", "content": html},
        })
        read_call = json.dumps({
            "name": "read_file",
            "arguments": {"path": "calculator/index.html"},
        })
        result = self.run_worker([
            answer(write_call),
            answer(read_call),
        ], tools=["write_file", "read_file"],
            task_characteristics={"requires_filesystem_write": True},
            prompt=render_task_spec(spec),
            config={
                "output": {"format": "structured",
                           "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
                "verification": {
                    "enabled": True, "inspect_changes": False, "run_available_tests": False,
                    "require_tool_evidence": True, "completion_criteria": [criterion],
                },
            })
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertTrue(result["verification"]["passed"])
        self.assertEqual(result["workspace_changes"], 1)
        written = self.workspace / "calculator" / "index.html"
        written_contents = written.read_text()
        self.assertEqual(written_contents, html)
        for expression in ("left + right", "left - right", "left * right", "left / right"):
            self.assertIn(expression, written_contents)
        self.assertEqual([item["path"] for item in result["result"]["artifacts"]],
                         ["calculator/index.html"])
        self.assertEqual(result["result"]["verification"]["evidence"][0]["check"],
                         "filesystem:read_file:calculator/index.html")

        evaluation = self.evaluate_result(Evaluator(offline=True),
            planned_task=planned_task,
            runtime_task=result,
            execution_node={"selected_agent_id": "calculator-worker",
                            "runtime_task_id": "calculator-runtime", "attempt": 1},
        )
        self.assertEqual(evaluation["status"], "accepted")
        self.assertEqual(evaluation["criteria"][0]["status"], "satisfied")
        self.assertTrue(any(item.startswith("final_file:calculator/index.html")
                            for item in evaluation["criteria"][0]["evidence"]))

    def test_prose_wrapped_json_action_is_executed(self):
        wrapped = ('I will inspect the workspace and then create the requested file.\n'
                   '{"name":"write_file","arguments":{"path":"wrapped.py","content":"print(2 + 2)"}}')
        result = self.run_worker([answer(wrapped), answer('{"action":"finish","message":"Created wrapped.py"}')])
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual((self.workspace / "wrapped.py").read_text(), "print(2 + 2)")
        finished = [event["event"] for event in self.events
                    if event.get("event", {}).get("event_type") == "step.finished"]
        self.assertEqual(finished[0]["tool"], "write_file")

    def test_concatenated_json_actions_execute_only_after_real_observations(self):
        batched = ('{"name":"write_file","arguments":{"path":"verified.txt","content":"OK"}}\n'
                   '{"name":"read_file","arguments":{"path":"verified.txt"}}\n'
                   '{"name":"finish","message":"invented before tools"}')
        read_then_finish = ('{"name":"read_file","arguments":{"path":"verified.txt"}}\n'
                            '{"name":"finish","message":"invented before read"}')
        result = self.run_worker([answer(batched), answer(read_then_finish),
                                  answer('{"name":"finish","message":"Verified from tool output"}')])
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(result["result"], "Verified from tool output")
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual((self.workspace / "verified.txt").read_text(), "OK")
        finished = [event["event"] for event in self.events
                    if event.get("event", {}).get("event_type") == "step.finished"]
        self.assertEqual([event["tool"] for event in finished], ["write_file", "read_file"])

    def test_read_retry_is_bounded_and_writes_are_never_retried(self):
        config = {**DEFAULT_CONFIG, "retries": 2}
        box = PolicyToolbox(self.root, self.workspace, config, ["read_file", "write_file"])
        failure = ToolResult("read_file", "transient read failure", False, .01)
        success = ToolResult("read_file", "data", True, .01)
        with patch.object(box, "invoke", side_effect=[failure, success, ToolResult("write_file", "write failed", False, .01)]) as call:
            result = self.run_worker([answer(calls=[("read_file", {"path": "x"}), ("write_file", {"path": "x", "content": "y"})]), answer()], toolbox=box)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(result["tool_calls"], 3)
        self.assertEqual([e["event"]["attempt"] for e in self.events if e.get("event", {}).get("event_type") == "step.finished"], [1, 2, 1])

    def test_cumulative_token_budget_blocks_tools_after_large_provider_response(self):
        result = self.run_worker([answer(calls=[("write_file", {"path": "x", "content": "bad"})], prompt_eval_count=129)],
                                 config={"max_tokens": 128})
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["tool_calls"], 0)
        self.assertFalse((self.workspace / "x").exists())
        self.assertEqual(self.payloads[0]["options"]["num_predict"], 128)

    def test_unlimited_cumulative_budget_keeps_per_call_output_bounded(self):
        result = self.run_worker([answer("Finished.")], config={"max_tokens": 0})
        self.assertEqual(result["status"], "Success")
        self.assertEqual(self.payloads[0]["options"]["num_predict"], 2048)

    def test_ten_successful_validations_auto_complete_after_a_write(self):
        responses = [answer(calls=[("write_file", {"path": "verified.txt", "content": "OK"})])]
        responses.extend(answer(calls=[("read_file", {"path": "verified.txt"})]) for _ in range(10))
        result = self.run_worker(responses)
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(len(self.payloads), 11)
        self.assertTrue(any(event.get("event", {}).get("event_type") == "task.auto_completed"
                            for event in self.events))
        self.assertTrue(result["verification"]["passed"])

    def test_repeated_successful_reads_stop_as_no_progress_before_step_limit(self):
        responses = [answer(calls=[("list_files", {"path": "."})]) for _ in range(3)] + [terminal_answer()]
        result = self.run_worker(responses, tools=["list_files"], config={"max_steps": 20})
        self.assertEqual(result["status"], "Success")
        self.assertEqual(result["failure_class"], "")
        self.assertTrue(result["no_progress_detected"])
        self.assertLess(result["steps"], 20)
        no_progress = [event["event"] for event in self.events
                       if event.get("event", {}).get("event_type") == "task.no_progress"]
        self.assertEqual(len(no_progress), 1)
        self.assertIn("read-only", no_progress[0]["reason"])

    def test_three_denied_invented_tools_stop_as_blocked_cycle(self):
        responses = [
            answer(calls=[("request_new_capabilities", {"capability": "shell"})])
            for _ in range(3)
        ]
        result = self.run_worker(responses, tools=["read_file"], config={"max_steps": 20})
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["failure_class"], "blocked_action_cycle")
        self.assertEqual(result["blocked_actions"], 3)
        self.assertLess(result["steps"], 20)
        blocked = [event["event"] for event in self.events
                   if event.get("event", {}).get("event_type") == "task.blocked"]
        self.assertEqual(len(blocked), 1)

    def test_zero_tool_budget_and_step_budget(self):
        call = answer(calls=[("write_file", {"path": "x", "content": "one"}), ("write_file", {"path": "y", "content": "two"})])
        result = self.run_worker([call], config={"max_tool_calls": 0})
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["tool_calls"], 0)
        result = self.run_worker([call], config={"max_steps": 1})
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["tool_calls"], 1)
        self.assertFalse((self.workspace / "y").exists())

    def test_model_call_limit_stops_repeated_invalid_actions(self):
        result = self.run_worker([answer('{"action":"disabled"}')], config={"max_model_calls": 1}, tools=[])
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["model_calls"], 1)

    def test_secrets_and_analysis_are_removed_from_persistent_output(self):
        result = self.run_worker([answer('<analysis>PRIVATE</analysis>key is secret-value; password=hunter2')], token="secret-value")
        serialized = json.dumps(self.events) + json.dumps(result)
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("PRIVATE", serialized)

    def test_allowed_directories_and_permissions_apply_to_every_tool(self):
        (self.workspace / "allowed").mkdir()
        (self.workspace / "outside.txt").write_text("private")
        config = {**DEFAULT_CONFIG, "allowed_directories": ["allowed"], "permissions": "read_only"}
        box = PolicyToolbox(self.root, self.workspace, config, ["list_files", "read_file", "write_file", "run_command", "git_diff"])
        for name, args in (("read_file", {"path": "outside.txt"}), ("list_files", {"path": "."}),
                           ("write_file", {"path": "allowed/x", "content": "bad"}),
                           ("run_command", {"argv": ["python", "x.py"]}), ("git_diff", {})):
            self.assertFalse(box.invoke(name, args).success, name)
        self.assertTrue(box.invoke("list_files", {"path": "allowed"}).success)

    def test_non_git_workspace_hides_git_diff_and_marks_direct_request_inapplicable(self):
        box = PolicyToolbox(self.root, self.workspace, DEFAULT_CONFIG,
                            ["list_files", "read_file", "git_diff"])
        self.assertNotIn("git_diff", box.enabled)
        self.assertFalse(any(schema["function"]["name"] == "git_diff" for schema in box.schemas))
        result = box.invoke("git_diff", {})
        self.assertFalse(result.success)
        self.assertEqual(result.error_class, "not_applicable")

    def test_symlink_escape_is_neither_read_nor_listed_nor_searched(self):
        outside = self.root / "secret.txt"
        outside.write_text("private content")
        try:
            (self.workspace / "leak.txt").symlink_to(outside)
        except OSError:
            self.skipTest("This Windows account cannot create symlinks.")
        box = PolicyToolbox(self.root, self.workspace, DEFAULT_CONFIG, ["read_file", "list_files", "search_code"])
        self.assertFalse(box.invoke("read_file", {"path": "leak.txt"}).success)
        self.assertNotIn("leak.txt", box.invoke("list_files", {}).output)
        self.assertNotIn("private content", box.invoke("search_code", {"query": "private"}).output)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = FakeOllama()
        self.addCleanup(self.server.close)
        self.store = Store(self.root / "state.sqlite3")
        self.runtime = Runtime(self.store, self.root, ROOT)
        self.runtime.start()
        self.addCleanup(self.runtime.shutdown)

    def agent(self, **config):
        return self.store.create_agent(normalize_agent({"name": "Test agent", "config": {"endpoint": self.server.url, **config},
                                                        "tools": ["write_file", "read_file", "list_files"]}))

    def wait_for(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.025)
        self.fail("Timed out; tasks=" + json.dumps(self.store.list_tasks()))

    def completed(self, task):
        return self.wait_for(lambda: (item if (item := self.store.get_task(task["id"]))["status"] in {"Success", "Failed", "Cancelled"} else None))

    def test_real_process_http_tools_events_and_final_result(self):
        self.server.responses.extend([answer(calls=[("write_file", {"path": "created.txt", "content": "written by tool"})]), answer("Created file.")])
        task = self.runtime.submit(self.agent()["id"], "Create a file")
        final = self.completed(task)
        self.assertEqual(final["status"], "Success", final["error"])
        self.assertEqual((Path(final["workspace"]) / "created.txt").read_text(), "written by tool")
        self.assertEqual(final["model_calls"], 3)
        self.assertEqual(final["total_tokens"], 15)
        self.assertEqual(self.store.list_steps(task["id"])[0]["status"], "Success")
        self.assertFalse(self.runtime.active)

    def test_approval_pauses_task_and_resolves_once(self):
        policy = {"capabilities": {
            "filesystem": {
                "create": {"mode": "ask"},
                "overwrite": {"mode": "ask"},
            },
            "execution": {},
            "git": {},
        }}
        self.server.responses.extend([
            answer(calls=[("write_file", {"path": "approval.txt", "content": "approved-content"})]),
            answer("Created after approval."),
        ])
        agent = self.agent(capability_policy=policy)
        task = self.runtime.submit(agent["id"], "Create a file after approval")
        waiting = self.wait_for(lambda: self.store.get_task(task["id"])
                                if self.store.get_task(task["id"])["status"] == "WaitingForApproval" else None)
        approvals = self.store.list_approvals(status="pending", task_id=task["id"])
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["capability"], "filesystem.create")
        self.assertEqual(approvals[0]["arguments"]["content"]["redacted"], True)
        self.runtime.resolve_approval(approvals[0]["id"], "approved_once")
        final = self.completed(waiting)
        self.assertEqual(final["status"], "Success", final["error"])
        self.assertEqual((Path(final["workspace"]) / "approval.txt").read_text(), "approved-content")
        self.assertEqual(self.store.get_approval(approvals[0]["id"])["status"], "approved_once")
        self.assertFalse(self.runtime.active)

    def test_configured_workspace_is_reused_for_task_files(self):
        selected = self.root / "selected-project"
        selected.mkdir()
        self.server.responses.extend([answer(calls=[("write_file", {"path": "created.txt", "content": "persistent"})]), answer()])
        task = self.runtime.submit(self.agent(workspace_path=str(selected))["id"], "Use selected workspace")
        final = self.completed(task)
        self.assertEqual(final["status"], "Success", final["error"])
        self.assertEqual(Path(final["workspace"]), selected.resolve())
        self.assertEqual((selected / "created.txt").read_text(), "persistent")

    def test_task_workspace_override_wins_and_empty_override_uses_new_workspace(self):
        agent_workspace = self.root / "agent-default"
        task_workspace = self.root / "task-choice"
        agent_workspace.mkdir()
        task_workspace.mkdir()
        agent = self.agent(workspace_path=str(agent_workspace))
        selected = self.runtime.submit(agent["id"], "Override agent workspace", str(task_workspace))
        self.assertEqual(Path(selected["workspace"]), task_workspace.resolve())
        self.runtime.cancel(selected["id"])
        automatic = self.runtime.submit(agent["id"], "Use a fresh workspace", "")
        self.assertTrue(Path(automatic["workspace"]).is_relative_to(self.root / "workspaces"))
        self.runtime.cancel(automatic["id"])

    def test_agents_sharing_workspace_are_serialized(self):
        selected = self.root / "shared-project"
        selected.mkdir()
        self.server.release.clear()
        first_agent = self.agent(workspace_path=str(selected))
        second_agent = self.agent(workspace_path=str(selected))
        first = self.runtime.submit(first_agent["id"], "First")
        self.assertTrue(self.server.entered.wait(10))
        second = self.runtime.submit(second_agent["id"], "Second")
        time.sleep(.2)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.store.get_task(second["id"])["status"], "Queued")
        self.server.release.set()
        self.assertEqual(self.completed(first)["status"], "Success")
        self.assertEqual(self.completed(second)["status"], "Success")

    def test_per_agent_serialization_parallel_agents_and_queued_cancel(self):
        self.server.release.clear()
        first_agent, second_agent = self.agent(), self.agent()
        first = self.runtime.submit(first_agent["id"], "First")
        self.assertTrue(self.server.entered.wait(10))
        second = self.runtime.submit(first_agent["id"], "Same agent queued")
        third = self.runtime.submit(second_agent["id"], "Parallel agent")
        self.wait_for(lambda: len(self.server.requests) == 2)
        self.assertEqual(self.store.get_task(second["id"])["status"], "Queued")
        self.assertEqual(len(self.runtime.active), 2)
        self.assertEqual(self.runtime.cancel(second["id"])["status"], "Cancelled")
        self.server.release.set()
        self.assertEqual(self.completed(first)["status"], "Success")
        self.assertEqual(self.completed(third)["status"], "Success")

    def test_pause_is_cooperative_and_resume_executes_pending_tool(self):
        self.server.release.clear()
        self.server.responses.extend([answer(calls=[("write_file", {"path": "after.txt", "content": "yes"})]), answer()])
        agent = self.agent()
        task = self.runtime.submit(agent["id"], "Pause during model call")
        self.assertTrue(self.server.entered.wait(10))
        self.runtime.pause(agent["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "Running")
        self.server.release.set()
        self.wait_for(lambda: self.store.get_task(task["id"])["status"] == "Paused")
        self.assertFalse((Path(task["workspace"]) / "after.txt").exists())
        self.runtime.resume(agent["id"])
        self.assertEqual(self.completed(task)["status"], "Success")
        self.assertTrue((Path(task["workspace"]) / "after.txt").exists())

    def test_wallclock_budget_includes_paused_time(self):
        self.server.release.clear()
        agent = self.agent(max_seconds=2)
        task = self.runtime.submit(agent["id"], "Pause until deadline")
        self.assertTrue(self.server.entered.wait(10))
        self.runtime.pause(agent["id"])
        self.server.release.set()
        final = self.completed(task)
        self.assertEqual(final["status"], "Failed")
        self.assertIn("execution time", final["error"])
        self.assertLess(final["duration_seconds"], 3)

    def test_cancel_active_and_restart_cancel_remaining_work(self):
        self.server.release.clear()
        agent = self.agent()
        first = self.runtime.submit(agent["id"], "Blocked model")
        self.assertTrue(self.server.entered.wait(10))
        second = self.runtime.submit(agent["id"], "Queued")
        process = self.runtime.active[first["id"]]["process"]
        pid = process.pid
        self.assertEqual(self.runtime.cancel(first["id"])["status"], "Cancelled")
        self.runtime.restart(agent["id"])
        self.assertEqual(self.store.get_task(second["id"])["status"], "Cancelled")
        self.assertEqual(self.store.get_agent(agent["id"])["status"], "Idle")
        self.assertNotIn(pid, [worker["process"].pid for worker in self.runtime.active.values()])

    def test_shutdown_cancels_running_and_queued_and_is_idempotent(self):
        self.server.release.clear()
        agent = self.agent()
        first = self.runtime.submit(agent["id"], "Running")
        self.assertTrue(self.server.entered.wait(10))
        second = self.runtime.submit(agent["id"], "Queued")
        self.runtime.shutdown()
        self.runtime.shutdown()
        self.assertEqual(self.store.get_task(first["id"])["status"], "Cancelled")
        self.assertEqual(self.store.get_task(second["id"])["status"], "Cancelled")
        self.assertFalse(self.runtime.active)


class TransportTests(unittest.TestCase):
    def test_authorized_requests_never_follow_redirects(self):
        server = FakeOllama()
        self.addCleanup(server.close)
        server.redirect = server.url + "/other"
        with self.assertRaisesRegex(TransportError, "HTTP 302"):
            request_json("POST", server.url + "/api/chat", {}, token="credential")
        self.assertEqual(len(server.requests), 1)


if __name__ == "__main__":
    unittest.main()
