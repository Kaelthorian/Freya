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

from control_center.config import DEFAULT_CONFIG, normalize_agent
from control_center.runtime import Runtime
from control_center.storage import Store
from control_center.transport import TransportError, request_json
from control_center.worker import PolicyToolbox, run_task
from control_center.tools import ToolResult


ROOT = Path(__file__).resolve().parents[1]


def answer(content="Finished.", calls=None, **metrics):
    result = {"message": {"role": "assistant", "content": content}, "prompt_eval_count": 3, "eval_count": 2}
    if calls:
        result["message"]["tool_calls"] = [{"function": {"name": name, "arguments": args}} for name, args in calls]
    result.update(metrics)
    return result


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

    def run_worker(self, responses, *, config=None, tools=None, toolbox=None, token=""):
        config = {**DEFAULT_CONFIG, **(config or {})}
        task = {"config": config, "tools": tools if tools is not None else ["write_file", "read_file"],
                "workspace": str(self.workspace), "prompt": "Create hello.py"}
        replies = iter(responses)
        self.payloads = []
        def transport(method, url, body, **kwargs):
            self.payloads.append(body)
            return next(replies)
        return run_task(task, self.root, self.events.append, lambda: None,
                        transport=transport, toolbox=toolbox, token=token)

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

    def test_fallback_disabled_tool_is_a_visible_error_without_mutation(self):
        result = self.run_worker([answer('{"action":"write_file","path":"blocked.txt","content":"bad"}'),
                                  answer('{"action":"finish","message":"Could not write"}')], tools=["read_file"])
        self.assertEqual(result["status"], "Success")
        self.assertFalse((self.workspace / "blocked.txt").exists())
        errors = [e for e in self.events if e.get("event", {}).get("status") == "Failed"]
        self.assertEqual(errors[0]["event"]["tool"], "write_file")
        self.assertIn("disabled", errors[0]["event"]["error"])

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
        self.assertEqual(final["model_calls"], 2)
        self.assertEqual(final["total_tokens"], 10)
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
