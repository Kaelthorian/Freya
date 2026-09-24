from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from control_center.api import Application
from control_center.http import ControlServer
from control_center.orchestrator import Orchestrator
from control_center.planner import Planner
from control_center.storage import Store


class StubRuntime:
    max_workers = 2

    def __init__(self, store):
        self.store = store
        self.submissions = []

    def submit(self, agent_id, prompt, workspace_path=None):
        self.submissions.append((agent_id, prompt, workspace_path))
        return self.store.create_task(agent_id, prompt, workspace_path or "test-workspace")


class StubOrchestrator:
    def __init__(self):
        self.submissions = []

    def submit(self, prompt, workspace_path=None):
        self.submissions.append((prompt, workspace_path))
        return {"id": "orchestration", "prompt": prompt, "status": "Queued"}

    def cancel(self, orchestration_id):
        return {"id": orchestration_id, "status": "Cancelled"}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / "test.sqlite3")
        self.runtime = StubRuntime(self.store)
        app = Application(self.store, self.runtime, self.directory)
        self.server = ControlServer(("127.0.0.1", 0), app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        connection.request(method, path, json.dumps(body) if body is not None else None, request_headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data)

    def test_crud_snapshot_and_historical_logs_survive_delete(self):
        status, agent = self.request("POST", "/api/agents", {"name": "Developer"})
        self.assertEqual(status, 201, agent)
        agent_id = agent["id"]
        status, changed = self.request("PATCH", "/api/agents/" + agent_id, {"config": {"max_steps": 5}})
        self.assertEqual(changed["config"]["max_steps"], 5)
        status, task = self.request("POST", f"/api/agents/{agent_id}/tasks", {"prompt": "Inspect"})
        self.assertEqual(status, 201, task)
        self.assertEqual(task["config"]["max_steps"], 5)
        status, _ = self.request("DELETE", "/api/agents/" + agent_id, {})
        self.assertEqual(status, 409)
        self.store.update_task(task["id"], status="Success", total_tokens=21)
        self.store.append_event(task["id"], {"event_type": "task.finished", "status": "Success", "output": "password=hidden"})
        self.assertEqual(self.request("DELETE", "/api/agents/" + agent_id, {})[0], 200)
        status, detail = self.request("GET", "/api/tasks/" + task["id"])
        self.assertEqual(status, 200)
        self.assertEqual(detail["total_tokens"], 21)
        self.assertNotIn("hidden", json.dumps(detail))
        self.assertEqual(self.request("GET", "/api/agents")[1], [])

    def test_orchestration_activity_endpoint_includes_system_actor_rows(self):
        run = self.store.create_orchestration("Track system activity")
        self.store.add_orchestration_event(run["id"], {
            "event_type": "task_analysis.started", "status": "Analyzing",
            "actor_type": "task_analyst", "message": "Analyst started.",
        })
        self.store.add_orchestration_event(run["id"], {
            "event_type": "task_analysis.updated", "status": "Planning",
            "actor_type": "task_analyst", "agent_id": None,
            "metrics": {"model": "test-model", "model_calls": 1, "fallback_used": False},
            "message": "Analyst completed.",
        })
        status, activity = self.request(
            "GET", f"/api/orchestrations/{run['id']}/activity",
        )
        self.assertEqual(status, 200)
        self.assertEqual(activity["orchestration_id"], run["id"])
        self.assertTrue(any(event["component"] == "Task Analyst"
                            for event in activity["events"]))
        self.assertTrue(any(phase["name"] == "task_analysis"
                            for phase in activity["phases"]))

    def test_duplicate_agent_preserves_instructions_and_names_remain_unique(self):
        status, agent = self.request("POST", "/api/agents", {
            "name": "Original",
            "role": "Engineer",
            "instructions": "Keep this instruction",
        })
        self.assertEqual(status, 201, agent)
        status, duplicate = self.request("POST", f"/api/agents/{agent['id']}/duplicate", {})
        self.assertEqual(status, 201, duplicate)
        self.assertEqual(duplicate["instructions"], "Keep this instruction")
        status, body = self.request("POST", "/api/agents", {"name": "original"})
        self.assertEqual(status, 409)
        self.assertIn("already exists", body["error"])

    def test_pipeline_agent_presets_are_listed_and_qa_can_be_created(self):
        status, presets = self.request("GET", "/api/agent-presets")
        self.assertEqual(status, 200)
        self.assertEqual({item["id"] for item in presets}, {
            "programmer", "task-analyst", "qa-tester", "code-auditor",
        })
        status, qa = self.request("POST", "/api/agent-presets/qa-tester", {})
        self.assertEqual(status, 201, qa)
        self.assertEqual(qa["config"]["orchestration_role"], "qa")
        self.assertIn("run_command", qa["tools"])
        self.assertNotIn("write_file", qa["tools"])

    def test_agent_creation_accepts_editor_capability_policy_inside_config(self):
        status, agent = self.request("POST", "/api/agents", {
            "name": "Policy editor",
            "config": {
                "capability_policy": {
                    "capabilities": {
                        "filesystem": {"read": {"mode": "allow"}}
                    }
                }
            },
        })
        self.assertEqual(status, 201, agent)
        self.assertEqual(agent["config"]["capability_policy"]["capabilities"]["filesystem"]["read"]["mode"], "allow")
        self.assertEqual(agent["capability_policy"]["capabilities"]["filesystem"]["read"]["mode"], "allow")

    def test_origin_host_and_body_guards(self):
        for headers in ({"Origin": "https://evil.example"}, {"Host": "evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("POST", "/api/agents", {"name": "bad"}, headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/agents", {"name": "bad"}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/agents", {"name": "bad", "tools": ["browser"]})[0], 400)
        self.assertEqual(self.request("GET", "/api/tasks?limit=99999")[0], 400)

    def test_orchestration_prompt_accepts_long_input(self):
        orchestrator = StubOrchestrator()
        self.server.application.orchestrator = orchestrator
        prompt = "x" * 5_000
        status, body = self.request("POST", "/api/orchestrations", {
            "prompt": prompt,
        })
        self.assertEqual(status, 201)
        self.assertEqual(body["prompt"], prompt)
        self.assertEqual(orchestrator.submissions, [(prompt, "")])

    def test_cancelling_terminal_orchestration_is_http_idempotent(self):
        orchestrator = Orchestrator(self.store, self.runtime, planner=Planner(offline=True))
        self.server.application.orchestrator = orchestrator
        run = self.store.create_orchestration("Cancel once")
        first_status, first = self.request("POST", f"/api/orchestrations/{run['id']}/cancel", {})
        second_status, second = self.request("POST", f"/api/orchestrations/{run['id']}/cancel", {})
        self.assertEqual((first_status, second_status), (200, 200))
        self.assertEqual((first["status"], second["status"]), ("Cancelled", "Cancelled"))
        self.assertEqual([event["event_type"] for event in second["events"]], ["freya.cancelled"])

    def test_workspace_browser_lists_directories_and_rejects_bad_paths(self):
        child = self.directory / "Project A"
        child.mkdir()
        (self.directory / "file.txt").write_text("not a folder")
        encoded = urlencode({"path": str(self.directory)})
        status, result = self.request("GET", "/api/workspaces/browse?" + encoded)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["path"], str(self.directory.resolve()))
        self.assertIn({"name": "Project A", "path": str(child.resolve())}, result["directories"])
        self.assertNotIn("file.txt", [item["name"] for item in result["directories"]])
        self.assertEqual(self.request("GET", "/api/workspaces/browse?path=relative")[0], 400)

    def test_task_workspace_override_is_validated_and_preserved_on_retry(self):
        agent = self.request("POST", "/api/agents", {"name": "Workspace"})[1]
        selected = self.directory / "task-project"
        selected.mkdir()
        status, task = self.request("POST", f"/api/agents/{agent['id']}/tasks",
                                    {"prompt": "Work here", "workspace_path": str(selected)})
        self.assertEqual(status, 201, task)
        self.assertEqual(task["workspace"], str(selected.resolve()))
        self.assertEqual(self.runtime.submissions[-1][2], str(selected.resolve()))
        self.assertEqual(self.request("POST", f"/api/agents/{agent['id']}/tasks",
                                      {"prompt": "bad", "workspace_path": "relative"})[0], 400)
        self.store.update_task(task["id"], status="Success")
        status, retried = self.request("POST", f"/api/tasks/{task['id']}/retry", {})
        self.assertEqual(status, 201, retried)
        self.assertEqual(retried["workspace"], str(selected.resolve()))

    def test_sse_replays_only_events_after_cursor(self):
        agent = self.request("POST", "/api/agents", {"name": "SSE"})[1]
        task = self.store.create_task(agent["id"], "Task", "workspace")
        first = self.store.append_event(task["id"], {"event_type": "test.first", "status": "Running"})
        second = self.store.append_event(task["id"], {"event_type": "test.second", "status": "Success"})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        connection.request("GET", f"/api/tasks/{task['id']}/events", headers={"Last-Event-ID": str(first["id"])})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        lines = []
        while len(lines) < 12:
            line = response.readline().decode()
            lines.append(line)
            if line.startswith("data:"):
                break
        connection.close()
        text = "".join(lines)
        self.assertIn(f"id: {second['id']}", text)
        self.assertNotIn("test.first", text)


if __name__ == "__main__":
    unittest.main()
