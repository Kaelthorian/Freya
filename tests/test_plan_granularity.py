"""Logical Tasks absorb mechanical prerequisites without crossing real boundaries."""
from __future__ import annotations

import copy
import unittest

from control_center.plan_compiler import compile_semantic_plan, OverfragmentedPlan
from control_center.plan_evidence import verification_mode
from control_center.plan_granularity import normalize_task_granularity
from control_center.planner import Planner, semantic_plan_response_format, validate_plan
from control_center.runtime_resources import RuntimeResourceCatalog
from control_center.task_spec import deterministic_task_spec


CONVERTER_REQUEST = (
    "Creá un archivo temperature_converter.py que convierta grados Celsius a Fahrenheit. "
    "El programa debe pedir una temperatura por consola, mostrar el resultado con "
    "2 decimales y manejar correctamente entradas inválidas. "
    "Después ejecutalo al menos con 0, 100 y una entrada inválida para verificar que funciona.")


def task(key, operation, path="app.py", *, depends=(), kind=None, criteria=None, objective=None, **metadata):
    operations = operation if isinstance(operation, list) else [operation]
    writer = bool(set(operations) & {"create_file", "modify_file", "overwrite_file"})
    result = {"key": key, "task_kind": kind or ("program_creation" if writer else "general"),
              "objective": objective or key, "description": objective or key,
              "depends_on": list(depends), "semantic_needs": [objective or key],
              "operations": operations,
              "owned_paths": [path] if "create_file" in operations else [],
              "write_targets": [path] if writer else [],
              "success_criteria": criteria or [f"{path} exists."]}
    result.update(metadata)
    return result


def plan(tasks, *, complexity="simple", strategy="single_worker", **metadata):
    return {"summary": "Implement the requested application", "tasks": tasks,
            "success_criteria": [], "unsupported_requirements": [],
            "task_complexity": complexity, "execution_strategy": strategy,
            "decomposition_reason": "One Worker can implement the logical outcome and collect verification evidence.",
            **metadata}


def converter_plan():
    path = "temperature_converter.py"
    return plan([
        task("create", "create_file", path, objective="Create temperature_converter.py"),
        task("implement", "modify_file", path, depends=["create"],
             objective="Implement temperature_converter.py", criteria=[
                 "The source defines Celsius conversion, console input, two-decimal formatting and ValueError handling."]),
        task("verify", "run_python_script", path, depends=["implement"], kind="testing",
             objective="Verify temperature_converter.py", criteria=[
                 "The script command records stdout and exit code for all requested inputs."],
             verification_mode="independent_cases", verification_cases=[
                 {"id": "case_0", "input": "0\n"}, {"id": "case_100", "input": "100\n"},
                 {"id": "case_invalid", "input": "invalid\n"}]),
    ])


class PlanGranularityTests(unittest.TestCase):
    def setUp(self):
        self.spec = deterministic_task_spec("Create app.py in Python with complete implementation.")
        self.catalog = RuntimeResourceCatalog.build()

    def compile(self, semantic, spec=None):
        return compile_semantic_plan(semantic, spec or self.spec, resource_catalog=self.catalog)

    def events(self, name):
        return [event for event in self.catalog.compiler_events if event["event_type"] == name]

    def test_create_then_write_same_file_is_one_implementation_task(self):
        semantic = plan([
            task("create", "create_file", objective="Create app.py"),
            task("implement", "modify_file", depends=["create"], objective="Implement app.py",
                 criteria=["The source contains the requested application logic."]),
        ])
        original = copy.deepcopy(semantic)
        compiled = self.compile(semantic)
        self.assertEqual(semantic, original, "Do not mutate the Planner audit proposal.")
        self.assertEqual(compiled["task_count"], 1)
        merged = compiled["tasks"][0]
        self.assertEqual(merged["id"], "task-1")
        self.assertEqual(merged["objective"], "Implement app.py")
        self.assertEqual(merged["semantic_operations"], ["create_file", "modify_file"])
        self.assertEqual(merged["required_capabilities"], ["filesystem.create", "filesystem.modify", "filesystem.read"])
        self.assertEqual(merged["required_tools"], ["write_file", "edit_file", "read_file"])
        self.assertEqual(len(merged["semantic_needs"]), 2)
        self.assertEqual(merged["depends_on"], [])
        self.assertEqual(compiled["write_owners"], {"app.py": "task-1"})
        self.assertEqual(merged["foreign_write_targets"], [])
        self.assertEqual(self.events("plan_compiler.tasks_merged")[0]["source_task_ids"], ["task-1", "task-2"])

    def test_empty_scaffold_is_absorbed_into_its_populator(self):
        compiled = self.compile(plan([
            task("scaffold", "create_file", objective="Create empty scaffold app.py",
                 criteria=["The empty scaffold file exists."]),
            task("populate", "modify_file", depends=["scaffold"], objective="Populate app.py",
                 criteria=["The source contains conversion logic."]),
        ]))
        self.assertEqual(len(compiled["tasks"]), 1)
        self.assertEqual(compiled["tasks"][0]["success_criteria"], ["The source contains conversion logic."])

    def test_directory_and_multiple_files_of_one_component_are_one_task(self):
        directory = task("directory", [], "component", kind="general", objective="Create directory component",
                         criteria=["The directory component exists."])
        directory["owned_paths"] = ["component"]
        first = task("first", "create_file", "component/a.py", depends=["directory"])
        second = task("second", "create_file", "component/b.py", depends=["first"])
        implement = task("implement", "modify_file", "component/a.py", depends=["second"],
                         objective="Implement the component", criteria=["The source contains the component implementation."])
        implement["write_targets"] = ["component/a.py", "component/b.py"]
        compiled = self.compile(plan([directory, first, second, implement]))
        self.assertEqual(compiled["task_count"], 1)
        self.assertEqual(compiled["tasks"][0]["objective"], "Implement the component")
        self.assertEqual(compiled["tasks"][0]["owned_paths"], ["component/a.py", "component/b.py"])
        self.assertNotIn("component", compiled["write_owners"])
        self.assertEqual(compiled["tasks"][0]["semantic_operations"], ["create_file", "modify_file"])
        self.assertEqual(len(self.events("plan_compiler.tasks_merged")), 3)

    def test_read_modify_save_prerequisite_uses_one_task(self):
        read = task("read", "read_file", "config.json", objective="Read config.json",
                    criteria=["config.json is readable."])
        update = task("update", "modify_file", "config.json", depends=["read"],
                      objective="Update configuration", criteria=["The config content contains the requested setting."])
        compiled = self.compile(plan([read, update]))
        self.assertEqual(len(compiled["tasks"]), 1)
        self.assertEqual(compiled["tasks"][0]["semantic_operations"], ["read_file", "modify_file"])

    def test_reading_a_backup_does_not_match_the_target_by_substring(self):
        compiled = self.compile(plan([
            task("read", "read_file", "config.json.bak", objective="Read config.json.bak",
                 criteria=["config.json.bak is readable."]),
            task("update", "modify_file", "config.json", depends=["read"],
                 criteria=["The config content contains the requested setting."])]))
        self.assertEqual(len(compiled["tasks"]), 2)

    def test_pure_create_presence_merge_does_not_depend_on_objective_similarity(self):
        compiled = self.compile(plan([
            task("implement", "create_file", objective="Implement app.py"),
            task("extend", "modify_file", depends=["implement"],
                 criteria=["The source contains an additional method."])]))
        self.assertEqual(len(compiled["tasks"]), 1)

    def test_real_planner_presence_variants_merge(self):
        for criterion in ("temperature_converter.py exists.", "`temperature_converter.py` exists.",
                          "The file temperature_converter.py is created in the workspace.",
                          "The file `temperature_converter.py` is created in the workspace.",
                          "El archivo temperature_converter.py existe.",
                          "El archivo `temperature_converter.py` fue creado."):
            with self.subTest(criterion=criterion):
                proposal = converter_plan()
                proposal["tasks"][0]["success_criteria"] = [criterion]
                compiled = self.compile(proposal, deterministic_task_spec(CONVERTER_REQUEST))
                self.assertEqual(compiled["task_count"], 2)
                self.assertEqual(compiled["tasks"][0]["semantic_operations"], ["create_file", "modify_file"])
                self.assertEqual(len(self.events("plan_compiler.tasks_merged")), 1)

    def test_semantic_presence_with_backticks_is_not_a_mechanical_microtask(self):
        criterion = "`app.py` exists and already implements the authentication layer correctly."
        self.assertEqual(verification_mode(criterion), "semantic")
        proposal = plan([
            task("create", "create_file", criteria=[criterion]),
            task("extend", "modify_file", depends=["create"], criteria=["The source contains an additional method."])])
        tasks, _, summary = normalize_task_granularity(proposal, proposal["tasks"], ["create", "extend"], self.spec)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(summary["merges"], [])

    def test_implementation_and_verification_remain_separate(self):
        semantic = converter_plan()
        compiled = self.compile(semantic, deterministic_task_spec(CONVERTER_REQUEST))
        self.assertEqual((compiled["task_count"], compiled["worker_count"]), (2, 1))
        implementation, verification = compiled["tasks"]
        self.assertEqual(implementation["objective"], "Implement temperature_converter.py")
        self.assertEqual(verification["depends_on"], ["task-1"])
        self.assertEqual(verification["verification_cases"], semantic["tasks"][2]["verification_cases"])
        self.assertEqual(verification["required_capabilities"], ["execution.python_script"])
        self.assertEqual(implementation["owned_paths"], ["temperature_converter.py"])
        self.assertEqual(compiled["worker_assignments"], [{"worker_id": "worker-1", "task_ids": ["task-1", "task-2"]}])
        self.assertTrue(all("exists" not in check for check in implementation["success_criteria"]))
        self.assertEqual(validate_plan(compiled), compiled)

    def test_current_converter_prompt_is_normalized_without_another_model_call(self):
        calls = []
        def decide(prompt, context):
            calls.append(prompt)
            return converter_plan()
        planner = Planner(decide)
        compiled = planner.create_plan_for_spec(deterministic_task_spec(CONVERTER_REQUEST))
        self.assertEqual(len(calls), 1)
        self.assertIn("TASK is a meaningful unit", calls[0])
        self.assertIn("no successor ever ran", calls[0])
        self.assertIn("1-2 Tasks", calls[0])
        self.assertEqual(len(compiled["tasks"]), 2)
        summary = planner.metrics["granularity_summary"]
        self.assertEqual((summary["task_count_before"], summary["task_count_after"]), (3, 2))
        self.assertIn("planner.granularity_summary", [event["event_type"] for event in planner.metrics["planner_events"]])

    def test_shared_intermediate_artifact_with_multiple_consumers_is_preserved(self):
        first = task("create", "create_file")
        modify = task("modify", "modify_file", depends=["create"], criteria=["The source contains new logic."])
        inspect = task("inspect", "read_file", depends=["create"], kind="review", criteria=["The source content is reviewed."])
        compiled = self.compile(plan([first, modify, inspect],
            granularity_reason="The shared producer has multiple consumers for modification and independent review."))
        self.assertEqual(len(compiled["tasks"]), 3)
        self.assertEqual(self.events("plan_compiler.tasks_merged"), [])
        preserved = self.events("plan_compiler.granularity_analyzed")[0]["preserved_boundaries"]
        self.assertIn("multiple_consumers", [item["reason"] for item in preserved])

    def test_parallel_work_is_not_fused(self):
        compiled = self.compile(plan([task("one", "create_file", "one.py"),
                                      task("two", "create_file", "two.py")]))
        self.assertEqual(len(compiled["tasks"]), 2)
        self.assertEqual(self.events("plan_compiler.tasks_merged"), [])

    def test_additional_dependency_preserves_a_mechanical_boundary(self):
        compiled = self.compile(plan([
            task("shared", "create_file", "shared.py"),
            task("create", "create_file"),
            task("implement", "modify_file", depends=["shared", "create"],
                 criteria=["The source contains the requested application logic."])],
            granularity_reason="A shared module is an independent dependency required by the final implementation."))
        self.assertEqual(len(compiled["tasks"]), 3)
        self.assertEqual(self.events("plan_compiler.tasks_merged"), [])

    def test_runtime_identity_and_policy_or_recovery_metadata_prevent_merging(self):
        for field, value in (("id", "runtime-existing"), ("worker_id", "worker-existing"),
                             ("capability_policy", {}), ("recovery_links", ["task-existing"]),
                             ("task_characteristics", {"requires_user_input": True})):
            with self.subTest(field=field):
                semantic = plan([task("create", "create_file", **{field: value}),
                                 task("modify", "modify_file", depends=["create"])])
                tasks, _, summary = normalize_task_granularity(
                    semantic, semantic["tasks"], ["create", "modify"], self.spec)
                self.assertEqual(len(tasks), 2)
                self.assertEqual(tasks[0][field], value)
                self.assertEqual(summary["merges"], [])

    def test_merge_after_independent_prefix_rebuilds_all_downstream_references(self):
        prefix = task("prefix", "create_file", "prefix.py",
                      criteria=["The source defines an independently useful library API."])
        semantic = converter_plan()
        semantic["tasks"][0]["depends_on"] = ["prefix"]
        semantic["tasks"].insert(0, prefix)
        semantic["granularity_reason"] = "The independent prefix API and controlled QA form separately recoverable outcomes."
        compiled = self.compile(semantic, deterministic_task_spec(CONVERTER_REQUEST))
        self.assertEqual([item["depends_on"] for item in compiled["tasks"]],
                         [[], ["task-1"], ["task-2"]])
        self.assertEqual(compiled["write_owners"],
                         {"prefix.py": "task-1", "temperature_converter.py": "task-2"})
        self.assertEqual(compiled["worker_assignments"][0]["task_ids"],
                         ["task-1", "task-2", "task-3"])
        self.assertEqual(self.events("plan_compiler.tasks_merged")[0]["result_task_id"], "task-2")
        self.assertEqual(validate_plan(compiled), compiled)

    def test_different_workers_are_not_fused_by_normalizer(self):
        semantic = converter_plan()
        semantic["execution_strategy"] = "multi_worker"
        tasks, keys, summary = normalize_task_granularity(
            semantic, semantic["tasks"], [item["key"] for item in semantic["tasks"]], self.spec)
        self.assertEqual(len(tasks), 3)
        self.assertEqual(summary["merges"], [])
        self.assertEqual(keys, ["create", "implement", "verify"])

    def test_explicit_user_phase_or_intermediate_requirement_preserves_tasks(self):
        for request in ("Create app.py in Python in separate phases: create the file, then populate it.",
                        "Create an empty app.py in Python, retain this intermediate artifact, then populate it."):
            with self.subTest(request=request):
                compiled = self.compile(plan([
                    task("create", "create_file"), task("modify", "modify_file", depends=["create"])]),
                    deterministic_task_spec(request))
                self.assertEqual(len(compiled["tasks"]), 2)

    def test_negative_scaffold_constraint_does_not_request_an_intermediate_artifact(self):
        spec = deterministic_task_spec("Create app.py in Python. Do not create empty files or placeholders.")
        compiled = self.compile(plan([
            task("create", "create_file"),
            task("implement", "modify_file", depends=["create"],
                 criteria=["The source contains the requested application logic."])]), spec)
        self.assertEqual(len(compiled["tasks"]), 1)

    def test_declared_approval_security_recovery_and_independent_value_boundaries_are_preserved(self):
        for field in ("preserve_boundary", "independent_value"):
            for reason in ("Human approval is required before the next phase.",
                           "Security policy separates these changes.", "Independent rollback checkpoint.",
                           "The initial module defines a usable public interface."):
                with self.subTest(field=field, reason=reason):
                    compiled = self.compile(plan([
                        task("create", "create_file", granularity={field: reason}),
                        task("modify", "modify_file", depends=["create"])]))
                    self.assertEqual(len(compiled["tasks"]), 2)

    def test_explicit_overwrite_approval_boundary_is_preserved(self):
        compiled = self.compile(plan([
            task("create", "create_file"), task("overwrite", "overwrite_file", depends=["create"],
                granularity={"preserve_boundary": "Human approval before replacing the complete source."})]))
        self.assertEqual(len(compiled["tasks"]), 2)
        self.assertEqual(self.events("plan_compiler.tasks_merged"), [])

    def test_independently_useful_source_and_final_content_are_not_scaffolds(self):
        for criterion in ("The source contains a public API.", "app.py exists and contains working logic.",
                          "The source contains a handler for empty input.", "The source contains no placeholders."):
            with self.subTest(criterion=criterion):
                tasks = [
                    task("create", "create_file", criteria=[criterion]),
                    task("extend", "modify_file", depends=["create"], criteria=["The source defines an additional method."])]
                # "working logic" requires runtime proof under the existing
                # evidence policy; retain that claim and provide a real verifier.
                if "working logic" in criterion:
                    tasks.append(task("verify", "run_python_script", depends=["extend"], kind="testing",
                                      criteria=["The command records stdout and exit code."]))
                compiled = self.compile(plan(tasks,
                    granularity_reason="The independent public API is extended then receives controlled QA verification."))
                self.assertEqual(len(compiled["tasks"]), len(tasks))
                self.assertEqual(self.events("plan_compiler.tasks_merged"), [])

    def test_shared_folder_alone_is_not_a_logical_component_boundary(self):
        compiled = self.compile(plan([
            task("one", "create_file", "component/a.py"),
            task("two", "create_file", "component/b.py", depends=["one"])]))
        self.assertEqual(len(compiled["tasks"]), 2)

    def test_declared_logical_component_can_absorb_mechanical_file_prerequisites(self):
        compiled = self.compile(plan([
            task("one", "create_file", "component/a.py", granularity={"logical_outcome": "Component"}),
            task("two", "create_file", "component/b.py", depends=["one"],
                 granularity={"logical_outcome": "Component"}, criteria=["The source contains the component implementation."])]))
        self.assertEqual(len(compiled["tasks"]), 1)
        self.assertEqual(set(compiled["write_owners"]), {"component/a.py", "component/b.py"})

    def test_six_simple_microtasks_normalize_to_one_with_a_warning(self):
        tasks = [task("create", "create_file")]
        for index in range(1, 6):
            tasks.append(task(f"step_{index}", "modify_file", depends=[tasks[-1]["key"]]))
        compiled = self.compile(plan(tasks))
        self.assertEqual(compiled["task_count"], 1)
        self.assertEqual(len(self.events("plan_compiler.tasks_merged")), 5)
        self.assertEqual(self.events("plan_compiler.granularity_warning")[0]["stage"], "before_normalization")
        self.assertIn("implementation content", compiled["tasks"][0]["success_criteria"][0])

    def test_advisory_range_does_not_reject_justified_meaningful_tasks(self):
        tasks = [task(f"module_{index}", "create_file", f"module_{index}.py",
                      criteria=["The source defines the independently usable module API."]) for index in range(6)]
        for complexity in ("simple", "multi_step", "complex"):
            with self.subTest(complexity=complexity):
                compiled = self.compile(plan(tasks, complexity=complexity,
                    granularity_reason="Six separately recoverable modules define independent public interfaces."))
                self.assertEqual(len(compiled["tasks"]), 6)
                warnings = self.events("plan_compiler.granularity_warning")
                if complexity != "complex":
                    self.assertEqual(warnings[-1]["reason"], "above_normal_range_justified")
                else:
                    self.assertEqual(warnings, [])

    def test_simple_single_worker_excess_without_justification_is_rejected(self):
        with self.assertRaises(OverfragmentedPlan):
            self.compile(plan([task(str(index), "create_file", f"{index}.py") for index in range(3)]))
        self.assertEqual(self.events("plan_compiler.granularity_warning")[-1]["reason"],
                         "explicit_granularity_justification_missing_or_invalid")
        self.assertEqual(self.events("plan_compiler.overfragmented")[-1]["stage"], "after_normalization")

    def test_vague_reason_or_unprotected_microtasks_cannot_bypass_enforcement(self):
        for reason in ("There are multiple files and many steps.",
                       "Independent modules and QA need separate progress units."):
            with self.subTest(reason=reason), self.assertRaises(OverfragmentedPlan):
                self.compile(plan([task(str(index), "create_file", f"{index}.py") for index in range(3)],
                                  granularity_reason=reason))

    def test_three_tasks_with_real_boundary_and_reason_are_preserved(self):
        compiled = self.compile(plan([
            task("create", "create_file", granularity={"preserve_boundary": "Human approval checkpoint."}),
            task("populate", "modify_file", depends=["create"], criteria=["The source contains the requested logic."]),
            task("audit", "read_file", depends=["populate"], kind="review", criteria=["The source content is reviewed."])],
            granularity_reason="Human approval separates creation from implementation and independent audit."))
        self.assertEqual(compiled["task_count"], 3)

    def test_global_presence_criteria_keep_auxiliary_local_links_after_merge(self):
        semantic = converter_plan()
        semantic["success_criteria"] = ["temperature_converter.py exists."]
        semantic["tasks"][0]["success_criteria"] = [" temperature_converter.py exists. "]
        compiled = self.compile(semantic, deterministic_task_spec(CONVERTER_REQUEST))
        implementation = compiled["tasks"][0]
        self.assertNotIn("exists", implementation["success_criteria"][0])
        self.assertIn("temperature_converter.py exists.", implementation["success_criteria"])
        task_ids = {item["id"] for item in compiled["tasks"]}
        self.assertTrue(all(link["task_id"] in task_ids for link in compiled["criterion_links"]["local"]))
        self.assertEqual(compiled["write_owners"], {"temperature_converter.py": "task-1"})
        self.assertEqual(self.events("plan_compiler.granularity_analyzed")[0]["task_id_map"],
                         {"task-1": "task-1", "task-2": "task-1", "task-3": "task-2"})

    def test_semantic_operations_alias_and_multiple_needs_resolve_on_one_task(self):
        work = task("implement", ["create_file", "read_file", "modify_file"])
        work["semantic_operations"] = work.pop("operations")
        work["semantic_needs"] = ["Create the module", "Implement the requested source"]
        compiled = self.compile(plan([work]))
        self.assertEqual(len(compiled["tasks"]), 1)
        self.assertEqual(compiled["tasks"][0]["semantic_operations"], ["create_file", "read_file", "modify_file"])
        self.assertEqual(set(compiled["tasks"][0]["required_tools"]), {"write_file", "read_file", "edit_file"})
        conflicting = {**work, "operations": ["read_file"]}
        with self.assertRaisesRegex(ValueError, "must not contradict"):
            self.compile(plan([conflicting]))

    def test_cycles_unknown_dependencies_and_duplicate_creators_are_not_hidden(self):
        invalid = [
            [task("create", "create_file", depends=["modify"]), task("modify", "modify_file", depends=["create"])],
            [task("create", "create_file"), task("modify", "modify_file", depends=["missing"])],
            [task("create", "create_file"), task("duplicate", "create_file", depends=["create"])],
        ]
        for tasks in invalid:
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                self.compile(plan(tasks))

    def test_schema_has_optional_value_metadata_and_multiple_operation_arrays(self):
        schema = semantic_plan_response_format({})
        implementation = schema["properties"]["tasks"]["items"]["anyOf"][0]
        self.assertNotIn("granularity", implementation["required"])
        self.assertEqual(implementation["properties"]["operations"]["type"], "array")
        self.assertEqual(implementation["properties"]["semantic_needs"]["type"], "array")
        self.assertIn("preserve_boundary", implementation["properties"]["granularity"]["properties"])

    def test_granularity_metadata_cannot_declare_permissions(self):
        for metadata in ({"required_capabilities": ["filesystem.overwrite"]},
                         {"preserve_boundary": True}, {"independent_value": "x" * 1001}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.compile(plan([task("create", "create_file", granularity=metadata)]))

    def test_unknown_operation_is_rejected_before_any_merge(self):
        semantic = plan([task("create", "create_file"),
                         task("modify", "unregistered_operation", depends=["create"])])
        with self.assertRaises(ValueError):
            self.compile(semantic)
        self.assertEqual(self.events("plan_compiler.tasks_merged"), [])


if __name__ == "__main__":
    unittest.main()
