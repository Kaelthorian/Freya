import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from control_center.api import Application
from control_center.config import normalize_agent
from control_center.execution_graph import ExecutionGraph
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
        self.assertEqual(graph.refresh_dependencies(), [])
        graph.apply_runtime_status("b", "Success")
        graph.refresh_dependencies()
        self.assertEqual(graph.node("join")["state"], "ready")

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
        status = self.initial.get(objective, "Running")
        task = self.store.update_task(task["id"], status=status)
        self.submissions.append((objective, agent_id, task["id"]))
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
                status = outcomes.get(task["prompt"], "Success")
                fields = {"status": status, "result": task["prompt"] + " result"}
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

    def run_graph(self, plan, mapping, runtime, wait, **config):
        run = self.store.create_orchestration("Execute graph")
        orchestrator = Orchestrator(
            self.store, runtime, planner=Planner(lambda prompt, context: json.dumps(plan)),
            selector=MappingSelector(mapping), wait=wait,
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
