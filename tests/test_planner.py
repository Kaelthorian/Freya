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
from control_center.task_analyst import TaskAnalyst, deterministic_task_analysis
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
        self.assertEqual(len(result["tasks"]), 5)
        self.assertEqual(result["tasks"][-1]["preferred_skills"], ["code-review"])

    def test_linear_single_artifact_plan_adds_one_audit_task(self):
        raw = plan(
            complexity="multi_step",
            goal="Create a calculator suma.bat that asks for two numbers and returns their sum.",
            tasks=[
                task("create-file", "Create suma.bat", required_capabilities=["filesystem.create"]),
                task("write-content", "Write the calculator code into suma.bat", depends_on=["create-file"],
                     required_capabilities=["filesystem.modify"]),
                task("verify-file", "Verify suma.bat", depends_on=["write-content"],
                     required_capabilities=["filesystem.read"]),
            ],
        )
        result = Planner(lambda prompt, context: json.dumps(raw)).create_plan(raw["goal"])
        self.assertEqual(result["complexity"], "multi_step")
        self.assertEqual(len(result["tasks"]), 2)
        self.assertIn("suma.bat", result["tasks"][0]["objective"])
        self.assertEqual(result["tasks"][0]["required_capabilities"],
                         ["filesystem.create", "filesystem.modify", "filesystem.read"])
        self.assertEqual(result["tasks"][1]["preferred_skills"], ["code-review"])

    def test_trivial_program_analysis_uses_one_implementation_task(self):
        raw = plan(
            complexity="simple",
            goal="Create and run a Hello World program.",
            tasks=[task(
                "hello", "Create a Hello World Python program",
                required_capabilities=["filesystem.create", "execution.python_script"],
                success_criteria=["The program outputs 'Hello World' when executed"],
            )],
        )
        analysis = {
            "task_type": "Program Creation",
            "task_characteristics": {
                "interactive": False, "requires_user_input": False,
                "long_running": False, "requires_external_service": False,
                "requires_gui": False, "requires_elevated_privileges": False,
            },
        }
        result = Planner(lambda prompt, context: json.dumps(raw)).create_plan(
            raw["goal"], {"task_analysis": analysis}
        )
        self.assertEqual(result["complexity"], "simple")
        self.assertEqual([item["id"] for item in result["tasks"]], ["hello"])
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

    def test_failure_logs_serialize_structured_input_and_output(self):
        run = self.store.create_orchestration("Create hello.txt")
        enter_planning(self.store, run["id"])
        plan = fallback_plan("Create hello.txt")
        self.store.save_orchestration_plan(run["id"], plan, PLAN_SCHEMA_VERSION)
        graph = ExecutionGraph(plan)
        self.store.initialize_execution_graph(run["id"], graph.serialize())
        self.store.add_orchestration_event(run["id"], {
            "event_type": "freya.test.structured_failure", "status": "Failed",
            "input": {"argv": ["python", "hello.py"], "nested": [1, {"safe": True}]},
            "output": {"error": "policy denied", "details": ["existing.py"]},
            "message": "Structured failure fixture.",
        })
        logs = Orchestrator(self.store, None)._failure_logs(run["id"], graph)
        entry = next(item for item in logs if item.get("event_type") == "freya.test.structured_failure")
        self.assertIn('"safe":true', entry["input"])
        self.assertIn('"existing.py"', entry["output"])

    def test_orchestration_allocates_one_persisted_workspace_for_all_nodes(self):
        class RuntimeStub:
            def __init__(self, data_dir):
                self.data_dir = data_dir

        run = self.store.create_orchestration("Create and audit a file", {"workspace_path": ""})
        orchestrator = Orchestrator(self.store, RuntimeStub(self.temporary.name), planner=Planner(offline=True))
        first = orchestrator._workspace_for_run(run)
        second = orchestrator._workspace_for_run(self.store.get_orchestration(run["id"]))
        self.assertEqual(first, second)
        self.assertTrue(Path(first).is_dir())
        self.assertEqual(self.store.get_orchestration(run["id"])["config"]["workspace_path"], first)
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

    def test_blocked_task_analysis_stops_before_plan_or_delegation(self):
        analyst = self.store.create_agent(normalize_agent({
            "name": "Task Analyst", "config": {"orchestration_role": "task_analyst"},
        }))
        calls = []

        class BlockedAdapter:
            metrics = {"model_calls": 1}

            def analyze(self, prompt, agent):
                analysis = deterministic_task_analysis(prompt)
                analysis["ready_for_execution"] = False
                analysis["blocking_reason"] = (
                    "The user needs to specify the target database and authentication method."
                )
                return analysis

        run = self.store.create_orchestration("Connect the service to a database.")
        orchestrator = Orchestrator(
            self.store, None,
            task_analyst=TaskAnalyst(BlockedAdapter()),
            planner=Planner(lambda prompt, context: calls.append(True)),
        )
        orchestrator._run(run["id"])
        stored = self.store.get_orchestration(run["id"])
        self.assertEqual(stored["status"], "Failed")
        self.assertIsNone(stored["plan"])
        self.assertEqual(calls, [])
        self.assertIn("target database", stored["error"])
        self.assertIn("freya.task_analysis.blocked", [event["event_type"] for event in stored["events"]])

    def test_graph_timeout_runs_failure_analysis_before_failing(self):
        run = self.store.create_orchestration("Human source prompt")
        enter_planning(self.store, run["id"])
        operational_plan = fallback_plan("Task Analyst operational brief")
        self.store.save_orchestration_plan(run["id"], operational_plan, PLAN_SCHEMA_VERSION)
        self.store.initialize_execution_graph(
            run["id"], ExecutionGraph(operational_plan).serialize(),
        )
        self.store.transition_orchestration(run["id"], ("Planned",), "Running")
        Orchestrator(self.store, None, planner=Planner(offline=True))._timeout(run["id"])
        final = self.store.get_orchestration(run["id"])
        event_types = [event["event_type"] for event in final["events"]]
        self.assertEqual(final["status"], "Failed")
        self.assertIn("freya.timeout", event_types)
        self.assertIn("freya.failure_analysis.started", event_types)
        self.assertIn("freya.failure_analysis.completed", event_types)
        self.assertIn("time limit", (final["error"] or "").lower())

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
        marker = "DELEGATED PLAN STEP:\n"
        label = objective.split(marker, 1)[1].split("\n\n", 1)[0] if marker in objective else objective
        status, result = self.outcomes.get(label, self.outcomes.get(objective, ("Success", objective + " result")))
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
        self.store.transition_orchestration(terminal["id"], ("Queued",), "Success",
                                            legacy_without_graph=True)

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


class TaskAnalysisPlannerTests(unittest.TestCase):
    def test_task_kind_reconciles_verification_capabilities_for_python(self):
        analysis = deterministic_task_analysis("Crea un programa Python que imprima Hello World")
        generated = plan(tasks=[task(
            required_capabilities=["filesystem.create"],
            preferred_skills=[],
            success_criteria=["The program outputs 'Hello World' when executed"],
        )])
        result = Planner(lambda prompt, context: generated).create_plan(
            analysis["operational_prompt"], {"task_analysis": analysis},
        )
        first = result["tasks"][0]
        self.assertEqual(analysis["task_kind"], "program_creation")
        self.assertIn("filesystem.create", first["required_capabilities"])
        self.assertIn("filesystem.read", first["required_capabilities"])
        self.assertIn("execution.python_script", first["required_capabilities"])
        self.assertIn("python-development", first["preferred_skills"])
        self.assertEqual(len(result["tasks"]), 1)

    def test_inconsistent_model_hello_world_plan_is_repaired_before_factory(self):
        analysis = deterministic_task_analysis("Execute a simple 'Hello World' program.")
        analysis["operational_prompt"] = "{}"
        analysis["task_kind"] = "file_creation"
        analysis["task_characteristics"] = dict(analysis["task_characteristics"])
        analysis["task_characteristics"].update(
            requires_filesystem_read=False, requires_filesystem_write=False,
        )
        generated = plan(
            complexity="multi_step",
            goal="{}",
            tasks=[
                task("t1", "Execute a simple Hello World Python program",
                     required_capabilities=[
                         "filesystem.create", "filesystem.overwrite", "execution.python_script",
                     ], preferred_skills=["python-development"]),
                task("code-audit", "Audit the generated code", depends_on=["t1"],
                     required_capabilities=["filesystem.read"], preferred_skills=["code-review"]),
            ],
        )
        result = Planner(lambda prompt, context: generated).create_plan(
            "Execute a simple 'Hello World' program.", {"task_analysis": analysis},
        )
        first = result["tasks"][0]
        self.assertEqual(result["complexity"], "simple")
        self.assertEqual([item["id"] for item in result["tasks"]], ["t1"])
        self.assertEqual(first["preferred_skills"][0], "python-development")
        self.assertEqual(first["required_capabilities"], [
            "filesystem.create", "execution.python_script", "filesystem.read",
        ])
        self.assertNotIn("filesystem.overwrite", first["required_capabilities"])

    def test_windows_script_analysis_prevents_python_only_plan(self):
        generated = plan(tasks=[task(
            required_capabilities=["execution.python_script"],
            preferred_skills=["python-development"],
        )])
        planner = Planner(lambda prompt, context: generated)
        analysis = {
            "task_type": "windows_command_script",
            "task_characteristics": {"requires_filesystem_write": True},
        }
        result = planner.create_plan("Create a calculator CMD script", {"task_analysis": analysis})
        first = result["tasks"][0]
        self.assertIn("filesystem.create", first["required_capabilities"])
        self.assertNotIn("execution.python_script", first["required_capabilities"])
        self.assertNotIn("python-development", first["preferred_skills"])
        self.assertRegex(first["description"], r"CMD/BAT|\.cmd|\.bat")

    def test_task_analysis_is_sent_to_planner_prompt(self):
        seen = {}
        def decide(prompt, context):
            seen["prompt"] = prompt
            return plan()
        planner = Planner(decide)
        analysis = {"task_type": "windows_command_script", "task_characteristics": {"requires_filesystem_write": True}}
        planner.create_plan("Create a .bat file", {"task_analysis": analysis})
        self.assertIn("Task Analyst structured analysis", seen["prompt"])
        self.assertIn("windows_command_script", seen["prompt"])

    def test_interactive_analysis_inserts_qa_before_code_audit(self):
        generated = plan(tasks=[task(
            "implement", "Implement calculator",
            required_capabilities=["filesystem.create", "execution.python_script"],
            preferred_skills=["python-development"],
        )])
        analysis = {
            "task_type": "python_cli",
            "task_characteristics": {
                "requires_filesystem_write": True,
                "interactive": True,
                "requires_user_input": True,
            },
            "validation": {"interactive_validation_required": True},
        }
        result = Planner(lambda prompt, context: generated).create_plan(
            "Create an interactive calculator", {"task_analysis": analysis},
        )
        self.assertEqual([item["id"] for item in result["tasks"]],
                         ["implement", "qa-interactive-test", "code-audit"])
        self.assertEqual(result["tasks"][1]["depends_on"], ["implement"])
        self.assertIn("interactive-testing", result["tasks"][1]["preferred_skills"])
        self.assertEqual(result["tasks"][2]["depends_on"],
                         ["implement", "qa-interactive-test"])

    def test_existing_model_audit_is_normalized_after_qa(self):
        generated = plan(complexity="multi_step", tasks=[
            task("implement", "Implement calculator", required_capabilities=["filesystem.create"]),
            task("review", "Perform code review", depends_on=["implement"],
                 required_capabilities=["filesystem.read"], preferred_skills=["code-review"]),
        ])
        analysis = {
            "task_type": "python_cli",
            "task_characteristics": {
                "requires_filesystem_write": True, "interactive": True,
                "requires_user_input": True,
            },
            "validation": {"interactive_validation_required": True},
        }
        result = Planner(lambda prompt, context: generated).create_plan(
            "Interactive calculator", {"task_analysis": analysis},
        )
        self.assertEqual([item["id"] for item in result["tasks"]],
                         ["implement", "qa-interactive-test", "review"])
        self.assertEqual(result["tasks"][-1]["depends_on"],
                         ["implement", "qa-interactive-test"])


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
