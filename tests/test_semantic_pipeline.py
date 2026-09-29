"""Regressions for canonical intent and semantic plan compilation."""
import json
import unittest
from unittest.mock import patch

from control_center.plan_compiler import (
    OwnershipAmbiguous, compile_semantic_plan, planned_write_target_grants,
)
from control_center.planner import Planner, PlanGenerationError, semantic_plan_response_format
from control_center.runtime_resources import RuntimeResourceCatalog, UnsupportedResourceRequirement
from control_center.task_spec import (
    TASK_SPEC_RESPONSE_FORMAT, TaskSpecAnalyst, _action_facts,
    _nearest_semantic_token, deterministic_task_spec, validate_task_spec,
)


def web_spec():
    return deterministic_task_spec(
        "Crea una calculadora web que pueda sumar restar dividir y mutiplicar")


def task(key, operation, path="calculator.js", dependencies=(), criterion="File exists."):
    return {"key": key, "task_kind": "program_creation", "objective": key,
            "description": key, "depends_on": list(dependencies),
            "semantic_needs": [key], "operations": [operation],
            "owned_paths": [path], "success_criteria": [criterion]}


class SemanticPipelineTests(unittest.TestCase):
    def test_typo_and_web_clarification_reach_valid_single_owner_plan(self):
        prompt = "Crea una calculadora totalmente funcional que pueda sumar restar dividir y mutiplicar"
        pending = deterministic_task_spec(prompt)
        candidate = deterministic_task_spec(prompt, pending, {"CQ-1": "web"})
        candidate["deliverables"] = [{"description": "Calculadora web", "source": "explicit"}]
        response = {key: candidate[key] for key in TASK_SPEC_RESPONSE_FORMAT["properties"]
                    if key in candidate}
        response["clarification_questions"] = []
        analyst = TaskSpecAnalyst(request=lambda *args, **kwargs: {
            "message": {"content": json.dumps(response, ensure_ascii=False)}})
        spec = analyst.analyze_spec(prompt, previous=pending, answers={"CQ-1": "web"})
        semantic = {"summary": "Implement web calculator", "success_criteria": [],
                    "unsupported_requirements": [], "tasks": [
                        task("create", "create_file", path="calculator.js"),
                        task("implement", "modify_file", path="calculator.js",
                             dependencies=["create"], criterion="Four operations work.")]}
        planner = Planner(lambda planner_prompt, context: semantic)
        compiled = planner.create_plan_for_spec(spec)
        self.assertFalse(analyst.metrics["fallback_used"])
        self.assertEqual(analyst.metrics["model_calls"], 1)
        self.assertEqual(planner.metrics["model_calls"], 1)
        self.assertEqual(len(compiled["tasks"]), 2)
        self.assertEqual(compiled["tasks"][0]["owned_paths"], ["calculator.js"])
        self.assertEqual(compiled["tasks"][1]["owned_paths"], [])
        self.assertEqual(compiled["tasks"][1]["foreign_write_targets"],
                         [{"path": "calculator.js", "owner_plan_task_id": "task-1"}])
        self.assertIn("task_analysis.semantic_normalization",
                      [event["event_type"] for event in analyst.diagnostic_events])
        self.assertIn("plan_compiler.foreign_write_routed",
                      [event["event_type"] for event in planner.metrics["compiler_events"]])
        self.assertEqual({fact["action"] for entry in spec["requirements"]
                          for fact in _action_facts(entry["description"], entry["source"])},
                         {"sum", "subtract", "divide", "multiply"})

    def test_bounded_typo_preserves_four_actions_and_source(self):
        prompt = "Crea una calculadora que pueda sumar restar dividir y mutiplicar"
        pending = deterministic_task_spec(prompt)
        spec = deterministic_task_spec(prompt, pending, {"CQ-1": "web"})
        facts = [fact for entry in spec["requirements"]
                 for fact in _action_facts(entry["description"], entry["source"])]
        self.assertEqual({fact["action"] for fact in facts},
                         {"sum", "subtract", "divide", "multiply"})
        self.assertIn("mutiplicar", json.dumps(spec["requirements"], ensure_ascii=False))
        analyst = TaskSpecAnalyst(offline=True)
        analyst.analyze_spec(prompt)
        self.assertIn({"original": "mutiplicar", "canonical": "multiplicar",
                       "semantic_action": "multiply", "method": "bounded_typo_normalization"},
                      analyst.metrics["semantic_normalization"])

    def test_ambiguous_registered_typos_are_not_guessed(self):
        self.assertIsNone(_nearest_semantic_token(
            "multipli", {"multiply": "multiply", "multiplie": "other"}))

    def test_clarified_web_modifier_is_separated_before_scope_validation(self):
        prompt = "crea una calculadora que pueda sumar"
        pending = deterministic_task_spec(prompt)
        candidate = deterministic_task_spec(prompt, pending, {"CQ-1": "web"})
        candidate["deliverables"] = [{"description": "Calculadora web", "source": "explicit"}]
        response = {key: candidate[key] for key in TASK_SPEC_RESPONSE_FORMAT["properties"]
                    if key in candidate}
        response["clarification_questions"] = []
        calls = []

        def request(*args, **kwargs):
            calls.append(args)
            return {"message": {"content": json.dumps(response, ensure_ascii=False)}}

        analyst = TaskSpecAnalyst(request=request)
        spec = analyst.analyze_spec(prompt, previous=pending, answers={"CQ-1": "web"})
        self.assertEqual(len(calls), 1)
        self.assertFalse(analyst.metrics["fallback_used"])
        self.assertEqual(spec["deliverables"], [{"description": "Calculadora", "source": "explicit"}])
        self.assertIn({"description": "interfaz web", "source": "clarified"}, spec["constraints"])
        self.assertEqual(spec["user_decisions"]["interface"], "web")

    def test_direct_create_modify_same_path_keeps_distinct_plan_tasks(self):
        semantic = {"summary": "Implement", "success_criteria": [],
                    "unsupported_requirements": [], "tasks": [
                        task("create", "create_file"),
                        task("implement", "modify_file", dependencies=["create"],
                             criterion="All requested operations work.")]}
        catalog = RuntimeResourceCatalog.build()
        compiled = compile_semantic_plan(semantic, web_spec(), resource_catalog=catalog)
        self.assertEqual(len(compiled["tasks"]), 2)
        self.assertEqual(compiled["tasks"][0]["owned_paths"], ["calculator.js"])
        self.assertEqual(compiled["tasks"][1]["owned_paths"], [])
        self.assertEqual(compiled["tasks"][1]["depends_on"], ["task-1"])
        self.assertEqual(compiled["write_owners"], {"calculator.js": "task-1"})
        self.assertEqual(compiled["tasks"][1]["foreign_write_targets"],
                         [{"path": "calculator.js", "owner_plan_task_id": "task-1"}])
        self.assertEqual(planned_write_target_grants(compiled, "task-2"),
                         [{"path": "calculator.js", "owner_plan_task_id": "task-1"}])
        self.assertNotIn("plan_compiler.tasks_coalesced",
                      [event["event_type"] for event in catalog.compiler_events])

    def test_dependency_without_exact_foreign_write_target_does_not_grant(self):
        creator = task("create", "create_file", path="calculator.js")
        dependent = task("implement", "modify_file", path="other.js", dependencies=["create"])
        dependent["owned_paths"] = []
        dependent["write_targets"] = ["other.js"]
        compiled = compile_semantic_plan({
            "summary": "Create and implement", "success_criteria": [],
            "unsupported_requirements": [], "tasks": [creator, dependent],
        }, web_spec())
        self.assertEqual(compiled["tasks"][1]["depends_on"], ["task-1"])
        self.assertEqual(planned_write_target_grants(compiled, "task-2"), [])

    def test_single_writer_without_explicit_owner_becomes_permanent_owner(self):
        writer = task("implement", "modify_file")
        writer["owned_paths"] = []
        writer["write_targets"] = ["calculator.js"]
        compiled = compile_semantic_plan({"summary": "Implement", "success_criteria": [],
                                          "unsupported_requirements": [], "tasks": [writer]}, web_spec())
        self.assertEqual(compiled["write_owners"], {"calculator.js": "task-1"})
        self.assertEqual(compiled["tasks"][0]["owned_paths"], ["calculator.js"])
        self.assertEqual(compiled["tasks"][0]["foreign_write_targets"], [])

    def test_two_modifiers_without_creator_request_one_bounded_repair(self):
        first = task("first", "modify_file")
        second = task("second", "modify_file")
        original = {"summary": "Implement", "success_criteria": [],
                    "unsupported_requirements": [], "tasks": [first, second]}
        repaired = {**original, "tasks": [first, {**second, "owned_paths": [],
                                                   "write_targets": ["calculator.js"]}]}
        calls = []
        planner = Planner(lambda prompt, context:
                          (calls.append(prompt), original if len(calls) == 1 else repaired)[1])
        result = planner.create_plan_for_spec(web_spec())
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(calls[1])["compiler_error"]["type"], "OwnershipAmbiguous")
        self.assertEqual(result["write_owners"], {"calculator.js": "task-1"})
        self.assertEqual(result["tasks"][1]["foreign_write_targets"],
                         [{"path": "calculator.js", "owner_plan_task_id": "task-1"}])

    def test_two_creators_of_one_path_are_invalid(self):
        semantic = {"summary": "Implement", "success_criteria": [],
                    "unsupported_requirements": [], "tasks": [
                        task("first", "create_file"), task("second", "create_file")]}
        with self.assertRaisesRegex(ValueError, "Two creators claim calculator.js"):
            compile_semantic_plan(semantic, web_spec())

    def test_unique_creator_resolves_duplicate_ownership_without_repair(self):
        first = task("create", "create_file", criterion="Independent result works.")
        second = task("implement", "modify_file", dependencies=["create"])
        unaffected = {"key": "review", "task_kind": "review", "objective": "Review result",
                      "description": "Inspect the result", "depends_on": ["implement"],
                      "semantic_needs": ["Inspect result"], "operations": ["read_file"],
                      "owned_paths": [], "success_criteria": ["Review complete."]}
        original = {"summary": "Implement", "success_criteria": [],
                    "unsupported_requirements": [], "tasks": [first, second, unaffected]}
        repaired = {**original, "tasks": [
            {**second, "key": "implement", "operations": ["create_file", "modify_file"],
             "depends_on": []}, unaffected]}
        calls = []

        def decide(prompt, context):
            calls.append(prompt)
            return original if len(calls) == 1 else repaired

        planner = Planner(decide)
        result = planner.create_plan_for_spec(web_spec())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result["tasks"]), 3)
        self.assertEqual(result["tasks"][1]["owned_paths"], [])
        self.assertEqual(result["tasks"][2]["objective"], "Review result")
        self.assertEqual(planner.metrics.get("planner_events", []), [])

    def test_false_unsupported_claim_uses_one_repair(self):
        original = {"summary": "Implement", "success_criteria": [], "tasks": [
            task("implement", "create_file")], "unsupported_requirements": [
                {"semantic_need": "multiplication", "reason": "unsupported"}]}
        repaired = {**original, "unsupported_requirements": []}
        calls = []
        planner = Planner(lambda prompt, context:
                          (calls.append(prompt), original if len(calls) == 1 else repaired)[1])
        planner.create_plan_for_spec(web_spec())
        self.assertEqual(len(calls), 2)
        self.assertIn("previous_semantic_plan", calls[1])

    def test_repair_cannot_reinvent_unaffected_task(self):
        original = {"summary": "Implement", "success_criteria": [],
                    "unsupported_requirements": [], "tasks": [
                        task("create", "create_file", criterion="Independent result works."),
                        task("implement", "modify_file", dependencies=["create"]),
                        {"key": "review", "task_kind": "review", "objective": "Review result",
                         "description": "Inspect result", "depends_on": ["implement"],
                         "semantic_needs": ["Inspect result"], "operations": ["read_file"],
                         "owned_paths": [], "success_criteria": ["Reviewed."]}]}
        original["tasks"][0]["operations"] = ["modify_file"]
        revised = {**original, "tasks": [
            {**original["tasks"][0], "owned_paths": []},
            {**original["tasks"][1], "owned_paths": ["calculator.js"]},
            {**original["tasks"][2], "objective": "Invented replacement"}]}
        calls = []
        planner = Planner(lambda prompt, context:
                          (calls.append(prompt), original if len(calls) == 1 else revised)[1])
        with self.assertRaises(PlanGenerationError):
            planner.create_plan_for_spec(web_spec())
        self.assertEqual(len(calls), 2)

    def test_genuine_requested_external_operation_is_typed_unsupported(self):
        spec = deterministic_task_spec("Create a web tool and publish it to remote hosting")
        semantic = {"summary": "Create and publish", "success_criteria": [],
                    "unsupported_requirements": [{"semantic_need": "publish on remote server",
                                                   "reason": "No operation"}],
                    "tasks": [task("implement", "create_file")]}
        with self.assertRaises(UnsupportedResourceRequirement):
            compile_semantic_plan(semantic, spec)

    def test_genuine_requested_local_operation_is_typed_unsupported(self):
        prompt = "Capture a screen image"
        spec = validate_task_spec({
            "schema_version": 1, "version": 1, "status": "READY_FOR_PLANNING",
            "source_prompt": prompt, "objective": prompt, "user_intent": prompt,
            "deliverables": [{"description": "Screen image", "source": "explicit"}],
            "requirements": [{"description": prompt, "source": "explicit"}],
            "constraints": [], "user_decisions": {}, "assumptions": [],
            "validation_expectations": [], "context": {},
            "clarification_questions": [], "clarification_history": [],
            "readiness_reason": "The requested local result is specified.",
        })
        semantic = {"summary": prompt, "success_criteria": [],
                    "unsupported_requirements": [{"semantic_need": prompt,
                                                   "reason": "No registered capture operation"}],
                    "tasks": [{"key": "inspect", "task_kind": "review", "objective": "Inspect workspace",
                               "description": "Inspect available files", "depends_on": [],
                               "semantic_needs": ["Inspect workspace"], "operations": ["list_workspace"],
                               "owned_paths": [], "success_criteria": ["Workspace inspected."]}]}
        with self.assertRaises(UnsupportedResourceRequirement):
            compile_semantic_plan(semantic, spec)

    def test_compiler_is_single_scope_and_resource_validation_authority(self):
        semantic = {"summary": "Implement", "success_criteria": [],
                    "unsupported_requirements": [],
                    "tasks": [task("implement", "create_file")]}
        catalog = RuntimeResourceCatalog.build()
        with patch("control_center.plan_compiler.reconcile_plan_scope",
                   wraps=__import__("control_center.plan_scope", fromlist=["reconcile_plan_scope"]).reconcile_plan_scope) as scope:
            with patch.object(catalog, "validate_semantic_plan",
                              wraps=catalog.validate_semantic_plan) as validate:
                with patch("control_center.planner.RuntimeResourceCatalog.from_context",
                           return_value=catalog):
                    Planner(lambda prompt, context: semantic).create_plan_for_spec(web_spec())
        self.assertEqual(scope.call_count, 1)
        self.assertEqual(validate.call_count, 1)

    def test_global_criterion_stays_out_of_last_local_task(self):
        spec = web_spec()
        spec["validation_expectations"] = ["Integrated web calculator works."]
        semantic = {"summary": "Implement", "success_criteria": [],
                    "unsupported_requirements": [],
                    "tasks": [task("implement", "create_file", criterion="File exists.")]}
        compiled = compile_semantic_plan(semantic, spec)
        self.assertIn("Integrated web calculator works.", compiled["success_criteria"])
        self.assertNotIn("Integrated web calculator works.",
                         compiled["tasks"][-1]["success_criteria"])

    def test_semantic_schema_excludes_runtime_authority(self):
        schema = semantic_plan_response_format({})["properties"]["tasks"]["items"]
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("required_capabilities", schema["properties"])
        self.assertNotIn("required_tools", schema["properties"])


if __name__ == "__main__":
    unittest.main()
