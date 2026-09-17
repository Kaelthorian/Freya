"""Persistence, atomicity and execution reconstruction regression tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from control_center.config import normalize_agent, TOOL_CATALOG
from control_center.storage import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state" / "control.sqlite3"
        self.store = Store(self.path)

    def agent(self, name="Local coder", **changes):
        return self.store.create_agent(normalize_agent({"name": name, **changes}))

    def task(self, agent=None, prompt="Inspect project"):
        return self.store.create_task((agent or self.agent())["id"], prompt, "workspaces/task")

    def test_empty_database_only_seeds_real_tool_catalogue(self):
        self.assertEqual(self.store.list_agents(), [])
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual(self.store.list_events(), [])
        metrics = self.store.metrics()
        self.assertEqual(metrics["total_tokens"], 0)
        self.assertEqual(metrics["tools"], [])
        self.assertEqual(len(metrics["history"]), 7)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tools").fetchone()[0], len(TOOL_CATALOG))
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_snapshots_and_soft_deleted_history_survive_reopening(self):
        agent = self.agent(config={"model": "original-model"}, tools=["read_file"])
        task = self.task(agent)
        self.store.update_agent(agent["id"], normalize_agent(
            {"name": "Renamed", "config": {"model": "replacement-model"}, "tools": ["list_files"]}, agent))
        reopened = Store(self.path)
        snapshot = reopened.get_task(task["id"])
        self.assertEqual(snapshot["agent_name"], "Local coder")
        self.assertEqual(snapshot["config"]["model"], "original-model")
        self.assertEqual(snapshot["tools"], ["read_file"])
        self.assertNotEqual(snapshot["id"], snapshot["execution_id"])
        reopened.delete_agent(agent["id"])
        self.assertEqual(reopened.list_agents(), [])
        with self.assertRaises(KeyError):
            reopened.get_agent(agent["id"])
        self.assertEqual(reopened.get_task(task["id"])["agent_name"], "Local coder")
        self.assertEqual(reopened.metrics()["total_tasks"], 1)

    def test_agent_config_change_rolls_back_if_tools_are_invalid(self):
        agent = self.agent()
        replacement = normalize_agent({"name": "Changed"}, agent)
        replacement["tools"] = ["nonexistent_tool"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update_agent(agent["id"], replacement)
        self.assertEqual(self.store.get_agent(agent["id"])["name"], agent["name"])
        self.assertEqual(self.store.get_agent(agent["id"])["tools"], agent["tools"])

    def test_redaction_occurs_before_persistence_and_preserves_token_metrics(self):
        agent = self.agent(description="password=hunter2", config={"secret_env": "ACC_SECRET_LOCAL"})
        task = self.task(agent, 'Use api_key="top-secret-value"')
        self.store.update_task(task["id"], prompt_tokens=20, generated_tokens=5, total_tokens=25,
                               result={"api_key": "sensitive-value", "thinking": "private reasoning"})
        event = self.store.append_event(task["id"], {
            "event_type": "step.finished", "step_id": "s1", "status": "Success",
            "input": {"password": "raw-pass"}, "output": "Bearer abcdef123456",
        })
        self.assertEqual(event["input"]["password"], "[REDACTED]")
        self.assertNotIn("abcdef123456", event["output"])
        final = self.store.get_task(task["id"])
        self.assertEqual(final["total_tokens"], 25)
        self.assertEqual(final["config"]["secret_env"], "ACC_SECRET_LOCAL")
        self.assertNotIn("thinking", final["result"])
        with sqlite3.connect(self.path) as connection:
            dump = "\n".join(connection.iterdump())
        for secret in ("hunter2", "top-secret-value", "sensitive-value", "private reasoning", "raw-pass", "abcdef123456"):
            self.assertNotIn(secret, dump)

    def test_step_finished_merges_started_input_and_keeps_retry_events(self):
        task = self.task()
        self.store.append_event(task["id"], {
            "event_type": "step.started", "step_id": "one", "step_number": 1,
            "tool": "read_file", "input": {"path": "main.py"}, "reason": "Inspect the entry point",
            "timestamp": "2026-09-10T10:00:00+00:00",
        })
        self.store.append_event(task["id"], {
            "event_type": "tool.retry", "step_id": "one", "attempt": 2, "level": "WARNING",
        })
        self.store.append_event(task["id"], {
            "event_type": "step.finished", "step_id": "one", "status": "Success", "output": "content",
            "duration_seconds": 1.2, "attempt": 2, "timestamp": "2026-09-10T10:00:01.200+00:00",
        })
        steps = Store(self.path).list_steps(task["id"])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["input"], {"path": "main.py"})
        self.assertEqual(steps[0]["tool"], "read_file")
        self.assertEqual(steps[0]["status"], "Success")
        self.assertEqual(steps[0]["attempt"], 2)
        self.assertEqual(steps[0]["timestamp"], "2026-09-10T10:00:00+00:00")
        self.assertEqual(len(self.store.list_events(task["id"])), 3)

    def test_event_filters_and_sse_cursor_include_agent_lifecycle(self):
        first, second = self.agent("One"), self.agent("Two")
        first_task, second_task = self.task(first), self.task(second)
        lifecycle = self.store.append_event(None, {"agent_id": first["id"], "event_type": "agent.created"})
        failed = self.store.append_event(first_task["id"], {
            "event_type": "step.finished", "tool": "read_file", "level": "ERROR", "error": "missing",
            "timestamp": "2026-09-10T23:59:59+00:00",
        })
        self.store.append_event(first_task["id"], {
            "event_type": "step.finished", "tool": "list_files", "level": "INFO",
            "timestamp": "2026-09-11T00:00:00+00:00",
        })
        self.store.append_event(second_task["id"], {"event_type": "execution.started", "level": "INFO"})
        self.assertIsNone(lifecycle["task_id"])
        selected = self.store.list_events(agent_id=first["id"], level="error", tool="read_file", error_only=True,
                                          date_from="2026-09-10", date_to="2026-09-10")
        self.assertEqual([row["id"] for row in selected], [failed["id"]])
        self.assertEqual(len(self.store.list_events(first_task["id"])), 2)
        self.assertEqual(len(self.store.list_events(after=failed["id"])), 2)
        self.assertEqual(len(self.store.list_events(limit=1)), 1)

    def test_metrics_agent_aggregation_and_history_use_stored_execution_data(self):
        first, second = self.agent("One"), self.agent("Two", enabled=False)
        passed, failed, running = self.task(first), self.task(first), self.task(second)
        self.store.update_task(passed["id"], status="Success", steps=4, duration_seconds=10, total_tokens=30,
                               model_calls=2, tool_calls=1)
        self.store.update_task(failed["id"], status="Failed", steps=2, duration_seconds=6, total_tokens=10,
                               model_calls=1, tool_calls=1)
        self.store.update_task(running["id"], status="Running", total_tokens=5, model_calls=1)
        self.store.set_agent_state(first["id"], "Error")
        for task, duration in ((passed, 0.5), (failed, 1.5)):
            self.store.append_event(task["id"], {"event_type": "model.finished", "duration_seconds": duration})
        self.store.append_event(passed["id"], {"event_type": "step.finished", "tool": "read_file", "status": "Success"})
        self.store.append_event(failed["id"], {"event_type": "step.finished", "tool": "read_file", "status": "Failed", "error": "missing"})
        metrics = self.store.metrics()
        self.assertEqual(metrics["total_agents"], 2)
        self.assertEqual(metrics["active_agents"], 1)
        self.assertEqual(metrics["error_agents"], 1)
        self.assertEqual(metrics["running_tasks"], 1)
        self.assertEqual(metrics["avg_duration_seconds"], 8)
        self.assertEqual(metrics["avg_steps"], 3)
        self.assertEqual(metrics["avg_model_seconds"], 1)
        self.assertEqual(metrics["total_tokens"], 45)
        self.assertEqual(metrics["tools"], [{"name": "read_file", "calls": 2, "errors": 1}])
        self.assertEqual(metrics["errors_by_agent"], [{"name": "One", "errors": 1}])
        self.assertEqual(metrics["history"][-1]["tasks"], 3)
        self.assertEqual(metrics["history"][-1]["tokens"], 45)
        self.assertEqual(self.store.metrics(first["id"])["total_tokens"], 40)
        summary = self.store.get_agent(first["id"])
        self.assertEqual(summary["success_rate"], 50)
        self.assertEqual(summary["task_count"], 2)
        self.assertIsNone(summary["current_task"])
        self.assertEqual(self.store.get_agent(second["id"])["current_task"]["id"], running["id"])

    def test_recovery_closes_only_unfinished_tasks_and_steps_once(self):
        agent = self.agent()
        queued, running, paused, passed = [self.task(agent) for _ in range(4)]
        self.store.update_task(running["id"], status="Running",
                               started_at=(datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat())
        self.store.update_task(paused["id"], status="Paused")
        self.store.update_task(passed["id"], status="Success", result="Finished")
        self.store.append_event(running["id"], {"event_type": "step.started", "step_id": "active"})
        self.assertEqual(Store(self.path).recover_interrupted(), 3)
        for task in (queued, running, paused):
            result = self.store.get_task(task["id"])
            self.assertEqual(result["status"], "Failed")
            self.assertIsNotNone(result["finished_at"])
            self.assertIn("restart", result["error"])
        self.assertGreaterEqual(self.store.get_task(running["id"])["duration_seconds"], 3)
        self.assertEqual(self.store.list_steps(running["id"])[0]["status"], "Failed")
        self.assertEqual(self.store.get_task(passed["id"])["result"], "Finished")
        self.assertEqual(self.store.get_agent(agent["id"])["status"], "Error")
        self.assertEqual(len(self.store.list_events(error_only=True)), 3)
        self.assertEqual(self.store.recover_interrupted(), 0)

    def test_parallel_writers_preserve_monotonic_event_ids(self):
        task = self.task()
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(lambda index: self.store.append_event(task["id"], {
                "event_type": "log", "output": str(index),
            })["id"], range(36)))
        self.assertEqual(len(set(ids)), 36)
        self.assertEqual([event["id"] for event in self.store.list_events(task["id"])], sorted(ids))

    def test_retry_reopens_step_and_cancellation_closes_it(self):
        task = self.task()
        common = {"step_id": "one", "step_number": 1, "tool": "read_file"}
        self.store.append_event(task["id"], {**common, "event_type": "step.finished", "status": "Failed",
                                             "error": "timeout", "output": "timeout", "duration_seconds": 10})
        self.store.append_event(task["id"], {**common, "event_type": "step.started", "attempt": 2})
        reopened = self.store.list_steps(task["id"])[0]
        self.assertEqual(reopened["status"], "Running")
        for field in ("error", "output", "finished_at", "duration_seconds"):
            self.assertIsNone(reopened[field])
        self.store.update_task(task["id"], status="Cancelled", error="Stopped by user")
        cancelled = self.store.list_steps(task["id"])[0]
        self.assertEqual(cancelled["status"], "Cancelled")
        self.assertEqual(cancelled["error"], "Stopped by user")
        self.assertIsNotNone(cancelled["finished_at"])

    def test_approval_records_are_sanitized_and_resolvable(self):
        agent = self.agent(config={"capability_policy": {"capabilities": {
            "filesystem": {"create": {"mode": "ask"}}, "execution": {}, "git": {},
        }}})
        task = self.store.create_task(agent["id"], "Create a file", "workspaces/task")
        approval = self.store.create_approval(
            task["id"], agent["id"], "filesystem.create", "write_file",
            {"path": "safe.txt", "content": "local-content"},
            "Create a file", "safe.txt", "Capability policy requires approval.",
        )
        self.assertEqual(approval["status"], "pending")
        self.assertTrue(approval["arguments"]["content"]["redacted"])
        resolved = self.store.resolve_approval(approval["id"], "approved_once")
        self.assertEqual(resolved["status"], "approved_once")
        with self.assertRaises(ValueError):
            self.store.resolve_approval(approval["id"], "denied")
        second = self.store.create_approval(
            task["id"], agent["id"], "filesystem.create", "write_file",
            {"path": "other.txt"}, "Create another file", "other.txt", "test",
        )
        self.assertEqual(self.store.cancel_pending_approvals(task["id"], "cancelled"), 1)
        self.assertEqual(self.store.get_approval(second["id"])["status"], "denied")

    def test_status_and_update_field_allowlists(self):
        task = self.task()
        with self.assertRaises(ValueError):
            self.store.update_task(task["id"], status="Unknown")
        with self.assertRaises(ValueError):
            self.store.update_task(task["id"], **{"workspace": "outside"})
        with self.assertRaises(ValueError):
            self.store.set_agent_state(task["agent_id"], "Unknown")
        with self.assertRaises(KeyError):
            self.store.get_task("nonexistent")
        with self.assertRaises(KeyError):
            self.store.append_event("nonexistent", {"event_type": "log"})


if __name__ == "__main__":
    unittest.main()
