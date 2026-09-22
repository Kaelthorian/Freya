import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from control_center.agent_selector import AgentSelector
from control_center.api import Application
from control_center.config import normalize_agent
from control_center.execution_graph import ExecutionGraph
from control_center.evaluator import Evaluator
from control_center.orchestrator import Orchestrator
from control_center.planner import Planner
from control_center.storage import Store


def planned_task(task_id, depends_on=None):
    return {
        "id": task_id,
        "objective": task_id,
        "description": "Execute " + task_id,
        "depends_on": list(depends_on or []),
        "required_capabilities": [],
        "preferred_skills": [],
        "success_criteria": [task_id + " completes"],
    }


def execution_plan(tasks):
    return {
        "goal": "Execute a deterministic graph",
        "summary": "Exercise dependency scheduling.",
        "complexity": "simple" if len(tasks) == 1 else "multi_step",
        "tasks": tasks,
        "success_criteria": ["The graph reaches a terminal state."],
    }


def evaluation_decision(criteria, status="accepted"):
    return {
        "status": status, "confidence": 0.9, "summary": status + " by test evaluator.",
        "criteria": [{
            "criterion": item,
            "status": "satisfied" if status == "accepted" else "unsatisfied",
            "reason": "Test evidence was evaluated.", "evidence": ["fixture"],
        } for item in criteria],
        "issues": [] if status == "accepted" else ["Contradictory evidence."],
        "missing_evidence": [],
        "recommended_action": "accept" if status == "accepted" else "reject",
    }


class ExecutionGraphTests(unittest.TestCase):
    def chain(self):
        return execution_plan([
            planned_task("a"), planned_task("b", ["a"]), planned_task("c", ["b"]),
        ])

    def test_initial_states_follow_dependencies(self):
        graph = ExecutionGraph(self.chain())
        self.assertEqual([node["state"] for node in graph.serialize()],
                         ["ready", "pending", "pending"])

    def test_plan_snapshot_is_defensively_copied(self):
        source = self.chain()
        graph = ExecutionGraph(source)
        source["tasks"][0]["objective"] = "changed"
        self.assertEqual(graph.tasks["a"]["objective"], "a")

    def test_ready_tasks_preserve_original_plan_order(self):
        graph = ExecutionGraph(execution_plan([
            planned_task("z"), planned_task("a"), planned_task("m"),
        ]))
        self.assertEqual([task["id"] for task in graph.ready_tasks()], ["z", "a", "m"])

    def test_selection_is_persisted_once(self):
        graph = ExecutionGraph(execution_plan([planned_task("a")]))
        self.assertTrue(graph.mark_selected("a", "agent", "selection"))
        self.assertFalse(graph.mark_selected("a", "agent", "selection"))
        with self.assertRaisesRegex(ValueError, "only be selected once"):
            graph.mark_selected("a", "other", "other-selection")

    def test_dispatch_requires_selection(self):
        graph = ExecutionGraph(execution_plan([planned_task("a")]))
        with self.assertRaisesRegex(ValueError, "selected before dispatch"):
            graph.mark_running("a", "runtime", "delegation")

    def test_dispatch_increments_attempt_once(self):
        graph = ExecutionGraph(execution_plan([planned_task("a")]))
        graph.mark_selected("a", "agent", "selection")
        graph.mark_running("a", "runtime", "delegation")
        self.assertEqual(graph.node("a")["attempt"], 1)
        with self.assertRaisesRegex(ValueError, "ready and selected"):
            graph.mark_running("a", "runtime-2", "delegation-2")

    def test_success_unlocks_direct_child(self):
        graph = ExecutionGraph(self.chain())
        graph.mark_selected("a", "agent", "selection")
        graph.mark_running("a", "runtime", "delegation")
        graph.apply_runtime_status("a", "Success", result="done")
        self.assertEqual(graph.node("a")["state"], "evaluating")
        self.assertEqual(graph.refresh_dependencies(), [])
        graph.apply_evaluation("a", "evaluation", "accepted", "accepted")
        self.assertEqual(graph.refresh_dependencies(),
                         [{"task_id": "b", "from": "pending", "to": "ready"}])

    def test_join_waits_for_every_dependency(self):
        graph = ExecutionGraph(execution_plan([
            planned_task("a"), planned_task("b"), planned_task("join", ["a", "b"]),
        ]))
        for task_id in ("a", "b"):
            graph.mark_selected(task_id, task_id, "s-" + task_id)
            graph.mark_running(task_id, "r-" + task_id, "d-" + task_id)
        graph.apply_runtime_status("a", "Success")
        graph.apply_evaluation("a", "e-a", "accepted", "accepted")
        self.assertEqual(graph.refresh_dependencies(), [])
        graph.apply_runtime_status("b", "Success")
        graph.apply_evaluation("b", "e-b", "accepted", "accepted")
        graph.refresh_dependencies()
        self.assertEqual(graph.node("join")["state"], "ready")

    def test_nonaccepted_evaluation_waits_for_recovery_without_blocking_descendants(self):
        graph = ExecutionGraph(self.chain())
        graph.mark_selected("a", "agent", "selection")
        graph.mark_running("a", "runtime", "delegation")
        graph.apply_runtime_status("a", "Success", result="claim")
        graph.apply_evaluation("a", "evaluation", "rejected", "Evidence contradicts claim.")
        graph.refresh_dependencies()
        self.assertEqual(graph.node("a")["state"], "recovery_pending")
        self.assertEqual(graph.node("a")["evaluation_status"], "rejected")
        self.assertEqual(graph.node("b")["state"], "pending")

    def test_evaluation_is_applied_only_once(self):
        graph = ExecutionGraph(execution_plan([planned_task("a")]))
        graph.mark_selected("a", "agent", "selection")
        graph.mark_running("a", "runtime", "delegation")
        graph.apply_runtime_status("a", "Success", result="done")
        self.assertTrue(graph.apply_evaluation("a", "evaluation", "accepted", "accepted"))
        self.assertFalse(graph.apply_evaluation("a", "evaluation", "accepted", "accepted"))

    def test_failure_blocks_all_descendants(self):
        graph = ExecutionGraph(self.chain())
        graph.mark_failed("a", "boom")
        transitions = graph.refresh_dependencies()
        self.assertEqual([item["task_id"] for item in transitions], ["b", "c"])
        self.assertEqual([graph.node(item)["state"] for item in ("b", "c")],
                         ["blocked", "blocked"])

    def test_failure_does_not_block_independent_branch(self):
        graph = ExecutionGraph(execution_plan([
            planned_task("failed"), planned_task("child", ["failed"]),
            planned_task("independent"),
        ]))
        graph.mark_failed("failed", "boom")
        graph.refresh_dependencies()
        self.assertEqual(graph.node("child")["state"], "blocked")
        self.assertEqual(graph.node("independent")["state"], "ready")

    def test_waiting_for_approval_can_return_to_running(self):
        graph = ExecutionGraph(execution_plan([planned_task("a")]))
        graph.mark_selected("a", "agent", "selection")
        graph.mark_running("a", "runtime", "delegation")
        graph.apply_runtime_status("a", "WaitingForApproval")
        self.assertEqual(graph.node("a")["state"], "waiting_for_approval")
        graph.apply_runtime_status("a", "Running")
        self.assertEqual(graph.node("a")["state"], "running")

    def test_runtime_paused_is_nonterminal_running_with_reason(self):
        graph = ExecutionGraph(execution_plan([planned_task("a")]))
        graph.mark_selected("a", "agent", "selection")
        graph.mark_running("a", "runtime", "delegation")
        graph.apply_runtime_status("a", "Paused")
        self.assertEqual(graph.node("a")["state"], "running")
        self.assertIn("paused", graph.node("a")["waiting_reason"])

    def test_terminal_state_cannot_be_reopened(self):
        graph = ExecutionGraph(execution_plan([planned_task("a")]))
        graph.mark_failed("a", "boom")
        with self.assertRaisesRegex(ValueError, "final"):
            graph.mark_selected("a", "agent", "selection")

    def test_cancel_marks_only_nonterminal_nodes(self):
        graph = ExecutionGraph(execution_plan([planned_task("a"), planned_task("b")]))
        graph.mark_failed("a", "boom")
        self.assertEqual(graph.cancel_nonterminal(reason="cancel"), ["b"])
        self.assertEqual(graph.node("a")["state"], "failed")
        self.assertEqual(graph.node("b")["state"], "cancelled")

    def test_summary_reports_completion_and_failures(self):
        graph = ExecutionGraph(execution_plan([planned_task("a"), planned_task("b")]))
        graph.mark_failed("a", "boom")
        graph.cancel_nonterminal()
        summary = graph.summary()
        self.assertTrue(summary["complete"])
        self.assertEqual((summary["failed"], summary["cancelled"]), (1, 1))

    def test_loading_requires_exactly_one_node_per_task(self):
        graph = ExecutionGraph(self.chain())
        with self.assertRaisesRegex(ValueError, "exactly once"):
            ExecutionGraph(self.chain(), graph.serialize()[:-1])


class MappingSelector:
    version = 1

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def select_agent(self, task, agents, context=None):
        self.calls.append(task["id"])
        agent_id = self.mapping.get(task["id"])
        return {
            "task_id": task["id"],
            "status": "selected" if agent_id else "no_eligible_agent",
            "selected_agent_id": agent_id,
            "score": 100 if agent_id else None,
            "classification": "eligible" if agent_id else "ineligible",
            "approval_required": False,
            "selector_version": 1,
            "reasons": ["test mapping"] if agent_id else [],
            "warnings": [] if agent_id else ["No mapped agent."],
            "candidates": [],
        }


class ControlledRuntime:
    def __init__(self, store, initial=None):
        self.store = store
        self.initial = initial or {}
        self.submissions = []
        self.cancelled = []

    def submit(self, agent_id, objective, workspace_path=None):
        task = self.store.create_task(agent_id, objective, workspace_path or "workspace")
        marker = "DELEGATED PLAN STEP:\n"
        label = objective.split(marker, 1)[1].split("\n\n", 1)[0] if marker in objective else objective
        status = self.initial.get(label, self.initial.get(objective, "Running"))
        task = self.store.update_task(task["id"], status=status)
        self.submissions.append((label, agent_id, task["id"]))
        return task

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        task = self.store.get_task(task_id)
        if task["status"] not in {"Success", "Failed", "Cancelled"}:
            task = self.store.update_task(task_id, status="Cancelled", error="cancelled")
        return task

    def finish_active(self, outcomes=None):
        outcomes = outcomes or {}
        active = []
        for task in self.store.list_tasks(limit=10000):
            if task["status"] in {"Queued", "Running", "WaitingForApproval", "Paused"}:
                active.append(task)
                marker = "DELEGATED PLAN STEP:\n"
                label = task["prompt"].split(marker, 1)[1].split("\n\n", 1)[0] if marker in task["prompt"] else task["prompt"]
                status = outcomes.get(label, outcomes.get(task["prompt"], "Success"))
                fields = {"status": status, "result": task["prompt"] + " result"}
                if status == "Success":
                    fields["verification"] = {
                        "requested": True, "attempted": True, "passed": True,
                        "failed": False, "unavailable": False,
                        "skipped_with_reason": "", "evidence": [{
                            "check": "controlled fixture verification",
                            "status": "passed", "output": "verified",
                        }],
                    }
                if status == "Failed":
                    fields["error"] = task["prompt"] + " failed"
                self.store.update_task(task["id"], **fields)
        return active


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, name):
        return self.store.create_agent(normalize_agent({"name": name, "role": "Engineer"}))

    def run_graph(self, plan, mapping, runtime, wait, evaluator=None, **config):
        run = self.store.create_orchestration("Execute graph")
        orchestrator = Orchestrator(
            self.store, runtime, planner=Planner(lambda prompt, context: json.dumps(plan)),
            selector=MappingSelector(mapping), wait=wait,
            evaluator=evaluator,
            config={"max_wallclock_seconds": 10, **config},
        )
        orchestrator._run(run["id"])
        return self.store.get_orchestration(run["id"])

    def test_sequential_chain_dispatches_in_dependency_order(self):
        agent = self.agent("One")
        runtime = ControlledRuntime(self.store)
        final = self.run_graph(
            execution_plan([planned_task("a"), planned_task("b", ["a"])]),
            {"a": agent["id"], "b": agent["id"]}, runtime,
            lambda seconds: runtime.finish_active(),
        )
        self.assertEqual(final["status"], "Success")
        self.assertEqual([item[0] for item in runtime.submissions], ["a", "b"])

    def test_parallel_branches_and_join(self):
        agents = [self.agent(name) for name in ("A", "B", "Join")]
        runtime = ControlledRuntime(self.store)
        active_counts = []

        def advance(seconds):
            active_counts.append(len(runtime.finish_active()))

        final = self.run_graph(
            execution_plan([
                planned_task("a"), planned_task("b"), planned_task("join", ["a", "b"]),
            ]),
            {"a": agents[0]["id"], "b": agents[1]["id"], "join": agents[2]["id"]},
            runtime, advance, max_parallel_tasks=2,
        )
        self.assertEqual(final["status"], "Success")
        self.assertIn(2, active_counts)
        self.assertEqual(runtime.submissions[-1][0], "join")

    def test_real_selector_distributes_equivalent_parallel_work(self):
        agents = [self.agent(name) for name in ("Equivalent 1", "Equivalent 2")]
        runtime = ControlledRuntime(self.store)
        active_counts = []

        def advance(seconds):
            active_counts.append(len(runtime.finish_active()))

        run = self.store.create_orchestration("Use both equivalent agents")
        plan = execution_plan([planned_task("a"), planned_task("b")])
        orchestrator = Orchestrator(
            self.store, runtime, planner=Planner(lambda prompt, context: json.dumps(plan)),
            selector=AgentSelector(), wait=advance,
            config={"max_wallclock_seconds": 10, "max_parallel_tasks": 2},
        )
        orchestrator._run(run["id"])
        final = self.store.get_orchestration(run["id"])

        self.assertEqual(final["status"], "Success")
        self.assertIn(2, active_counts)
        selected_ids = {item[1] for item in runtime.submissions}
        manual_ids = {agent["id"] for agent in agents}
        self.assertEqual(len(selected_ids), 2)
        self.assertTrue(selected_ids.isdisjoint(manual_ids))
        self.assertEqual({agent["id"] for agent in self.store.list_agents()}, manual_ids)
        self.assertEqual(len(final["selections"]), 2)
        self.assertEqual(sum(event["event_type"] == "freya.agent_created"
                             for event in final["events"]), 2)

    def test_failure_blocks_descendant_but_independent_branch_completes(self):
        agents = [self.agent(name) for name in ("Bad", "Independent")]
        runtime = ControlledRuntime(self.store)
        final = self.run_graph(
            execution_plan([
                planned_task("bad"), planned_task("child", ["bad"]),
                planned_task("independent"),
            ]),
            {"bad": agents[0]["id"], "child": agents[0]["id"],
             "independent": agents[1]["id"]}, runtime,
            lambda seconds: runtime.finish_active({"bad": "Failed"}),
            max_parallel_tasks=2,
        )
        graph = self.store.get_execution_graph(final["id"])
        states = {node["plan_task_id"]: node["state"] for node in graph["nodes"]}
        self.assertEqual(final["status"], "Failed")
        self.assertEqual(states, {"bad": "failed", "child": "blocked", "independent": "success"})
        self.assertEqual([item[0] for item in runtime.submissions], ["bad", "independent"])

    def test_failed_selection_runs_log_only_failure_analysis(self):
        runtime = ControlledRuntime(self.store)
        final = self.run_graph(
            execution_plan([planned_task("unavailable")]),
            {"unavailable": None}, runtime, lambda seconds: None,
        )
        self.assertEqual(final["status"], "Failed")
        self.assertEqual(runtime.submissions, [])
        self.assertIn("No mapped agent", final["error"])
        self.assertIn("Failure diagnosis", final["response"])
        events = {event["event_type"]: event for event in final["events"]}
        self.assertIn("freya.failure_analysis.started", events)
        self.assertIn("freya.failure_analysis.completed", events)
        diagnosis = json.loads(events["freya.failure_analysis.completed"]["payload_json"])
        self.assertEqual(diagnosis["analysis_mode"], "deterministic")
        self.assertTrue(diagnosis["evidence_log_ids"])

    def test_runtime_failure_cause_is_added_to_persisted_diagnostic_logs(self):
        agent = self.agent("Fails")
        runtime = ControlledRuntime(self.store)
        final = self.run_graph(
            execution_plan([planned_task("broken")]), {"broken": agent["id"]}, runtime,
            lambda seconds: runtime.finish_active({"broken": "Failed"}),
        )
        self.assertEqual(final["status"], "Failed")
        self.assertIn("Planned task entered failed", final["error"])
        analysis_event = next(event for event in final["events"]
                              if event["event_type"] == "freya.failure_analysis.completed")
        analysis = json.loads(analysis_event["payload_json"])
        self.assertIn("Planned task entered failed", analysis["cause"])
        self.assertTrue(all(str(item).startswith(("orchestration:", "task:"))
                            for item in analysis["evidence_log_ids"]))

    def test_waiting_for_approval_is_nonterminal(self):
        agent = self.agent("Approval")
        runtime = ControlledRuntime(self.store, {"approval": "WaitingForApproval"})
        waits = []

        def advance(seconds):
            waits.append(True)
            tasks = self.store.list_tasks(limit=10)
            if len(waits) == 2:
                self.assertEqual(
                    self.store.list_orchestrations(1)[0]["graph_summary"]["counts"]
                    ["waiting_for_approval"], 1,
                )
                self.store.update_task(tasks[0]["id"], status="Running")
            elif len(waits) >= 3:
                runtime.finish_active()

        final = self.run_graph(
            execution_plan([planned_task("approval")]), {"approval": agent["id"]},
            runtime, advance,
        )
        self.assertEqual(final["status"], "Success")
        self.assertIn("freya.task.waiting_for_approval",
                      [event["event_type"] for event in final["events"]])

    def test_paused_selected_agent_waits_without_duplicate_submit(self):
        agent = self.agent("Paused")
        self.store.set_agent_state(agent["id"], "Paused")
        runtime = ControlledRuntime(self.store)
        waits = []

        def advance(seconds):
            waits.append(True)
            if len(waits) == 1:
                graph = self.store.get_execution_graph(
                    self.store.list_orchestrations(1)[0]["id"],
                )
                self.assertIn("Paused", graph["nodes"][0]["waiting_reason"])
                self.assertEqual(runtime.submissions, [])
                self.store.set_agent_state(agent["id"], "Idle")
            else:
                runtime.finish_active()

        final = self.run_graph(
            execution_plan([planned_task("paused")]), {"paused": agent["id"]},
            runtime, advance,
        )
        self.assertEqual(final["status"], "Success")
        self.assertEqual(len(runtime.submissions), 1)
        self.assertEqual(len(final["selections"]), 1)

    def test_offline_after_selection_waits_then_dispatches_exactly_once(self):
        agent = self.agent("Offline")
        runtime = ControlledRuntime(self.store)
        original_save = self.store.save_agent_selection

        def save_then_offline(oid, selection):
            selection_id = original_save(oid, selection)
            if selection_id:
                self.store.set_agent_state(agent["id"], "Offline")
                self.store.save_agent_selection = original_save
            return selection_id

        self.store.save_agent_selection = save_then_offline
        waits = []

        def advance(seconds):
            waits.append(True)
            graph = self.store.get_execution_graph(self.store.list_orchestrations(1)[0]["id"])
            if len(waits) == 1:
                self.assertEqual(graph["nodes"][0]["state"], "ready")
                self.assertIn("Offline", graph["nodes"][0]["waiting_reason"])
                self.assertEqual(runtime.submissions, [])
                self.store.set_agent_state(agent["id"], "Idle")
            else:
                runtime.finish_active()

        final = self.run_graph(
            execution_plan([planned_task("offline")]), {"offline": agent["id"]},
            runtime, advance,
        )
        self.assertEqual(final["status"], "Success")
        self.assertEqual(len(runtime.submissions), 1)
        self.assertEqual(len(final["selections"]), 1)

    def test_disabled_after_selection_fails_and_blocks_descendant(self):
        agent = self.agent("Disabled")
        runtime = ControlledRuntime(self.store)
        original_save = self.store.save_agent_selection

        def save_then_disable(oid, selection):
            selection_id = original_save(oid, selection)
            if selection_id:
                current = self.store.get_agent(agent["id"])
                self.store.update_agent(
                    agent["id"], normalize_agent({"enabled": False}, current),
                )
                self.store.save_agent_selection = original_save
            return selection_id

        self.store.save_agent_selection = save_then_disable
        final = self.run_graph(
            execution_plan([planned_task("a"), planned_task("b", ["a"])]),
            {"a": agent["id"], "b": agent["id"]}, runtime, lambda seconds: None,
        )
        nodes = {node["plan_task_id"]: node
                 for node in self.store.get_execution_graph(final["id"])["nodes"]}
        self.assertEqual(final["status"], "Failed")
        self.assertEqual(nodes["a"]["state"], "failed")
        self.assertIn("became disabled after selection", nodes["a"]["error"])
        self.assertEqual(nodes["b"]["state"], "blocked")
        self.assertEqual(runtime.submissions, [])
        failures = [event for event in final["events"]
                    if event["event_type"] == "freya.task.failed"]
        self.assertEqual(len(failures), 1)
        self.assertIn("became disabled", failures[0]["message"])

    def test_deleted_after_selection_fails_without_reselection(self):
        agent = self.agent("Deleted")
        other = self.agent("Unused")
        runtime = ControlledRuntime(self.store)
        original_save = self.store.save_agent_selection

        def save_then_delete(oid, selection):
            selection_id = original_save(oid, selection)
            if selection_id:
                self.store.delete_agent(agent["id"])
                self.store.save_agent_selection = original_save
            return selection_id

        self.store.save_agent_selection = save_then_delete
        final = self.run_graph(
            execution_plan([planned_task("deleted")]), {"deleted": agent["id"]},
            runtime, lambda seconds: None,
        )
        node = self.store.get_execution_graph(final["id"])["nodes"][0]
        self.assertEqual(final["status"], "Failed")
        self.assertEqual(node["state"], "failed")
        self.assertIn("no longer exists", node["error"])
        self.assertEqual(runtime.submissions, [])
        self.assertIsNotNone(other["id"])

    def test_same_agent_tasks_are_serialized(self):
        agent = self.agent("Shared")
        runtime = ControlledRuntime(self.store)
        max_active = []

        def advance(seconds):
            max_active.append(len(runtime.finish_active()))

        final = self.run_graph(
            execution_plan([planned_task("a"), planned_task("b")]),
            {"a": agent["id"], "b": agent["id"]}, runtime, advance,
            max_parallel_tasks=2,
        )
        self.assertEqual(final["status"], "Success")
        self.assertEqual(max(max_active), 1)
        self.assertEqual(len(final["selections"]), 2)

    def test_global_parallel_limit_is_enforced(self):
        agents = [self.agent(str(index)) for index in range(3)]
        runtime = ControlledRuntime(self.store)
        max_active = []

        def advance(seconds):
            max_active.append(len(runtime.finish_active()))

        final = self.run_graph(
            execution_plan([planned_task("a"), planned_task("b"), planned_task("c")]),
            {key: agent["id"] for key, agent in zip(("a", "b", "c"), agents)},
            runtime, advance, max_parallel_tasks=2,
        )
        self.assertEqual(final["status"], "Success")
        self.assertEqual(max(max_active), 2)

    def test_cancellation_during_execution_closes_every_nonterminal_node(self):
        agent = self.agent("Cancelable")
        runtime = ControlledRuntime(self.store)
        entered_wait = threading.Event()
        release_wait = threading.Event()

        def blocked_wait(seconds):
            entered_wait.set()
            release_wait.wait(2)

        plan = execution_plan([planned_task("running"), planned_task("later", ["running"])])
        run = self.store.create_orchestration("Cancel graph")
        orchestrator = Orchestrator(
            self.store, runtime, planner=Planner(lambda prompt, context: json.dumps(plan)),
            selector=MappingSelector({"running": agent["id"], "later": agent["id"]}),
            wait=blocked_wait, config={"max_wallclock_seconds": 10},
        )
        thread = threading.Thread(target=orchestrator._run, args=(run["id"],))
        thread.start()
        self.assertTrue(entered_wait.wait(2))
        cancelled = orchestrator.cancel(run["id"])
        release_wait.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(cancelled["status"], "Cancelled")
        states = [node["state"] for node in self.store.get_execution_graph(run["id"])["nodes"]]
        self.assertEqual(states, ["cancelled", "cancelled"])
        self.assertEqual(len(runtime.submissions), 1)

    def test_semantic_rejection_blocks_descendant_while_independent_branch_continues(self):
        agents = [self.agent(name) for name in ("Rejected", "Independent")]
        runtime = ControlledRuntime(self.store)
        calls = []

        def model(prompt, context):
            task = context["planned_task"]
            calls.append(task["id"])
            status = "rejected" if task["id"] == "bad" else "accepted"
            return evaluation_decision(task["success_criteria"], status)

        final = self.run_graph(
            execution_plan([
                planned_task("bad"), planned_task("child", ["bad"]),
                planned_task("independent"),
            ]),
            {"bad": agents[0]["id"], "child": agents[0]["id"],
             "independent": agents[1]["id"]}, runtime,
            lambda seconds: runtime.finish_active(), evaluator=Evaluator(model),
            max_parallel_tasks=2, max_semantic_attempts_per_task=1,
        )
        nodes = {item["plan_task_id"]: item
                 for item in self.store.get_execution_graph(final["id"])["nodes"]}
        self.assertEqual(final["status"], "Failed")
        self.assertEqual(nodes["bad"]["evaluation_status"], "rejected")
        self.assertEqual(nodes["child"]["state"], "blocked")
        self.assertEqual(nodes["independent"]["state"], "success")
        self.assertEqual(calls.count("bad"), 1)
        self.assertEqual(calls.count("independent"), 1)
        self.assertNotIn("child", calls)
        self.assertEqual(len(final["evaluations"]), 2)

    def test_accepted_evaluation_runs_once_and_unlocks_dependency(self):
        agent = self.agent("Accepted")
        runtime = ControlledRuntime(self.store)
        calls = []

        def model(prompt, context):
            calls.append(context["planned_task"]["id"])
            return evaluation_decision(context["planned_task"]["success_criteria"])

        final = self.run_graph(
            execution_plan([planned_task("a"), planned_task("b", ["a"])]),
            {"a": agent["id"], "b": agent["id"]}, runtime,
            lambda seconds: runtime.finish_active(), evaluator=Evaluator(model),
        )
        self.assertEqual(final["status"], "Success")
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(len(final["evaluations"]), 2)
        event_types = [item["event_type"] for item in final["events"]]
        self.assertEqual(event_types.count("freya.evaluation.started"), 2)
        self.assertEqual(event_types.count("freya.evaluation.completed"), 2)

    def test_default_evaluator_blocks_unverified_runtime_success(self):
        agent = self.agent("Unverified")
        runtime = ControlledRuntime(self.store, {"a": "Success"})
        final = self.run_graph(
            execution_plan([planned_task("a")]), {"a": agent["id"]}, runtime,
            lambda seconds: None,
            max_semantic_attempts_per_task=1,
        )
        graph_node = self.store.get_execution_graph(final["id"])["nodes"][0]
        self.assertEqual(final["status"], "Failed")
        self.assertEqual(graph_node["state"], "failed")
        self.assertEqual(graph_node["evaluation_status"], "blocked")
        self.assertEqual(final["evaluations"][0]["status"], "blocked")

    def test_default_evaluator_accepts_objectively_verified_runtime_success(self):
        agent = self.agent("Verified")
        runtime = ControlledRuntime(self.store)
        final = self.run_graph(
            execution_plan([planned_task("a")]), {"a": agent["id"]}, runtime,
            lambda seconds: runtime.finish_active(),
        )
        graph_node = self.store.get_execution_graph(final["id"])["nodes"][0]
        self.assertEqual(final["status"], "Success")
        self.assertEqual(graph_node["state"], "success")
        self.assertEqual(graph_node["evaluation_status"], "accepted")
        self.assertEqual(final["evaluations"][0]["status"], "accepted")

    def test_cancellation_during_evaluation_discards_late_result(self):
        agent = self.agent("Cancel evaluation")
        runtime = ControlledRuntime(self.store, {"a": "Success"})
        entered = threading.Event()
        release = threading.Event()

        def model(prompt, context):
            entered.set()
            release.wait(2)
            return evaluation_decision(context["planned_task"]["success_criteria"])

        plan = execution_plan([planned_task("a")])
        run = self.store.create_orchestration("Cancel evaluation")
        orchestrator = Orchestrator(
            self.store, runtime, planner=Planner(lambda prompt, context: json.dumps(plan)),
            selector=MappingSelector({"a": agent["id"]}), evaluator=Evaluator(model),
            config={"max_wallclock_seconds": 10},
        )
        thread = threading.Thread(target=orchestrator._run, args=(run["id"],))
        thread.start()
        self.assertTrue(entered.wait(2))
        cancelled = orchestrator.cancel(run["id"])
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(cancelled["status"], "Cancelled")
        self.assertEqual(self.store.list_evaluations(run["id"]), [])
        self.assertEqual(self.store.get_execution_graph(run["id"])["nodes"][0]["state"],
                         "cancelled")

    def test_timeout_during_evaluation_discards_late_result(self):
        class Clock:
            value = 0.0

            def __call__(self):
                return self.value

        clock = Clock()
        agent = self.agent("Timeout evaluation")
        runtime = ControlledRuntime(self.store, {"a": "Success"})

        def model(prompt, context):
            clock.value = 2.0
            return evaluation_decision(context["planned_task"]["success_criteria"])

        plan = execution_plan([planned_task("a")])
        run = self.store.create_orchestration("Timeout evaluation")
        orchestrator = Orchestrator(
            self.store, runtime, planner=Planner(lambda prompt, context: json.dumps(plan)),
            selector=MappingSelector({"a": agent["id"]}), evaluator=Evaluator(model),
            clock=clock, wait=lambda seconds: None,
            config={"max_wallclock_seconds": 1},
        )
        orchestrator._run(run["id"])
        self.assertEqual(self.store.get_orchestration(run["id"])["status"], "Failed")
        self.assertEqual(self.store.list_evaluations(run["id"]), [])
        self.assertEqual(self.store.get_execution_graph(run["id"])["nodes"][0]["state"],
                         "cancelled")

    def test_parallel_runtime_successes_are_persisted_as_evaluating_before_calls(self):
        agents = [self.agent("Parallel A"), self.agent("Parallel B")]
        runtime = ControlledRuntime(self.store)
        plan = execution_plan([planned_task("a"), planned_task("b")])
        run = self.store.create_orchestration("Parallel evaluation")
        observed = []

        def model(prompt, context):
            states = {item["plan_task_id"]: item["state"]
                      for item in self.store.get_execution_graph(run["id"])["nodes"]}
            observed.append(states)
            return evaluation_decision(context["planned_task"]["success_criteria"])

        orchestrator = Orchestrator(
            self.store, runtime, planner=Planner(lambda prompt, context: json.dumps(plan)),
            selector=MappingSelector({"a": agents[0]["id"], "b": agents[1]["id"]}),
            evaluator=Evaluator(model), wait=lambda seconds: runtime.finish_active(),
            config={"max_wallclock_seconds": 10, "max_parallel_tasks": 2},
        )
        orchestrator._run(run["id"])
        self.assertEqual(self.store.get_orchestration(run["id"])["status"], "Success")
        self.assertEqual(observed[0], {"a": "evaluating", "b": "evaluating"})
        self.assertEqual(len(observed), 2)

    def test_needs_revision_is_persisted_and_fails_node_without_retry(self):
        agent = self.agent("Revision")
        runtime = ControlledRuntime(self.store, {"a": "Success"})

        def model(prompt, context):
            criteria = context["planned_task"]["success_criteria"]
            value = evaluation_decision(criteria)
            value.update(status="needs_revision", recommended_action="revise",
                         summary="A small part is missing.")
            value["criteria"][0]["status"] = "partial"
            return value

        final = self.run_graph(
            execution_plan([planned_task("a")]), {"a": agent["id"]}, runtime,
            lambda seconds: None, evaluator=Evaluator(model),
            max_semantic_attempts_per_task=1,
        )
        node_state = self.store.get_execution_graph(final["id"])["nodes"][0]
        self.assertEqual(final["status"], "Failed")
        self.assertEqual(node_state["state"], "failed")
        self.assertEqual(node_state["evaluation_status"], "needs_revision")
        self.assertIn("requires revision", node_state["error"])
        self.assertEqual(len(runtime.submissions), 1)

    def test_evaluator_technical_failure_is_persisted_and_emits_failed_event(self):
        agent = self.agent("Broken evaluator")
        runtime = ControlledRuntime(self.store, {"a": "Success"})

        def broken(prompt, context):
            raise RuntimeError("adapter unavailable")

        final = self.run_graph(
            execution_plan([planned_task("a")]), {"a": agent["id"]}, runtime,
            lambda seconds: None, evaluator=Evaluator(broken),
        )
        evaluation = final["evaluations"][0]
        node_state = self.store.get_execution_graph(final["id"])["nodes"][0]
        self.assertEqual(evaluation["status"], "error")
        self.assertEqual(node_state["evaluation_status"], "error")
        self.assertIn("freya.evaluation.failed",
                      [item["event_type"] for item in final["events"]])

    def test_configured_delegation_limit_fails_before_graph_or_dispatch(self):
        agent = self.agent("Limited")
        runtime = ControlledRuntime(self.store)
        final = self.run_graph(
            execution_plan([planned_task("a"), planned_task("b")]),
            {"a": agent["id"], "b": agent["id"]}, runtime, lambda seconds: None,
            max_delegated_tasks=1,
        )
        self.assertEqual(final["status"], "Failed")
        self.assertIn("max_delegated_tasks", final["error"])
        self.assertEqual(self.store.get_execution_graph(final["id"])["nodes"], [])
        self.assertEqual(runtime.submissions, [])

    def test_restart_recovery_never_leaves_nodes_running(self):
        agent = self.agent("Interrupted")
        run = self.store.create_orchestration("Interrupted graph")
        self.store.transition_orchestration(run["id"], "Queued", "Planning")
        plan = execution_plan([planned_task("a"), planned_task("b", ["a"])])
        self.store.save_orchestration_plan(run["id"], plan, 1)
        graph = ExecutionGraph(plan)
        self.store.initialize_execution_graph(run["id"], graph.serialize())
        self.store.transition_orchestration(run["id"], "Planned", "Running")
        selection = MappingSelector({"a": agent["id"]}).select_agent(plan["tasks"][0], [])
        selection_id = self.store.save_agent_selection(run["id"], selection)
        task = self.store.create_task(agent["id"], "a", "workspace")
        delegation_id = self.store.add_delegation(run["id"], agent["id"], "a", task["id"])
        graph.mark_selected("a", agent["id"], selection_id)
        graph.mark_running("a", task["id"], delegation_id)
        self.store.save_execution_graph(run["id"], graph.serialize())

        self.assertEqual(self.store.recover_interrupted_orchestrations(), 1)
        recovered = self.store.get_execution_graph(run["id"])
        self.assertEqual([node["state"] for node in recovered["nodes"]],
                         ["cancelled", "skipped"])
        self.assertEqual(self.store.get_orchestration(run["id"])["status"], "Failed")

    def test_timeout_cancels_runtime_and_closes_every_graph_node(self):
        class Clock:
            value = 0.0

            def __call__(self):
                return self.value

        clock = Clock()
        agent = self.agent("Timeout")
        runtime = ControlledRuntime(self.store)

        def advance(seconds):
            clock.value += seconds

        plan = execution_plan([
            planned_task("forever"), planned_task("dependent", ["forever"]),
        ])
        run = self.store.create_orchestration("Timeout graph")
        orchestrator = Orchestrator(
            self.store, runtime, planner=Planner(lambda prompt, context: json.dumps(plan)),
            selector=MappingSelector({"forever": agent["id"], "dependent": agent["id"]}),
            clock=clock, wait=advance,
            config={"max_wallclock_seconds": .5, "max_parallel_tasks": 1},
        )
        orchestrator._run(run["id"])

        final = self.store.get_orchestration(run["id"])
        graph = self.store.get_execution_graph(run["id"])
        self.assertEqual(final["status"], "Failed")
        self.assertIn("time limit", final["error"])
        self.assertEqual(len(runtime.cancelled), 1)
        self.assertEqual(self.store.get_task(runtime.submissions[0][2])["status"], "Cancelled")
        self.assertTrue(all(node["state"] in {"success", "failed", "blocked", "cancelled", "skipped"}
                            for node in graph["nodes"]))
        self.assertFalse(any(node["state"] in {"pending", "ready", "running",
                                               "waiting_for_approval"}
                             for node in graph["nodes"]))
        completed = [event for event in final["events"]
                     if event["event_type"] == "freya.graph.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["status"], "Failed")

    def test_post_submit_persistence_failure_cancels_without_duplicate_dispatch(self):
        agent = self.agent("Atomic dispatch")
        runtime = ControlledRuntime(self.store)
        original_add = self.store.add_delegation
        calls = []

        def fail_once(*args, **kwargs):
            calls.append(True)
            self.store.add_delegation = original_add
            raise RuntimeError("simulated delegation persistence failure")

        self.store.add_delegation = fail_once
        final = self.run_graph(
            execution_plan([planned_task("atomic")]), {"atomic": agent["id"]},
            runtime, lambda seconds: None,
        )
        node = self.store.get_execution_graph(final["id"])["nodes"][0]
        self.assertEqual(final["status"], "Failed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(runtime.submissions), 1)
        self.assertEqual(len(runtime.cancelled), 1)
        self.assertEqual(node["state"], "cancelled")
        self.assertNotIn(node["state"], {"pending", "ready", "running",
                                         "waiting_for_approval"})
        self.assertEqual(len(final["selections"]), 1)

    def test_graph_api_exposes_nodes_and_summary(self):
        run = self.store.create_orchestration("Historical")
        app = Application(self.store, object(), Path(self.temporary.name))
        status, body = app.dispatch("GET", f"/api/orchestrations/{run['id']}/graph", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"orchestration_id": run["id"], "nodes": [], "summary": None})

    def test_pre43_database_migrates_with_empty_historical_graph(self):
        legacy = Path(self.temporary.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy)
        connection.execute(
            "CREATE TABLE orchestration_runs (id TEXT PRIMARY KEY,prompt TEXT NOT NULL,"
            "status TEXT NOT NULL DEFAULT 'Queued',response TEXT NOT NULL DEFAULT '',error TEXT,"
            "config_json TEXT NOT NULL DEFAULT '{}',plan_json TEXT,plan_schema_version INTEGER,"
            "plan_created_at TEXT,planning_metrics_json TEXT NOT NULL DEFAULT '{}',"
            "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO orchestration_runs(id,prompt,created_at,updated_at) VALUES(?,?,?,?)",
            ("old", "old", "before", "before"),
        )
        connection.commit()
        connection.close()
        migrated = Store(legacy)
        self.assertEqual(migrated.get_execution_graph("old")["nodes"], [])


if __name__ == "__main__":
    unittest.main()
