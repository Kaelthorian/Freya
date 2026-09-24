"""Canonical Task Spec, clarification lifecycle and semantic plan compiler."""
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_center.api import Application
from control_center.agent_factory import AgentFactory
from control_center.config import normalize_agent
from control_center.orchestrator import Orchestrator
from control_center.plan_compiler import compile_semantic_plan
from control_center.planner import PlanGenerationError, Planner, semantic_plan_response_format
from control_center.runtime_resources import RuntimeResourceCatalog, UnsupportedResourceRequirement
from control_center.storage import Store
from control_center.task_spec import (
    TASK_SPEC_RESPONSE_FORMAT, TaskSpecAnalyst, deterministic_task_spec,
    render_task_spec, revise_ready_task_spec, validate_task_spec,
)


def _analyst_response(spec, **overrides):
    result = {key: spec[key] for key in TASK_SPEC_RESPONSE_FORMAT["properties"] if key in spec}
    for question in result.get("clarification_questions", []):
        question.pop("id", None)
    result.update(overrides)
    return result


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

        valid = _analyst_response(deterministic_task_spec(
            "crea una calculadora de consola en Python que sume"))
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
        self.assertFalse(analyst.metrics["fallback_used"])
        self.assertTrue(analyst.metrics["repair_attempted"])
        self.assertEqual(calls[0]["tools"], [])
        self.assertEqual(calls[0]["format"], TASK_SPEC_RESPONSE_FORMAT)
        repair = json.loads(calls[1]["messages"][1]["content"])
        self.assertEqual(repair["validation_error"]["error_path"], "$@1:2")
        self.assertIn("expected_schema", repair)

    def test_model_cannot_skip_material_clarification(self):
        invented = _analyst_response(deterministic_task_spec(
            "crea una calculadora de consola en Python que sume"))
        def request(method, url, payload, timeout):
            return {"message": {"content": json.dumps(invented)},
                    "prompt_eval_count": 1, "eval_count": 1}
        analyst = TaskSpecAnalyst(request=request)
        result = analyst.analyze_spec("haz una calculadora", {
            "config": {"model": "test-model", "endpoint": "http://127.0.0.1:11434"}})
        self.assertEqual(result["status"], "NEEDS_CLARIFICATION")
        self.assertTrue(result["clarification_questions"])

    def test_valid_llm_spec_uses_one_call_without_repair_or_fallback(self):
        valid = _analyst_response(deterministic_task_spec(
            "crea una calculadora de consola en Python que sume dos números"))
        calls = []

        def request(method, url, payload, timeout):
            calls.append(payload)
            return {"message": {"content": json.dumps(valid)},
                    "prompt_eval_count": 12, "eval_count": 8}

        analyst = TaskSpecAnalyst(request=request)
        result = analyst.analyze_spec(
            "crea una calculadora de consola en Python que sume dos números",
            {"config": {"model": "test-model", "endpoint": "http://127.0.0.1:11434"}},
        )
        self.assertEqual(result["status"], "READY_FOR_PLANNING")
        self.assertEqual(analyst.metrics["model_calls"], 1)
        self.assertEqual(analyst.metrics["mode"], "llm")
        self.assertFalse(analyst.metrics["fallback_used"])
        self.assertFalse(analyst.metrics["repair_attempted"])
        self.assertEqual(analyst.metrics["total_tokens"], 20)
        self.assertEqual(len(calls), 1)

    def test_enum_case_alias_null_containers_and_question_ids_normalize_without_repair(self):
        valid = _analyst_response(deterministic_task_spec("haz una calculadora"),
                                  status="needs_clarification")
        valid["clarification_questions"][0]["id"] = "model-id-is-runtime-owned"
        valid["context"] = None
        valid["user_decisions"] = None
        valid["constraints"] = None
        valid["assumptions"] = None
        valid["validation_expectations"] = None
        valid["requirements"] = None
        valid["deliverables"] = None
        calls = []

        def request(method, url, payload, timeout):
            calls.append(payload)
            return {"message": {"content": json.dumps(valid)}}

        analyst = TaskSpecAnalyst(request=request)
        result = analyst.analyze_spec("haz una calculadora", {
            "config": {"model": "test-model", "endpoint": "http://127.0.0.1:11434"}})
        self.assertEqual(result["status"], "NEEDS_CLARIFICATION")
        self.assertEqual(result["clarification_questions"][0]["id"], "CQ-1")
        self.assertEqual(analyst.metrics["model_calls"], 1)
        self.assertFalse(analyst.metrics["repair_attempted"])
        self.assertFalse(analyst.metrics["fallback_used"])
        self.assertIn("status:enum_case_or_whitespace", analyst.metrics["normalization_changes"])
        self.assertTrue(any("id:runtime_assigned" in item
                            for item in analyst.metrics["normalization_changes"]))
        self.assertTrue(any(event["event_type"] == "task_analysis.normalization_succeeded"
                            for event in analyst.diagnostic_events))

    def test_source_alias_is_explicit_and_normalizes_to_assumed(self):
        valid = _analyst_response(deterministic_task_spec(
            "crea una calculadora de consola en Python que sume"))
        valid["requirements"][0]["source"] = " INFERRED "
        calls = []

        def request(method, url, payload, timeout):
            calls.append(payload)
            return {"message": {"content": json.dumps(valid)}}

        analyst = TaskSpecAnalyst(request=request)
        result = analyst.analyze_spec(
            "crea una calculadora de consola en Python que sume",
            {"config": {"model": "test-model", "endpoint": "http://127.0.0.1:11434"}},
        )
        self.assertEqual(result["requirements"][0]["source"], "assumed")
        self.assertEqual(analyst.metrics["model_calls"], 1)
        self.assertFalse(analyst.metrics["repair_attempted"])

    def test_two_invalid_outputs_use_fallback_with_exact_field_diagnostics(self):
        invalid = _analyst_response(deterministic_task_spec(
            "crea una calculadora de consola en Python que sume"))
        invalid["requirements"] = []
        calls = []

        def request(method, url, payload, timeout):
            calls.append(payload)
            return {"message": {"content": json.dumps(invalid)},
                    "prompt_eval_count": 1, "eval_count": 1}

        analyst = TaskSpecAnalyst(request=request)
        result = analyst.analyze_spec(
            "crea una calculadora de consola en Python que sume",
            {"config": {"model": "test-model", "endpoint": "http://127.0.0.1:11434"}},
        )
        self.assertEqual(result["status"], "READY_FOR_PLANNING")
        self.assertEqual(analyst.metrics["model_calls"], 2)
        self.assertTrue(analyst.metrics["fallback_used"])
        self.assertEqual(analyst.metrics["initial_validation_error"]["error_path"], "requirements")
        self.assertEqual(analyst.metrics["repair_validation_error"]["error_path"], "requirements")
        self.assertIn("requirements", analyst.metrics["fallback_reason"])
        contract = [event for event in analyst.diagnostic_events
                    if event["event_type"] == "task_analysis.contract_invalid"]
        self.assertEqual([item["stage"] for item in contract], ["initial", "repair"])
        self.assertTrue(all(item["normalization_attempted"] for item in contract))
        self.assertTrue(any(event["event_type"] == "task_analysis.fallback_used"
                            for event in analyst.diagnostic_events))

    def test_orchestrator_persists_task_analyst_contract_events(self):
        invalid = _analyst_response(deterministic_task_spec(
            "crea una calculadora de consola en Python que sume"))
        invalid["requirements"] = []

        def request(method, url, payload, timeout):
            return {"message": {"content": json.dumps(invalid)}}

        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            store.create_agent(normalize_agent({
                "name": "Analyst", "config": {
                    "orchestration_role": "task_analyst", "model": "test-model",
                    "endpoint": "http://127.0.0.1:11434",
                },
            }))
            run = store.create_orchestration(
                "crea una calculadora de consola en Python que sume")
            orchestrator = Orchestrator(
                store, None, planner=Planner(offline=True),
                task_analyst=TaskSpecAnalyst(request=request),
            )
            orchestrator._analyze_task_spec(run["id"], run, {})
            events = store.get_orchestration(run["id"])["events"]
        invalid_events = [item for item in events
                          if item["event_type"] == "task_analysis.contract_invalid"]
        self.assertEqual([json.loads(item["payload_json"])["stage"] for item in invalid_events],
                         ["initial", "repair"])
        self.assertTrue(any(item["event_type"] == "task_analysis.repair_started" for item in events))
        self.assertTrue(any(item["event_type"] == "task_analysis.fallback_used" for item in events))
        self.assertTrue(all(json.loads(item["payload_json"])["actor_type"] == "task_analyst"
                            for item in invalid_events))

    def test_bad_source_diagnostic_names_exact_path_expected_and_received(self):
        invalid = _analyst_response(deterministic_task_spec(
            "crea una calculadora de consola en Python que sume"))
        invalid["requirements"][0]["source"] = "invention"
        calls = []

        def request(method, url, payload, timeout):
            calls.append(payload)
            fixed = dict(invalid)
            fixed["requirements"] = [dict(invalid["requirements"][0], source="explicit")]
            return {"message": {"content": json.dumps(invalid if len(calls) == 1 else fixed)}}

        analyst = TaskSpecAnalyst(request=request)
        analyst.analyze_spec(
            "crea una calculadora de consola en Python que sume",
            {"config": {"model": "test-model", "endpoint": "http://127.0.0.1:11434"}},
        )
        diagnostic = next(event for event in analyst.diagnostic_events
                          if event["event_type"] == "task_analysis.contract_invalid")
        self.assertEqual(diagnostic["error_path"], "requirements[0].source")
        self.assertEqual(diagnostic["expected"], "one of explicit, clarified, assumed")
        self.assertEqual(diagnostic["received"], "invention")
        self.assertEqual(diagnostic["stage"], "initial")
        self.assertIn("requirements[0].source", diagnostic["raw_response_excerpt"])

    def test_ambiguous_and_sufficient_requests_keep_readiness_gate(self):
        cases = [
            ("haz una calculadora", "NEEDS_CLARIFICATION", True),
            ("crea una calculadora Python de consola que sume dos números",
             "READY_FOR_PLANNING", False),
        ]
        for prompt, expected, questions_expected in cases:
            with self.subTest(prompt=prompt):
                candidate = _analyst_response(deterministic_task_spec(
                    "crea una calculadora de consola en Python que sume dos números"))

                def request(method, url, payload, timeout):
                    return {"message": {"content": json.dumps(candidate)}}

                analyst = TaskSpecAnalyst(request=request)
                result = analyst.analyze_spec(prompt, {
                    "config": {"model": "test-model", "endpoint": "http://127.0.0.1:11434"}})
                self.assertEqual(result["status"], expected)
                self.assertEqual(bool(result["clarification_questions"]), questions_expected)
                self.assertEqual(analyst.metrics["model_calls"], 1)
                self.assertFalse(analyst.metrics["fallback_used"])

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

    def test_calculator_plan_uses_registered_resources_and_descriptions(self):
        spec = deterministic_task_spec(
            "crea una calculadora que pueda sumar 2 numeros y devolver el resultado en python; consola")
        catalog = RuntimeResourceCatalog.build().as_dict()
        captured = {}
        semantic = {"summary": "Create and verify a Python console calculator.",
            "success_criteria": ["The calculator returns the sum of two input numbers."],
            "unsupported_requirements": [], "tasks": [{
                "key": "implement", "objective": "Create calculator.py",
                "description": "Write a Python console calculator that adds two numbers.",
                "depends_on": [], "semantic_needs": [
                    "Create a Python source file.", "Run it with two controlled input lines."],
                "required_capabilities": ["filesystem.create", "filesystem.read",
                                          "execution.python_script"],
                "required_tools": ["write_file", "read_file", "run_command"],
                "preferred_skills": ["python-development"],
                "success_criteria": ["calculator.py exists and returns the requested sum."]}]}

        def decide(prompt, context):
            captured["prompt"], captured["context"] = prompt, context
            return semantic

        planner = Planner(decide)
        result = planner.create_plan_for_spec(spec, catalog)
        implementation = result["tasks"][0]
        qa = result["tasks"][1]
        self.assertEqual(AgentFactory.orchestration_role(implementation), "worker")
        self.assertNotIn("interactive-testing", implementation["required_capabilities"])
        self.assertEqual(AgentFactory.orchestration_role(implementation), "worker")
        self.assertEqual(set(implementation["required_capabilities"]), {
            "filesystem.create", "filesystem.read"})
        self.assertEqual(set(implementation["required_tools"]), {
            "write_file", "read_file"})
        self.assertIn("run_command", qa["required_tools"])
        self.assertEqual(qa["preferred_skills"], [])
        self.assertTrue(qa["task_characteristics"]["single_case_verification"])
        python_capability = next(item for item in captured["context"]["capabilities"]
                                 if item["id"] == "execution.python_script")
        self.assertTrue(any("stdin" in item for item in python_capability["operations"]))
        self.assertIn("Runtime resource catalog", captured["prompt"])
        self.assertEqual(captured["context"]["_semantic_plan"], True)
        compiler = planner.metrics["semantic_compiler"]
        self.assertEqual(compiler["status"], "Success")
        self.assertGreaterEqual(compiler["duration_seconds"], 0)
        self.assertIsNotNone(compiler["started_at"])
        self.assertIsNotNone(compiler["completed_at"])

    def test_oversegmented_calculator_model_plan_is_compacted_before_agent_factory(self):
        prompt = "crea una calculadora que pueda sumar 2 numeros y devolver el resultado en python"
        pending = deterministic_task_spec(prompt)
        answers = {item["id"]: "consola" for item in pending["clarification_questions"]}
        spec = deterministic_task_spec(prompt, pending, answers)
        catalog = RuntimeResourceCatalog.build().as_dict()
        semantic = {"summary": "Create and test a calculator.", "success_criteria": [],
            "unsupported_requirements": [], "tasks": [
                {"key": "create", "objective": "Create calculator.py", "description": "Create source.",
                 "depends_on": [], "semantic_needs": ["calculator.py"],
                 "required_capabilities": ["filesystem.create"], "required_tools": ["write_file"],
                 "preferred_skills": ["python-development"], "success_criteria": ["Source exists."]},
                {"key": "implement", "objective": "Implement the sum function", "description": "Edit source.",
                 "depends_on": ["create"], "semantic_needs": ["calculator.py"],
                 "required_capabilities": ["filesystem.read", "filesystem.modify"],
                 "required_tools": ["read_file", "edit_file"], "preferred_skills": ["python-development"],
                 "success_criteria": ["The function sums values."]},
                {"key": "test", "objective": "Create test_calculator.py", "description": "Create tests.",
                 "depends_on": ["implement"], "semantic_needs": ["test_calculator.py"],
                 "required_capabilities": ["filesystem.create"], "required_tools": ["write_file"],
                 "preferred_skills": ["software-testing"], "success_criteria": ["Tests exist."]},
                {"key": "run-tests", "objective": "Run calculator tests", "description": "Run pytest.",
                 "depends_on": ["test"], "semantic_needs": ["Run tests."],
                 "required_capabilities": ["execution.pytest"], "required_tools": ["run_command"],
                 "preferred_skills": ["software-testing"], "success_criteria": ["Tests pass."]},
            ]}
        # Include a second dependent code audit in the graph so the compactor
        # also proves that redundant QA/audit model nodes are not retained.
        semantic["tasks"].append({
            "key": "audit", "objective": "Audit calculator code", "description": "Review it.",
            "depends_on": ["run-tests"], "semantic_needs": ["Review code."],
            "required_capabilities": ["filesystem.read"], "required_tools": ["read_file"],
            "preferred_skills": ["code-review"], "success_criteria": ["Code is reviewed."]})
        result = Planner(lambda prompt, context: semantic).create_plan_for_spec(spec, catalog)
        self.assertEqual([item["id"] for item in result["tasks"]],
                         ["task-1", "qa-interactive-test"])
        implementation, qa = result["tasks"]
        self.assertEqual(AgentFactory.orchestration_role(implementation), "worker")
        self.assertEqual(AgentFactory.orchestration_role(qa), "qa")
        self.assertEqual(set(implementation["required_capabilities"]), {
            "filesystem.create", "filesystem.read"})
        self.assertEqual(set(implementation["required_tools"]), {
            "write_file", "read_file"})
        self.assertEqual(implementation["success_criteria"], [
            "calculator.py exists in the selected workspace and can be read."])
        self.assertNotIn("filesystem.modify", implementation["required_capabilities"])
        self.assertNotIn("execution.pytest", implementation["required_capabilities"])
        self.assertIn("execution.python_script", qa["required_capabilities"])
        self.assertIn("run_command", qa["required_tools"])
        self.assertEqual(qa["preferred_skills"], [])
        self.assertTrue(any("lines 3 and 5" in need for need in qa["semantic_needs"]))
        self.assertEqual(qa["success_criteria"], [
            "The bounded command outputs '8' and exits successfully."])
        behavior_id = next(item["id"] for item in result["criterion_links"]["global"]
                           if "La calculadora produce" in item["criterion"])
        qa_link = next(item for item in result["criterion_links"]["local"]
                       if item["task_id"] == qa["id"])
        self.assertIn(behavior_id, qa_link["supports_global_criteria"])

    def test_interactive_input_selects_real_python_execution_resources(self):
        spec = deterministic_task_spec("crea una calculadora Python interactiva de consola que sume dos números")
        catalog = RuntimeResourceCatalog.build().as_dict()
        semantic = {"summary": "Create the interactive program.", "success_criteria": [],
            "unsupported_requirements": [], "tasks": [{
                "key": "implement", "objective": "Create calculator.py",
                "description": "Create the console calculator.", "depends_on": [],
                "semantic_needs": ["Provide two numbers through bounded stdin."],
                "required_capabilities": ["filesystem.create", "execution.python_script"],
                "required_tools": ["write_file", "run_command"],
                "preferred_skills": ["python-development"],
                "success_criteria": ["The sum is printed correctly."]}]}
        result = Planner(lambda prompt, context: semantic).create_plan_for_spec(spec, catalog)
        qa = next(item for item in result["tasks"] if item["id"].startswith("qa-interactive-test"))
        self.assertIn("execution.python_script", qa["required_capabilities"])
        self.assertIn("run_command", qa["required_tools"])
        self.assertNotIn("interactive-testing", qa["required_capabilities"])

    def test_unknown_capability_is_rejected_before_compilation_or_repair(self):
        spec = deterministic_task_spec("crea una calculadora Python de consola que sume dos números")
        calls = []
        semantic = {"summary": "Create calculator.", "success_criteria": [],
            "unsupported_requirements": [], "tasks": [{
                "key": "implement", "objective": "Create calculator.py", "description": "Create it.",
                "depends_on": [], "semantic_needs": ["Run the calculator."],
                "required_capabilities": ["made-up-capability"], "required_tools": [],
                "preferred_skills": [], "success_criteria": ["The file exists."]}]}
        planner = Planner(lambda prompt, context: (calls.append(prompt), semantic)[1])
        with self.assertRaises(UnsupportedResourceRequirement) as caught:
            planner.create_plan_for_spec(spec, RuntimeResourceCatalog.build().as_dict())
        self.assertEqual(caught.exception.resource_type, "capability")
        self.assertEqual(caught.exception.unknown_resource_id, "made-up-capability")
        self.assertEqual(len(calls), 1)
        self.assertEqual(planner.metrics["model_calls"], 1)

    def test_unknown_skill_is_rejected_before_compilation(self):
        spec = deterministic_task_spec("crea una calculadora Python de consola que sume dos números")
        semantic = {"summary": "Create calculator.", "success_criteria": [],
            "unsupported_requirements": [], "tasks": [{
                "key": "implement", "objective": "Create calculator.py", "description": "Create it.",
                "depends_on": [], "semantic_needs": [], "required_capabilities": ["filesystem.create"],
                "required_tools": ["write_file"], "preferred_skills": ["imaginary-python-skill"],
                "success_criteria": ["The file exists."]}]}
        with self.assertRaises(UnsupportedResourceRequirement) as caught:
            Planner(lambda prompt, context: semantic).create_plan_for_spec(
                spec, RuntimeResourceCatalog.build().as_dict())
        self.assertEqual(caught.exception.resource_type, "skill")

    def test_unknown_tool_is_rejected_before_compilation(self):
        spec = deterministic_task_spec("crea una calculadora Python de consola que sume dos números")
        semantic = {"summary": "Create calculator.", "success_criteria": [],
            "unsupported_requirements": [], "tasks": [{
                "key": "implement", "objective": "Create calculator.py", "description": "Create it.",
                "depends_on": [], "semantic_needs": [], "required_capabilities": ["filesystem.create"],
                "required_tools": ["screen_capture"], "preferred_skills": [],
                "success_criteria": ["The file exists."]}]}
        with self.assertRaises(UnsupportedResourceRequirement) as caught:
            Planner(lambda prompt, context: semantic).create_plan_for_spec(
                spec, RuntimeResourceCatalog.build().as_dict())
        self.assertEqual(caught.exception.resource_type, "tool")
        self.assertEqual(caught.exception.unknown_resource_id, "screen_capture")

    def test_unique_declared_resource_aliases_normalize_without_llm_repair(self):
        spec = deterministic_task_spec("Crea un archivo Python hello.py que imprima hola")
        semantic = {"summary": "Create and run hello.py.", "success_criteria": [],
            "unsupported_requirements": [], "tasks": [{
                "key": "create", "objective": "Create hello.py", "description": "Create it.",
                "depends_on": [], "semantic_needs": ["Execute the Python file."],
                "required_capabilities": ["run_python_script"],
                "required_tools": ["run_workspace_command"], "preferred_skills": [],
                "success_criteria": ["hello.py runs successfully."]}]}
        calls = []
        result = Planner(lambda prompt, context: (calls.append(prompt), semantic)[1]).create_plan_for_spec(
            spec, RuntimeResourceCatalog.build().as_dict())
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["tasks"][0]["required_capabilities"], ["execution.python_script"])
        self.assertEqual(result["tasks"][0]["required_tools"], ["run_command"])

    def test_new_registered_skill_enters_catalog_and_dynamic_schema(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            orchestrator = Orchestrator(store, None, planner=Planner(offline=True))
            first = orchestrator._planning_context()
            store.create_skill({
                "id": "arithmetic-checks", "name": "Arithmetic Checks",
                "description": "Check basic arithmetic programs with representative input.",
                "category": "Verification", "version": 1,
                "instructions": ["PRIVATE_WORKER_INSTRUCTION"], "procedures": [],
                "recommended_capabilities": ["execution.python_script"],
                "required_capabilities": ["filesystem.read"], "tags": ["arithmetic", "cli"],
                "source": "user", "metadata": {"use_when": "When checking a small CLI arithmetic program."},
                "enabled": True,
            })
            context = orchestrator._planning_context()
            skill = next(item for item in context["skills"] if item["id"] == "arithmetic-checks")
            self.assertNotEqual(first["resource_catalog_version"], context["resource_catalog_version"])
            self.assertEqual(skill["use_when"], "When checking a small CLI arithmetic program.")
            self.assertNotIn("PRIVATE_WORKER_INSTRUCTION", json.dumps(context))
            format_schema = semantic_plan_response_format(context)
            skill_enum = format_schema["properties"]["tasks"]["items"]["properties"]["preferred_skills"]["items"]["enum"]
            capability_enum = format_schema["properties"]["tasks"]["items"]["properties"]["required_capabilities"]["items"]["enum"]
            self.assertIn("arithmetic-checks", skill_enum)
            self.assertNotIn("interactive-testing", capability_enum)


class ClarificationIntegrationTests(unittest.TestCase):
    def test_resource_catalog_and_unknown_id_are_logged_without_repair(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            calls = []
            semantic = {"summary": "Create a calculator.", "success_criteria": [],
                "unsupported_requirements": [], "tasks": [{
                    "key": "implement", "objective": "Create calculator.py", "description": "Create it.",
                    "depends_on": [], "semantic_needs": ["Create the Python source file."],
                    "required_capabilities": ["made-up-capability"], "required_tools": [],
                    "preferred_skills": [], "success_criteria": ["The file exists."]}]}
            planner = Planner(lambda prompt, context: (calls.append(prompt), semantic)[1])
            orchestrator = Orchestrator(
                store, None, planner=planner, task_analyst=TaskSpecAnalyst(offline=True))
            run = store.create_orchestration(
                "crea una calculadora Python de consola que sume dos números")
            orchestrator._run(run["id"])
            stored = store.get_orchestration(run["id"])
            events = {event["event_type"]: json.loads(event["payload_json"])
                      for event in stored["events"]}
            self.assertEqual(stored["status"], "Failed")
            self.assertIsNone(stored["plan"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(events["freya.planning.resources"]["resource_catalog_version"],
                             stored["planning_metrics"]["resource_catalog_version"])
            self.assertIn("run_command", events["freya.planning.resources"]["available_tool_ids"])
            self.assertEqual(events["freya.planning.failed"]["resource_type"], "capability")
            self.assertEqual(events["freya.planning.failed"]["unknown_resource_id"],
                             "made-up-capability")

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
            event_types = [event["event_type"] for event in final["events"]]
            self.assertIn("freya.plan_compiler.started", event_types)
            self.assertIn("freya.plan_compiler.completed", event_types)
            compiler_event = next(event for event in final["events"]
                                  if event["event_type"] == "freya.plan_compiler.completed")
            self.assertGreaterEqual(
                json.loads(compiler_event["payload_json"])["duration_seconds"], 0,
            )


if __name__ == "__main__":
    unittest.main()
