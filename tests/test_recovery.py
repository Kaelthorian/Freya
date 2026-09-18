import json
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from uuid import uuid4

from control_center.agent_selector import AgentSelector
from control_center.api import Application
from control_center.config import normalize_agent
from control_center.evaluator import Evaluator
from control_center.execution_graph import ExecutionGraph
from control_center.orchestrator import Orchestrator
from control_center.planner import Planner
from control_center.recovery import (
    RECOVERY_VERSION,
    RecoveryController,
    RecoveryGenerationError,
    RecoveryValidationError,
    Replanner,
    build_retry_prompt,
    semantic_failure_fingerprint,
    validate_recovery_decision,
)
from control_center.storage import Store
from tests.test_execution_graph import ControlledRuntime, MappingSelector, execution_plan, planned_task


def evaluation(status="needs_revision"):
    action = {"accepted": "accept", "needs_revision": "revise", "rejected": "reject",
              "blocked": "gather_evidence"}[status]
    criterion_status = "satisfied" if status == "accepted" else "unsatisfied"
    return {
        "status": status, "confidence": 0.9, "summary": status + " in recovery fixture.",
        "criteria": [{"criterion": "a completes", "status": criterion_status,
                      "reason": "Fixture evidence.", "evidence": ["fixture"]}],
        "issues": [] if status == "accepted" else ["Implementation is incomplete."],
        "missing_evidence": [] if status != "blocked" else ["a completes"],
        "recommended_action": action,
    }


def recovery_decision(action="retry_same_agent", *, task_id="a", excluded=None):
    return {
        "action": action, "reason": "A bounded retry can address the evaluation.",
        "instructions": "Correct the incomplete implementation and verify it.",
        "exclude_agent_ids": list(excluded or []), "affected_task_ids": [task_id],
    }


class RecoveryContractTests(unittest.TestCase):
    def task(self):
        return planned_task("a")

    def node(self, attempt=1, agent="agent-a"):
        return {"plan_task_id": "a", "attempt": attempt, "selected_agent_id": agent}

    def limits(self, **overrides):
        return {"max_semantic_attempts_per_task": 3, "max_plan_revisions": 2,
                "max_recovery_actions": 8, "max_recovery_model_calls": 16,
                "recovery_action_count": 0,
                "plan_revision_count": 0, "recovery_model_calls_used": 0} | overrides

    def test_strict_schema_rejects_unknown_fields(self):
        value = recovery_decision() | {"extra": True}
        with self.assertRaisesRegex(RecoveryValidationError, "unknown fields"):
            validate_recovery_decision(value, source_task_id="a", known_task_ids={"a"})

    def test_different_agent_requires_exclusion(self):
        with self.assertRaisesRegex(RecoveryValidationError, "exclude"):
            validate_recovery_decision(recovery_decision("retry_different_agent"))

    def test_model_gets_exactly_one_repair(self):
        calls = []

        def model(prompt, context):
            calls.append(prompt)
            return "not json" if len(calls) == 1 else recovery_decision()

        result = RecoveryController(model).decide(
            planned_task=self.task(), execution_node=self.node(), evaluation=evaluation(),
            history=[], available_agents=[{"id": "agent-a", "enabled": True}],
            plan=execution_plan([self.task()]), limits=self.limits(),
        )
        self.assertEqual(result["action"], "retry_same_agent")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["metrics"]["model_calls"], 2)

    def test_second_invalid_model_output_fails(self):
        controller = RecoveryController(lambda prompt, context: "not json")
        with self.assertRaises(RecoveryGenerationError):
            controller.decide(
                planned_task=self.task(), execution_node=self.node(), evaluation=evaluation(),
                history=[], available_agents=[{"id": "agent-a", "enabled": True}],
                plan=execution_plan([self.task()]), limits=self.limits(),
            )

    def test_offline_fallback_fails_conservatively(self):
        result = RecoveryController(offline=True).decide(
            planned_task=self.task(), execution_node=self.node(), evaluation=evaluation(),
            history=[], available_agents=[{"id": "agent-a", "enabled": True}],
            plan=execution_plan([self.task()]), limits=self.limits(),
        )
        self.assertEqual(result["action"], "fail")
        self.assertEqual(result["metrics"]["model_calls"], 0)

    def test_attempt_budget_prevents_model_call(self):
        calls = []
        result = RecoveryController(lambda prompt, context: calls.append(True)).decide(
            planned_task=self.task(), execution_node=self.node(attempt=3),
            evaluation=evaluation(), history=[],
            available_agents=[{"id": "agent-a", "enabled": True}],
            plan=execution_plan([self.task()]), limits=self.limits(),
        )
        self.assertEqual(result["action"], "fail")
        self.assertEqual(calls, [])

    def test_exhausted_model_call_budget_prevents_model_call(self):
        calls = []
        result = RecoveryController(lambda prompt, context: calls.append(True)).decide(
            planned_task=self.task(), execution_node=self.node(), evaluation=evaluation(),
            history=[], available_agents=[{"id": "agent-a", "enabled": True}],
            plan=execution_plan([self.task()]),
            limits=self.limits(max_recovery_model_calls=1, recovery_model_calls_used=1),
        )
        self.assertEqual(result["action"], "fail")
        self.assertEqual(calls, [])

    def test_invalid_response_cannot_exceed_remaining_model_call(self):
        calls = []

        def model(prompt, context):
            calls.append(prompt)
            return "not json"

        result = RecoveryController(model).decide(
            planned_task=self.task(), execution_node=self.node(), evaluation=evaluation(),
            history=[], available_agents=[{"id": "agent-a", "enabled": True}],
            plan=execution_plan([self.task()]),
            limits=self.limits(max_recovery_model_calls=1),
        )
        self.assertEqual(result["action"], "fail")
        self.assertEqual(len(calls), 1)
    def test_repeated_fingerprint_stops_loop(self):
        fingerprint = semantic_failure_fingerprint("a", "agent-a", evaluation())
        history = [{"fingerprint": fingerprint}, {"fingerprint": fingerprint}]
        result = RecoveryController(lambda prompt, context: recovery_decision()).decide(
            planned_task=self.task(), execution_node=self.node(), evaluation=evaluation(),
            history=history, available_agents=[{"id": "agent-a", "enabled": True}],
            plan=execution_plan([self.task()]), limits=self.limits(),
        )
        self.assertEqual(result["action"], "fail")
        self.assertIn("repeated", result["reason"])

    def test_different_agent_has_no_silent_same_agent_fallback(self):
        result = RecoveryController(
            lambda prompt, context: recovery_decision("retry_different_agent", excluded=["agent-a"])
        ).decide(
            planned_task=self.task(), execution_node=self.node(), evaluation=evaluation("rejected"),
            history=[], available_agents=[{"id": "agent-a", "enabled": True}],
            plan=execution_plan([self.task()]), limits=self.limits(),
        )
        self.assertEqual(result["action"], "fail")
        self.assertIn("No different", result["reason"])

    def test_fingerprint_is_stable_and_agent_specific(self):
        first = semantic_failure_fingerprint("a", "agent-a", evaluation())
        second = semantic_failure_fingerprint("a", "agent-a", evaluation())
        third = semantic_failure_fingerprint("a", "agent-b", evaluation())
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_retry_prompt_contains_feedback_not_previous_result(self):
        prompt = build_retry_prompt(self.task(), evaluation("blocked"), "Gather proof.", attempt=2)
        self.assertIn("Gather proof", prompt)
        self.assertIn("Missing evidence", prompt)
        self.assertIn("attempt 2", prompt)


class RecoveryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def pending_fixture(self, status="needs_revision"):
        agent = self.store.create_agent(normalize_agent({"name": "Recovery", "role": "Engineer"}))
        plan = execution_plan([planned_task("a")])
        run = self.store.create_orchestration("Recover")
        self.store.transition_orchestration(run["id"], "Queued", "Planning")
        self.store.save_orchestration_plan(run["id"], plan, 1)
        graph = ExecutionGraph(plan)
        self.store.initialize_execution_graph(run["id"], graph.serialize())
        self.store.transition_orchestration(run["id"], "Planned", "Running")
        selection = {
            "task_id": "a", "status": "selected", "selected_agent_id": agent["id"],
            "score": 1, "classification": "eligible", "approval_required": False,
            "selector_version": 1, "reasons": [], "warnings": [], "candidates": [],
            "attempt": 1,
        }
        selection_id = self.store.save_agent_selection(run["id"], selection)
        task = self.store.create_task(agent["id"], "a", "workspace")
        delegation_id = self.store.add_delegation(run["id"], agent["id"], "a", task["id"])
        graph.mark_selected("a", agent["id"], selection_id)
        graph.mark_running("a", task["id"], delegation_id)
        self.store.save_execution_graph(run["id"], graph.serialize())
        self.store.record_execution_attempt(
            run["id"], "a", selected_agent_id=agent["id"], selection_id=selection_id,
            runtime_task_id=task["id"], delegation_id=delegation_id, attempt=1, prompt="a",
        )
        self.store.update_task(task["id"], status="Success", result="incomplete")
        graph.apply_runtime_status("a", "Success", result="incomplete")
        self.store.save_execution_graph(run["id"], graph.serialize())
        self.store.commit_evaluation(
            "evaluation-1", run["id"], "a", runtime_task_id=task["id"],
            agent_id=agent["id"], attempt=1, evaluator_version=1,
            evaluation=evaluation(status), metrics={}, snapshot={},
            context_truncated=False, deterministic=True,
        )
        return run, agent, plan

    def commit(self, run, action="retry_same_agent"):
        decision = recovery_decision(action, excluded=["old"] if action == "retry_different_agent" else [])
        decision |= {"fingerprint": "f" * 64, "metrics": {"model_calls": 1}}
        return self.store.commit_recovery_action(
            "recovery-1", run["id"], "a", source_attempt=1,
            source_evaluation_id="evaluation-1", decision=decision,
            recovery_version=RECOVERY_VERSION, prompt="retry prompt", snapshot={"bounded": True},
        )

    def test_nonaccepted_evaluation_is_recovery_pending(self):
        run, _, _ = self.pending_fixture()
        node = self.store.get_execution_graph(run["id"])["nodes"][0]
        self.assertEqual(node["state"], "recovery_pending")
        self.assertEqual(node["evaluation_id"], "evaluation-1")

    def test_retry_commit_preserves_attempt_and_opens_ready_node(self):
        run, _, _ = self.pending_fixture()
        self.assertIsNotNone(self.commit(run))
        node = self.store.get_execution_graph(run["id"])["nodes"][0]
        self.assertEqual(node["state"], "ready")
        self.assertEqual(node["attempt"], 1)
        self.assertIsNone(node["evaluation_id"])
        self.assertEqual(node["attempt_prompt"], "retry prompt")
        attempts = self.store.list_execution_attempts(run["id"])
        self.assertEqual(attempts[0]["status"], "recovered")
        self.assertEqual(attempts[0]["recovery_action_id"], "recovery-1")

    def test_terminal_attempt_update_sets_finished_at(self):
        run, _, _ = self.pending_fixture()
        self.store.update_execution_attempt(run["id"], "a", 1, status="failed")
        attempt = self.store.list_execution_attempts(run["id"])[0]
        self.assertEqual(attempt["status"], "failed")
        self.assertIsNotNone(attempt["finished_at"])
    def test_duplicate_source_evaluation_cannot_create_second_recovery(self):
        run, _, _ = self.pending_fixture()
        self.commit(run)
        with self.assertRaises(sqlite3.IntegrityError):
            # Force the durable unique constraint directly; the live node guard is otherwise idempotent.
            with self.store._connection(write=True) as connection:
                connection.execute(
                    "INSERT INTO orchestration_recovery_actions("
                    "id,orchestration_id,plan_task_id,source_attempt,source_evaluation_id,action,"
                    "reason,instructions,exclude_agent_ids_json,affected_task_ids_json,fingerprint,"
                    "recovery_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("duplicate", run["id"], "a", 1, "evaluation-1", "fail", "x", "",
                     "[]", "[]", "x", 1, "now"),
                )

    def test_fail_commit_is_terminal_and_keeps_evaluation(self):
        run, _, _ = self.pending_fixture()
        self.commit(run, "fail")
        node = self.store.get_execution_graph(run["id"])["nodes"][0]
        self.assertEqual(node["state"], "failed")
        self.assertEqual(node["evaluation_id"], "evaluation-1")

    def test_restart_closes_recovery_pending(self):
        run, _, _ = self.pending_fixture()
        self.assertEqual(self.store.recover_interrupted_orchestrations(), 1)
        node = self.store.get_execution_graph(run["id"])["nodes"][0]
        self.assertEqual(node["state"], "skipped")
        self.assertNotEqual(node["state"], "recovery_pending")

    def test_recovery_and_revision_api_routes(self):
        run, _, plan = self.pending_fixture()
        decision = recovery_decision("replan_subgraph") | {
            "fingerprint": "f" * 64, "metrics": {},
        }
        self.store.commit_recovery_action(
            "recovery-1", run["id"], "a", source_attempt=1,
            source_evaluation_id="evaluation-1", decision=decision,
            recovery_version=1,
        )
        revised = execution_plan([
            planned_task("a"),
            {**planned_task("replacement"), "objective": "replacement"},
        ])
        self.store.commit_plan_revision(
            "revision-1", run["id"], "recovery-1", summary="Replace failed task.",
            plan=revised, superseded_task_ids=["a"], metrics={},
        )

        class Runtime:
            max_workers = 1

        app = Application(self.store, Runtime(), Path(self.temporary.name))
        for route in ("attempts", "recoveries", "plan-revisions", "effective-plan"):
            status, payload = app.dispatch(
                "GET", f"/api/orchestrations/{run['id']}/{route}", {}, {},
            )
            self.assertEqual(status, 200)
            self.assertTrue(payload)
        self.assertEqual(self.store.get_orchestration(run["id"])["plan"], plan)
        self.assertEqual(self.store.get_execution_graph(run["id"])["nodes"][0]["state"],
                         "superseded")

    def test_restart_preserves_superseded_history(self):
        run, _, _ = self.pending_fixture()
        decision = recovery_decision("replan_subgraph") | {
            "fingerprint": "f" * 64, "metrics": {},
        }
        self.store.commit_recovery_action(
            "recovery-1", run["id"], "a", source_attempt=1,
            source_evaluation_id="evaluation-1", decision=decision,
            recovery_version=1,
        )
        revised = execution_plan([planned_task("a"), planned_task("replacement")])
        self.store.commit_plan_revision(
            "revision-1", run["id"], "recovery-1", summary="Replace failed task.",
            plan=revised, superseded_task_ids=["a"], metrics={},
        )
        self.assertEqual(self.store.recover_interrupted_orchestrations(), 1)
        states = {item["plan_task_id"]: item["state"]
                  for item in self.store.get_execution_graph(run["id"])["nodes"]}
        self.assertEqual(states["a"], "superseded")
        self.assertEqual(states["replacement"], "skipped")
    def test_pre45_database_migrates_recovery_columns_and_tables(self):
        legacy_path = Path(self.temporary.name) / "pre-4-5.sqlite3"
        schema = Path("control_center/schema.sql").read_text(encoding="utf-8")
        for column in (
                "    effective_plan_json TEXT,\n",
                "    current_plan_revision INTEGER NOT NULL DEFAULT 0,\n",
                "    attempt INTEGER NOT NULL DEFAULT 1,\n",
                "    recovery_action_id TEXT,\n",
                "    attempt_prompt TEXT NOT NULL DEFAULT '',\n",
                "    plan_revision INTEGER NOT NULL DEFAULT 0,\n"):
            schema = schema.replace(column, "", 1)
        for table, index in (
                ("orchestration_execution_attempts", "idx_orch_attempts"),
                ("orchestration_recovery_actions", "idx_orch_recoveries"),
                ("orchestration_plan_revisions", "idx_orch_plan_revisions")):
            schema = re.sub(
                rf"CREATE TABLE IF NOT EXISTS {table} \([\s\S]*?"
                rf"CREATE INDEX IF NOT EXISTS {index}\s*[\s\S]*?;\n",
                "", schema, count=1,
            )
        connection = sqlite3.connect(legacy_path)
        connection.executescript(schema)
        connection.close()

        migrated = Store(legacy_path)
        with migrated._connection() as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            run_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(orchestration_runs)"
            )}
            selection_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(orchestration_selections)"
            )}
            node_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(orchestration_task_nodes)"
            )}
        self.assertTrue({
            "orchestration_execution_attempts", "orchestration_recovery_actions",
            "orchestration_plan_revisions",
        } <= tables)
        self.assertTrue({"effective_plan_json", "current_plan_revision"} <= run_columns)
        self.assertIn("attempt", selection_columns)
        self.assertTrue({
            "recovery_action_id", "attempt_prompt", "plan_revision",
        } <= node_columns)


class ReplannerTests(unittest.TestCase):
    def current(self):
        return execution_plan([planned_task("a"), planned_task("child", ["a"])])

    def valid_revision(self):
        return {
            "summary": "Replace the failed branch.",
            "plan": execution_plan([
                planned_task("a"), planned_task("child", ["replacement"]),
                planned_task("replacement"),
            ]),
            "superseded_task_ids": ["a"],
        }

    def test_valid_revision_preserves_failed_snapshot_and_adds_new_task(self):
        result = Replanner(lambda prompt, context, revision=False: self.valid_revision()).create_revision(
            current_plan=self.current(), source_task_id="a", affected_task_ids=["a", "child"],
            accepted_task_ids=set(), historical_task_ids={"a", "child"},
        )
        self.assertIn("replacement", {task["id"] for task in result["plan"]["tasks"]})
        self.assertEqual(result["superseded_task_ids"], ["a"])

    def test_revision_cannot_supersede_accepted_task(self):
        with self.assertRaisesRegex(RecoveryValidationError, "Accepted"):
            Replanner(lambda prompt, context, revision=False: self.valid_revision()).create_revision(
                current_plan=self.current(), source_task_id="a", affected_task_ids=["a", "child"],
                accepted_task_ids={"a"}, historical_task_ids={"a", "child"},
            )

    def test_revision_rejects_historical_id_reuse(self):
        with self.assertRaisesRegex(RecoveryValidationError, "historical"):
            Replanner(lambda prompt, context, revision=False: self.valid_revision()).create_revision(
                current_plan=self.current(), source_task_id="a", affected_task_ids=["a", "child"],
                accepted_task_ids=set(), historical_task_ids={"a", "child", "replacement"},
            )


class RecoverySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, name):
        return self.store.create_agent(normalize_agent({"name": name, "role": "Engineer"}))

    def test_same_agent_retry_creates_two_real_attempts_then_succeeds(self):
        agent = self.agent("Same")
        runtime = ControlledRuntime(self.store)
        evaluation_calls = []

        def evaluator_model(prompt, context):
            evaluation_calls.append(context["execution"]["attempt"])
            return evaluation("needs_revision" if len(evaluation_calls) == 1 else "accepted")

        recovery = RecoveryController(lambda prompt, context: recovery_decision())
        run = self.store.create_orchestration("Retry")
        orchestrator = Orchestrator(
            self.store, runtime,
            planner=Planner(lambda prompt, context: json.dumps(execution_plan([planned_task("a")]))),
            selector=MappingSelector({"a": agent["id"]}), evaluator=Evaluator(evaluator_model),
            recovery=recovery, wait=lambda seconds: runtime.finish_active(),
            config={"max_wallclock_seconds": 10},
        )
        orchestrator._run(run["id"])
        final = self.store.get_orchestration(run["id"])
        self.assertEqual(final["status"], "Success")
        self.assertEqual([item["attempt"] for item in final["attempts"]], [1, 2])
        self.assertEqual(len(final["evaluations"]), 2)
        self.assertEqual(len(final["recoveries"]), 1)
        self.assertEqual(len(final["selections"]), 2)
        self.assertNotEqual(runtime.submissions[0][0], runtime.submissions[1][0])
        events = [item["event_type"] for item in final["events"]]
        self.assertIn("freya.recovery.retry_scheduled", events)

    def test_different_agent_retry_excludes_first_agent(self):
        self.agent("One")
        self.agent("Two")
        runtime = ControlledRuntime(self.store)
        calls = []

        def evaluator_model(prompt, context):
            calls.append(context["execution"]["attempt"])
            return evaluation("rejected" if len(calls) == 1 else "accepted")

        def recovery_model(prompt, context):
            previous = context["execution"]["selected_agent_id"]
            return recovery_decision("retry_different_agent", excluded=[previous])

        run = self.store.create_orchestration("Different")
        orchestrator = Orchestrator(
            self.store, runtime,
            planner=Planner(lambda prompt, context: json.dumps(execution_plan([planned_task("a")]))),
            selector=AgentSelector(), evaluator=Evaluator(evaluator_model),
            recovery=RecoveryController(recovery_model),
            wait=lambda seconds: runtime.finish_active(), config={"max_wallclock_seconds": 10},
        )
        orchestrator._run(run["id"])
        final = self.store.get_orchestration(run["id"])
        self.assertEqual(final["status"], "Success")
        self.assertEqual(len(final["attempts"]), 2)
        self.assertNotEqual(final["attempts"][0]["selected_agent_id"],
                            final["attempts"][1]["selected_agent_id"])

    def test_different_agent_retry_excludes_every_prior_attempt_agent(self):
        for name in ("One", "Two", "Three"):
            self.agent(name)
        runtime = ControlledRuntime(self.store)
        evaluation_calls = []

        def evaluator_model(prompt, context):
            evaluation_calls.append(context["execution"]["attempt"])
            return evaluation("rejected" if len(evaluation_calls) < 3 else "accepted")

        def recovery_model(prompt, context):
            previous = context["execution"]["selected_agent_id"]
            return recovery_decision("retry_different_agent", excluded=[previous])

        run = self.store.create_orchestration("Try every agent once")
        orchestrator = Orchestrator(
            self.store, runtime,
            planner=Planner(lambda prompt, context: json.dumps(execution_plan([planned_task("a")]))),
            selector=AgentSelector(), evaluator=Evaluator(evaluator_model),
            recovery=RecoveryController(recovery_model),
            wait=lambda seconds: runtime.finish_active(), config={"max_wallclock_seconds": 10},
        )
        orchestrator._run(run["id"])
        final = self.store.get_orchestration(run["id"])
        selected = [item["selected_agent_id"] for item in final["attempts"]]
        self.assertEqual(final["status"], "Success")
        self.assertEqual(len(selected), 3)
        self.assertEqual(len(set(selected)), 3)
    def test_replan_subgraph_executes_replacement_and_preserves_original_plan(self):
        agent = self.agent("Replanner")
        runtime = ControlledRuntime(self.store)
        original = execution_plan([planned_task("a")])
        revised = execution_plan([planned_task("a"), planned_task("replacement")])

        def evaluator_model(prompt, context):
            result = evaluation(
                "needs_revision" if context["planned_task"]["id"] == "a" else "accepted"
            )
            result["criteria"][0]["criterion"] = context["planned_task"]["success_criteria"][0]
            return result

        recovery = RecoveryController(
            lambda prompt, context: recovery_decision("replan_subgraph")
        )
        replanner = Replanner(lambda prompt, context, revision=False: {
            "summary": "Replace the rejected task with new work.",
            "plan": revised,
            "superseded_task_ids": ["a"],
        })
        run = self.store.create_orchestration("Replan")
        orchestrator = Orchestrator(
            self.store, runtime,
            planner=Planner(lambda prompt, context: json.dumps(original)),
            selector=MappingSelector({"a": agent["id"], "replacement": agent["id"]}),
            evaluator=Evaluator(evaluator_model), recovery=recovery, replanner=replanner,
            wait=lambda seconds: runtime.finish_active(), config={"max_wallclock_seconds": 10},
        )
        orchestrator._run(run["id"])
        final = self.store.get_orchestration(run["id"])
        states = {item["plan_task_id"]: item["state"]
                  for item in self.store.get_execution_graph(run["id"])["nodes"]}
        self.assertEqual(final["status"], "Success")
        self.assertEqual(final["plan"], original)
        self.assertEqual(final["effective_plan"], revised)
        self.assertEqual(states, {"a": "superseded", "replacement": "success"})
        self.assertEqual(len(final["plan_revisions"]), 1)
        self.assertIn("1 executable task", final["response"])
        self.assertIn("1 historical task", final["response"])
    def test_cancellation_discards_late_recovery_decision(self):
        agent = self.agent("Cancel")
        runtime = ControlledRuntime(self.store)
        entered = threading.Event()
        release = threading.Event()

        def recovery_model(prompt, context):
            entered.set()
            release.wait(2)
            return recovery_decision()

        run = self.store.create_orchestration("Cancel recovery")
        orchestrator = Orchestrator(
            self.store, runtime,
            planner=Planner(lambda prompt, context: json.dumps(execution_plan([planned_task("a")]))),
            selector=MappingSelector({"a": agent["id"]}),
            evaluator=Evaluator(lambda prompt, context: evaluation("needs_revision")),
            recovery=RecoveryController(recovery_model),
            wait=lambda seconds: runtime.finish_active(), config={"max_wallclock_seconds": 10},
        )
        thread = threading.Thread(target=orchestrator._run, args=(run["id"],))
        thread.start()
        self.assertTrue(entered.wait(2))
        orchestrator.cancel(run["id"])
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        final = self.store.get_orchestration(run["id"])
        self.assertEqual(final["status"], "Cancelled")
        self.assertEqual(final["recoveries"], [])
        self.assertEqual(final["graph_summary"]["counts"]["cancelled"], 1)


if __name__ == "__main__":
    unittest.main()
