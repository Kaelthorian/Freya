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

    def test_simple_artifact_collapse_keeps_explicit_criterion_link(self):
        global_text = "hola_mundo.txt contains exactly hola mundo."
        first = task("create", "Create hola_mundo.txt", required_capabilities=["filesystem.create"],
                     success_criteria=["The file exists."])
        second = task("verify", "Read hola_mundo.txt", depends_on=["create"],
                      required_capabilities=["filesystem.read"],
                      success_criteria=["The exact contents were checked."])
        generated = plan(tasks=[first, second], complexity="multi_step",
                         goal="Create hola_mundo.txt", success_criteria=[global_text],
                         criterion_links={
                             "global": [{"id": "gc-file", "criterion": global_text}],
                             "local": [
                                 {"id": "tc-create", "task_id": "create", "criterion": first["success_criteria"][0],
                                  "supports_global_criteria": []},
                                 {"id": "tc-verify", "task_id": "verify", "criterion": second["success_criteria"][0],
                                  "supports_global_criteria": ["gc-file"]},
                             ],
                         })
        result = Planner(lambda prompt, context: generated).create_plan(
            "Create hola_mundo.txt", {"task_analysis": deterministic_task_analysis("Create hola_mundo.txt")})
        self.assertEqual(len(result["tasks"]), 1)
        linked = next(item for item in result["criterion_links"]["local"]
                      if item["id"] == "tc-verify")
        self.assertEqual(linked["task_id"], "create")
        self.assertEqual(linked["supports_global_criteria"], ["gc-file"])

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

    def test_global_criterion_ids_are_assigned_when_missing(self):
        criteria = ["archivo existe", "contenido correcto"]
        raw = plan(success_criteria=criteria, criterion_links={
            "global": [{"criterion": criterion} for criterion in criteria],
            "local": [],
        })
        result = validate_plan(raw)
        self.assertEqual([item["id"] for item in result["criterion_links"]["global"]],
                         ["gc-1", "gc-2"])
        self.assertEqual([item["criterion"] for item in result["criterion_links"]["global"]], criteria)

    def test_missing_global_link_rows_are_synthesized_in_success_criteria_order(self):
        criteria = ["A", "B", "C"]
        raw = plan(success_criteria=criteria, criterion_links={
            "global": [
                {"id": "gc-third", "criterion": "C"},
                {"criterion": "A"},
            ],
            "local": [],
        })
        result = validate_plan(raw)
        self.assertEqual(result["criterion_links"]["global"], [
            {"id": "gc-1", "criterion": "A"},
            {"id": "gc-2", "criterion": "B"},
            {"id": "gc-third", "criterion": "C"},
        ])

    def test_empty_global_links_are_filled_from_success_criteria(self):
        criteria = ["A", "B"]
        raw = plan(success_criteria=criteria, criterion_links={"global": [], "local": []})
        self.assertEqual(validate_plan(raw)["criterion_links"]["global"], [
            {"id": "gc-1", "criterion": "A"},
            {"id": "gc-2", "criterion": "B"},
        ])

    def test_synthesized_global_link_is_available_to_local_references(self):
        raw = plan(success_criteria=["First", "Second"], criterion_links={
            "global": [{"id": "ac-first", "criterion": "First"}],
            "local": [{"id": "lc-1", "task_id": "task-1",
                       "criterion": "The task outcome is verified.",
                       "supports_global_criteria": ["gc-1"]}],
        })
        result = validate_plan(raw)
        self.assertEqual([item["id"] for item in result["criterion_links"]["global"]],
                         ["ac-first", "gc-1"])
        self.assertEqual(result["criterion_links"]["local"][0]["supports_global_criteria"], ["gc-1"])

    def test_local_reusing_global_id_gets_distinct_id_and_task_check(self):
        raw = plan(
            tasks=[task("TASK-1", "Crear el artefacto", success_criteria=["Resultado correcto"])],
            success_criteria=["Resultado correcto"],
            criterion_links={
                "global": [{"id": "AC-1", "criterion": "Resultado correcto"}],
                "local": [{"id": "AC-1", "task_id": "TASK-1", "criterion": "Resultado correcto",
                           "supports_global_criteria": ["AC-1"]}],
            },
        )
        result = validate_plan(raw)
        self.assertEqual(result["criterion_links"]["global"][0]["id"], "ac-1")
        self.assertEqual(result["criterion_links"]["local"][0]["id"], "lc-1")
        self.assertNotEqual(result["tasks"][0]["success_criteria"][0], "Resultado correcto")
        self.assertEqual(result["criterion_links"]["local"][0]["criterion"],
                         result["tasks"][0]["success_criteria"][0])

    def test_unrelated_local_substitution_keeps_strict_error(self):
        raw = plan(success_criteria=["Overall result is correct"], criterion_links={
            "global": [{"id": "ac-1", "criterion": "Overall result is correct"}],
            "local": [{"id": "lc-1", "task_id": "task-1", "criterion": "Unrelated claim",
                       "supports_global_criteria": ["ac-1"]}],
        })
        with self.assertRaisesRegex(PlanValidationError,
                                    "Local criterion links duplicate or substitute a task criterion"):
            validate_plan(raw)

    def test_global_link_rows_cannot_add_or_duplicate_plan_criteria(self):
        for links in (
            [{"criterion": "A"}, {"criterion": "B"}],
            [{"criterion": "A"}, {"criterion": "A"}],
        ):
            with self.subTest(links=links):
                raw = plan(success_criteria=["A"], criterion_links={"global": links, "local": []})
                with self.assertRaisesRegex(PlanValidationError, "match one unique plan criterion"):
                    validate_plan(raw)

    def test_legacy_plan_synthesizes_local_ids_and_exact_global_reference(self):
        criterion = "The task outcome is verified."
        result = validate_plan(plan(success_criteria=[criterion]))
        self.assertEqual(result["criterion_links"]["global"], [{"id": "gc-1", "criterion": criterion}])
        self.assertEqual(result["criterion_links"]["local"], [{
            "id": "tc-task-1-1", "task_id": "task-1", "criterion": criterion,
            "supports_global_criteria": ["gc-1"],
        }])

    def test_global_criterion_ids_are_assigned_for_null_and_blank_values(self):
        criteria = ["A", "B", "C"]
        raw = plan(success_criteria=criteria, criterion_links={
            "global": [
                {"id": "", "criterion": "A"},
                {"id": None, "criterion": "B"},
                {"id": "   ", "criterion": "C"},
            ],
            "local": [],
        })
        self.assertEqual([item["id"] for item in normalize_plan(raw)["criterion_links"]["global"]],
                         ["gc-1", "gc-2", "gc-3"])

    def test_duplicate_global_criterion_ids_are_resolved_deterministically(self):
        criteria = ["A", "B", "C"]
        raw = plan(success_criteria=criteria, criterion_links={
            "global": [
                {"id": "GC-1", "criterion": "A"},
                {"id": "gc-1", "criterion": "B"},
                {"id": "gc-2", "criterion": "C"},
            ],
            "local": [],
        })
        diagnostics = {}
        first = validate_plan(raw, diagnostics=diagnostics)
        second = validate_plan(raw)
        self.assertEqual([item["id"] for item in first["criterion_links"]["global"]],
                         ["gc-1", "gc-3", "gc-2"])
        self.assertEqual(first, second)
        self.assertEqual(diagnostics["stable_ids"]["duplicates_resolved"], 1)

    def test_duplicate_global_id_reference_uses_unambiguous_criterion_text(self):
        raw = plan(
            success_criteria=["First result", "Second result"],
            criterion_links={
                "global": [{"id": "AC-1", "criterion": "First result"},
                           {"id": "AC-1", "criterion": "Second result"}],
                "local": [{"id": "LC-1", "task_id": "task-1", "criterion": "Second result",
                           "supports_global_criteria": ["AC-1"]}],
            },
        )
        result = validate_plan(raw)
        self.assertEqual([item["id"] for item in result["criterion_links"]["global"]],
                         ["ac-1", "ac-2"])
        self.assertEqual(result["criterion_links"]["local"][0]["supports_global_criteria"],
                         ["ac-2"])

    def test_duplicate_global_id_reference_without_text_match_fails(self):
        raw = plan(
            success_criteria=["First result", "Second result"],
            criterion_links={
                "global": [{"id": "AC-1", "criterion": "First result"},
                           {"id": "AC-1", "criterion": "Second result"}],
                "local": [{"id": "LC-1", "task_id": "task-1",
                           "criterion": "The task outcome is verified.",
                           "supports_global_criteria": ["AC-1"]}],
            },
        )
        with self.assertRaisesRegex(PlanValidationError, "ambiguous global criterion ID"):
            validate_plan(raw)

    def test_normalized_global_id_and_legacy_local_reference_stay_linked(self):
        raw = plan(success_criteria=["A"], criterion_links={
            "global": [{"id": "Criteria_Foo", "criterion": "A"}],
            "local": [{"id": "lc-1", "task_id": "task-1",
                       "criterion": "The task outcome is verified.",
                       "supports_global_criteria": ["Criteria_Foo"]}],
        })
        result = normalize_plan(raw)
        global_ids = {item["id"] for item in result["criterion_links"]["global"]}
        self.assertEqual(global_ids, {"criteria-foo"})
        self.assertEqual(result["criterion_links"]["local"][0]["supports_global_criteria"],
                         ["criteria-foo"])
        self.assertTrue(all(set(item["supports_global_criteria"]) <= global_ids
                            for item in result["criterion_links"]["local"]))

    def test_multiple_local_links_keep_one_normalized_global_reference(self):
        raw = plan(tasks=[task(success_criteria=["First check", "Second check"])],
                   success_criteria=["A"], criterion_links={
            "global": [{"id": "GC_001", "criterion": "A"}],
            "local": [
                {"task_id": "task-1", "criterion": "First check",
                 "supports_global_criteria": ["GC_001"]},
                {"task_id": "task-1", "criterion": "Second check",
                 "supports_global_criteria": ["gc-001"]},
            ],
        })
        result = normalize_plan(raw)
        self.assertEqual([item["supports_global_criteria"]
                          for item in result["criterion_links"]["local"]],
                         [["gc-001"], ["gc-001"]])

    def test_multiple_global_ids_are_normalized_without_merging_criteria(self):
        criteria = ["First obligation", "Second obligation"]
        raw = plan(success_criteria=criteria, criterion_links={
            "global": [{"id": "GC_010", "criterion": criteria[0]},
                       {"id": "GC_020", "criterion": criteria[1]}],
            "local": [{"task_id": "task-1", "criterion": "The task outcome is verified.",
                       "supports_global_criteria": ["GC_010", "GC_020"]}],
        })
        result = normalize_plan(raw)
        global_rows = result["criterion_links"]["global"]
        self.assertEqual([item["criterion"] for item in global_rows], criteria)
        self.assertEqual([item["id"] for item in global_rows], ["gc-010", "gc-020"])
        self.assertEqual(result["criterion_links"]["local"][0]["supports_global_criteria"],
                         ["gc-010", "gc-020"])

    def test_unknown_local_global_reference_keeps_strict_validator_error(self):
        raw = plan(criterion_links={
            "global": [{"id": "gc-known", "criterion": "The requested outcome is complete."}],
            "local": [{"task_id": "task-1", "criterion": "The task outcome is verified.",
                       "supports_global_criteria": ["gc-unknown"]}],
        })
        with self.assertRaisesRegex(
                PlanValidationError,
                "Local criterion links reference an unknown global criterion ID"):
            validate_plan(raw)

    def test_normalized_local_references_all_belong_to_global_ids(self):
        raw = plan(success_criteria=["A", "B"], criterion_links={
            "global": [{"id": "GC_1", "criterion": "A"},
                       {"id": "GC_2", "criterion": "B"}],
            "local": [{"task_id": "task-1", "criterion": "The task outcome is verified.",
                       "supports_global_criteria": ["GC_1", "GC_2"]}],
        })
        result = normalize_plan(raw)
        global_ids = {item["id"] for item in result["criterion_links"]["global"]}
        self.assertTrue(all(ref in global_ids
                            for item in result["criterion_links"]["local"]
                            for ref in item["supports_global_criteria"]))

    def test_existing_global_id_is_preserved_and_reserved_before_assignment(self):
        criteria = ["A", "B"]
        raw = plan(success_criteria=criteria, criterion_links={
            "global": [
                {"criterion": "A"},
                {"id": "gc-1", "criterion": "B"},
            ],
            "local": [],
        })
        result = validate_plan(raw)
        self.assertEqual([item["id"] for item in result["criterion_links"]["global"]], ["gc-2", "gc-1"])

    def test_local_criterion_id_is_assigned_and_references_generated_global_id(self):
        global_criterion = "The requested outcome is complete."
        local_criterion = "The task outcome is verified."
        raw = plan(criterion_links={
            "global": [{"criterion": global_criterion}],
            "local": [{
                "id": None, "task_id": "task-1", "criterion": local_criterion,
                "supports_global_criteria": ["gc-1"],
            }],
        })
        result = validate_plan(raw)
        self.assertEqual(result["criterion_links"]["local"][0]["id"], "tc-task-1-1")
        self.assertEqual(result["criterion_links"]["local"][0]["supports_global_criteria"], ["gc-1"])

    def test_duplicate_local_criterion_id_is_replaced_without_losing_coverage(self):
        local_criteria = ["First task check.", "Second task check."]
        raw = plan(tasks=[task(success_criteria=local_criteria)], criterion_links={
            "global": [{"id": "gc-overall", "criterion": "The requested outcome is complete."}],
            "local": [
                {"id": "tc-shared", "task_id": "task-1", "criterion": local_criteria[0],
                 "supports_global_criteria": []},
                {"id": "tc-shared", "task_id": "task-1", "criterion": local_criteria[1],
                 "supports_global_criteria": []},
            ],
        })
        result = validate_plan(raw)
        local_links = result["criterion_links"]["local"]
        self.assertEqual([item["id"] for item in local_links], ["tc-shared", "tc-task-1-2"])
        self.assertEqual([item["criterion"] for item in local_links], local_criteria)

    def test_non_string_criterion_id_remains_a_contract_error(self):
        raw = plan(criterion_links={
            "global": [{"id": 7, "criterion": "The requested outcome is complete."}],
            "local": [],
        })
        with self.assertRaisesRegex(PlanValidationError, "must be a string or null"):
            validate_plan(raw)

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
    def test_hello_world_has_distinct_requirement_global_task_and_local_ids(self):
        analysis = deterministic_task_analysis("Crea un hola mundo")
        generated = plan(
            goal=analysis["operational_prompt"],
            tasks=[task("TASK-1", "Crear el artefacto solicitado",
                        success_criteria=["El artefacto fue creado.",
                                          "El artefacto representa hola mundo."])],
            success_criteria=["El resultado cumple el objetivo original del usuario."],
            criterion_links={
                "global": [{"id": "AC-1", "criterion":
                            "El resultado cumple el objetivo original del usuario."}],
                "local": [
                    {"id": "LC-1", "task_id": "TASK-1", "criterion": "El artefacto fue creado.",
                     "supports_global_criteria": ["AC-1"]},
                    {"id": "LC-2", "task_id": "TASK-1",
                     "criterion": "El artefacto representa hola mundo.",
                     "supports_global_criteria": ["AC-1"]},
                ],
            },
        )
        result = Planner(lambda prompt, context: generated).create_plan(
            analysis["operational_prompt"], {"task_analysis": analysis})
        requirement_ids = {item["id"] for item in analysis["requirements"]}
        acceptance_ids = {item["id"] for item in analysis["acceptance_criteria"]}
        global_ids = {item["id"] for item in result["criterion_links"]["global"]}
        local_ids = {item["id"] for item in result["criterion_links"]["local"]}
        self.assertTrue(all(identifier.startswith("REQ-") for identifier in requirement_ids))
        self.assertTrue(all(identifier.startswith("AC-") for identifier in acceptance_ids))
        self.assertTrue(all(task["id"].startswith("task-") for task in result["tasks"]))
        self.assertTrue(all(identifier.startswith("ac-") for identifier in global_ids))
        self.assertTrue(all(identifier.startswith("lc-") for identifier in local_ids))
        self.assertFalse(global_ids & local_ids)
        self.assertTrue(all(set(item["verifies"]) <= requirement_ids
                            for item in analysis["acceptance_criteria"]))
        self.assertTrue(all(set(item["supports_global_criteria"]) <= global_ids
                            for item in result["criterion_links"]["local"]))

    def test_real_ollama_analyst_id_placeholder_is_reconciled_by_exact_text(self):
        analysis = deterministic_task_analysis("Crea un hola mundo")
        criteria = ["El archivo 'hola_mundo.txt' existe en el directorio actual.",
                    "El contenido del archivo 'hola_mundo.txt' es 'hola mundo'."]
        generated = plan(
            goal=analysis["operational_prompt"],
            tasks=[task("T-1", "Crear hola_mundo.txt", success_criteria=criteria)],
            success_criteria=criteria,
            criterion_links={
                "global": [{"criterion": "AC-1", "id": "CL-1"}],
                "local": [{"task_id": "T-1", "criterion": criterion,
                           "supports_global_criteria": ["CL-1"]} for criterion in criteria],
            },
        )
        calls = []
        planner = Planner(lambda prompt, context: calls.append(prompt) or generated)
        result = planner.create_plan(analysis["operational_prompt"], {"task_analysis": analysis})
        self.assertEqual(len(calls), 1)
        self.assertEqual([item["criterion"] for item in result["criterion_links"]["global"]], criteria)
        self.assertEqual([item["id"] for item in result["criterion_links"]["global"]],
                         ["gc-1", "gc-2"])
        self.assertEqual([item["supports_global_criteria"]
                          for item in result["criterion_links"]["local"]],
                         [["gc-1"], ["gc-2"]])
        self.assertEqual(result["tasks"][0]["id"], "task-1")
        self.assertTrue(all(item["task_id"] == "task-1"
                            for item in result["criterion_links"]["local"]))
        self.assertEqual(planner.metrics["normalization"]["analyst_acceptance_placeholders_removed"], 1)

    def test_model_task_shorthand_updates_dependencies_and_local_links(self):
        generated = plan(
            goal="Review inventory", summary="Review inventory in two stages",
            tasks=[task("T-1", "Inventory items", success_criteria=["Items counted."]),
                   task("T-2", "Check counts", depends_on=["T-1"],
                        success_criteria=["Counts reconciled."])],
            success_criteria=["Items counted.", "Counts reconciled."],
            criterion_links={
                "global": [{"id": "gc-1", "criterion": "Items counted."},
                           {"id": "gc-2", "criterion": "Counts reconciled."}],
                "local": [{"task_id": "T-1", "criterion": "Items counted.",
                           "supports_global_criteria": ["gc-1"]},
                          {"task_id": "T-2", "criterion": "Counts reconciled.",
                           "supports_global_criteria": ["gc-2"]}],
            },
        )
        planner = Planner(lambda prompt, context: generated)
        result = planner.create_plan("Review inventory")
        self.assertEqual([item["id"] for item in result["tasks"]], ["task-1", "task-2"])
        self.assertEqual(result["tasks"][1]["depends_on"], ["task-1"])
        self.assertEqual([item["task_id"] for item in result["criterion_links"]["local"]],
                         ["task-1", "task-2"])
        self.assertEqual(planner.metrics["normalization"]["task_ids_expanded"], 2)

    def test_real_ollama_analyst_description_placeholder_keeps_two_global_obligations(self):
        analysis = deterministic_task_analysis("Crea un hola mundo")
        criteria = ["El archivo 'hola_mundo.txt' existe en el directorio actual.",
                    "El contenido del archivo 'hola_mundo.txt' es 'hola mundo'."]
        generated = plan(
            goal=analysis["operational_prompt"],
            tasks=[task("T-1", "Crear hola_mundo.txt", success_criteria=criteria)],
            success_criteria=criteria,
            criterion_links={
                "global": [{"criterion": analysis["acceptance_criteria"][0]["description"],
                            "id": "AC-1"}],
                "local": [{"task_id": "T-1", "criterion": criterion,
                           "supports_global_criteria": ["AC-1"]} for criterion in criteria],
            },
        )
        calls = []
        planner = Planner(lambda prompt, context: calls.append(prompt) or generated)
        result = planner.create_plan(analysis["operational_prompt"], {"task_analysis": analysis})
        self.assertEqual(len(calls), 1)
        self.assertEqual([item["criterion"] for item in result["criterion_links"]["global"]], criteria)
        self.assertEqual([item["supports_global_criteria"]
                          for item in result["criterion_links"]["local"]],
                         [["gc-1"], ["gc-2"]])
        self.assertEqual(planner.metrics["normalization"]["analyst_acceptance_placeholders_removed"], 1)

    def test_analyst_id_placeholder_without_exact_local_match_still_fails(self):
        analysis = deterministic_task_analysis("Crea un hola mundo")
        generated = plan(
            tasks=[task(success_criteria=["The task result is checked."])],
            success_criteria=["The requested result exists."],
            criterion_links={
                "global": [{"criterion": "AC-1", "id": "CL-1"}],
                "local": [{"task_id": "task-1", "criterion": "The task result is checked.",
                           "supports_global_criteria": ["CL-1"]}],
            },
        )
        with self.assertRaisesRegex(PlanGenerationError, "unknown global criterion ID"):
            Planner(lambda prompt, context: generated).create_plan(
                analysis["operational_prompt"], {"task_analysis": analysis})

    def test_unknown_global_reference_is_resolved_only_from_exact_wording(self):
        generated = plan(
            tasks=[task(success_criteria=["The artifact is present."])],
            success_criteria=["The requested artifact exists."],
            criterion_links={
                "global": [{"id": "AC-1", "criterion": "The requested artifact exists."}],
                "local": [{"id": "LC-1", "task_id": "task-1",
                           "criterion": "The requested artifact exists.",
                           "supports_global_criteria": ["AC-999"]}],
            },
        )
        result = Planner(lambda prompt, context: generated).create_plan("Create the artifact")
        self.assertEqual(result["criterion_links"]["local"][0]["criterion"],
                         "The artifact is present.")
        self.assertEqual(result["criterion_links"]["local"][0]["supports_global_criteria"], ["ac-1"])

    def test_model_copied_global_text_with_distinct_id_becomes_task_check(self):
        generated = plan(
            tasks=[task("TASK-1", "Crear hola mundo", success_criteria=["Resultado correcto."])],
            success_criteria=["Resultado correcto."],
            criterion_links={
                "global": [{"id": "AC-1", "criterion": "Resultado correcto."}],
                "local": [{"id": "LC-1", "task_id": "TASK-1",
                           "criterion": "Resultado correcto.",
                           "supports_global_criteria": ["AC-1"]}],
            },
        )
        result = Planner(lambda prompt, context: generated).create_plan("Crea un hola mundo")
        local = result["criterion_links"]["local"][0]
        self.assertEqual(local["id"], "lc-1")
        self.assertIn("Crear hola mundo", local["criterion"])
        self.assertNotEqual(local["criterion"], result["success_criteria"][0])

    def test_ambiguous_global_copy_cannot_pick_one_of_multiple_task_checks(self):
        generated = plan(
            tasks=[task(success_criteria=["The file exists.", "The content is correct."])],
            success_criteria=["The requested artifact is correct."],
            criterion_links={
                "global": [{"id": "AC-1", "criterion": "The requested artifact is correct."}],
                "local": [
                    {"id": "LC-2", "task_id": "task-1", "criterion": "The file exists.",
                     "supports_global_criteria": []},
                    {"id": "LC-1", "task_id": "task-1",
                     "criterion": "The requested artifact is correct.",
                     "supports_global_criteria": ["AC-1"]},
                ],
            },
        )
        with self.assertRaisesRegex(PlanGenerationError,
                                    "Local criterion links duplicate or substitute a task criterion"):
            Planner(lambda prompt, context: generated).create_plan("Create the artifact")

    def test_unknown_global_reference_without_wording_match_fails(self):
        generated = plan(success_criteria=["The result is correct."], criterion_links={
            "global": [{"id": "AC-1", "criterion": "The result is correct."}],
            "local": [{"id": "LC-1", "task_id": "task-1",
                       "criterion": "The task outcome is verified.",
                       "supports_global_criteria": ["AC-999"]}],
        })
        calls = []
        planner = Planner(lambda prompt, context: calls.append(prompt) or generated)
        with self.assertRaisesRegex(PlanGenerationError, "unknown global criterion ID"):
            planner.create_plan("Create the artifact")
        self.assertEqual(len(calls), 2)

    def test_duplicate_local_ids_remain_distinct_and_can_share_global_support(self):
        generated = plan(
            tasks=[task(success_criteria=["The file exists.", "The contents are correct."])],
            success_criteria=["The requested artifact is correct."],
            criterion_links={
                "global": [{"id": "AC-1", "criterion": "The requested artifact is correct."}],
                "local": [
                    {"id": "LC-1", "task_id": "task-1", "criterion": "The file exists.",
                     "supports_global_criteria": ["AC-1"]},
                    {"id": "LC-1", "task_id": "task-1", "criterion": "The contents are correct.",
                     "supports_global_criteria": ["AC-1"]},
                ],
            },
        )
        result = Planner(lambda prompt, context: generated).create_plan("Create the artifact")
        self.assertEqual([item["id"] for item in result["criterion_links"]["local"]],
                         ["lc-1", "lc-2"])

    def test_two_tasks_may_support_the_same_global_criterion(self):
        generated = plan(
            tasks=[task("TASK-1", "Produce component A", success_criteria=["The file exists."]),
                   task("TASK-2", "Verify component B", depends_on=["TASK-1"],
                        success_criteria=["The content is correct."])],
            complexity="multi_step",
            success_criteria=["The requested artifact is correct."],
            criterion_links={
                "global": [{"id": "AC-1", "criterion": "The requested artifact is correct."}],
                "local": [
                    {"id": "LC-1", "task_id": "TASK-1", "criterion": "The file exists.",
                     "supports_global_criteria": ["AC-1"]},
                    {"id": "LC-2", "task_id": "TASK-2", "criterion": "The content is correct.",
                     "supports_global_criteria": ["AC-1"]},
                ],
            },
        )
        result = Planner(lambda prompt, context: generated).create_plan("Create and check the artifact")
        self.assertEqual([item["task_id"] for item in result["criterion_links"]["local"]],
                         ["task-1", "task-2"])
        self.assertEqual([item["supports_global_criteria"] for item in result["criterion_links"]["local"]],
                         [["ac-1"], ["ac-1"]])

    def test_uncovered_global_criterion_fails_before_execution(self):
        generated = plan(criterion_links={
            "global": [{"id": "AC-1", "criterion": "The requested result is complete."}],
            "local": [{"id": "LC-1", "task_id": "task-1",
                       "criterion": "The task outcome is verified.",
                       "supports_global_criteria": []}],
        }, success_criteria=["The requested result is complete."])
        with self.assertRaisesRegex(PlanGenerationError, "no explicit local task coverage"):
            Planner(lambda prompt, context: generated).create_plan("Complete the request")

    def test_invalid_analyst_requirement_reference_stops_before_model_call(self):
        analysis = deterministic_task_analysis("Crea un hola mundo")
        analysis["acceptance_criteria"][0]["verifies"] = ["REQ-999"]
        calls = []
        planner = Planner(lambda prompt, context: calls.append(prompt) or plan())
        with self.assertRaisesRegex(PlanGenerationError, "Task Analyst references are invalid"):
            planner.create_plan("Crea un hola mundo", {"task_analysis": analysis})
        self.assertEqual(calls, [])

    def test_planner_cannot_reuse_analyst_ac_id_for_different_obligation(self):
        analysis = deterministic_task_analysis("Crea un hola mundo")
        generated = plan(
            success_criteria=["An unrelated result is accepted."],
            criterion_links={
                "global": [{"id": "AC-1", "criterion": "An unrelated result is accepted."}],
                "local": [{"id": "LC-1", "task_id": "task-1",
                           "criterion": "The task outcome is verified.",
                           "supports_global_criteria": ["AC-1"]}],
            },
        )
        with self.assertRaisesRegex(PlanGenerationError,
                                    "does not match a Task Analyst acceptance criterion"):
            Planner(lambda prompt, context: generated).create_plan(
                analysis["operational_prompt"], {"task_analysis": analysis})

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
        self.assertIn("Repair only the field or criterion-link structure", calls[1])

    def test_missing_criterion_ids_do_not_trigger_model_repair(self):
        criteria = ["The hello world output is correct."]
        generated = plan(
            goal="Crea un hola mundo",
            success_criteria=criteria,
            criterion_links={
                "global": [{"criterion": criteria[0]}],
                "local": [{"id": "tc-task-1-1", "task_id": "task-1",
                           "criterion": "The task outcome is verified.",
                           "supports_global_criteria": ["gc-1"]}],
            },
        )
        calls = []
        planner = Planner(lambda prompt, context: calls.append(prompt) or generated)
        result = planner.create_plan("Crea un hola mundo")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["criterion_links"]["global"][0]["id"], "gc-1")
        self.assertEqual(planner.metrics["normalization"]["stable_ids"]["assigned"], 1)

    def test_missing_global_link_rows_do_not_trigger_model_repair(self):
        criteria = ["The file exists.", "The content is correct."]
        generated = plan(
            goal="Crea un hola mundo",
            success_criteria=criteria,
            tasks=[task(success_criteria=["The file is present.", "The file content matches."])],
            criterion_links={
                "global": [],
                "local": [
                    {"id": "tc-task-1-1", "task_id": "task-1", "criterion": "The file is present.",
                     "supports_global_criteria": ["gc-1"]},
                    {"id": "tc-task-1-2", "task_id": "task-1",
                     "criterion": "The file content matches.",
                     "supports_global_criteria": ["gc-2"]},
                ],
            },
        )
        calls = []
        planner = Planner(lambda prompt, context: calls.append(prompt) or generated)
        result = planner.create_plan("Crea un hola mundo")
        self.assertEqual(len(calls), 1)
        self.assertEqual([item["id"] for item in result["criterion_links"]["global"]],
                         ["gc-1", "gc-2"])
        self.assertEqual(planner.metrics["normalization"]["stable_ids"]["assigned"], 2)

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

    def test_hello_world_plan_emits_stable_id_normalization_and_reaches_execution(self):
        criterion = "The Hello World result is verified."
        generated = plan(
            goal="Crea un hola mundo",
            success_criteria=[criterion],
            criterion_links={
                "global": [{"criterion": criterion}],
                "local": [{"id": "tc-task-1-1", "task_id": "task-1",
                           "criterion": "The task outcome is verified.",
                           "supports_global_criteria": ["gc-1"]}],
            },
        )
        planner_calls = []
        planner = Planner(lambda prompt, context: planner_calls.append(prompt) or generated)
        run = self.store.create_orchestration("Crea un hola mundo")
        Orchestrator(
            self.store, None,
            decide=lambda prompt, agents, results: {"action": "respond", "message": "Planning passed."},
            planner=planner,
        )._run(run["id"])

        stored = self.store.get_orchestration(run["id"])
        event_types = [event["event_type"] for event in stored["events"]]
        normalized = next(event for event in stored["events"]
                          if event["event_type"] == "freya.planner.normalized")
        payload = json.loads(normalized["payload_json"])
        self.assertEqual(stored["status"], "Success")
        self.assertEqual(len(planner_calls), 1)
        self.assertEqual(stored["plan"]["criterion_links"]["global"][0]["id"], "gc-1")
        self.assertIn("freya.plan.created", event_types)
        self.assertEqual(payload["stable_ids"]["assigned"], 1)
        self.assertEqual(payload["stable_ids"]["structures"], {
            "criterion_links.global": {"assigned": 1, "duplicates_resolved": 0},
        })

    def test_failed_planning_emits_failure_without_plan(self):
        run = self.store.create_orchestration("Create hello.txt")
        broken = Planner(lambda prompt, context: "not json")
        Orchestrator(self.store, None, planner=broken)._run(run["id"])
        stored = self.store.get_orchestration(run["id"])
        self.assertEqual(stored["status"], "Failed")
        self.assertIsNone(stored["plan"])
        self.assertEqual(stored["planning_metrics"]["model_calls"], 2)
        self.assertIn("freya.planning.failed", [event["event_type"] for event in stored["events"]])

    def test_invalid_criterion_reference_never_initializes_graph_or_programmer(self):
        invalid = plan(success_criteria=["The requested result is complete."], criterion_links={
            "global": [{"id": "AC-1", "criterion": "The requested result is complete."}],
            "local": [{"id": "LC-1", "task_id": "task-1",
                       "criterion": "The task outcome is verified.",
                       "supports_global_criteria": ["AC-999"]}],
        })
        run = self.store.create_orchestration("Create hello.txt")
        Orchestrator(self.store, None, planner=Planner(lambda prompt, context: invalid))._run(run["id"])
        stored = self.store.get_orchestration(run["id"])
        self.assertEqual(stored["status"], "Failed")
        self.assertIsNone(stored["plan"])
        self.assertEqual(self.store.get_execution_graph(run["id"])["nodes"], [])
        self.assertNotIn("freya.graph.initialized",
                         [event["event_type"] for event in stored["events"]])

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
