from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from control_center.api import Application
from control_center.http import ControlServer
from control_center.storage import Store


class StubRuntime:
    max_workers = 2

    def __init__(self, store):
        self.store = store

    def submit(self, agent_id, prompt):
        return self.store.create_task(agent_id, prompt, "test-workspace")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / "test.sqlite3")
        app = Application(self.store, StubRuntime(self.store), self.directory)
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

    def test_origin_host_and_body_guards(self):
        for headers in ({"Origin": "https://evil.example"}, {"Host": "evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("POST", "/api/agents", {"name": "bad"}, headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/agents", {"name": "bad"}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/agents", {"name": "bad", "tools": ["browser"]})[0], 400)
        self.assertEqual(self.request("GET", "/api/tasks?limit=99999")[0], 400)

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
