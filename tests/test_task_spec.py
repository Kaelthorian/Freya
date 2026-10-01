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
    TASK_SPEC_RESPONSE_FORMAT, TaskSpecAnalyst, TaskSpecError, deterministic_task_spec,
    normalize_task_spec_candidate, render_task_spec, revise_ready_task_spec,
    validate_task_spec,
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
    def test_question_ids_are_runtime_owned_and_not_renumbered_by_validation(self):
        first = deterministic_task_spec("haz una calculadora")
        self.assertEqual([item["id"] for item in first["clarification_questions"]],
                         ["CQ-1", "CQ-2"])
        self.assertEqual(validate_task_spec(first)["clarification_questions"],
                         first["clarification_questions"])
        second = deterministic_task_spec("haz una calculadora", first,
                                         {"CQ-1": "Web"})
        self.assertEqual([item["id"] for item in second["clarification_questions"]], ["CQ-2"])
        self.assertEqual(second["clarification_history"][0]["question_id"], "CQ-1")

    def test_reserved_question_field_is_rejected_by_validator(self):
        spec = deterministic_task_spec("haz una calculadora")
        spec["clarification_questions"][0]["field"] = "user_decisions"
        with self.assertRaises(TaskSpecError) as caught:
            validate_task_spec(spec)
        self.assertEqual(caught.exception.error_type, "invalid_question_field")

    def test_no_is_a_resolved_answer_and_paraphrase_same_field_is_deduplicated(self):
        prompt = "crea una calculadora web que sume"
        baseline = deterministic_task_spec(prompt)
        calls = []
        def request(method, url, payload, timeout):
            calls.append(payload)
            candidate = _analyst_response(baseline)
            candidate["status"] = "NEEDS_CLARIFICATION"
            candidate["clarification_questions"] = [{
                "question": "¿Debe funcionar sin conexión?" if len(calls) == 1
                            else "¿Quieres uso offline?",
                "reason": "Define un requisito del producto.",
                "field": "offline_mode", "required": True,
            }]
            return {"message": {"content": json.dumps(candidate)}}
        analyst = TaskSpecAnalyst(request=request)
        agent = {"config": {"model": "test-model", "endpoint": "http://127.0.0.1:11434"}}
        pending = analyst.analyze_spec(prompt, agent)
        self.assertEqual(pending["clarification_questions"][0]["id"], "CQ-1")
        ready = analyst.analyze_spec(prompt, agent, pending, {"CQ-1": "No"})
        self.assertEqual(ready["status"], "READY_FOR_PLANNING")
        self.assertEqual(ready["user_decisions"]["offline_mode"], "No")
        self.assertEqual(len(ready["clarification_history"]), 1)
        self.assertEqual(ready["clarification_history"][0]["answer"], "No")
        types = [item["event_type"] for item in analyst.diagnostic_events]
        self.assertIn("task_analysis.clarification_resolved", types)
        self.assertIn("task_analysis.clarification_deduplicated", types)

    def test_model_question_with_container_field_is_discarded(self):
        prompt = "crea una calculadora web que sume"
        candidate = _analyst_response(deterministic_task_spec(prompt))
        candidate["status"] = "NEEDS_CLARIFICATION"
        candidate["clarification_questions"] = [{"question": "¿Algo más?", "reason": "Detalles.",
                                                 "field": "user_decisions", "required": True}]
        analyst = TaskSpecAnalyst(request=lambda *args, **kwargs: {"message": {"content": json.dumps(candidate)}})
        result = analyst.analyze_spec(prompt, {"config": {"model": "test-model"}})
        self.assertEqual(result["status"], "READY_FOR_PLANNING")
        self.assertEqual(result["clarification_questions"], [])
        self.assertEqual(analyst.metrics["model_calls"], 1)
        self.assertFalse(analyst.metrics["fallback_used"])
        self.assertTrue(any(item["event_type"] == "task_analysis.clarification_rejected"
                            for item in analyst.diagnostic_events))

    def test_uncertain_answer_does_not_resolve_material_question(self):
        prompt = "haz una calculadora"
        analyst = TaskSpecAnalyst(offline=True)
        first = analyst.analyze_spec(prompt)
        answer = {first["clarification_questions"][0]["id"]: "No sé",
                  first["clarification_questions"][1]["id"]: "sumar"}
        second = analyst.analyze_spec(prompt, previous=first, answers=answer)
        self.assertEqual(second["status"], "NEEDS_CLARIFICATION")
        self.assertEqual([item["id"] for item in second["clarification_questions"]], ["CQ-1"])
        self.assertEqual(second["clarification_history"][0]["question_id"], "CQ-2")
        self.assertNotIn("interface", second["user_decisions"])

    def test_reported_extra_features_loop_resolves_no_once(self):
        prompt = "Hace una calculadora que pueda sumar restar multiplicar y dividir"
        basis = deterministic_task_spec(prompt)
        previous = validate_task_spec({**basis, "source_prompt": prompt, "version": 2,
            "status": "NEEDS_CLARIFICATION", "user_decisions": {"platform": "Web"},
            "clarification_history": [{"question_id": "CQ-1", "field": "platform",
                                      "question": "¿Web, desktop o consola?", "answer": "Web"}],
            "clarification_questions": [{"id": "CQ-2", "field": "additional_features",
                 "question": "Do you want any additional features?",
                 "reason": "Possible extras.", "required": True}]})
        candidate = _analyst_response(deterministic_task_spec(
            prompt, previous, {"CQ-2": "No"}))
        candidate.update(status="NEEDS_CLARIFICATION", clarification_questions=[
            {"field": "additional_features", "question": "Would you like any extra functionality?",
             "reason": "Possible extras.", "required": True},
            {"field": "operations", "question": "Which operations?",
             "reason": "Core behavior.", "required": True}])
        analyst = TaskSpecAnalyst(request=lambda *args, **kwargs: {
            "message": {"content": json.dumps(candidate)}})
        ready = analyst.analyze_spec(prompt, {"config": {"model": "test-model"}},
                                     previous, {"CQ-2": "No"})
        self.assertEqual(ready["status"], "READY_FOR_PLANNING")
        self.assertEqual(ready["clarification_questions"], [])
        self.assertEqual(ready["user_decisions"], {"platform": "Web", "additional_features": "No"})
        self.assertEqual([item["question_id"] for item in ready["clarification_history"]],
                         ["CQ-1", "CQ-2"])
        self.assertEqual(analyst.metrics["model_calls"], 1)
        self.assertFalse(analyst.metrics["fallback_used"])
        self.assertFalse(analyst.metrics["repair_attempted"])
        self.assertIn("task_analysis.clarification_deduplicated",
                      [item["event_type"] for item in analyst.diagnostic_events])

    def test_platform_then_material_operations_get_distinct_ids(self):
        prompt = "haz una calculadora"
        first = deterministic_task_spec(prompt)
        first["clarification_questions"] = [first["clarification_questions"][0]]
        first = validate_task_spec(first)
        second = deterministic_task_spec(prompt, first, {"CQ-1": "Web"})
        self.assertEqual(second["clarification_questions"][0]["field"], "operations")
        self.assertEqual(second["clarification_questions"][0]["id"], "CQ-2")
        self.assertEqual(second["clarification_history"][0]["question_id"], "CQ-1")

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

    def test_console_program_without_language_does_not_assume_a_language(self):
        spec = deterministic_task_spec("crea una calculadora de consola que sume")
        self.assertEqual(spec["status"], "READY_FOR_PLANNING")
        self.assertEqual(spec["assumptions"], [])
        self.assertNotIn("Python", spec["objective"])

    def test_explicit_language_beats_python_default(self):
        spec = deterministic_task_spec("crea una calculadora de consola en JavaScript que sume")
        self.assertEqual(spec["status"], "READY_FOR_PLANNING")
        self.assertIn("JavaScript", spec["objective"])
        self.assertFalse(any("Python 3.10+" in item["description"]
                             for item in spec["assumptions"]))

    def test_named_python_script_does_not_assume_a_console_interface(self):
        spec = deterministic_task_spec("crea calculator.py en Python que sume dos números")
        self.assertEqual(spec["status"], "NEEDS_CLARIFICATION")
        self.assertEqual([item["field"] for item in spec["clarification_questions"]],
                         ["interface"])
        self.assertEqual(spec["assumptions"], [])

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

    def test_builtin_task_analyst_uses_its_own_model_without_an_agent_argument(self):
        prompt = "Crea una calculadora que sume reste multiplique y divida"
        floor = deterministic_task_spec(prompt)
        calls = []

        def request(method, url, payload, timeout):
            calls.append((url, payload, timeout))
            return {"message": {"content": json.dumps(_analyst_response(floor), ensure_ascii=False)}}

        analyst = TaskSpecAnalyst(request=request)
        spec = analyst.analyze_spec(prompt)
        self.assertEqual(analyst.model, "qwen2.5-coder:7b")
        self.assertEqual(calls[0][0], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(calls[0][1]["model"], "qwen2.5-coder:7b")
        self.assertEqual(calls[0][1]["options"]["num_ctx"], 8192)
        self.assertEqual(calls[0][2], 120.0)
        self.assertEqual(analyst.metrics["component"], "task_analyst")
        self.assertEqual(analyst.metrics["mode"], "llm")
        self.assertEqual(analyst.metrics["model_calls"], 1)
        self.assertTrue(analyst.metrics["system_component"])
        self.assertIn("SCOPE PRESERVATION", calls[0][1]["messages"][0]["content"])
        self.assertEqual([item["description"] for item in spec["requirements"]],
                         ["sume", "reste", "multiplique", "divida"])
        self.assertEqual(spec["clarification_questions"][0]["field"], "interface")
        self.assertEqual(spec["assumptions"], [])

    def test_scope_guard_repairs_once_then_falls_back_without_losing_actions(self):
        prompt = "Crea una calculadora que sume reste multiplique y divida"
        invalid = _analyst_response(deterministic_task_spec(prompt))
        invalid["requirements"] = invalid["requirements"][:1]
        calls = []

        def request(method, url, payload, timeout):
            calls.append(payload)
            return {"message": {"content": json.dumps(invalid, ensure_ascii=False)}}

        analyst = TaskSpecAnalyst(model="scope-test", request=request)
        spec = analyst.analyze_spec(prompt)
        requirements = " ".join(item["description"] for item in spec["requirements"])
        for verb in ("sume", "reste", "multiplique", "divida"):
            self.assertIn(verb, requirements)
        self.assertEqual(analyst.metrics["model_calls"], 2)
        self.assertEqual(analyst.metrics["mode"], "deterministic_fallback")
        self.assertEqual(analyst.metrics["initial_validation_error"]["error_type"],
                         "lost_explicit_requirement")

    def test_scope_guard_rejects_unrequested_product_features(self):
        prompt = "Crea una calculadora que sume reste multiplique y divida"
        invalid = _analyst_response(deterministic_task_spec(prompt))
        invalid["requirements"].append({
            "description": "Responsive design",
            "source": "explicit",
        })
        calls = []

        def request(method, url, payload, timeout):
            calls.append(payload)
            return {"message": {"content": json.dumps(invalid, ensure_ascii=False)}}

        analyst = TaskSpecAnalyst(model="scope-test", request=request)
        spec = analyst.analyze_spec(prompt)
        self.assertTrue(analyst.metrics["fallback_used"])
        self.assertEqual(analyst.metrics["initial_validation_error"]["error_type"],
                         "unsupported_scope")
        self.assertNotIn("responsive", json.dumps(spec, ensure_ascii=False).casefold())
        self.assertEqual(analyst.metrics["model_calls"], 2)

    def test_assumption_conflicting_with_pending_interface_question_is_rejected(self):
        prompt = "Crea una calculadora que sume reste multiplique y divida"
        invalid = _analyst_response(deterministic_task_spec(prompt))
        invalid["assumptions"] = [{
            "description": "Use a CLI interface",
            "reason": "This is a typical calculator default.",
        }]

        def request(method, url, payload, timeout):
            return {"message": {"content": json.dumps(invalid, ensure_ascii=False)}}

        analyst = TaskSpecAnalyst(model="scope-test", request=request)
        spec = analyst.analyze_spec(prompt)
        self.assertTrue(analyst.metrics["fallback_used"])
        self.assertEqual(analyst.metrics["initial_validation_error"]["error_type"],
                         "assumption_question_conflict")
        self.assertEqual(spec["assumptions"], [])
        self.assertEqual([item["field"] for item in spec["clarification_questions"]],
                         ["interface"])

    def test_semantically_duplicate_interface_questions_collapse(self):
        prompt = "Crea una calculadora que sume reste multiplique y divida"
        candidate = _analyst_response(deterministic_task_spec(prompt))
        candidate["status"] = "NEEDS_CLARIFICATION"
        candidate["clarification_questions"].append({
            "question": "Do you want CLI or GUI?",
            "reason": "Choose a user interface.",
            "field": "platform",
            "required": True,
        })

        def request(method, url, payload, timeout):
            return {"message": {"content": json.dumps(candidate, ensure_ascii=False)}}

        analyst = TaskSpecAnalyst(model="scope-test", request=request)
        spec = analyst.analyze_spec(prompt)
        self.assertEqual(analyst.metrics["model_calls"], 1)
        self.assertFalse(analyst.metrics["fallback_used"])
        self.assertEqual(len(spec["clarification_questions"]), 1)
        self.assertEqual(spec["clarification_questions"][0]["field"], "interface")

    def test_clarification_web_preserves_actions_and_marks_source_clarified(self):
        prompt = "Crea una calculadora que sume reste multiplique y divida"
        first = deterministic_task_spec(prompt)
        second = deterministic_task_spec(prompt, first, {"CQ-1": "web"})
        responses = [_analyst_response(first), _analyst_response(second)]

        def request(method, url, payload, timeout):
            return {"message": {"content": json.dumps(responses.pop(0), ensure_ascii=False)}}

        analyst = TaskSpecAnalyst(model="scope-test", request=request)
        pending = analyst.analyze_spec(prompt)
        ready = analyst.analyze_spec(prompt, previous=pending, answers={"CQ-1": "web"})
        requirements = " ".join(item["description"] for item in ready["requirements"])
        for verb in ("sume", "reste", "multiplique", "divida"):
            self.assertIn(verb, requirements)
        self.assertEqual(ready["status"], "READY_FOR_PLANNING")
        self.assertEqual(ready["user_decisions"]["interface"], "web")
        self.assertTrue(any(item["description"] == "interfaz web"
                            and item["source"] == "clarified"
                            for item in ready["constraints"]))
        self.assertEqual(ready["clarification_questions"], [])
        self.assertEqual(analyst.metrics["model_calls"], 1)

    def test_generic_coordinated_actions_are_preserved_without_added_scope(self):
        prompt = "Crea una herramienta que importe CSV, filtre filas y exporte JSON."
        candidate = _analyst_response(deterministic_task_spec(prompt))

        def request(method, url, payload, timeout):
            return {"message": {"content": json.dumps(candidate, ensure_ascii=False)}}

        analyst = TaskSpecAnalyst(model="scope-test", request=request)
        spec = analyst.analyze_spec(prompt)
        requirements = " ".join(item["description"] for item in spec["requirements"]).casefold()
        for phrase in ("importe csv", "filtre filas", "exporte json"):
            self.assertIn(phrase, requirements)
        self.assertEqual(spec["status"], "READY_FOR_PLANNING")
        self.assertEqual(spec["assumptions"], [])
        self.assertFalse(any(item in json.dumps(spec, ensure_ascii=False).casefold()
                             for item in ("database", "gui", "api", "cloud", "security", "testing")))

    def test_system_task_analyst_is_independent_of_agent_records(self):
        prompt = "Crea una calculadora que sume reste multiplique y divida"
        floor = deterministic_task_spec(prompt)
        scenarios = [
            ("fresh database without agents", []),
            ("deleted preset", []),
            ("disabled legacy agent", [{
                "name": "Disabled Analyst", "enabled": False,
                "config": {"orchestration_role": "task_analyst", "model": "agent-model"},
            }]),
            ("user-created agent with the same name", [{
                "name": "Task Analyst", "role": "Worker",
                "config": {"orchestration_role": "worker", "model": "agent-model"},
            }]),
        ]
        for label, agents in scenarios:
            with self.subTest(scenario=label), TemporaryDirectory() as directory:
                store = Store(Path(directory) / "state.sqlite3")
                for agent in agents:
                    store.create_agent(normalize_agent(agent))
                run = store.create_orchestration(prompt)
                store.list_agents = lambda: self.fail("Task Analyst must not query persisted agents")
                calls = []

                def request(method, url, payload, timeout):
                    calls.append(payload)
                    return {"message": {"content": json.dumps(
                        _analyst_response(floor), ensure_ascii=False)}}

                analyst = TaskSpecAnalyst(model="system-model", request=request)
                orchestrator = Orchestrator(
                    store, None, planner=Planner(offline=True), task_analyst=analyst,
                )
                spec, metrics = orchestrator._analyze_task_spec(run["id"], run, {})
                self.assertEqual(spec["status"], "NEEDS_CLARIFICATION")
                self.assertEqual(metrics["component"], "task_analyst")
                self.assertEqual(metrics["mode"], "llm")
                self.assertEqual(metrics["model"], "system-model")
                self.assertEqual(metrics["model_calls"], 1)
                self.assertTrue(metrics["system_component"])
                self.assertEqual(calls[0]["model"], "system-model")
                started = store.get_orchestration(run["id"])["events"][0]
                payload = json.loads(started["payload_json"])
                self.assertIsNone(payload["agent_id"])
                self.assertEqual(payload["actor_type"], "task_analyst")
                self.assertEqual(payload["actor_name"], "Task Analyst")
                self.assertEqual(payload["actor_role"], "Task Analyst")
                self.assertTrue(payload["system_component"])


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
        candidate = _analyst_response(deterministic_task_spec(
            "crea una calculadora de consola en Python que sume"))
        candidate["requirements"][0]["source"] = " INFERRED "
        normalized, changes = normalize_task_spec_candidate(candidate)
        self.assertEqual(normalized["requirements"][0]["source"], "assumed")
        self.assertIn("requirements[0].source:enum_case_or_alias", changes)

    def test_two_invalid_outputs_use_fallback_with_exact_field_diagnostics(self):
        invalid = _analyst_response(deterministic_task_spec(
            "crea una calculadora de consola en Python que sume"))
        invalid["requirements"][0]["source"] = "assumed"
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
        invalid["requirements"][0]["source"] = "assumed"

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
                candidate = _analyst_response(deterministic_task_spec(prompt))

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
            {"key": "implement_calculator", "task_kind": "program_creation",
             "objective": "Implement calculator", "operations": ["create_file"],
             "semantic_needs": ["Create calculator.py."], "depends_on": [],
             "owned_paths": ["calculator.py"],
             "success_criteria": ["Program exists"]},
            {"key": "test_calculator", "task_kind": "testing",
             "objective": "Test calculator", "operations": ["run_python_script"],
             "semantic_needs": ["Run calculator.py."],
             "depends_on": ["implement_calculator"],
             "success_criteria": ["Output is correct"]},
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
            return {"tasks": [{"key": "implement", "task_kind": "program_creation",
                "objective": "Implement calculator",
                "description": "Write the requested Python console calculator.",
                "depends_on": [], "operations": ["create_file", "read_file"],
                "semantic_needs": ["Create and inspect calculator.py."],
                "owned_paths": ["calculator.py"],
                "success_criteria": ["Program exists"]}]}
        plan = Planner(decide).create_plan_for_spec(spec)
        self.assertNotIn("source_prompt", captured["context"]["task_spec"])
        self.assertIn("Canonical Task Spec", captured["prompt"])
        self.assertEqual(len(plan["tasks"]), 2)
        self.assertEqual(plan["tasks"][1]["id"], "qa-interactive-test")

    def test_web_calculator_without_framework_uses_default_without_preference_task(self):
        prompt = "Create a web calculator supporting addition, subtraction, multiplication, and division."
        spec = deterministic_task_spec(
            "Create a web calculator that can add, subtract, multiply, and divide.")
        spec.update({
            "objective": "Create a web calculator.",
            "user_intent": prompt,
            "deliverables": [{"description": "A web calculator.", "source": "explicit"}],
            "requirements": [
                {"description": "Support addition.", "source": "explicit"},
                {"description": "Support subtraction.", "source": "explicit"},
                {"description": "Support multiplication.", "source": "explicit"},
                {"description": "Support division.", "source": "explicit"},
            ],
            "validation_expectations": [
                "The calculator supports addition.",
                "The calculator supports subtraction.",
                "The calculator supports multiplication.",
                "The calculator supports division.",
            ],
        })
        spec = validate_task_spec(spec)
        captured = {}
        semantic = {
            "summary": "Implement the requested web calculator.",
            "success_criteria": list(spec["validation_expectations"]),
            "unsupported_requirements": [],
            "tasks": [{
                "key": "implement_calculator",
                "task_kind": "file_creation",
                "objective": "Implement the web calculator.",
                "description": (
                    "Assumption: use plain HTML, CSS, and JavaScript. "
                    "Create the calculator UI and implement all four requested operations."),
                "depends_on": [],
                "semantic_needs": ["Create a web calculator artifact."],
                "operations": ["create_file"],
                "owned_paths": ["index.html"],
                "success_criteria": list(spec["validation_expectations"]),
            }],
        }

        def decide(planner_prompt, context):
            captured["prompt"] = planner_prompt
            captured["context"] = context
            return semantic

        result = Planner(decide).create_plan_for_spec(spec)
        self.assertEqual(len(result["tasks"]), 1)
        implementation = result["tasks"][0]
        self.assertIn("Assumption: use plain HTML, CSS, and JavaScript.",
                      implementation["description"])
        task_text = " ".join((implementation["objective"], implementation["description"])).casefold()
        self.assertNotRegex(task_text, r"user.?preference|framework selection|user selection|discover.*framework")
        self.assertEqual(implementation["depends_on"], [])
        planner_prompt = captured["prompt"].casefold()
        self.assertIn("canonical task spec is the source of truth for user intent", planner_prompt)
        self.assertIn("do not create a task whose purpose is to discover a user preference", planner_prompt)
        self.assertIn("never assume that user preferences are stored in workspace files", planner_prompt)
        self.assertIn("prefer the simplest implementation that satisfies the task spec", planner_prompt)
        self.assertEqual(captured["context"]["task_spec"]["assumptions"], [])

    def test_calculator_plan_uses_registered_resources_and_descriptions(self):
        spec = deterministic_task_spec(
            "crea una calculadora que pueda sumar 2 numeros y devolver el resultado en python; consola")
        catalog = RuntimeResourceCatalog.build().as_dict()
        captured = {}
        semantic = {"summary": "Create and verify a Python console calculator.",
            "success_criteria": ["The calculator returns the sum of two input numbers."],
            "unsupported_requirements": [], "tasks": [{
                "key": "implement", "task_kind": "program_creation",
                "objective": "Create calculator.py",
                "description": "Write a Python console calculator that adds two numbers.",
                "depends_on": [], "semantic_needs": [
                    "Create a Python source file.", "Run it with two controlled input lines."],
                "operations": ["create_file", "read_file", "run_python_script"],
                "owned_paths": ["calculator.py"],
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
        self.assertNotIn("capabilities", captured["context"])
        self.assertNotIn("tools", captured["context"])
        self.assertNotIn("skills", captured["context"])
        self.assertIn("run_python_script", {
            item["id"] for item in captured["context"]["semantic_operations"]})
        self.assertIn("Semantic operation catalog", captured["prompt"])
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
                {"key": "create", "task_kind": "program_creation",
                 "objective": "Create calculator.py", "description": "Create source.",
                 "depends_on": [], "semantic_needs": ["calculator.py"],
                 "operations": ["create_file", "modify_file", "read_file"],
                 "owned_paths": ["calculator.py"],
                 "success_criteria": ["Source exists."]},
                {"key": "test", "task_kind": "file_creation",
                 "objective": "Create test_calculator.py", "description": "Create tests.",
                 "depends_on": ["create"], "semantic_needs": ["test_calculator.py"],
                 "operations": ["create_file"], "owned_paths": ["test_calculator.py"],
                 "success_criteria": ["Tests exist."]},
                {"key": "run-tests", "task_kind": "testing",
                 "objective": "Run calculator tests", "description": "Run pytest.",
                 "depends_on": ["test"], "semantic_needs": ["Run tests."],
                 "operations": ["run_pytest"], "success_criteria": ["Tests pass."]},
            ]}
        # Include a second dependent code audit in the graph so the compactor
        # also proves that redundant QA/audit model nodes are not retained.
        semantic["tasks"].append({
            "key": "audit", "task_kind": "review",
            "objective": "Audit calculator code", "description": "Review it.",
            "depends_on": ["run-tests"], "semantic_needs": ["Review code."],
            "operations": ["read_file"], "success_criteria": ["Code is reviewed."]})
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
                           if "prints 8" in item["criterion"])
        qa_link = next(item for item in result["criterion_links"]["local"]
                       if item["task_id"] == qa["id"])
        self.assertIn(behavior_id, qa_link["supports_global_criteria"])

    def test_interactive_input_selects_real_python_execution_resources(self):
        spec = deterministic_task_spec("crea una calculadora Python interactiva de consola que sume dos números")
        catalog = RuntimeResourceCatalog.build().as_dict()
        semantic = {"summary": "Create the interactive program.", "success_criteria": [],
            "unsupported_requirements": [], "tasks": [{
                "key": "implement", "task_kind": "program_creation",
                "objective": "Create calculator.py",
                "description": "Create the console calculator.", "depends_on": [],
                "semantic_needs": ["Provide two numbers through bounded stdin."],
                "operations": ["create_file"],
                "owned_paths": ["calculator.py"],
                "success_criteria": ["The sum is printed correctly."]}]}
        result = Planner(lambda prompt, context: semantic).create_plan_for_spec(spec, catalog)
        qa = next(item for item in result["tasks"] if item["id"].startswith("qa-interactive-test"))
        self.assertIn("execution.python_script", qa["required_capabilities"])
        self.assertIn("run_command", qa["required_tools"])
        self.assertNotIn("interactive-testing", qa["required_capabilities"])

    def test_unknown_operation_is_rejected_before_compilation_or_repair(self):
        spec = deterministic_task_spec("crea una calculadora Python de consola que sume dos números")
        calls = []
        semantic = {"summary": "Create calculator.", "success_criteria": [],
            "unsupported_requirements": [], "tasks": [{
                "key": "implement", "objective": "Create calculator.py", "description": "Create it.",
                "depends_on": [], "semantic_needs": ["Run the calculator."],
                "operations": ["made-up-operation"], "success_criteria": ["The file exists."]}]}
        planner = Planner(lambda prompt, context: (calls.append(prompt), semantic)[1])
        with self.assertRaises(UnsupportedResourceRequirement) as caught:
            planner.create_plan_for_spec(spec, RuntimeResourceCatalog.build().as_dict())
        self.assertEqual(caught.exception.resource_type, "semantic_operation")
        self.assertEqual(caught.exception.unknown_resource_id, "made-up-operation")
        self.assertEqual(len(calls), 1)
        self.assertEqual(planner.metrics["model_calls"], 1)

    def test_planner_skill_and_capability_declarations_are_non_authoritative(self):
        spec = deterministic_task_spec("crea una calculadora Python de consola que sume dos números")
        semantic = {"summary": "Create calculator.", "success_criteria": [],
            "unsupported_requirements": [], "tasks": [{
                "key": "implement", "task_kind": "program_creation",
                "objective": "Create calculator.py", "description": "Create it.",
                "depends_on": [], "semantic_needs": [], "operations": ["create_file"],
                "required_capabilities": ["filesystem.overwrite", "execution.python_script"],
                "owned_paths": ["calculator.py"],
                "required_tools": ["write_file"], "preferred_skills": ["imaginary-python-skill"],
                "success_criteria": ["The file exists."]}]}
        planner = Planner(lambda prompt, context: semantic)
        plan = planner.create_plan_for_spec(spec, RuntimeResourceCatalog.build().as_dict())
        self.assertEqual(plan["tasks"][0]["required_capabilities"], ["filesystem.create"])
        self.assertEqual(plan["tasks"][0]["required_tools"], ["write_file"])
        self.assertEqual(plan["tasks"][0]["preferred_skills"], [])

    def test_structural_task_kind_contradiction_gets_one_bounded_repair(self):
        spec = deterministic_task_spec("Modify calculator.py documentation.")
        review_with_write = {"summary": "Update calculator documentation.",
            "success_criteria": [], "unsupported_requirements": [], "tasks": [{
                "key": "document", "task_kind": "review",
                "objective": "Document calculator.py", "description": "Edit its documentation.",
                "depends_on": [], "semantic_needs": ["Document calculator.py."],
                "operations": ["modify_file"], "owned_paths": ["calculator.py"],
                "success_criteria": ["The documentation is updated."],
            }]}
        corrected = {**review_with_write, "tasks": [{
            **review_with_write["tasks"][0], "task_kind": "code_change",
        }]}
        calls = []

        def model(prompt, context):
            calls.append(prompt)
            return review_with_write if len(calls) == 1 else corrected

        compiled = Planner(model).create_plan_for_spec(spec)
        implementation = compiled["tasks"][0]
        self.assertEqual(len(calls), 2)
        self.assertEqual(implementation["task_kind"], "code_change")
        self.assertEqual(implementation["required_tools"], ["edit_file"])
        self.assertEqual(implementation["required_capabilities"], ["filesystem.modify"])
        self.assertEqual(AgentFactory.orchestration_role(implementation), "worker")

    def test_unknown_tool_is_rejected_before_compilation(self):
        spec = deterministic_task_spec("crea una calculadora Python de consola que sume dos números")
        semantic = {"summary": "Create calculator.", "success_criteria": [],
            "unsupported_requirements": [], "tasks": [{
                "key": "implement", "task_kind": "program_creation",
                "objective": "Create calculator.py", "description": "Create it.",
                "depends_on": [], "semantic_needs": [], "operations": ["create_file"],
                "owned_paths": ["calculator.py"],
                "required_tools": ["screen_capture"],
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
                "key": "create", "task_kind": "program_creation",
                "objective": "Create hello.py", "description": "Create it.",
                "depends_on": [], "semantic_needs": ["Execute the Python file."],
                "required_tools": ["run_workspace_command"],
                "success_criteria": ["hello.py runs successfully."]}]}
        calls = []
        result = Planner(lambda prompt, context: (calls.append(prompt), semantic)[1]).create_plan_for_spec(
            spec, RuntimeResourceCatalog.build().as_dict())
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(result["tasks"][0]["required_capabilities"]),
                         {"filesystem.create", "execution.python_script"})
        self.assertEqual(set(result["tasks"][0]["required_tools"]),
                         {"write_file", "run_command"})

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
            properties = format_schema["properties"]["tasks"]["items"]["anyOf"][0]["properties"]
            self.assertIn("operations", properties)
            self.assertIn("task_kind", properties)
            self.assertNotIn("required_capabilities", properties)
            self.assertNotIn("required_tools", properties)
            self.assertNotIn("preferred_skills", properties)


class ClarificationIntegrationTests(unittest.TestCase):
    def test_model_calculator_web_answer_discards_extra_features_and_plans_once(self):
        prompt = "Hace una calculadora que pueda sumar restar multiplicar y dividir"
        ready_basis = deterministic_task_spec(prompt)
        calls = []
        def request(method, url, payload, timeout):
            calls.append(payload)
            candidate = _analyst_response(ready_basis)
            candidate["status"] = "NEEDS_CLARIFICATION"
            candidate["clarification_questions"] = [
                {"question": "¿Consola, escritorio o Web?", "reason": "La interfaz cambia el producto.",
                 "field": "interface", "required": True},
                {"question": "¿Quieres funcionalidades adicionales?",
                 "reason": "Posibles funciones extras.", "field": "additional_features", "required": True},
            ]
            return {"message": {"content": json.dumps(candidate)}}
        with TemporaryDirectory(dir=".") as folder:
            store = Store(Path(folder) / "state.sqlite3")
            store.create_agent(normalize_agent({"name": "Analyst", "config": {
                "orchestration_role": "task_analyst", "model": "test-model"}}))
            planner = CountingPlanner()
            analyst = TaskSpecAnalyst(request=request)
            orchestrator = Orchestrator(store, None, planner=planner, task_analyst=analyst)
            orchestrator._run_graph = lambda *args: None
            run = store.create_orchestration(prompt)
            orchestrator._run(run["id"])
            pending = store.get_orchestration(run["id"])
            self.assertEqual(pending["status"], "NeedsClarification")
            self.assertEqual([q["field"] for q in pending["task_spec"]["clarification_questions"]],
                             ["interface"])
            self.assertEqual(pending["task_spec"]["clarification_questions"][0]["id"], "CQ-1")
            store.record_clarification_answers(run["id"], {"CQ-1": "Web"})
            orchestrator._run(run["id"], {"CQ-1": "Web"})
            final = store.get_orchestration(run["id"])
            analyst_events = [event for event in final["events"]
                              if event["event_type"].startswith(
                                  ("task_analysis.", "freya.task_analysis.")
                              )]
            for event in analyst_events:
                payload = json.loads(event["payload_json"])
                self.assertTrue(payload["system_component"])
                self.assertIsNone(payload["agent_id"])
            self.assertEqual(final["task_spec"]["status"], "READY_FOR_PLANNING")
            self.assertEqual(final["task_spec"]["user_decisions"]["interface"], "Web")
            self.assertEqual(len(final["task_spec"]["clarification_history"]), 1)
            self.assertEqual(final["task_spec"]["clarification_history"][0]["question_id"], "CQ-1")
            self.assertEqual(len(planner.calls), 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(analyst.metrics["model_calls"], 1)
            self.assertFalse(analyst.metrics["fallback_used"])
            self.assertFalse(analyst.metrics["repair_attempted"])
            self.assertIn("task_analysis.clarification_deduplicated",
                          [event["event_type"] for event in final["events"]])

    def test_legitimate_second_question_gets_cq2_and_old_answer_replay_is_idempotent(self):
        prompt = "Hace una calculadora que pueda sumar restar multiplicar y dividir"
        basis = deterministic_task_spec(prompt)
        calls = []
        def request(method, url, payload, timeout):
            calls.append(payload)
            candidate = _analyst_response(basis)
            if len(calls) == 1:
                candidate.update(status="NEEDS_CLARIFICATION", clarification_questions=[
                    {"question": "¿Qué interfaz?", "reason": "Define el producto.",
                     "field": "interface", "required": True}])
            elif len(calls) == 2:
                candidate.update(status="NEEDS_CLARIFICATION", clarification_questions=[
                    {"question": "¿Se necesita precisión decimal especial?",
                     "reason": "Define el cálculo requerido.", "field": "precision_mode", "required": True}])
            return {"message": {"content": json.dumps(candidate)}}
        with TemporaryDirectory(dir=".") as folder:
            store = Store(Path(folder) / "state.sqlite3")
            store.create_agent(normalize_agent({"name": "Analyst", "config": {
                "orchestration_role": "task_analyst", "model": "test-model"}}))
            planner = CountingPlanner()
            orchestrator = Orchestrator(store, None, planner=planner,
                                        task_analyst=TaskSpecAnalyst(request=request))
            orchestrator._run_graph = lambda *args: None
            run = store.create_orchestration(prompt)
            orchestrator._run(run["id"])
            first = store.get_orchestration(run["id"])
            self.assertEqual(first["task_spec"]["clarification_questions"][0]["id"], "CQ-1")
            recorded = store.record_clarification_answers(run["id"], {"CQ-1": "Web"})
            self.assertFalse(recorded["_clarification_replayed"])
            self.assertTrue(store.record_clarification_answers(run["id"],
                                                               {"CQ-1": "Web"})["_clarification_replayed"])
            orchestrator._run(run["id"], {"CQ-1": "Web"})
            second = store.get_orchestration(run["id"])
            self.assertEqual(second["status"], "NeedsClarification")
            self.assertEqual(second["task_spec"]["clarification_questions"][0]["id"], "CQ-2")
            self.assertTrue(store.record_clarification_answers(run["id"],
                                                               {"CQ-1": "Web"})["_clarification_replayed"])
            self.assertEqual(len(second["clarification_answers"]), 1)
            with self.assertRaises(ValueError):
                store.record_clarification_answers(run["id"], {"CQ-1": "different"})
            store.record_clarification_answers(run["id"], {"CQ-2": "No"})
            orchestrator._run(run["id"], {"CQ-2": "No"})
            final = store.get_orchestration(run["id"])
            self.assertEqual(final["task_spec"]["status"], "READY_FOR_PLANNING")
            self.assertEqual([x["question_id"] for x in final["task_spec"]["clarification_history"]],
                             ["CQ-1", "CQ-2"])
            self.assertEqual(final["task_spec"]["user_decisions"]["precision_mode"], "No")
            self.assertEqual(len(planner.calls), 1)

    def test_clarification_cycle_fails_after_three_rounds_with_event(self):
        prompt = "Hace una calculadora que pueda sumar restar multiplicar y dividir"
        basis = deterministic_task_spec(prompt)
        fields = ["interface", "precision_mode", "data_mode", "deployment_region"]
        def request(method, url, payload, timeout):
            previous = json.loads(payload["messages"][1]["content"])["previous_task_spec"]
            submitted = json.loads(payload["messages"][1]["content"])["answers_to_pending_questions"]
            index = previous["version"] - 1 + bool(submitted)
            candidate = _analyst_response(basis)
            candidate.update(status="NEEDS_CLARIFICATION", clarification_questions=[
                {"question": "Indica " + fields[index], "reason": "Decisión requerida.",
                 "field": fields[index], "required": True}])
            return {"message": {"content": json.dumps(candidate)}}
        with TemporaryDirectory(dir=".") as folder:
            store = Store(Path(folder) / "state.sqlite3")
            store.create_agent(normalize_agent({"name": "Analyst", "config": {
                "orchestration_role": "task_analyst", "model": "test-model"}}))
            planner = CountingPlanner()
            orchestrator = Orchestrator(store, None, planner=planner,
                                        task_analyst=TaskSpecAnalyst(request=request))
            run = store.create_orchestration(prompt)
            orchestrator._run(run["id"])
            for answer in ("Web", "No", "Local"):
                current = store.get_orchestration(run["id"])
                question = current["task_spec"]["clarification_questions"][0]
                store.record_clarification_answers(run["id"], {question["id"]: answer})
                orchestrator._run(run["id"], {question["id"]: answer})
            final = store.get_orchestration(run["id"])
            self.assertEqual(final["status"], "Failed")
            self.assertIsNone(final["plan"])
            self.assertEqual(len(planner.calls), 0)
            cycle = next(json.loads(event["payload_json"]) for event in final["events"]
                         if event["event_type"] == "task_analysis.clarification_cycle_detected")
            self.assertEqual(cycle["rounds"], 3)
            self.assertEqual(cycle["pending_fields"], ["deployment_region"])

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
