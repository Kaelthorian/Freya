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
from control_center.planner import PLAN_SCHEMA_VERSION, validate_plan
from control_center.evaluator import EVALUATOR_VERSION, Evaluator
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
        self.workspace = Path(store.path).parent / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.submissions = []
        self.max_active = 0

    def submit(self, agent_id, prompt, workspace_path=None, runtime_context=None):
        task = self.store.create_task(
            agent_id, prompt, workspace_path or str(self.workspace),
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

    def finish_active(self, seconds=None, status="Success"):
        for task in self.store.list_tasks(limit=10000):
            if task["status"] in {"Queued", "Running", "WaitingForApproval", "Paused"}:
                if status == "Failed":
                    self.store.update_task(task["id"], status=status,
                                           error="Controlled Runtime failure.")
                    continue
                path = task["id"] + ".txt"
                self.store.update_task(task["id"], status=status, result={
                    "summary": "Controlled fixture completed.",
                    "actions": [{"tool": "read_file", "success": True}],
                    "artifacts": [{"path": path, "status": "observed"}],
                    "workspace_diffs": [{"path": path, "diff": "fixture change"}],
                    "already_satisfied_candidate": {"artifact_observations": [{
                        "path": path, "content_sha256": "fixture-hash",
                    }]},
                }, verification={
                    "requested": False, "attempted": False, "passed": False,
                    "failed": False, "unavailable": False,
                    "evidence": [{"type": "file_readback", "path": path,
                                  "content": "observed by " + task["id"],
                                  "supports_acceptance_criteria": [
                                      "The completed app is readable from the shared workspace.",
                                  ]}],
                })


class RecordingEvaluator:
    def __init__(self, status_by_criterion=None, status_by_call=None):
        self.status_by_criterion = status_by_criterion or {}
        self.status_by_call = list(status_by_call or [])
        self.calls = []
        self.events = []
        self.metrics = {}
        self.last_context = {}

    def evaluate(self, *, planned_task, runtime_task, execution_node):
        self.calls.append({
            "planned_task": planned_task, "runtime_task": runtime_task,
            "execution_node": execution_node,
        })
        per_call = self.status_by_call[min(len(self.calls) - 1,
                                            len(self.status_by_call) - 1)] if self.status_by_call else {}
        per_call = per_call or {}
        criteria = []
        for item in planned_task["acceptance_criteria"]:
            criterion = item["criterion"]
            status = per_call.get(criterion, self.status_by_criterion.get(criterion, "satisfied"))
            criteria.append({
                "criterion": criterion, "status": status,
                "reason": "Controlled criterion decision.",
                "evidence": ([f"observed: {criterion}"] if status == "satisfied" else []),
            })
        statuses = {item["status"] for item in criteria}
        status = ("accepted" if statuses <= {"satisfied"} else
                  "blocked" if "unknown" in statuses else "needs_revision")
        self.metrics = {"model_calls": 1, "prompt_tokens": 10,
                        "generated_tokens": 5, "total_tokens": 15,
                        "duration_seconds": 0.01}
        self.last_context = {"worker_evidence_pool": runtime_task["verification"]["evidence"]}
        return {
            "status": status, "confidence": 1.0 if status == "accepted" else 0.0,
            "summary": "Controlled Worker Assignment evaluation.",
            "criteria": criteria, "issues": [],
            "missing_evidence": ([item["criterion"] for item in criteria
                                  if item["status"] == "unknown"]),
            "recommended_action": {"accepted": "accept", "blocked": "gather_evidence",
                                   "needs_revision": "revise"}[status],
            "metrics": dict(self.metrics), "context_truncated": False,
            "deterministic": True,
            "context_snapshot": {
                "worker_evidence_pool": runtime_task["verification"]["evidence"],
                "worker_context": planned_task["worker_context"],
                "runtime_task": runtime_task,
            },
            "events": [],
        }


class FailRecovery:
    def __init__(self):
        self.sources = []

    def decide(self, *, planned_task, **kwargs):
        self.sources.append(planned_task["id"])
        return {"action": "fail", "reason": "Controlled recovery stop.",
                "instructions": "", "exclude_agent_ids": [],
                "affected_task_ids": [planned_task["id"]]}


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


def compile_plan(tasks, *, strategy="single_worker", complexity="multi_step", reason=None,
                 global_criteria=None):
    return compile_semantic_plan({
        "summary": "Build a staged artifact.",
        "task_complexity": complexity,
        "execution_strategy": strategy,
        "decomposition_reason": reason or "One Worker preserves the shared implementation context across ordered steps.",
        "tasks": tasks,
        "success_criteria": list(global_criteria or []),
        "unsupported_requirements": [],
    }, deterministic_task_spec("Build a staged application with verified artifacts."))


class WorkerAssignmentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def start_run(self, plan, runtime=None, wait=None, evaluator=None, recovery=None):
        run = self.store.create_orchestration("Build a staged application")
        self.store.transition_orchestration(run["id"], "Queued", "Planning")
        self.store.save_orchestration_plan(run["id"], plan, PLAN_SCHEMA_VERSION)
        graph = ExecutionGraph(plan)
        self.store.initialize_execution_graph(run["id"], graph.serialize())
        self.store.transition_orchestration(run["id"], "Planned", "Running")
        orchestrator = Orchestrator(
            self.store, runtime or IdleRuntime(), wait=wait,
            evaluator=evaluator, recovery=recovery,
            config={"max_wallclock_seconds": 10},
        )
        return run["id"], orchestrator

    def execute_plan(self, plan, *, max_parallel_tasks=4, status_by_criterion=None,
                     status_by_call=None, recovery=None, finish_status="Success"):
        runtime = ControlledRuntime(self.store)
        evaluator = RecordingEvaluator(status_by_criterion, status_by_call)
        oid, orchestrator = self.start_run(
            plan, runtime, lambda seconds: runtime.finish_active(status=finish_status),
            evaluator=evaluator, recovery=recovery,
        )
        orchestrator.config["max_parallel_tasks"] = max_parallel_tasks
        orchestrator._complete_or_integrate = lambda *args, **kwargs: None
        run = self.store.get_orchestration(oid)
        orchestrator._run_graph(
            oid, run, orchestrator.clock() + 10,
            operational_prompt="Build the staged calculator.",
        )
        return oid, orchestrator, runtime, evaluator

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

        oid, orchestrator, runtime, evaluator = self.execute_plan(plan)
        selected_agent_ids = [item["agent_id"] for item in runtime.submissions]
        self.assertEqual(len(runtime.submissions), 7)
        self.assertEqual(len(set(selected_agent_ids)), 1)
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(evaluator.calls[0]["execution_node"]["assigned_task_ids"],
                         [f"task-{index}" for index in range(1, 8)])
        evidence_sources = {item["source_task_id"] for item in
                            evaluator.calls[0]["runtime_task"]["verification"]["evidence"]}
        self.assertEqual(evidence_sources, {f"task-{index}" for index in range(1, 8)})
        task_context = {item["task_id"]: item for item in
                        evaluator.calls[0]["planned_task"]["worker_context"]["tasks"]}
        self.assertEqual(task_context["task-7"]["success_criteria"][0]["origin_task_id"],
                         "task-7")
        evaluations = self.store.list_evaluations(oid)
        self.assertEqual(len(evaluations), 1)
        self.assertEqual(evaluations[0]["worker_id"], "worker-1")
        self.assertEqual(evaluations[0]["evaluated_task_ids"], [f"task-{i}" for i in range(1, 8)])
        self.assertEqual(
            {item["state"] for item in self.store.get_execution_graph(oid)["nodes"]},
            {"runtime_success"},
        )
        events = self.store.get_orchestration(oid)["events"]
        self.assertEqual(sum(item["event_type"] == "worker.created" for item in events), 1)
        self.assertEqual(sum(item["event_type"] == "worker.reused" for item in events), 6)
        self.assertEqual(sum(item["event_type"] == "freya.agent_created" for item in events), 1)
        self.assertEqual(sum(item["event_type"] == "worker.task_started" for item in events), 7)
        self.assertEqual(sum(item["event_type"] == "worker.task_completed" for item in events), 7)
        self.assertEqual(sum(item["event_type"] == "worker.evaluation_started" for item in events), 1)
        self.assertEqual(sum(item["event_type"] == "worker.evaluation.completed" for item in events), 1)
        self.assertEqual(self.store.list_workers(oid)[0]["status"], "accepted")
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
        oid, _, runtime, evaluator = self.execute_plan(plan, max_parallel_tasks=2)
        ids = {item["agent_id"] for item in runtime.submissions}
        self.assertEqual(len(ids), 2)
        self.assertEqual(runtime.max_active, 2)
        self.assertEqual(len(runtime.submissions), 2)
        self.assertEqual(len(evaluator.calls), 2)
        evaluations = self.store.list_evaluations(oid)
        self.assertEqual(len(evaluations), 2)
        self.assertEqual({item["worker_id"] for item in evaluations},
                         {item["worker_id"] for item in plan["worker_assignments"]})
        self.assertTrue(all(len(item["evaluated_task_ids"]) == 1 for item in evaluations))
        events = self.store.get_orchestration(oid)["events"]
        self.assertEqual(sum(item["event_type"] == "worker.created" for item in events), 2)
        self.assertFalse(any(item["event_type"] == "worker.reused" for item in events))

    def test_worker_evaluation_receives_other_assigned_tasks_evidence_with_origin(self):
        criterion = "The completed app is readable from the shared workspace."
        plan = compile_plan([
            semantic_task("create", "Create app.html", "create_file", "app.html",
                          criteria=["app.html exists."]),
            semantic_task("inspect", "Read app.html", "read_file", "app.html",
                          depends=["create"], criteria=[criterion], task_kind="review"),
        ])
        oid, _, _, evaluator = self.execute_plan(plan)
        call = evaluator.calls[0]
        evidence = call["runtime_task"]["verification"]["evidence"]
        self.assertEqual({item["source_task_id"] for item in evidence}, {"task-1", "task-2"})
        self.assertTrue(any(item["source_task_id"] == "task-1"
                            and item["source_runtime_task_id"]
                            and item["worker_id"] == "worker-1"
                            for item in evidence))
        criteria = call["planned_task"]["worker_context"]["tasks"]
        inspect = next(item for item in criteria if item["task_id"] == "task-2")
        self.assertEqual(inspect["success_criteria"][0]["criterion"], criterion)
        self.assertEqual(inspect["success_criteria"][0]["origin_task_id"], "task-2")

        observed_context = {}

        def review_with_fixture(prompt, context):
            observed_context.update(context)
            return {"criteria": [{
                "criterion": item, "status": "unknown",
                "reason": "The controlled fixture does not decide semantics.",
                "evidence": [], "confidence": 0.5,
            } for item in context["planned_task"]["success_criteria"]]}

        Evaluator(review_with_fixture).evaluate(
            planned_task=call["planned_task"], runtime_task=call["runtime_task"],
            execution_node=call["execution_node"],
        )
        criterion_evidence = next(
            item for item in observed_context["evidence_by_criterion"]
            if item["criterion"] == criterion
        )
        source_evidence = next(item for item in criterion_evidence["evidence"]
                               if item["source_task_id"] == "task-1")
        self.assertEqual(source_evidence["worker_id"], "worker-1")
        self.assertEqual(
            source_evidence["content"],
            "observed by " + source_evidence["source_runtime_task_id"],
        )

    def test_runtime_failure_prevents_worker_semantic_evaluation(self):
        plan = compile_plan([
            semantic_task("create", "Create app.html", "create_file", "app.html"),
            semantic_task("inspect", "Read app.html", "read_file", "app.html",
                          depends=["create"], task_kind="review"),
        ])
        oid, _, runtime, evaluator = self.execute_plan(plan, finish_status="Failed")
        self.assertEqual(len(runtime.submissions), 1)
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(self.store.list_workers(oid)[0]["status"], "failed")
        events = self.store.get_orchestration(oid)["events"]
        self.assertFalse(any(item["event_type"] == "worker.evaluation_started" for item in events))

    def test_failed_worker_criterion_recovers_its_originating_task(self):
        failed_criterion = "The read task criterion is satisfied."
        plan = compile_plan([
            semantic_task("create", "Create app.html", "create_file", "app.html",
                          criteria=["app.html exists."]),
            semantic_task("inspect", "Read app.html", "read_file", "app.html",
                          depends=["create"], criteria=[failed_criterion], task_kind="review"),
        ])
        recovery = FailRecovery()
        oid, _, _, evaluator = self.execute_plan(
            plan, status_by_criterion={failed_criterion: "unsatisfied"}, recovery=recovery,
        )
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(recovery.sources, ["task-2"])
        recovery_events = [item for item in self.store.get_orchestration(oid)["events"]
                           if item["event_type"] == "worker.recovery_started"]
        self.assertEqual(len(recovery_events), 1)
        payload = json.loads(recovery_events[0]["payload_json"])
        self.assertEqual(payload["failed_task_ids"], ["task-2"])

    def test_missing_evidence_retries_only_a_read_only_task_in_the_same_worker(self):
        criterion = "The completed app can be read back from the workspace."
        plan = compile_plan([
            semantic_task("create", "Create app.html", "create_file", "app.html",
                          criteria=[criterion]),
            semantic_task("inspect", "Read app.html and report its current content", "read_file",
                          "app.html", depends=["create"],
                          criteria=["The observation reports the current app content."],
                          task_kind="review"),
        ])
        oid, _, runtime, evaluator = self.execute_plan(
            plan, status_by_call=[{criterion: "unknown"}, {}],
        )
        self.assertEqual(len(runtime.submissions), 3)
        self.assertEqual(len(evaluator.calls), 2)
        self.assertEqual(evaluator.calls[0]["execution_node"]["worker_id"], "worker-1")
        self.assertEqual(evaluator.calls[1]["execution_node"]["worker_id"], "worker-1")
        retries = [item for item in self.store.get_orchestration(oid)["events"]
                   if item["event_type"] == "worker.recovery_completed"]
        self.assertEqual(len(retries), 1)
        retry_payload = json.loads(retries[0]["payload_json"])
        self.assertEqual(retry_payload["action"], "gather_evidence")
        self.assertEqual(retry_payload["affected_task_ids"], ["task-1", "task-2"])
        nodes = {item["plan_task_id"]: item
                 for item in self.store.get_execution_graph(oid)["nodes"]}
        self.assertEqual(nodes["task-1"]["state"], "runtime_success")
        self.assertEqual(nodes["task-2"]["state"], "runtime_success")

    def test_single_worker_global_proof_can_be_reused_but_multi_worker_cannot(self):
        global_criterion = "The complete app is present and validated."
        single_plan = compile_plan([
            semantic_task("create", "Create app.html", "create_file", "app.html",
                          criteria=[global_criterion]),
        ], complexity="simple", global_criteria=[global_criterion])
        oid, orchestrator, _, _ = self.execute_plan(single_plan)
        evaluation = self.store.list_evaluations(oid)[0]
        run = self.store.get_orchestration(oid)
        from control_center.integration import criterion_key
        prepared = {
            "snapshot": {"active_task_ids": ["task-1"]},
            "context": {"global_success_criteria": [global_criterion]},
            "proof_refs_by_criterion": {criterion_key(global_criterion): [
                f"evidence:{evaluation['id']}:2",
            ]},
            "context_truncated": False,
        }
        reused = orchestrator._single_worker_global_outcome(oid, run, prepared)
        self.assertIsNotNone(reused)
        self.assertEqual(reused["reused_worker_evaluation"], evaluation["id"])
        self.assertEqual(reused["metrics"]["model_calls"], 0)

        multi_plan = compile_plan([
            semantic_task("frontend", "Create the frontend", "create_file", "frontend.html"),
            semantic_task("backend", "Create the backend", "create_file", "backend.py"),
        ], strategy="multi_worker", complexity="complex", reason=(
            "The frontend and backend have independent interfaces and can be implemented in parallel."),
            global_criteria=[global_criterion])
        multi_oid, multi_orchestrator, _, _ = self.execute_plan(multi_plan, max_parallel_tasks=2)
        multi_run = self.store.get_orchestration(multi_oid)
        multi_prepared = {**prepared, "snapshot": {"active_task_ids": [
            item["id"] for item in multi_plan["tasks"]
        ]}}
        self.assertIsNone(multi_orchestrator._single_worker_global_outcome(
            multi_oid, multi_run, multi_prepared,
        ))

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
        graph.mark_selected("task-1", "agent", "selection")
        graph.mark_running("task-1", "runtime", "delegation")
        graph.apply_runtime_status("task-1", "Success")
        transitions = graph.refresh_dependencies()
        self.assertEqual(graph.node("task-1")["state"], "runtime_success")
        self.assertEqual(transitions, [{"task_id": "task-2", "from": "pending", "to": "ready"}])
        self.assertEqual([item["id"] for item in graph.ready_tasks()], ["task-2"])

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
        self.store.set_worker_status(
            oid, "worker-1", "evaluating", agent_id=agent_id,
            evaluation_id=evaluation_id,
        )
        self.store.commit_worker_evaluation(
            evaluation_id, oid, "worker-1", ["task-1"], "task-1",
            failed_task_id="task-1", runtime_task_id=runtime_task["id"],
            agent_id=agent_id, attempt=1, evaluator_version=EVALUATOR_VERSION,
            evaluation={"status": "needs_revision", "summary": "Retry the same Worker.",
                        "criteria": [{"criterion_id": "LC-1", "origin_task_id": "task-1",
                                      "origin_type": "task", "criterion": "recovery.html exists.",
                                      "status": "unsatisfied", "reason": "missing",
                                      "evidence": []}]},
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
