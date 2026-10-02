"""Regressions for canonical intent and semantic plan compilation."""
import json
import unittest
from unittest.mock import patch

from control_center.plan_compiler import (
    CriterionEvidenceMismatch, WriteScopeOverlap,
    compile_semantic_plan, planned_write_target_grants,
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
                             dependencies=["create"],
                             criterion="calculator.js contains handlers for four operations.")]}
        planner = Planner(lambda planner_prompt, context: semantic)
        compiled = planner.create_plan_for_spec(spec)
        self.assertFalse(analyst.metrics["fallback_used"])
        self.assertEqual(analyst.metrics["model_calls"], 1)
        self.assertEqual(planner.metrics["model_calls"], 1)
        self.assertEqual(len(compiled["tasks"]), 1)
        self.assertEqual(compiled["tasks"][0]["owned_paths"], ["calculator.js"])
        self.assertEqual(compiled["tasks"][0]["semantic_operations"], ["create_file", "modify_file"])
        self.assertIn("task_analysis.semantic_normalization",
                      [event["event_type"] for event in analyst.diagnostic_events])
        self.assertIn("plan_compiler.tasks_merged",
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

    def test_mechanical_create_modify_same_path_merges_before_ownership(self):
        semantic = {"summary": "Implement", "success_criteria": [],
                    "unsupported_requirements": [], "tasks": [
                        task("create", "create_file"),
                        task("implement", "modify_file", dependencies=["create"],
                             criterion="calculator.js contains all requested operation handlers.")]}
        catalog = RuntimeResourceCatalog.build()
        compiled = compile_semantic_plan(semantic, web_spec(), resource_catalog=catalog)
        self.assertEqual(len(compiled["tasks"]), 1)
        self.assertEqual(compiled["tasks"][0]["owned_paths"], ["calculator.js"])
        self.assertEqual(compiled["tasks"][0]["depends_on"], [])
        self.assertEqual(compiled["write_owners"], {"calculator.js": "task-1"})
        self.assertEqual(compiled["tasks"][0]["foreign_write_targets"], [])
        self.assertEqual(planned_write_target_grants(compiled, "task-1"), [])
        self.assertIn("plan_compiler.tasks_merged",
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
        first = task("first", "modify_file", criterion="The source defines the initial usable component.")
        second = task("second", "modify_file")
        original = {"summary": "Implement", "success_criteria": [],
                    "unsupported_requirements": [], "tasks": [first, second]}
        repaired = {**original, "tasks": [first, {**second, "owned_paths": [],
                                                   "write_targets": ["calculator.js"],
                                                   "depends_on": ["first"]}]}
        calls = []
        planner = Planner(lambda prompt, context:
                          (calls.append(prompt), original if len(calls) == 1 else repaired)[1])
        result = planner.create_plan_for_spec(web_spec())
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(calls[1])["compiler_error"]["type"], "WriteScopeOverlap")
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
        first = task("create", "create_file", criterion="The requested file exists.")
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
        self.assertEqual(len(result["tasks"]), 2)
        self.assertEqual(result["tasks"][0]["owned_paths"], ["calculator.js"])
        self.assertEqual(result["tasks"][1]["objective"], "Review result")
        self.assertFalse(any(event["event_type"] == "planner.repair_requested"
                             for event in planner.metrics.get("planner_events", [])))

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
        schema = semantic_plan_response_format({})["properties"]["tasks"]["items"]["anyOf"][0]
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("required_capabilities", schema["properties"])
        self.assertNotIn("required_tools", schema["properties"])


class PlanResponsibilityAndEvidenceTests(unittest.TestCase):
    @staticmethod
    def plan(*tasks):
        return {"summary": "Implement and verify", "success_criteria": [],
                "unsupported_requirements": [], "tasks": list(tasks)}

    @staticmethod
    def _testing_task(*, key="test", dependencies=("implement",),
                      criterion="Application performs calculation correctly."):
        return {"key": key, "task_kind": "testing", "objective": "Test behavior",
                "description": "Run the registered test suite and record results.",
                "depends_on": list(dependencies),
                "semantic_needs": ["Run tests and capture output."],
                "operations": ["run_pytest"], "owned_paths": [], "write_targets": [],
                "success_criteria": [criterion]}

    def test_static_implementation_and_runtime_testing_criteria_are_valid(self):
        implementation = task(
            "implement", "modify_file", path="app.py",
            criterion="app.py contains handlers for addition and subtraction.")
        testing = self._testing_task()
        catalog = RuntimeResourceCatalog.build()
        compiled = compile_semantic_plan(
            self.plan(implementation, testing), web_spec(), resource_catalog=catalog)
        self.assertEqual(compiled["tasks"][0]["success_criteria"],
                         ["app.py contains handlers for addition and subtraction."])
        self.assertEqual(compiled["tasks"][1]["success_criteria"],
                         ["Application performs calculation correctly."])
        classifications = [event for event in catalog.compiler_events
                           if event["event_type"] == "plan_compiler.criterion_classified"]
        self.assertEqual([item["evidence_type"] for item in classifications],
                         ["static_structure", "runtime_behavior"])
        self.assertTrue(all(item["verifiable"] for item in classifications))
        self.assertEqual([item["verification_mode"] for item in classifications], ["semantic", "semantic"])

    def test_runtime_criterion_without_runtime_capability_is_rejected(self):
        implementation = task(
            "implement", "modify_file", path="app.py",
            criterion="Application performs calculation correctly.")
        catalog = RuntimeResourceCatalog.build()
        with self.assertRaises(CriterionEvidenceMismatch):
            compile_semantic_plan(
                self.plan(implementation), web_spec(), resource_catalog=catalog)
        required = next(event for event in catalog.compiler_events
                        if event["event_type"] == "plan_compiler.plan_repair_required")
        self.assertEqual(required["evidence_type"], "runtime_behavior")
        self.assertEqual(required["task_id"], "task-1")

    def test_planner_gets_one_bounded_repair_for_unverifiable_criterion(self):
        invalid = self.plan(task(
            "implement", "modify_file", path="app.py",
            criterion="Application performs calculation correctly."))
        repaired = self.plan(task(
            "implement", "modify_file", path="app.py",
            criterion="app.py contains the requested calculation handlers."))
        calls = []
        planner = Planner(lambda prompt, context:
                          (calls.append(prompt), invalid if len(calls) == 1 else repaired)[1])
        compiled = planner.create_plan_for_spec(web_spec())
        self.assertEqual(len(calls), 2)
        payload = json.loads(calls[1])
        self.assertEqual(payload["compiler_error"]["type"], "CriterionEvidenceMismatch")
        self.assertEqual(payload["compiler_error"]["affected_tasks"], ["task-1"])
        self.assertEqual(compiled["tasks"][0]["success_criteria"],
                         ["app.py contains the requested calculation handlers."])

    def test_equivalent_sibling_writers_are_rejected(self):
        scaffold = task("scaffold", "create_file", path="script.js",
                        criterion="The script.js file exists.")
        first = task("interface", "modify_file", path="script.js", dependencies=("scaffold",),
                     criterion="script.js contains calculator event handlers.")
        second = task("logic", "modify_file", path="script.js", dependencies=("scaffold",),
                      criterion="script.js contains calculator operation handlers.")
        for writer in (first, second):
            writer["owned_paths"] = []
            writer["write_targets"] = ["script.js"]
        catalog = RuntimeResourceCatalog.build()
        with self.assertRaises(WriteScopeOverlap):
            compile_semantic_plan(
                self.plan(scaffold, first, second), web_spec(), resource_catalog=catalog)
        event = next(item for item in catalog.compiler_events
                     if item["event_type"] == "plan_compiler.overlap_detected"
                     and item["task_ids"] == ["task-2", "task-3"])
        self.assertEqual(event["path"], "script.js")
        self.assertFalse(event["dependency_ordered"])

    def test_sequential_distinct_modifiers_may_share_a_target(self):
        first = task("functions", "modify_file", path="a.js",
                     criterion="a.js contains the core functions.")
        second = task("errors", "modify_file", path="a.js", dependencies=("functions",),
                      criterion="a.js contains explicit error handlers.")
        second["owned_paths"] = []
        second["write_targets"] = ["a.js"]
        catalog = RuntimeResourceCatalog.build()
        compiled = compile_semantic_plan(
            self.plan(first, second), web_spec(), resource_catalog=catalog)
        self.assertEqual(compiled["tasks"][1]["depends_on"], ["task-1"])
        self.assertEqual(compiled["tasks"][1]["foreign_write_targets"],
                         [{"path": "a.js", "owner_plan_task_id": "task-1"}])
        self.assertIn("plan_compiler.overlap_validated",
                      [item["event_type"] for item in catalog.compiler_events])

    def test_create_then_modify_same_target_normalizes_to_one_valid_task(self):
        creator = task("scaffold", "create_file", path="a.js",
                       criterion="The a.js file exists.")
        modifier = task("implement", "modify_file", path="a.js", dependencies=("scaffold",),
                        criterion="a.js contains the requested functions.")
        modifier["owned_paths"] = []
        modifier["write_targets"] = ["a.js"]
        compiled = compile_semantic_plan(self.plan(creator, modifier), web_spec())
        self.assertEqual(compiled["write_owners"], {"a.js": "task-1"})
        self.assertEqual(len(compiled["tasks"]), 1)
        self.assertEqual(compiled["tasks"][0]["depends_on"], [])
        self.assertEqual(compiled["tasks"][0]["semantic_operations"], ["create_file", "modify_file"])

    def test_runtime_criterion_moves_to_one_dependent_testing_task(self):
        implementation = task(
            "implement", "modify_file", path="app.py",
            criterion="Application performs calculation correctly.")
        testing = self._testing_task(criterion="The test suite passes successfully.")
        catalog = RuntimeResourceCatalog.build()
        compiled = compile_semantic_plan(
            self.plan(implementation, testing), web_spec(), resource_catalog=catalog)
        self.assertNotIn("Application performs calculation correctly.",
                         compiled["tasks"][0]["success_criteria"])
        self.assertIn("Application performs calculation correctly.",
                      compiled["tasks"][1]["success_criteria"])
        reassigned = next(item for item in catalog.compiler_events
                          if item["event_type"] == "plan_compiler.criterion_reassigned")
        self.assertEqual(reassigned["source_task_id"], "task-1")
        self.assertEqual(reassigned["target_task_id"], "task-2")

    def test_static_source_criterion_is_supported_by_read_capability(self):
        review = {"key": "inspect", "task_kind": "review", "objective": "Inspect source",
                  "description": "Read source files.", "depends_on": [],
                  "semantic_needs": ["Read app.py."], "operations": ["read_file"],
                  "owned_paths": [], "write_targets": [],
                  "success_criteria": ["app.py contains functions X, Y and Z."]}
        compiled = compile_semantic_plan(self.plan(review), web_spec())
        self.assertEqual(compiled["tasks"][0]["required_capabilities"], ["filesystem.read"])

    def test_visual_criterion_without_visual_capability_is_rejected(self):
        review = {"key": "inspect", "task_kind": "review", "objective": "Inspect UI source",
                  "description": "Read the UI source.", "depends_on": [],
                  "semantic_needs": ["Read index.html."], "operations": ["read_file"],
                  "owned_paths": [], "write_targets": [],
                  "success_criteria": ["The UI looks visually correct and aligned."]}
        with self.assertRaises(CriterionEvidenceMismatch):
            compile_semantic_plan(self.plan(review), web_spec())

    def test_external_state_criterion_without_external_capability_is_rejected(self):
        spec = web_spec()
        spec["objective"] += " and deploy it to production"
        spec["user_intent"] += " and deploy it to production"
        spec["requirements"].append({
            "description": "Deploy the application to production.", "source": "explicit",
        })
        review = {"key": "inspect", "task_kind": "review", "objective": "Inspect release files",
                  "description": "Read the local release manifest.", "depends_on": [],
                  "semantic_needs": ["Read release.txt."], "operations": ["read_file"],
                  "owned_paths": [], "write_targets": [],
                  "success_criteria": ["The application is deployed and publicly accessible."]}
        with self.assertRaises(CriterionEvidenceMismatch):
            compile_semantic_plan(self.plan(review), spec)

    def test_sibling_tasks_with_distinct_paths_are_valid(self):
        scaffold = task("scaffold", "create_file", path="project.txt",
                        criterion="The project.txt file exists.")
        html = task("html", "create_file", path="index.html", dependencies=("scaffold",),
                    criterion="index.html contains calculator controls.")
        css = task("css", "create_file", path="styles.css", dependencies=("scaffold",),
                   criterion="styles.css contains calculator layout styles.")
        compiled = compile_semantic_plan(self.plan(scaffold, html, css), web_spec())
        self.assertEqual(len(compiled["write_owners"]), 3)

    def test_semantic_planner_prompt_requires_scoped_evidence_and_ordered_writes(self):
        captured = []
        semantic = self.plan(task("implement", "create_file", path="app.py",
                                  criterion="The app.py file exists."))
        Planner(lambda prompt, context: (captured.append(prompt), semantic)[1]).create_plan_for_spec(
            web_spec())
        self.assertIn("one primary responsibility", captured[0])
        self.assertIn("sibling writers of one path are rejected", captured[0])
        self.assertIn("execution/test operations can prove runtime behavior", captured[0])


if __name__ == "__main__":
    unittest.main()
