import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from control_center.api import Application
from control_center.config import normalize_agent
from control_center.orchestrator import Orchestrator
from control_center.planner import (
    MAX_PLAN_TASKS,
    MAX_GOAL_CHARS,
    OllamaPlanner,
    PLAN_SCHEMA_VERSION,
    PlanGenerationError,
    PlanValidationError,
    Planner,
    fallback_plan,
    normalize_plan,
    validate_plan,
)
from control_center.storage import Store
from control_center.transport import TransportError


def task(task_id="task-1", objective="Complete the request", **changes):
    value = {
        "id": task_id,
        "objective": objective,
        "description": "Perform the bounded work and collect evidence.",
        "depends_on": [],
        "required_capabilities": [],
        "preferred_skills": [],
        "success_criteria": ["The task outcome is verified."],
    }
    value.update(changes)
    return value


def plan(tasks=None, complexity="simple", **changes):
    value = {
        "goal": "Complete the requested work",
        "summary": "Complete the work safely and verify the outcome.",
        "complexity": complexity,
        "tasks": tasks or [task()],
        "success_criteria": ["The requested outcome is complete."],
    }
    value.update(changes)
    return value


def multi_step_plan():
    return plan(complexity="multi_step", tasks=[
        task("inspect-auth", "Inspect authentication", required_capabilities=["filesystem.read"]),
        task("diagnose-auth", "Diagnose the login bug", depends_on=["inspect-auth"],
             required_capabilities=["filesystem.read"]),
        task("implement-fix", "Implement the minimal fix", depends_on=["diagnose-auth"],
             required_capabilities=["filesystem.modify"]),
        task("verify-fix", "Run relevant tests", depends_on=["implement-fix"],
             required_capabilities=["execution.pytest"]),
    ])


def enter_planning(store, orchestration_id):
    return store.transition_orchestration(orchestration_id, ("Queued",), "Planning")


class PlannerValidationTests(unittest.TestCase):
    def test_valid_simple_plan_has_one_task(self):
        result = validate_plan(plan())
        self.assertEqual(result["complexity"], "simple")
        self.assertEqual(len(result["tasks"]), 1)

    def test_valid_multi_step_plan_preserves_dependency_order(self):
        result = validate_plan(multi_step_plan())
        self.assertGreater(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][2]["depends_on"], ["diagnose-auth"])
        self.assertEqual(result["tasks"][3]["depends_on"], ["implement-fix"])

    def test_normalization_is_stable_and_deduplicates_lists(self):
        raw = plan(tasks=[task(" Inspect Auth ", "  Inspect   auth  ",
                               depends_on=[],
                               required_capabilities=["filesystem.read", " filesystem.read "],
                               preferred_skills=["Python Development", "python-development"],
                               success_criteria=[" Evidence found ", "Evidence found"])],
                   goal="  Complete   work ")
        result = normalize_plan(raw)
        self.assertEqual(result["goal"], "Complete work")
        self.assertEqual(result["tasks"][0]["id"], "inspect-auth")
        self.assertEqual(result["tasks"][0]["required_capabilities"], ["filesystem.read"])
        self.assertEqual(result["tasks"][0]["preferred_skills"], ["python-development"])
        self.assertEqual(result["tasks"][0]["success_criteria"], ["Evidence found"])

    def test_complexity_is_canonicalized_from_task_count(self):
        self.assertEqual(normalize_plan(plan(complexity="multi_step"))["complexity"], "simple")
        mismatched = plan([task("one"), task("two", depends_on=["one"])], complexity="simple")
        self.assertEqual(normalize_plan(mismatched)["complexity"], "multi_step")

    def test_duplicate_normalized_task_ids_are_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "unique"):
            validate_plan(plan([task("Inspect Auth"), task("inspect-auth")], complexity="multi_step"))

    def test_missing_dependency_is_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "unknown task"):
            validate_plan(plan(tasks=[task(depends_on=["missing"])]))

    def test_self_dependency_is_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "itself"):
            validate_plan(plan(tasks=[task(depends_on=["task-1"])]))

    def test_dependency_cycle_is_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "cycle"):
            validate_plan(plan([task("a", depends_on=["b"]), task("b", depends_on=["a"])],
                               complexity="multi_step"))

    def test_unknown_capability_is_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "Unknown capability"):
            validate_plan(plan(tasks=[task(required_capabilities=["network.unrestricted"])]))

    def test_too_many_tasks_are_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "limit"):
            validate_plan(plan([task(f"task-{index}") for index in range(MAX_PLAN_TASKS + 1)],
                               complexity="multi_step"))

    def test_wrong_types_and_unknown_fields_are_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "must be an object"):
            validate_plan([])
        invalid = plan()
        invalid["extra"] = True
        with self.assertRaisesRegex(PlanValidationError, "unknown fields"):
            validate_plan(invalid)
        invalid = plan()
        invalid["tasks"] = "task"
        with self.assertRaisesRegex(PlanValidationError, "must be a list"):
            validate_plan(invalid)


class PlannerGenerationTests(unittest.TestCase):
    def test_model_json_can_create_multi_step_plan(self):
        result = Planner(lambda prompt, context: json.dumps(multi_step_plan())).create_plan(
            "Inspect authentication, diagnose it, fix it and run tests.",
            {"capabilities": [{"id": "filesystem.read"}]},
        )
        self.assertEqual(result["complexity"], "multi_step")
        self.assertEqual(len(result["tasks"]), 4)

    def test_invalid_json_receives_one_successful_repair(self):
        calls = []

        def decide(prompt, context):
            calls.append(prompt)
            return "not json" if len(calls) == 1 else json.dumps(plan())

        self.assertEqual(Planner(decide).create_plan("Create hello.txt")["complexity"], "simple")
        self.assertEqual(len(calls), 2)
        self.assertIn("Repair it once", calls[1])

    def test_second_invalid_output_fails_explicitly(self):
        calls = []

        def decide(prompt, context):
            calls.append(prompt)
            return "still not json"

        with self.assertRaisesRegex(PlanGenerationError, "one repair attempt"):
            Planner(decide).create_plan("Create hello.txt")
        self.assertEqual(len(calls), 2)

    def test_fallback_is_deterministic_and_safe(self):
        first = fallback_plan("Create hello.txt")
        second = Planner(offline=True).create_plan("Create hello.txt")
        self.assertEqual(first, second)
        self.assertEqual(first["complexity"], "simple")
        self.assertEqual(len(first["tasks"]), 1)
        self.assertEqual(first["tasks"][0]["required_capabilities"], [])


class PlannerPersistenceAndEventsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_plan_is_persisted_and_recovered_after_store_restart(self):
        run = self.store.create_orchestration("Create hello.txt")
        expected = fallback_plan("Create hello.txt")
        enter_planning(self.store, run["id"])
        saved = self.store.save_orchestration_plan(run["id"], expected, PLAN_SCHEMA_VERSION)
        self.assertEqual(saved["plan"], expected)
        self.assertEqual(saved["plan_schema_version"], PLAN_SCHEMA_VERSION)
        self.assertTrue(saved["plan_created_at"])
        reopened = Store(self.path).get_orchestration(run["id"])
        self.assertEqual(reopened["plan"], expected)

    def test_existing_orchestration_table_is_migrated_in_place(self):
        legacy_path = Path(self.temporary.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            "CREATE TABLE orchestration_runs (id TEXT PRIMARY KEY,prompt TEXT NOT NULL,"
            "status TEXT NOT NULL DEFAULT 'Queued',response TEXT NOT NULL DEFAULT '',error TEXT,"
            "config_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO orchestration_runs(id,prompt,created_at,updated_at) VALUES(?,?,?,?)",
            ("legacy-run", "Legacy request", "before", "before"),
        )
        connection.commit()
        connection.close()
        migrated = Store(legacy_path).get_orchestration("legacy-run")
        self.assertIsNone(migrated["plan"])
        self.assertIsNone(migrated["plan_schema_version"])
        self.assertIsNone(migrated["plan_created_at"])

    def test_plan_snapshot_is_immutable_and_unaffected_by_agent_edits(self):
        agent = self.store.create_agent(normalize_agent({"name": "Atlas", "role": "Engineer"}))
        run = self.store.create_orchestration("Create hello.txt")
        expected = fallback_plan("Create hello.txt")
        enter_planning(self.store, run["id"])
        self.store.save_orchestration_plan(run["id"], expected, PLAN_SCHEMA_VERSION)
        self.assertIsNone(self.store.save_orchestration_plan(
            run["id"], multi_step_plan(), PLAN_SCHEMA_VERSION,
        ))
        updated = normalize_agent({"name": "Atlas changed"}, self.store.get_agent(agent["id"]))
        self.store.update_agent(agent["id"], updated)
        self.assertEqual(self.store.get_orchestration(run["id"])["plan"], expected)

    def test_successful_planning_emits_started_and_created_events(self):
        run = self.store.create_orchestration("Create hello.txt")
        Orchestrator(self.store, None, decide=lambda prompt, agents, results:
                     {"action": "respond", "message": "planned"},
                     planner=Planner(offline=True))._run(run["id"])
        stored = self.store.get_orchestration(run["id"])
        event_types = [event["event_type"] for event in stored["events"]]
        self.assertIn("freya.planning.started", event_types)
        self.assertIn("freya.plan.created", event_types)
        created = next(event for event in stored["events"] if event["event_type"] == "freya.plan.created")
        payload = json.loads(created["payload_json"])
        self.assertEqual(payload["task_count"], 1)
        self.assertEqual(payload["task_ids"], ["task-1"])

    def test_failed_planning_emits_failure_without_plan(self):
        run = self.store.create_orchestration("Create hello.txt")
        broken = Planner(lambda prompt, context: "not json")
        Orchestrator(self.store, None, planner=broken)._run(run["id"])
        stored = self.store.get_orchestration(run["id"])
        self.assertEqual(stored["status"], "Failed")
        self.assertIsNone(stored["plan"])
        self.assertEqual(stored["planning_metrics"]["model_calls"], 2)
        self.assertIn("freya.planning.failed", [event["event_type"] for event in stored["events"]])

    def test_ollama_metrics_are_persisted_with_the_plan(self):
        def transport(method, url, body, **kwargs):
            return {"message": {"content": json.dumps(plan())},
                    "prompt_eval_count": 13, "eval_count": 8}

        run = self.store.create_orchestration("Create hello.txt")
        orchestrator = Orchestrator(
            self.store, None,
            decide=lambda prompt, agents, results: {"action": "respond", "message": "done"},
            planner=Planner(OllamaPlanner(request=transport)),
        )
        orchestrator._run(run["id"])
        stored = self.store.get_orchestration(run["id"])
        self.assertEqual(stored["status"], "Success")
        self.assertEqual(stored["planning_metrics"]["model_calls"], 1)
        self.assertEqual(stored["planning_metrics"]["total_tokens"], 21)

    def test_api_exposes_plan_with_version_metadata(self):
        run = self.store.create_orchestration("Create hello.txt")
        expected = fallback_plan("Create hello.txt")
        enter_planning(self.store, run["id"])
        self.store.save_orchestration_plan(run["id"], expected, PLAN_SCHEMA_VERSION)
        app = Application(self.store, object(), Path(self.temporary.name))
        status, body = app.dispatch("GET", f"/api/orchestrations/{run['id']}", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(body["plan"], expected)
        status, body = app.dispatch("GET", f"/api/orchestrations/{run['id']}/plan", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(body["plan_schema_version"], PLAN_SCHEMA_VERSION)

    def test_planner_capabilities_are_declarative_and_do_not_change_policy(self):
        agent = self.store.create_agent(normalize_agent({"name": "Atlas", "role": "Engineer"}))
        before = self.store.get_agent(agent["id"])["config"]["capability_policy"]
        declared = plan(tasks=[task(required_capabilities=["filesystem.modify"])])
        run = self.store.create_orchestration("Modify a file")
        enter_planning(self.store, run["id"])
        self.store.save_orchestration_plan(run["id"], validate_plan(declared), PLAN_SCHEMA_VERSION)
        after = self.store.get_agent(agent["id"])["config"]["capability_policy"]
        self.assertEqual(after, before)
        self.assertEqual(self.store.get_orchestration(run["id"])["plan"]["tasks"][0]
                         ["required_capabilities"], ["filesystem.modify"])


class DeterministicRuntime:
    def __init__(self, store, outcomes=None):
        self.store = store
        self.outcomes = outcomes or {}
        self.cancelled = []
        self.submitted = threading.Event()

    def submit(self, agent_id, objective, workspace_path=None):
        run = self.store.list_orchestrations(1)[0]
        if run["plan"] is None or run["status"] != "Running":
            raise AssertionError("Plan must be persisted before delegation.")
        created = self.store.create_task(agent_id, objective, workspace_path or "workspace")
        status, result = self.outcomes.get(objective, ("Success", objective + " result"))
        fields = {"status": status, "result": result}
        if status in {"Success", "Failed", "Cancelled"}:
            fields["finished_at"] = "2026-09-17T00:00:00+00:00"
        if status == "Failed":
            fields["error"] = str(result)
        task_result = self.store.update_task(created["id"], **fields)
        self.submitted.set()
        return task_result

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        task = self.store.get_task(task_id)
        if task["status"] not in {"Success", "Failed", "Cancelled"}:
            task = self.store.update_task(
                task_id, status="Cancelled", error="Cancelled by orchestration.",
                finished_at="2026-09-17T00:00:01+00:00",
            )
        return task


class OrchestrationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, name="Agent"):
        return self.store.create_agent(normalize_agent({"name": name, "role": "Engineer"}))

    def test_cancellation_while_planner_is_blocked_cannot_resurrect(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_planner(prompt, context):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test planner was not released")
            return plan()

        run = self.store.create_orchestration("Create hello.txt")
        orchestrator = Orchestrator(
            self.store, DeterministicRuntime(self.store),
            decide=lambda prompt, agents, results: {"action": "respond", "message": "late"},
            planner=Planner(blocked_planner),
        )
        thread = threading.Thread(target=orchestrator._run, args=(run["id"],))
        thread.start()
        self.assertTrue(entered.wait(2))
        cancelled = orchestrator.cancel(run["id"])
        events_at_cancel = list(cancelled["events"])
        self.assertEqual(cancelled["status"], "Cancelled")
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        final = self.store.get_orchestration(run["id"])
        self.assertEqual(final["status"], "Cancelled")
        self.assertIsNone(final["plan"])
        self.assertEqual(final["delegations"], [])
        self.assertEqual(final["events"], events_at_cancel)
        self.assertNotIn("freya.plan.created", [event["event_type"] for event in final["events"]])

    def test_terminal_cancellation_is_idempotent_and_cannot_become_active(self):
        run = self.store.create_orchestration("Create hello.txt")
        orchestrator = Orchestrator(self.store, DeterministicRuntime(self.store),
                                    planner=Planner(offline=True))
        first = orchestrator.cancel(run["id"])
        second = orchestrator.cancel(run["id"])
        self.assertEqual(first["status"], "Cancelled")
        self.assertEqual(len(first["events"]), len(second["events"]))
        with self.assertRaisesRegex(ValueError, "state is final"):
            self.store.transition_orchestration(run["id"], ("Cancelled",), "Running")
        self.assertEqual(self.store.get_orchestration(run["id"])["status"], "Cancelled")

    def test_recovery_fails_all_active_states_once_and_preserves_plan(self):
        runs = {}
        for status in ("Queued", "Planning", "Planned", "Running"):
            run = self.store.create_orchestration(status)
            if status != "Queued":
                enter_planning(self.store, run["id"])
            if status in {"Planned", "Running"}:
                self.store.save_orchestration_plan(
                    run["id"], fallback_plan(status), PLAN_SCHEMA_VERSION,
                )
            if status == "Running":
                self.store.transition_orchestration(run["id"], ("Planned",), "Running")
            runs[status] = run["id"]
        terminal = self.store.create_orchestration("terminal")
        self.store.transition_orchestration(terminal["id"], ("Queued",), "Success")

        self.assertEqual(self.store.recover_interrupted_orchestrations(), 4)
        for original, oid in runs.items():
            recovered = self.store.get_orchestration(oid)
            self.assertEqual(recovered["status"], "Failed")
            self.assertEqual([event["event_type"] for event in recovered["events"]],
                             ["freya.interrupted"])
            if original in {"Planned", "Running"}:
                self.assertIsNotNone(recovered["plan"])
        self.assertEqual(self.store.get_orchestration(terminal["id"])["status"], "Success")
        self.assertEqual(self.store.recover_interrupted_orchestrations(), 0)
        for oid in runs.values():
            self.assertEqual(len(self.store.get_orchestration(oid)["events"]), 1)

    def test_waiting_for_approval_is_waited_for_and_cancelled(self):
        agent = self.agent()
        runtime = DeterministicRuntime(self.store, {"needs approval": ("WaitingForApproval", None)})
        orchestrator = Orchestrator(
            self.store, runtime,
            decide=lambda prompt, agents, results: {
                "action": "delegate", "tasks": [{"agent_id": agent["id"], "objective": "needs approval"}],
            },
            planner=Planner(offline=True), config={"max_wallclock_seconds": 30},
        )
        run = self.store.create_orchestration("Do protected work")
        thread = threading.Thread(target=orchestrator._run, args=(run["id"],))
        thread.start()
        self.assertTrue(runtime.submitted.wait(2))
        cancelled = orchestrator.cancel(run["id"])
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(cancelled["status"], "Cancelled")
        final = self.store.get_orchestration(run["id"])
        self.assertEqual(final["status"], "Cancelled")
        self.assertEqual(final["delegations"][0]["status"], "Cancelled")
        self.assertEqual(len(runtime.cancelled), 1)

    def test_child_failure_fails_parent_and_persists_delegation_results(self):
        first, second = self.agent("First"), self.agent("Second")
        runtime = DeterministicRuntime(self.store, {
            "successful child": ("Success", "done"),
            "failed child": ("Failed", "child exploded"),
        })
        orchestrator = Orchestrator(
            self.store, runtime,
            decide=lambda prompt, agents, results: {"action": "delegate", "tasks": [
                {"agent_id": first["id"], "objective": "successful child"},
                {"agent_id": second["id"], "objective": "failed child"},
            ]}, planner=Planner(offline=True),
        )
        run = self.store.create_orchestration("Coordinate children")
        orchestrator._run(run["id"])
        final = self.store.get_orchestration(run["id"])
        self.assertEqual(final["status"], "Failed")
        self.assertIn("Delegated task failure", final["error"])
        self.assertEqual([item["status"] for item in final["delegations"]], ["Success", "Failed"])
        self.assertTrue(all(item["finished_at"] for item in final["delegations"]))
        self.assertEqual(final["delegations"][0]["result"]["result"], "done")
        self.assertEqual(final["delegations"][1]["result"]["error"], "child exploded")

    def test_single_deadline_includes_planning_and_timeout_cancels_children(self):
        agent = self.agent()

        class Clock:
            value = 0.0
            def __call__(self):
                return self.value

        clock = Clock()
        waited = []

        def planned_late(prompt, context):
            clock.value = 9.0
            return plan()

        def advance(seconds):
            waited.append(seconds)
            clock.value += seconds

        runtime = DeterministicRuntime(self.store, {"slow child": ("Running", None)})
        orchestrator = Orchestrator(
            self.store, runtime,
            decide=lambda prompt, agents, results: {
                "action": "delegate", "tasks": [{"agent_id": agent["id"], "objective": "slow child"}],
            }, planner=Planner(planned_late), config={"max_wallclock_seconds": 10},
            clock=clock, wait=advance,
        )
        run = self.store.create_orchestration("Use one deadline")
        orchestrator._run(run["id"])
        final = self.store.get_orchestration(run["id"])
        self.assertEqual(final["status"], "Failed")
        self.assertIn("time limit", final["error"])
        self.assertEqual(len(runtime.cancelled), 1)
        self.assertLessEqual(round(sum(waited), 6), 1.0)
        self.assertEqual(final["delegations"][0]["status"], "Cancelled")


class OllamaPlannerTests(unittest.TestCase):
    def test_simulated_ollama_produces_multi_step_plan_without_tools(self):
        requests = []

        def transport(method, url, body, **kwargs):
            requests.append((method, url, body, kwargs))
            return {"message": {"content": json.dumps(multi_step_plan())},
                    "prompt_eval_count": 41, "eval_count": 29,
                    "thinking": "must not be persisted"}

        planner = Planner(OllamaPlanner(
            model="planner-test", endpoint="http://127.0.0.1:11434",
            timeout_seconds=7, request=transport,
        ))
        result = planner.create_plan("Inspect, diagnose, fix and test", {
            "capabilities": [{"id": "filesystem.read"}], "agents": [], "skills": [],
        })
        self.assertEqual(result["complexity"], "multi_step")
        method, url, body, kwargs = requests[0]
        self.assertEqual((method, url), ("POST", "http://127.0.0.1:11434/api/chat"))
        self.assertEqual(body["tools"], [])
        self.assertEqual(body["format"]["type"], "object")
        self.assertFalse(body["format"]["additionalProperties"])
        self.assertFalse(body["stream"])
        self.assertFalse(body["think"])
        self.assertEqual(body["options"]["temperature"], 0.1)
        self.assertEqual(kwargs["timeout"], 7)
        self.assertEqual(planner.metrics["model_calls"], 1)
        self.assertEqual(planner.metrics["total_tokens"], 70)
        self.assertNotIn("must not be persisted", json.dumps(result) + json.dumps(planner.metrics))

    def test_provider_failure_is_explicit_and_never_falls_back(self):
        def unavailable(*args, **kwargs):
            raise TransportError("Ollama unavailable")

        planner = Planner(OllamaPlanner(request=unavailable))
        with self.assertRaisesRegex(PlanGenerationError, "Planner model call failed"):
            planner.create_plan("Create hello.txt", {"capabilities": []})
        self.assertEqual(planner.metrics["model_calls"], 1)

    def test_ollama_invalid_json_gets_exactly_one_repair(self):
        responses = iter([
            {"message": {"content": "invalid"}, "prompt_eval_count": 2, "eval_count": 1},
            {"message": {"content": json.dumps(plan())}, "prompt_eval_count": 3, "eval_count": 2},
        ])
        calls = []

        def transport(method, url, body, **kwargs):
            calls.append(body)
            return next(responses)

        planner = Planner(OllamaPlanner(request=transport))
        self.assertEqual(planner.create_plan("Create hello.txt", {"capabilities": []})["complexity"],
                         "simple")
        self.assertEqual(len(calls), 2)
        self.assertEqual(planner.metrics["model_calls"], 2)
        self.assertEqual(planner.metrics["total_tokens"], 8)

    def test_planning_context_excludes_secrets_logs_skill_procedures_and_results(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            store.create_skill({
                "id": "private-procedure", "name": "Private procedure", "description": "Summary",
                "category": "Testing", "version": 1, "instructions": ["DO_NOT_SEND_INSTRUCTION"],
                "procedures": [{"name": "Hidden", "steps": ["DO_NOT_SEND_PROCEDURE"]}],
                "recommended_capabilities": [], "required_capabilities": [], "tags": ["safe-tag"],
                "source": "user", "metadata": {}, "enabled": True,
            })
            agent = store.create_agent(normalize_agent({
                "name": "Planner candidate", "role": "Engineer",
                "skills": [{"id": "private-procedure"}],
                "config": {"secret_env": "ACC_SECRET_DO_NOT_SEND"},
            }))
            historical = store.create_task(agent["id"], "old", "workspace")
            store.update_task(historical["id"], status="Success", result="DO_NOT_SEND_RESULT")
            store.append_event(historical["id"], {"event_type": "log", "output": "DO_NOT_SEND_LOG"})
            orchestrator = Orchestrator(store, None, planner=Planner(offline=True))
            rendered = json.dumps(orchestrator._planning_context(store.list_agents()))
            self.assertNotIn("ACC_SECRET_DO_NOT_SEND", rendered)
            self.assertNotIn("DO_NOT_SEND_INSTRUCTION", rendered)
            self.assertNotIn("DO_NOT_SEND_PROCEDURE", rendered)
            self.assertNotIn("DO_NOT_SEND_RESULT", rendered)
            self.assertNotIn("DO_NOT_SEND_LOG", rendered)
            self.assertIn("private-procedure", rendered)


if __name__ == "__main__":
    unittest.main()
