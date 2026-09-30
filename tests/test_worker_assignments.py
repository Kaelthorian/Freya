"""Runtime consumption of Plan Compiler worker assignments."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from control_center.execution_graph import ExecutionGraph
from control_center.orchestrator import Orchestrator
from control_center.plan_compiler import compile_semantic_plan
from control_center.planner import PLAN_SCHEMA_VERSION
from control_center.evaluator import EVALUATOR_VERSION
from control_center.storage import Store
from control_center.task_spec import deterministic_task_spec
from control_center.worker_assignment import worker_assignment_map


class IdleRuntime:
    def submit(self, agent_id, prompt, workspace_path=None):  # pragma: no cover - selection-only fixture
        raise AssertionError("This test exercises selection and Worker activation only.")

    def cancel(self, task_id):  # pragma: no cover - selection-only fixture
        raise AssertionError("This test does not dispatch a Runtime task.")


class ControlledRuntime:
    def __init__(self, store):
        self.store = store
        self.submissions = []
        self.max_active = 0

    def submit(self, agent_id, prompt, workspace_path=None, runtime_context=None):
        task = self.store.create_task(
            agent_id, prompt, workspace_path or "workspace",
            runtime_context=runtime_context,
        )
        task = self.store.update_task(task["id"], status="Running")
        self.submissions.append(task)
        active = sum(
            item["status"] in {"Queued", "Running", "WaitingForApproval", "Paused"}
            for item in self.store.list_tasks(limit=10000)
        )
        self.max_active = max(self.max_active, active)
        return task

    def cancel(self, task_id):
        return self.store.update_task(task_id, status="Cancelled", error="cancelled")

    def finish_active(self, seconds=None):
        for task in self.store.list_tasks(limit=10000):
            if task["status"] in {"Queued", "Running", "WaitingForApproval", "Paused"}:
                self.store.update_task(
                    task["id"], status="Success", result="Controlled fixture completed.",
                )


def semantic_task(key, objective, operation, path, depends=(), criteria=None,
                  task_kind="program_creation"):
    value = {
        "key": key,
        "task_kind": task_kind,
        "objective": objective,
        "description": objective,
        "depends_on": list(depends),
        "semantic_needs": [objective],
        "operations": [operation],
        "owned_paths": [path] if operation == "create_file" else [],
        "write_targets": [path] if operation in {"create_file", "modify_file"} else [],
        "success_criteria": criteria or [f"{path} exists."],
    }
    return value


def compile_plan(tasks, *, strategy="single_worker", complexity="multi_step", reason=None):
    return compile_semantic_plan({
        "summary": "Build a staged artifact.",
        "task_complexity": complexity,
        "execution_strategy": strategy,
        "decomposition_reason": reason or "One Worker preserves the shared implementation context across ordered steps.",
        "tasks": tasks,
        "success_criteria": [],
        "unsupported_requirements": [],
    }, deterministic_task_spec("Build a staged application with verified artifacts."))


class WorkerAssignmentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def start_run(self, plan, runtime=None, wait=None):
        run = self.store.create_orchestration("Build a staged application")
        self.store.transition_orchestration(run["id"], "Queued", "Planning")
        self.store.save_orchestration_plan(run["id"], plan, PLAN_SCHEMA_VERSION)
        graph = ExecutionGraph(plan)
        self.store.initialize_execution_graph(run["id"], graph.serialize())
        self.store.transition_orchestration(run["id"], "Planned", "Running")
        orchestrator = Orchestrator(
            self.store, runtime or IdleRuntime(), wait=wait,
            config={"max_wallclock_seconds": 10},
        )
        return run["id"], orchestrator

    def execute_plan(self, plan, *, max_parallel_tasks=4):
        runtime = ControlledRuntime(self.store)
        oid, orchestrator = self.start_run(plan, runtime, runtime.finish_active)
        orchestrator.config["max_parallel_tasks"] = max_parallel_tasks

        def accept_runtime_result(run_id, current_plan, node, deadline):
            self.store.commit_evaluation(
                str(uuid4()), run_id, node["plan_task_id"],
                runtime_task_id=node["runtime_task_id"],
                agent_id=node["selected_agent_id"], attempt=int(node["attempt"]),
                evaluator_version=EVALUATOR_VERSION,
                evaluation={"status": "accepted", "summary": "Controlled fixture accepted."},
                metrics={}, snapshot={}, context_truncated=False, deterministic=True,
            )

        orchestrator._evaluate_graph_node = accept_runtime_result
        orchestrator._complete_or_integrate = lambda *args, **kwargs: None
        run = self.store.get_orchestration(oid)
        orchestrator._run_graph(
            oid, run, orchestrator.clock() + 10,
            operational_prompt="Build the staged calculator.",
        )
        return oid, orchestrator, runtime

    def select(self, oid, orchestrator, plan, task_id):
        return orchestrator._select_graph_task(
            oid, next(task for task in plan["tasks"] if task["id"] == task_id),
            self.store.get_orchestration(oid),
        )

    def test_three_tasks_reuse_one_worker_and_refresh_task_tools(self):
        plan = compile_plan([
            semantic_task("create", "Create calculator.html", "create_file", "calculator.html"),
            semantic_task("modify", "Update calculator.html", "modify_file", "calculator.html",
                          depends=["create"], criteria=["calculator.html includes the operation buttons."]),
            semantic_task("inspect", "Inspect calculator.html", "read_file", "calculator.html",
                          depends=["modify"], criteria=["calculator.html can be read."],
                          task_kind="review"),
        ])
        self.assertEqual(plan["worker_assignments"], [{
            "worker_id": "worker-1", "task_ids": ["task-1", "task-2", "task-3"],
        }])
        oid, orchestrator = self.start_run(plan)

        first = self.select(oid, orchestrator, plan, "task-1")
        first_agent_id = first["selection"]["selected_agent_id"]
        first_task = self.store.create_task(first_agent_id, "task one", "workspace")
        self.store.update_task(first_task["id"], status="Success")

        second = self.select(oid, orchestrator, plan, "task-2")
        self.assertEqual(second["selection"]["selected_agent_id"], first_agent_id)
        second_task = self.store.create_task(first_agent_id, "task two", "workspace")
        self.store.update_task(second_task["id"], status="Success")

        third = self.select(oid, orchestrator, plan, "task-3")
        self.assertEqual(third["selection"]["selected_agent_id"], first_agent_id)
        third_task = self.store.create_task(first_agent_id, "task three", "workspace")

        snapshots = [self.store.get_task(item["id"]) for item in
                     (first_task, second_task, third_task)]
        tool_sets = [set(item["tools"]) for item in snapshots]
        self.assertIn("write_file", tool_sets[0])
        self.assertIn("edit_file", tool_sets[1])
        self.assertIn("read_file", tool_sets[1])
        self.assertIn("read_file", tool_sets[2])
        self.assertNotIn("edit_file", tool_sets[2])
        self.assertEqual([item["agent_id"] for item in snapshots], [first_agent_id] * 3)
        self.assertNotEqual(snapshots[0]["capability_policy"], snapshots[1]["capability_policy"])
        self.assertNotEqual(snapshots[1]["capability_policy"], snapshots[2]["capability_policy"])

        agent = self.store.get_agent(first_agent_id)
        self.assertEqual(agent["name"], "Dynamic Worker [worker-1]")
        self.assertEqual(agent["config"]["worker_assignment"]["worker_id"], "worker-1")
        events = self.store.get_orchestration(oid)["events"]
        self.assertEqual(sum(item["event_type"] == "worker.created" for item in events), 1)
        self.assertEqual(sum(item["event_type"] == "worker.reused" for item in events), 2)

    def test_assignment_extension_preserves_existing_worker_lineage(self):
        first_plan = compile_plan([
            semantic_task("create", "Create calculator.html", "create_file", "calculator.html"),
        ], complexity="simple")
        expanded_plan = compile_plan([
            semantic_task("create", "Create calculator.html", "create_file", "calculator.html"),
            semantic_task("inspect", "Inspect calculator.html", "read_file", "calculator.html",
                          depends=["create"], criteria=["calculator.html can be read."],
                          task_kind="review"),
        ])
        oid, orchestrator = self.start_run(first_plan)
        selected = self.select(oid, orchestrator, first_plan, "task-1")
        agent_id = selected["selection"]["selected_agent_id"]
        assignment = worker_assignment_map(expanded_plan)["task-2"]

        existing, previous_task_id = orchestrator._worker_agent_for_assignment(
            oid, assignment,
        )
        self.assertEqual(existing["id"], agent_id)
        self.assertEqual(previous_task_id, "task-1")
        assignment["generation"] = existing["config"]["worker_assignment"]["generation"]
        activated = orchestrator.agent_factory.activate_task(
            agent_id, orchestrator._factory_task_scope(
                expanded_plan["tasks"][1], expanded_plan,
            ),
            orchestration_id=oid, worker_assignment=assignment,
        )

        self.assertEqual(activated["agent"]["id"], agent_id)
        self.assertEqual(
            activated["agent"]["config"]["worker_assignment"]["task_ids"],
            ["task-1", "task-2"],
        )
        self.assertIn("read_file", activated["effective_tools"])
        self.assertNotIn("write_file", activated["effective_tools"])

    def test_seven_task_calculator_plan_keeps_one_worker_identity(self):
        path = "calculator/index.html"
        plan = compile_plan([
            semantic_task("structure", "Create the calculator HTML and layout", "create_file", path,
                          criteria=["calculator page has a labeled display and keypad."]),
            semantic_task("display", "Add the calculator display and clear control", "modify_file", path,
                          depends=["structure"], criteria=["display and clear control are present."]),
            semantic_task("keypad", "Add digit and decimal keypad buttons", "modify_file", path,
                          depends=["display"], criteria=["digit and decimal buttons are present."]),
            semantic_task("operators", "Add arithmetic operator buttons", "modify_file", path,
                          depends=["keypad"], criteria=["four arithmetic operators are present."]),
            semantic_task("arithmetic", "Implement calculator arithmetic and result display", "modify_file", path,
                          depends=["operators"], criteria=["operator buttons calculate and display a result."]),
            semantic_task("keyboard", "Add keyboard input and backspace behavior", "modify_file", path,
                          depends=["arithmetic"], criteria=["keyboard digits and backspace update the display."]),
            semantic_task("review", "Read and review the completed calculator", "read_file", path,
                          depends=["keyboard"], criteria=["calculator source can be read for review."],
                          task_kind="review"),
        ])
        self.assertEqual((plan["task_count"], plan["worker_count"]), (7, 1))
        self.assertEqual(plan["worker_assignments"][0]["worker_id"], "worker-1")

        oid, orchestrator, runtime = self.execute_plan(plan)
        selected_agent_ids = [item["agent_id"] for item in runtime.submissions]
        self.assertEqual(len(runtime.submissions), 7)
        self.assertEqual(len(set(selected_agent_ids)), 1)
        self.assertEqual(
            {item["state"] for item in self.store.get_execution_graph(oid)["nodes"]},
            {"success"},
        )
        events = self.store.get_orchestration(oid)["events"]
        self.assertEqual(sum(item["event_type"] == "worker.created" for item in events), 1)
        self.assertEqual(sum(item["event_type"] == "worker.reused" for item in events), 6)
        self.assertEqual(sum(item["event_type"] == "freya.agent_created" for item in events), 1)
        self.assertEqual(sum(item["event_type"] == "worker.task_started" for item in events), 7)
        self.assertEqual(sum(item["event_type"] == "worker.task_completed" for item in events), 7)
        orchestrator._archive_dynamic_agents(oid)
        completed = [item for item in self.store.get_orchestration(oid)["events"]
                     if item["event_type"] == "worker.completed"]
        self.assertEqual(len(completed), 1)
        completed_payload = json.loads(completed[0]["payload_json"])
        self.assertEqual(completed_payload["worker_id"], "worker-1")
        self.assertEqual(completed_payload["agent_id"], selected_agent_ids[0])

    def test_multi_worker_assignments_create_independent_identities(self):
        plan = compile_plan([
            semantic_task("frontend", "Create the frontend", "create_file", "frontend.html"),
            semantic_task("backend", "Create the backend", "create_file", "backend.py"),
        ], strategy="multi_worker", complexity="complex", reason=(
            "The frontend and backend have independent interfaces and can be implemented in parallel."))
        mapping = worker_assignment_map(plan)
        self.assertEqual(len({item["worker_id"] for item in mapping.values()}), 2)
        oid, _, runtime = self.execute_plan(plan, max_parallel_tasks=2)
        ids = {item["agent_id"] for item in runtime.submissions}
        self.assertEqual(len(ids), 2)
        self.assertEqual(runtime.max_active, 2)
        self.assertEqual(len(runtime.submissions), 2)
        events = self.store.get_orchestration(oid)["events"]
        self.assertEqual(sum(item["event_type"] == "worker.created" for item in events), 2)
        self.assertFalse(any(item["event_type"] == "worker.reused" for item in events))

    def test_dependencies_still_gate_tasks_in_one_assignment(self):
        plan = compile_plan([
            semantic_task("create", "Create app.html", "create_file", "app.html"),
            semantic_task("modify", "Update app.html", "modify_file", "app.html",
                          depends=["create"], criteria=["app.html is updated."]),
        ])
        graph = ExecutionGraph(plan)
        self.assertEqual([item["id"] for item in graph.ready_tasks()], ["task-1"])
        self.assertEqual(worker_assignment_map(plan)["task-1"]["worker_id"],
                         worker_assignment_map(plan)["task-2"]["worker_id"])

    def test_declared_empty_assignments_fail_closed(self):
        plan = compile_plan([
            semantic_task("create", "Create app.html", "create_file", "app.html"),
        ], complexity="simple")
        plan["worker_assignments"] = []
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            worker_assignment_map(plan)

    def test_retry_same_agent_reuses_the_assigned_worker(self):
        plan = compile_plan([
            semantic_task("create", "Create recovery.html", "create_file", "recovery.html"),
        ], complexity="simple")
        oid, orchestrator = self.start_run(plan)
        first = self.select(oid, orchestrator, plan, "task-1")
        agent_id = first["selection"]["selected_agent_id"]
        selection_id = first["selection_id"]
        runtime_task = self.store.create_task(agent_id, "first attempt", "workspace")
        self.store.update_task(runtime_task["id"], status="Success", result="first attempt completed")
        delegation_id = self.store.add_delegation(
            oid, agent_id, "first attempt", runtime_task["id"],
        )

        graph = ExecutionGraph(plan, self.store.get_execution_graph(oid)["nodes"])
        graph.mark_selected("task-1", agent_id, selection_id)
        graph.mark_running("task-1", runtime_task["id"], delegation_id)
        self.store.save_execution_graph(oid, graph.serialize())
        self.store.record_execution_attempt(
            oid, "task-1", selected_agent_id=agent_id, selection_id=selection_id,
            runtime_task_id=runtime_task["id"], delegation_id=delegation_id,
            attempt=1, prompt="first attempt",
        )
        graph = ExecutionGraph(plan, self.store.get_execution_graph(oid)["nodes"])
        graph.apply_runtime_status("task-1", "Success", result="first attempt completed")
        self.store.save_execution_graph(oid, graph.serialize())
        evaluation_id = str(uuid4())
        self.store.commit_evaluation(
            evaluation_id, oid, "task-1", runtime_task_id=runtime_task["id"],
            agent_id=agent_id, attempt=1, evaluator_version=EVALUATOR_VERSION,
            evaluation={"status": "needs_revision", "summary": "Retry the same Worker."},
            metrics={}, snapshot={}, context_truncated=False, deterministic=True,
        )
        self.store.commit_recovery_action(
            str(uuid4()), oid, "task-1", source_attempt=1,
            source_evaluation_id=evaluation_id,
            decision={"action": "retry_same_agent", "reason": "Use the current Worker.",
                      "instructions": "Correct the incomplete artifact.",
                      "exclude_agent_ids": [], "affected_task_ids": ["task-1"]},
            recovery_version=1, prompt="Correct the incomplete artifact.",
        )

        retry = self.select(oid, orchestrator, plan, "task-1")
        self.assertEqual(retry["selection"]["selected_agent_id"], agent_id)
        events = [json.loads(item["payload_json"])
                  for item in self.store.get_orchestration(oid)["events"]]
        reused = [item for item in events if item["event_type"] == "worker.reused"]
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0]["previous_task_id"], "task-1")
        self.assertEqual(reused[0]["current_task_id"], "task-1")

        self.store.delete_agent(agent_id)
        recreated = self.select(oid, orchestrator, plan, "task-1")
        recreated_agent_id = recreated["selection"]["selected_agent_id"]
        self.assertNotEqual(recreated_agent_id, agent_id)
        recreated_events = [json.loads(item["payload_json"])
                            for item in self.store.get_orchestration(oid)["events"]]
        self.assertEqual(sum(item["event_type"] == "worker.recreated"
                             for item in recreated_events), 1)
        created_generations = [item["worker_generation"] for item in recreated_events
                               if item["event_type"] == "worker.created"]
        self.assertEqual(created_generations, [1, 2])


if __name__ == "__main__":
    unittest.main()
