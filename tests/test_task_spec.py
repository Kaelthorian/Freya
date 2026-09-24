"""Canonical Task Spec, clarification lifecycle and semantic plan compiler."""
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_center.api import Application
from control_center.orchestrator import Orchestrator
from control_center.plan_compiler import compile_semantic_plan
from control_center.planner import PlanGenerationError, Planner
from control_center.storage import Store
from control_center.task_spec import (
    TaskSpecAnalyst, deterministic_task_spec, render_task_spec,
    revise_ready_task_spec, validate_task_spec,
)


class CountingPlanner(Planner):
    def __init__(self):
        super().__init__(offline=True)
        self.calls = []

    def create_plan_for_spec(self, spec, context=None):
        self.calls.append(spec)
        return super().create_plan_for_spec(spec, context)


class TaskSpecTests(unittest.TestCase):
    def test_clear_request_is_ready_without_questions(self):
        spec = deterministic_task_spec("crea una calculadora usando Python, de consola, que solo sume")
        self.assertEqual(spec["status"], "READY_FOR_PLANNING")
        self.assertEqual(spec["clarification_questions"], [])
        self.assertIn("Python", spec["objective"])
        self.assertNotIn("source_prompt", render_task_spec(spec))

    def test_vague_calculator_waits_for_high_impact_answers(self):
        spec = deterministic_task_spec("haz una calculadora")
        self.assertEqual(spec["status"], "NEEDS_CLARIFICATION")
        self.assertEqual({q["field"] for q in spec["clarification_questions"]},
                         {"interface", "operations"})
        self.assertEqual(spec["version"], 1)

    def test_other_vague_requests_keep_the_material_question(self):
        self.assertEqual(
            deterministic_task_spec("monta un servidor de zomboid")["status"],
            "NEEDS_CLARIFICATION")
        self.assertEqual(
            deterministic_task_spec("automatiza esto")["status"],
            "NEEDS_CLARIFICATION")
        self.assertEqual(
            deterministic_task_spec("haz una web para gestionar alumnos")["status"],
            "READY_FOR_PLANNING")

    def test_console_program_without_language_records_python_default(self):
        spec = deterministic_task_spec("crea una calculadora de consola que sume")
        self.assertEqual(spec["status"], "READY_FOR_PLANNING")
        self.assertTrue(any("Python 3.10+" in item["description"]
                            for item in spec["assumptions"]))

    def test_explicit_language_beats_python_default(self):
        spec = deterministic_task_spec("crea una calculadora de consola en JavaScript que sume")
        self.assertEqual(spec["status"], "READY_FOR_PLANNING")
        self.assertIn("JavaScript", spec["objective"])
        self.assertFalse(any("Python 3.10+" in item["description"]
                             for item in spec["assumptions"]))

    def test_named_python_script_uses_safe_default(self):
        spec = deterministic_task_spec("crea calculator.py en Python que sume dos números")
        self.assertEqual(spec["status"], "READY_FOR_PLANNING")
        self.assertEqual(spec["clarification_questions"], [])
        self.assertTrue(any("consola" in item["description"].casefold()
                            for item in spec["assumptions"]))

    def test_user_revision_is_versioned_and_traced(self):
        spec = deterministic_task_spec("crea una calculadora de consola en Python que sume")
        changed = revise_ready_task_spec(spec, field="interface", value="desktop_gui",
                                         user_message="mejor quiero interfaz gráfica")
        self.assertEqual(changed["version"], spec["version"] + 1)
        self.assertEqual(changed["user_decisions"]["interface"], "desktop_gui")
        self.assertEqual(changed["revision_changes"][-1]["source"], "user")
        self.assertIn("interfaz gráfica", changed["objective"])
        self.assertEqual(spec["objective"], deterministic_task_spec(
            "crea una calculadora de consola en Python que sume")["objective"])

    def test_invalid_source_and_planner_fields_are_rejected_or_repaired(self):
        spec = deterministic_task_spec("crea una calculadora de consola en Python que sume")
        spec["requirements"][0]["source"] = "agent"
        with self.assertRaises(ValueError):
            validate_task_spec(spec)

        valid = deterministic_task_spec("crea una calculadora de consola en Python que sume")
        calls = []
        def request(method, url, payload, timeout):
            calls.append(payload)
            return {"message": {"content": "{" if len(calls) == 1 else json.dumps(valid)},
                    "prompt_eval_count": 2, "eval_count": 3}
        analyst = TaskSpecAnalyst(request=request)
        result = analyst.analyze_spec(
            "crea una calculadora de consola en Python que sume",
            {"config": {"model": "test-model", "endpoint": "http://127.0.0.1:11434"}})
        self.assertEqual(result["status"], "READY_FOR_PLANNING")
        self.assertEqual(analyst.metrics["model_calls"], 2)
        self.assertEqual(calls[0]["tools"], [])

    def test_model_cannot_skip_material_clarification(self):
        invented = deterministic_task_spec("crea una calculadora de consola en Python que sume")
        def request(method, url, payload, timeout):
            return {"message": {"content": json.dumps(invented)},
                    "prompt_eval_count": 1, "eval_count": 1}
        analyst = TaskSpecAnalyst(request=request)
        result = analyst.analyze_spec("haz una calculadora", {
            "config": {"model": "test-model", "endpoint": "http://127.0.0.1:11434"}})
        self.assertEqual(result["status"], "NEEDS_CLARIFICATION")
        self.assertTrue(result["clarification_questions"])

    def test_worker_context_uses_derived_canonical_spec(self):
        spec = deterministic_task_spec("crea una calculadora de consola en Python que sume")
        rendered = Orchestrator._execution_prompt(
            render_task_spec(spec), {"objective": "Implement the calculator"})
        self.assertIn("CANONICAL TASK SPEC", rendered)
        self.assertIn(spec["objective"], rendered)
        self.assertNotIn("source_prompt", rendered)

    def test_semantic_compiler_assigns_ids_and_rejects_cycle(self):
        spec = deterministic_task_spec("crea una calculadora de consola en Python que sume")
        semantic = {"tasks": [
            {"key": "implement_calculator", "objective": "Implement calculator",
             "depends_on": [], "required_capabilities": ["filesystem.create"],
             "preferred_skills": [], "success_criteria": ["Program exists"]},
            {"key": "test_calculator", "objective": "Test calculator",
             "depends_on": ["implement_calculator"], "required_capabilities": ["filesystem.read"],
             "preferred_skills": [], "success_criteria": ["Output is correct"]},
        ]}
        plan = compile_semantic_plan(semantic, spec)
        self.assertEqual([task["id"] for task in plan["tasks"]], ["task-1", "task-2"])
        self.assertEqual(plan["tasks"][1]["depends_on"], ["task-1"])
        self.assertTrue(plan["criterion_links"]["global"])
        semantic["tasks"][0]["depends_on"] = ["test_calculator"]
        with self.assertRaises(ValueError):
            compile_semantic_plan(semantic, spec)

    def test_planner_receives_spec_not_source_prompt_and_adds_qa(self):
        spec = deterministic_task_spec("crea una calculadora de consola en Python que sume")
        captured = {}
        def decide(prompt, context):
            captured["prompt"], captured["context"] = prompt, context
            return {"tasks": [{"key": "implement", "objective": "Implement calculator",
                "description": "Write the requested Python console calculator.",
                "depends_on": [], "required_capabilities": ["filesystem.create", "filesystem.read"],
                "preferred_skills": [], "success_criteria": ["Program exists"]}]}
        plan = Planner(decide).create_plan_for_spec(spec)
        self.assertNotIn("source_prompt", captured["context"]["task_spec"])
        self.assertIn("Canonical Task Spec", captured["prompt"])
        self.assertEqual(len(plan["tasks"]), 2)
        self.assertEqual(plan["tasks"][1]["id"], "qa-interactive-test")


class ClarificationIntegrationTests(unittest.TestCase):
    def test_revision_during_planning_discards_stale_plan(self):
        with TemporaryDirectory(dir=".") as folder:
            entered, release, dispatched = threading.Event(), threading.Event(), threading.Event()
            class BlockingPlanner(CountingPlanner):
                def create_plan_for_spec(self, spec, context=None):
                    if not self.calls:
                        entered.set()
                        if not release.wait(5):
                            raise RuntimeError("Planner test gate timed out.")
                    return super().create_plan_for_spec(spec, context)
            store = Store(Path(folder) / "test.sqlite3")
            planner = BlockingPlanner()
            orchestrator = Orchestrator(
                store, None, planner=planner, task_analyst=TaskSpecAnalyst(offline=True))
            orchestrator._run_graph = lambda oid, run, deadline, brief: dispatched.set()
            run = store.create_orchestration(
                "crea una calculadora de consola en Python que sume")
            first_thread = threading.Thread(target=orchestrator._run, args=(run["id"],))
            first_thread.start()
            self.assertTrue(entered.wait(5))
            app = Application(store, None, Path(folder), orchestrator)
            status, _ = app.dispatch("POST", "/api/orchestrations/" + run["id"] +
                                     "/revise-spec", {}, {
                "field": "interface", "value": "desktop_gui",
                "user_message": "mejor quiero interfaz gráfica"})
            self.assertEqual(status, 200)
            release.set()
            first_thread.join(5)
            self.assertFalse(first_thread.is_alive())
            self.assertTrue(dispatched.wait(5))
            final = store.get_orchestration(run["id"])
            self.assertEqual(final["task_spec"]["version"], 2)
            self.assertEqual([x["version"] for x in final["task_spec_revisions"]], [1, 2])
            self.assertIn("interfaz gráfica", final["plan"]["goal"])
            self.assertEqual([x["version"] for x in planner.calls], [1, 2])
            self.assertEqual(final["status"], "Running")

    def test_same_run_persists_answers_and_plans_once(self):
        with TemporaryDirectory(dir=".") as folder:
            store = Store(Path(folder) / "test.sqlite3")
            planner = CountingPlanner()
            orchestrator = Orchestrator(
                store, None, planner=planner, task_analyst=TaskSpecAnalyst(offline=True))
            dispatched = []
            orchestrator._run_graph = lambda oid, run, deadline, brief: dispatched.append(oid)
            run = store.create_orchestration("haz una calculadora")
            orchestrator._run(run["id"])
            pending = store.get_orchestration(run["id"])
            self.assertEqual(pending["status"], "NeedsClarification")
            self.assertIsNone(pending["plan"])
            reopened = Store(Path(folder) / "test.sqlite3")
            reopened.recover_interrupted_orchestrations()
            self.assertEqual(reopened.get_orchestration(run["id"])["status"],
                             "NeedsClarification")
            self.assertEqual(planner.calls, [])
            answers = {q["id"]: ("Python de consola" if q["field"] == "interface" else "solo sumar")
                       for q in pending["task_spec"]["clarification_questions"]}
            app = Application(store, None, Path(folder), orchestrator)
            status, _ = app.dispatch("POST", "/api/orchestrations/" + run["id"] +
                                     "/clarifications", {}, {"answers": answers})
            self.assertEqual(status, 200)
            for _ in range(200):
                final = store.get_orchestration(run["id"])
                if final["plan"] is not None:
                    break
                time.sleep(.01)
            self.assertEqual(final["id"], run["id"])
            self.assertEqual(final["task_spec"]["version"], 2)
            self.assertEqual(final["task_spec"]["status"], "READY_FOR_PLANNING")
            self.assertEqual(len(final["clarification_answers"]), 2)
            for _ in range(200):
                if dispatched:
                    break
                time.sleep(.01)
            self.assertEqual(len(planner.calls), 1)
            self.assertEqual(dispatched, [run["id"]])
            self.assertIn("task_analysis.clarification_received",
                          [event["event_type"] for event in final["events"]])
            self.assertIn("freya.planning.started",
                          [event["event_type"] for event in final["events"]])


if __name__ == "__main__":
    unittest.main()
