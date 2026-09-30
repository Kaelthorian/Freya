from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_center.agent_context import build_agent_context, build_effective_agent
from control_center.orchestrator import Orchestrator
from control_center.plan_context import MAX_PLAN_CONTEXT_CHARS, render_plan_context
from control_center.worker import PolicyToolbox, run_task


def planned(task_id: str, objective: str, *, kind: str = "code_change",
            dependencies: list[str] | None = None, targets: list[str] | None = None,
            criteria: list[str] | None = None, capabilities: list[str] | None = None,
            tools: list[str] | None = None) -> dict:
    return {
        "id": task_id, "task_kind": kind, "objective": objective,
        "description": objective + " in its own scope.",
        "depends_on": dependencies or [], "success_criteria": criteria or [objective],
        "write_targets": targets or [], "owned_paths": targets or [],
        "semantic_operations": ["create_file" if kind == "file_creation" else "modify_file"],
        "required_capabilities": capabilities or [], "required_tools": tools or [],
    }


class PlanContextTests(unittest.TestCase):
    def test_scaffold_and_html_css_js_tasks_are_all_visible_with_non_goals(self):
        tasks = [
            planned("scaffold", "Create empty project files", kind="file_creation",
                    targets=["app.html", "app.css", "app.js"], criteria=["Project files exist"]),
            planned("html", "Build interface structure", dependencies=["scaffold"], targets=["app.html"]),
            planned("css", "Style interface", dependencies=["html"], targets=["app.css"]),
            planned("js", "Implement runtime logic", dependencies=["scaffold"], targets=["app.js"]),
        ]
        context, metadata = render_plan_context({"tasks": tasks}, tasks[0])
        self.assertEqual(metadata["visible_task_ids"], ["scaffold", "html", "css", "js"])
        self.assertEqual(metadata["non_goal_count"], 3)
        self.assertIn("CURRENT TASK: scaffold", context)
        self.assertIn('"id":"scaffold"', context)
        self.assertIn('"reserved_by":"js","responsibility":"Implement runtime logic"', context)
        self.assertIn('"reserved_by":"css","responsibility":"Style interface"', context)
        self.assertIn("minimum valid scaffold", context)

    def test_shared_path_and_predecessor_summary_preserve_sequential_scope(self):
        tasks = [
            planned("create", "Create a.js", kind="file_creation", targets=["a.js"]),
            planned("implement", "Implement foo", dependencies=["create"], targets=["a.js"]),
            planned("errors", "Add error handling", dependencies=["implement"], targets=["a.js"]),
        ]
        nodes = [{"plan_task_id": "create", "state": "success", "result": {
            "result": {"summary": "Created a.js", "artifacts": [{"path": "a.js"}],
                       "actions": [{"output": "DO_NOT_INCLUDE_ACTION_OUTPUT"}]}}}]
        context, metadata = render_plan_context({"tasks": tasks}, tasks[1], nodes)
        self.assertIn('"id":"implement"', context)
        self.assertIn('"reserved_by":"errors","responsibility":"Add error handling"', context)
        self.assertIn('"status":"SUCCESS"', context)
        self.assertIn('"artifact_paths":["a.js"]', context)
        self.assertNotIn("DO_NOT_INCLUDE_ACTION_OUTPUT", context)
        self.assertEqual(metadata["dependency_ids"], ["create"])
        self.assertEqual(metadata["successor_ids"], ["errors"])

    def test_siblings_are_visible_even_without_dependency_or_shared_path(self):
        tasks = [planned("frontend", "Build frontend", targets=["ui.js"]),
                 planned("backend", "Build backend", targets=["api.py"])]
        context, _ = render_plan_context({"tasks": tasks}, tasks[0])
        self.assertIn('"reserved_by":"backend","responsibility":"Build backend"', context)
        self.assertIn('"write_targets":["api.py"]', context)

    def test_many_tasks_remain_bounded_without_hiding_nodes(self):
        tasks = [planned(f"task-{i}", "X" * 2000,
                         targets=[f"module_{i}_{j}_with_long_name.py" for j in range(20)],
                         criteria=["Y" * 1000 for _ in range(20)],
                         capabilities=["capability-" + str(j) for j in range(20)],
                         tools=["tool-" + str(j) for j in range(20)]) for i in range(20)]
        tasks[19]["depends_on"] = [task["id"] for task in tasks[:19]]
        nodes = [{"plan_task_id": task["id"], "state": "success", "result": {
            "result": {"summary": "Z" * 10_000,
                       "artifacts": [{"path": f"artifact_{j}.txt"} for j in range(100)]}}}
                 for task in tasks[:19]]
        context, metadata = render_plan_context({"tasks": tasks}, tasks[19], nodes)
        self.assertEqual(metadata["task_count"], 20)
        self.assertEqual(len(metadata["visible_task_ids"]), 20)
        self.assertLessEqual(len(context), MAX_PLAN_CONTEXT_CHARS)
        self.assertTrue(metadata["context_truncated"])
        self.assertIn('"id":"task-19"', context)
        self.assertNotIn("Z" * 1000, context)

    def test_statuses_are_current_and_other_tasks_keep_graph_state(self):
        tasks = [planned(f"task-{i}", f"Step {i}") for i in range(5)]
        nodes = [{"plan_task_id": f"task-{i}", "state": state}
                 for i, state in enumerate(("success", "running", "pending", "blocked", "failed"))]
        context, _ = render_plan_context({"tasks": tasks}, tasks[1], nodes)
        self.assertIn('"id":"task-0","status":"SUCCESS"', context)
        self.assertIn('"id":"task-1","status":"RUNNING"', context)
        self.assertIn('"id":"task-2","status":"PENDING"', context)
        self.assertIn('"id":"task-3","status":"BLOCKED"', context)
        self.assertIn('"id":"task-4","status":"FAILED"', context)

    def test_dispatch_emits_bounded_plan_context_metadata_without_prompt(self):
        tasks = [planned("create", "Create a.js", kind="file_creation", targets=["a.js"]),
                 planned("logic", "Implement foo", dependencies=["create"], targets=["a.js"])]
        events = []
        class StoreStub:
            def get_execution_graph(self, orchestration_id):
                return {"nodes": [{"plan_task_id": "create", "state": "success"},
                                  {"plan_task_id": "logic", "state": "ready"}]}
            def add_orchestration_event(self, orchestration_id, event):
                events.append(event)
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator.store = StoreStub()
        context = orchestrator._plan_context_for_dispatch("run", {"tasks": tasks}, tasks[1])
        self.assertIn('"id":"create","status":"SUCCESS"', context)
        self.assertEqual(events[0]["event_type"], "worker.plan_context_prepared")
        self.assertEqual(events[0]["visible_task_ids"], ["create", "logic"])
        self.assertEqual(events[0]["context_chars"], len(context))
        self.assertNotIn("prompt", events[0])

    def test_worker_sees_other_task_capability_without_receiving_its_tool(self):
        tasks = [planned("edit", "Edit source", targets=["a.js"],
                         capabilities=["filesystem.modify"], tools=["edit_file"]),
                 planned("qa", "Run tests", kind="testing", dependencies=["edit"],
                         capabilities=["execution.python_script"], tools=["run_command"])]
        context, _ = render_plan_context({"tasks": tasks}, tasks[0])
        self.assertIn('"required_tools":["run_command"]', context)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            config = {"capability_policy": {"capabilities": {"filesystem": {
                "modify": {"mode": "allow"}}}}, "permissions": "workspace"}
            box = PolicyToolbox(root, workspace, config, ["edit_file", "run_command"])
            self.assertNotIn("run_command", [schema["function"]["name"] for schema in box.schemas])

    def test_global_agent_guidance_applies_only_to_dynamic_workers(self):
        base = {"name": "Worker", "role": "Agent", "tools": [], "skills": [],
                "config": {"provenance": {"generated_by_freya": True}}}
        context = build_agent_context(build_effective_agent(base), "Do current task")
        self.assertIn("CURRENT TASK", context)
        self.assertIn("minimum", context)
        base["config"] = {}
        self.assertNotIn("Dynamic Task Agent coordination",
                         build_agent_context(build_effective_agent(base), "Do current task"))

    def test_worker_prompt_contains_one_plan_view_and_highlights_current_task(self):
        tasks = [planned("scaffold", "Create a.js", kind="file_creation", targets=["a.js"]),
                 planned("logic", "Implement foo", dependencies=["scaffold"], targets=["a.js"])]
        plan_context, _ = render_plan_context({"tasks": tasks}, tasks[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            captured = []
            config = {"model": "test-model", "max_steps": 2, "max_model_calls": 2,
                      "provenance": {"generated_by_freya": True, "plan_task_id": "logic"},
                      "capability_policy": {"capabilities": {}},
                      "task_characteristics": {"requires_filesystem_write": False}}
            result = run_task({"config": config, "tools": [], "workspace": str(workspace),
                               "prompt": "CURRENT TASK: logic\n" + plan_context}, root,
                              lambda event: None, lambda: None,
                              transport=lambda method, url, body, **kwargs: (
                                  captured.append(body["messages"]) or
                                  {"message": {"role": "assistant", "content": "Done"}}))
            self.assertTrue(captured, result)
            self.assertIn("CURRENT TASK: logic", captured[0][1]["content"])
            self.assertEqual(sum(message["content"].count("ORCHESTRATION PLAN")
                                 for message in captured[0][:2]), 1)


if __name__ == "__main__":
    unittest.main()
