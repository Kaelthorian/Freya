"""Planner strategy must reflect delegation benefit, not file or step count."""
import copy
import json
import unittest
from unittest.mock import patch

from control_center.plan_compiler import (
    OverfragmentedPlan, PlanValidationError, compile_semantic_plan,
)
from control_center.planner import (
    Planner, PlannerUnableToProduceAcceptablePlan,
    PlannerUnableToProduceMateriallyDifferentPlan, RejectedSemanticPlanRegistry,
    RepeatedSemanticPlanError, _semantic_plan_structure_fingerprint,
)
from control_center.task_spec import deterministic_task_spec
from control_center.execution_graph import ExecutionGraph
from control_center.runtime_resources import RuntimeResourceCatalog
from control_center.worker_assignment import occupied_workers, worker_id_for_task


def item(key, path, *, kind="program_creation", depends=(), operation="create_file"):
    return {"key": key, "task_kind": kind, "objective": f"Implement {key}",
            "description": f"Implement {key}", "depends_on": list(depends),
            "semantic_needs": [f"Produce {key}"], "operations": [operation],
            "owned_paths": [path] if operation == "create_file" else [],
            "write_targets": [path] if operation == "create_file" else [],
            "success_criteria": [f"{path} exists."]}


def plan(tasks, *, complexity="simple", strategy="single_worker", reason=None):
    return {"summary": "Complete the requested application", "success_criteria": [],
            "unsupported_requirements": [], "task_complexity": complexity,
            "execution_strategy": strategy,
            "decomposition_reason": reason or "One worker owns the cohesive implementation and local checks.",
            "tasks": tasks}


class DecompositionTests(unittest.TestCase):
    @staticmethod
    def calculator_steps():
        names = ["markup", "style", "state", "addition", "subtraction",
                 "multiplication", "division"]
        return [item(name, f"{name}.js", depends=[names[index - 1]] if index else [])
                for index, name in enumerate(names)]

    def test_seven_unjustified_simple_calculator_steps_are_rejected_even_with_one_worker(self):
        spec = deterministic_task_spec("Build a web calculator that adds, subtracts, multiplies and divides")
        with self.assertRaises(OverfragmentedPlan):
            compile_semantic_plan(plan(self.calculator_steps()), spec)

    def test_single_worker_excess_uses_existing_bounded_planner_repair(self):
        spec = deterministic_task_spec("Build a web calculator that adds, subtracts, multiplies and divides")
        original = plan(self.calculator_steps())
        repaired = plan([item("markup", "markup.js")])
        contexts = []
        def model(_prompt, context):
            contexts.append(context)
            return original if len(contexts) == 1 else repaired
        planner = Planner(model)
        compiled = planner.create_plan_for_spec(spec)
        self.assertEqual((len(contexts), compiled["task_count"], compiled["worker_count"]), (2, 1, 1))
        self.assertTrue(contexts[1]["_freya_repair"])
        requested = [event for event in planner.metrics["planner_events"]
                     if event["event_type"] == "planner.repair_requested"]
        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0]["error_type"], "OverfragmentedPlan")

    def test_repeated_rejected_plan_forces_full_replan_and_accepts_valid_alternative(self):
        spec = deterministic_task_spec(
            "Build a web calculator that adds, subtracts, multiplies and divides")
        fragmented = plan(self.calculator_steps())
        repeated = copy.deepcopy(fragmented)
        renamed = {task["key"]: f"renamed-{index}"
                   for index, task in enumerate(repeated["tasks"], 1)}
        repeated["summary"] = "A different summary cannot disguise the same plan."
        for task in repeated["tasks"]:
            old_key = task["key"]
            task["key"] = renamed[old_key]
            task["depends_on"] = [renamed[key] for key in task["depends_on"]]
            for field in ("objective", "description"):
                task[field] = f"  {task[field].upper()}  "
            task["description"] += " This wording does not change the task's actions."
            for field in ("semantic_needs", "success_criteria"):
                task[field] = [f"  {text.upper()}  " for text in task[field]]
        alternative = plan([item("integrated-calculator", "calculator.js")])
        contexts = []
        repair_payloads = []

        def model(prompt, context):
            contexts.append(context)
            if context.get("_freya_repair"):
                repair_payloads.append(json.loads(prompt))
            if len(contexts) <= 2:
                return copy.deepcopy(fragmented if len(contexts) == 1 else repeated)
            return alternative

        planner = Planner(model)
        compiled = planner.create_plan_for_spec(spec)

        self.assertEqual(len(contexts), 3)
        self.assertEqual([bool(item.get("_freya_repair")) for item in contexts],
                         [False, True, True])
        self.assertTrue(contexts[2]["_freya_replan"])
        self.assertEqual((compiled["task_count"], compiled["worker_count"]), (1, 1))
        self.assertEqual(len(planner.metrics["semantic_compiler_attempts"]), 2)
        self.assertEqual(
            [record["attempt"] for record in planner.metrics["semantic_compiler_attempts"]],
            [1, 2],
        )
        self.assertIn("required_plan_delta", repair_payloads[0])
        self.assertIn("expected_task_range", repair_payloads[0]["required_plan_delta"])
        self.assertTrue(repair_payloads[1]["rules"]["replan_entire_plan"])
        self.assertTrue(repair_payloads[1]["rules"]["must_differ_from_rejected_plans"])
        self.assertTrue(repair_payloads[1]["required_plan_delta"]["must_be_structurally_different"])
        self.assertEqual(len(repair_payloads[1]["rejected_semantic_plans"]), 1)
        self.assertEqual(
            repair_payloads[1]["compiler_error"]["type"],
            "OverfragmentedPlan",
        )
        events = planner.metrics["planner_events"]
        self.assertEqual(len([event for event in events
                              if event["event_type"] == "planner.repair_no_material_change"]), 1)
        self.assertEqual(len([event for event in events
                              if event["event_type"] == "planner.repair_material_change_accepted"]), 1)
        self.assertEqual(len([event for event in events
                              if event["event_type"] == "planner.plan_fingerprint_created"]), 3)
        rejected_delta = next(event for event in events
                              if event["event_type"] == "planner.repair_delta_checked")
        self.assertEqual(rejected_delta["previous_structure_fingerprint"],
                         rejected_delta["new_structure_fingerprint"])
        self.assertFalse(rejected_delta["materially_different"])
        repairs = [event for event in events if event["event_type"] == "planner.repair_requested"]
        self.assertEqual([event["error_type"] for event in repairs],
                         ["OverfragmentedPlan", "PlannerRepairGuard"])
        self.assertTrue(repairs[1]["full_replan_required"])

    def test_repeated_rejected_plan_fails_before_spending_more_compiler_attempts(self):
        spec = deterministic_task_spec(
            "Build a web calculator that adds, subtracts, multiplies and divides")
        fragmented = plan(self.calculator_steps())
        calls = []

        def model(_prompt, context):
            calls.append(context)
            return copy.deepcopy(fragmented)

        planner = Planner(model)
        with self.assertRaisesRegex(
                PlannerUnableToProduceAcceptablePlan,
                "PlannerUnableToProduceAcceptablePlan"):
            planner.create_plan_for_spec(spec)

        self.assertEqual(len(calls), 4)
        self.assertEqual(len(planner.metrics["semantic_compiler_attempts"]), 1)
        self.assertEqual(sum(event["event_type"] == "planner.repair_no_material_change"
                             for event in planner.metrics["planner_events"]), 3)
        self.assertEqual(sum(event["event_type"] == "planner.repair_delta_checked"
                             for event in planner.metrics["planner_events"]), 3)
        self.assertEqual(sum(event["event_type"] == "planner.rejected_plan_repeated"
                             for event in planner.metrics["planner_events"]), 3)
        self.assertEqual(
            next(event for event in planner.metrics["planner_events"]
                 if event["event_type"] == "planner.repair_no_material_change")["result"],
            "repeated_rejected_plan",
        )
        self.assertFalse(any(event.get("event_type") == "planner.duplicate_plan_rejected"
                             for event in planner.metrics["planner_events"]))

    def test_overfragmentation_repair_must_reduce_count_or_add_valid_reason(self):
        spec = deterministic_task_spec(
            "Build a web calculator that adds, subtracts, multiplies and divides")
        simple = plan(self.calculator_steps())
        corrected = plan([item("integrated-calculator", "calculator.js")])
        calls = []

        def model(_prompt, context):
            calls.append(context)
            return copy.deepcopy(simple if len(calls) == 1 else corrected)

        planner = Planner(model)
        compiled = planner.create_plan_for_spec(spec)

        self.assertEqual(len(calls), 2)
        self.assertEqual(compiled["task_count"], 1)
        self.assertFalse(any(event["event_type"] == "planner.duplicate_plan_rejected"
                             for event in planner.metrics["planner_events"]))
        self.assertTrue(any(event["event_type"] == "planner.repair_material_change_accepted"
                            for event in planner.metrics["planner_events"]))

    def test_same_graph_repair_needs_new_graph_grounded_granularity_reason(self):
        spec = deterministic_task_spec(
            "Build a web calculator that adds, subtracts, multiplies and divides")
        rejected = plan(self.calculator_steps())
        justified = copy.deepcopy(rejected)
        justified["granularity_reason"] = (
            "These independent outcomes are separately recoverable and preserve real component boundaries.")
        for task in justified["tasks"]:
            task["granularity"] = {
                "independent_value": f"The {task['key']} component is independently useful and recoverable."
            }
        calls = []

        def model(_prompt, context):
            calls.append(context)
            return copy.deepcopy(rejected if len(calls) == 1 else justified)

        planner = Planner(model)
        compiled = planner.create_plan_for_spec(spec)

        self.assertEqual(len(calls), 2)
        self.assertEqual(compiled["task_count"], 7)
        accepted = next(event for event in planner.metrics["planner_events"]
                        if event["event_type"] == "planner.repair_material_change_accepted")
        self.assertFalse(accepted["semantic_equivalent"])
        self.assertEqual(accepted["delta_reason"], "concrete_granularity_reason")
        self.assertEqual(len(planner.metrics["rejected_plan_history"]), 1)

    def test_three_task_calculator_repair_is_guarded_before_compiler(self):
        spec = deterministic_task_spec(
            "Create a web calculator that can add, subtract, divide and multiply")
        initial = plan([
            item("create_files", "index.html"),
            item("implement_logic", "calculator.js", depends=["create_files"]),
            item("test_logic", "test-output.txt", depends=["implement_logic"]),
        ])
        equivalent = copy.deepcopy(initial)
        renamed = {task["key"]: f"calculator-{index}"
                   for index, task in enumerate(equivalent["tasks"], 1)}
        for task in equivalent["tasks"]:
            task["key"] = renamed[task["key"]]
            task["depends_on"] = [renamed[key] for key in task["depends_on"]]
            task["objective"] = f"  {task['objective'].upper()}  "
            task["semantic_needs"] = [f"  {need.upper()}  " for need in task["semantic_needs"]]
        equivalent["summary"] = "Metadata and task labels do not change this plan."
        accepted = plan([item("complete-calculator", "calculator.html")])
        responses = [initial, equivalent, accepted]
        contexts = []
        payloads = []

        def model(prompt, context):
            contexts.append(context)
            if context.get("_freya_repair"):
                payloads.append(json.loads(prompt))
            return copy.deepcopy(responses[len(contexts) - 1])

        planner = Planner(model)
        compiled = planner.create_plan_for_spec(spec)

        self.assertEqual(compiled["task_count"], 1)
        self.assertEqual([bool(item.get("_freya_replan")) for item in contexts],
                         [False, False, True])
        self.assertEqual(len(planner.metrics["semantic_compiler_attempts"]), 2)
        self.assertTrue(payloads[0]["required_plan_delta"]["must_change"])
        self.assertTrue(payloads[1]["rules"]["replan_entire_plan"])
        self.assertIn("rejected_plan_fingerprints", payloads[1])
        guard = [event for event in planner.metrics["planner_events"]
                 if event["event_type"] == "planner.repair_delta_checked"]
        self.assertEqual(len(guard), 2)
        self.assertEqual(guard[0]["compiler_error"]["type"], "OverfragmentedPlan")
        self.assertIn("task_count", guard[0]["unchanged_fields"])
        self.assertEqual(guard[0]["result"], "repeated_rejected_plan")
        fingerprints = [event for event in planner.metrics["planner_events"]
                        if event["event_type"] == "planner.plan_fingerprint_created"]
        self.assertEqual(fingerprints[0]["structure_fingerprint"],
                         fingerprints[1]["structure_fingerprint"])

    def test_distinct_compiler_rejections_are_registered_and_latest_delta_is_enforced(self):
        spec = deterministic_task_spec(
            "Create a web calculator that can add, subtract, divide and multiply")
        overfragmented = plan(self.calculator_steps())
        invalid_dependency = plan([
            item("create_calculator", "index.html"),
            item("implement_calculator", "calculator.js", depends=["missing_task"]),
        ])
        corrected = plan([item("complete_calculator", "calculator.html")])
        responses = [overfragmented, invalid_dependency, corrected]
        contexts = []
        payloads = []

        def model(prompt, context):
            contexts.append(context)
            if context.get("_freya_repair"):
                payloads.append(json.loads(prompt))
            return copy.deepcopy(responses[len(contexts) - 1])

        planner = Planner(model)
        compiled = planner.create_plan_for_spec(spec, orchestration_id="orchestration-test")

        self.assertEqual(compiled["task_count"], 1)
        attempts = planner.metrics["semantic_compiler_attempts"]
        self.assertEqual([item["status"] for item in attempts], ["Failed", "Failed", "Success"])
        history = planner.metrics["rejected_plan_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual([item["rejection_error_type"] for item in history],
                         ["OverfragmentedPlan", "PlanValidationError"])
        self.assertEqual([item["compiler_attempt"] for item in history], [1, 2])
        self.assertEqual([item["planner_attempt"] for item in history], [1, 2])
        self.assertTrue(all(item["orchestration_id"] == "orchestration-test"
                            for item in history))
        self.assertEqual(payloads[1]["required_plan_delta"]["repair_check"], "dependency_graph")
        self.assertTrue(payloads[1]["rules"]["replan_entire_plan"])
        self.assertEqual(len(payloads[1]["rejected_plan_history"]), 2)
        self.assertEqual(
            [item["fingerprint"] for item in payloads[1]["rejected_plan_history"]],
            [item["fingerprint"] for item in history],
        )
        checked = [event for event in planner.metrics["planner_events"]
                   if event["event_type"] == "planner.repair_delta_checked"]
        self.assertEqual(len(checked), 2)
        self.assertEqual(
            [(event["seen_before"], event["required_delta_satisfied"],
              event["materially_different"]) for event in checked],
            [(False, True, True), (False, True, True)],
        )
        self.assertEqual(sum(event["event_type"] == "planner.rejected_plan_registered"
                             for event in planner.metrics["planner_events"]), 2)
        registered = [event for event in planner.metrics["planner_events"]
                      if event["event_type"] == "planner.rejected_plan_registered"]
        self.assertEqual([event["normalized_plan"]["task_count"] for event in registered],
                         [7, 2])
        self.assertTrue(all(event["orchestration_id"] == "orchestration-test"
                            for event in registered))

    def test_repair_guard_prevents_plan_a_plan_b_rotation_before_compiler(self):
        spec = deterministic_task_spec(
            "Create a web calculator that can add, subtract, divide and multiply")
        plan_a = plan(self.calculator_steps())
        plan_b = plan([
            item("create_calculator", "index.html"),
            item("implement_calculator", "calculator.js", depends=["missing_task"]),
        ])
        responses = [plan_a, plan_b, plan_a, plan_b]
        contexts = []
        payloads = []

        def model(prompt, context):
            contexts.append(context)
            if context.get("_freya_repair"):
                payloads.append(json.loads(prompt))
            return copy.deepcopy(responses[len(contexts) - 1])

        planner = Planner(model)
        with self.assertRaises(PlannerUnableToProduceAcceptablePlan) as raised:
            planner.create_plan_for_spec(spec)

        self.assertEqual(raised.exception.diagnostics["failure_reason"], "repeated_rejected_plan")
        self.assertEqual(raised.exception.diagnostics["max_repair_attempts"], 3)
        self.assertEqual(len(contexts), 4)
        self.assertEqual(len(planner.metrics["semantic_compiler_attempts"]), 2)
        repeated = [event for event in planner.metrics["planner_events"]
                    if event["event_type"] == "planner.rejected_plan_repeated"]
        self.assertEqual([event["matching_rejected_attempt"] for event in repeated], [1, 2])
        self.assertEqual([event["previous_error"]["type"] for event in repeated],
                         ["OverfragmentedPlan", "PlanValidationError"])
        self.assertEqual(len(planner.metrics["rejected_plan_history"]), 2)
        self.assertEqual(len(payloads[1]["rejected_plan_history"]), 2)
        self.assertTrue(payloads[1]["rules"]["replan_entire_plan"])
        self.assertEqual(
            sum(event["event_type"] == "planner.repair_required_delta_failed"
                for event in planner.metrics["planner_events"]),
            0,
        )

    def test_unique_repair_that_does_not_fix_required_delta_skips_compiler(self):
        spec = deterministic_task_spec(
            "Create a web calculator that can add, subtract, divide and multiply")
        rejected = plan(self.calculator_steps())
        still_fragmented = plan(self.calculator_steps()[:6])
        corrected = plan([item("complete_calculator", "calculator.html")])
        responses = [rejected, still_fragmented, corrected]

        planner = Planner(lambda _prompt, _context: copy.deepcopy(responses.pop(0)))
        compiled = planner.create_plan_for_spec(spec)

        self.assertEqual(compiled["task_count"], 1)
        self.assertEqual(len(planner.metrics["semantic_compiler_attempts"]), 2)
        failed = next(event for event in planner.metrics["planner_events"]
                      if event["event_type"] == "planner.repair_required_delta_failed")
        self.assertFalse(failed["required_plan_delta"].get("must_satisfy_any") is None)
        self.assertEqual(failed["compiler_attempts_consumed"], 1)
        history = planner.metrics["rejected_plan_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual([entry["rejection_kind"] for entry in history],
                         ["compiler", "repair_guard"])

    def test_required_delta_exhaustion_has_distinct_terminal_diagnostic(self):
        spec = deterministic_task_spec(
            "Create a web calculator that can add, subtract, divide and multiply")
        responses = [
            plan(self.calculator_steps()),
            plan(self.calculator_steps()[:6]),
            plan(self.calculator_steps()[:5]),
            plan(self.calculator_steps()[:4]),
        ]
        planner = Planner(lambda _prompt, _context: copy.deepcopy(responses.pop(0)))

        with self.assertRaises(PlannerUnableToProduceAcceptablePlan) as raised:
            planner.create_plan_for_spec(spec)

        self.assertEqual(raised.exception.diagnostics["failure_reason"],
                         "required_plan_delta_unsatisfied")
        self.assertEqual(raised.exception.diagnostics["max_repair_attempts"], 3)
        self.assertEqual(len(planner.metrics["semantic_compiler_attempts"]), 1)
        self.assertEqual(len(planner.metrics["rejected_plan_history"]), 4)
        self.assertEqual(sum(event["event_type"] == "planner.repair_required_delta_failed"
                             for event in planner.metrics["planner_events"]), 3)

    def test_invalid_repair_shape_is_rejected_before_compiler(self):
        spec = deterministic_task_spec(
            "Create a web calculator that can add, subtract, divide and multiply")
        responses = [plan(self.calculator_steps()), ["not", "a", "semantic plan"]]
        planner = Planner(lambda _prompt, _context: copy.deepcopy(responses.pop(0)))

        with self.assertRaises(PlannerUnableToProduceAcceptablePlan) as raised:
            planner.create_plan_for_spec(spec)

        self.assertEqual(raised.exception.diagnostics["failure_reason"],
                         "invalid_semantic_plan_shape")
        self.assertEqual(len(planner.metrics["semantic_compiler_attempts"]), 1)
        checked = next(event for event in planner.metrics["planner_events"]
                       if event["event_type"] == "planner.repair_delta_checked")
        self.assertEqual(checked["result"], "invalid_semantic_plan_shape")
        self.assertEqual(checked["candidate_type"], "list")

    def test_new_compiler_rejections_exhaust_budget_with_distinct_diagnostic(self):
        spec = deterministic_task_spec(
            "Create a web calculator that can add, subtract, divide and multiply")
        responses = [
            plan([item("step_a", "alpha.py", operation="create_file")]),
            plan([item("step_b", "beta.py", operation="modify_file")]),
            plan([item("step_c", "gamma.py", operation="run_python_script")]),
            plan([item("step_d", "delta.py", operation="run_pytest")]),
        ]
        planner = Planner(lambda _prompt, _context: copy.deepcopy(responses.pop(0)))
        compiler_errors = [
            PlanValidationError("Unsupported operation: alpha"),
            PlanValidationError("Unsupported operation: beta"),
            PlanValidationError("Unsupported operation: gamma"),
            PlanValidationError("Unsupported operation: delta"),
        ]

        with patch("control_center.plan_compiler.compile_semantic_plan",
                   side_effect=compiler_errors) as compiler:
            with self.assertRaises(PlannerUnableToProduceAcceptablePlan) as raised:
                planner.create_plan_for_spec(spec)

        self.assertEqual(raised.exception.diagnostics["failure_reason"],
                         "new_compiler_rejection_after_repair_limit")
        self.assertEqual(raised.exception.diagnostics["repair_attempt_count"], 3)
        self.assertEqual(compiler.call_count, 4)
        self.assertEqual(len(planner.metrics["rejected_plan_history"]), 4)

    def test_structure_fingerprint_normalizes_renamed_action_descriptors(self):
        original = {"tasks": [
            {"key": "create_files", "task_kind": "program_creation",
             "objective": "Create calculator files",
             "description": "Create the requested calculator files",
             "operations": ["create_files"], "owned_paths": ["index.html"],
             "write_targets": ["index.html"], "depends_on": []},
            {"key": "implement_logic", "task_kind": "program_creation",
             "objective": "Implement calculator logic",
             "description": "Implement calculator functionality",
             "operations": ["implement_logic"], "owned_paths": ["calculator.js"],
             "write_targets": ["calculator.js"], "depends_on": ["create_files"]},
            {"key": "test_logic", "task_kind": "testing",
             "objective": "Test calculator behavior",
             "description": "Test the arithmetic behavior",
             "operations": ["test_logic"], "owned_paths": [], "write_targets": [],
             "depends_on": ["implement_logic"]},
        ]}
        renamed = copy.deepcopy(original)
        renamed["tasks"][0].update(
            key="create_calculator_files", operations=["create_calculator_files"],
            objective="Generate calculator assets", description="Generate calculator assets")
        renamed["tasks"][1].update(
            key="implement_calculator_functionality",
            operations=["implement_calculator_functionality"],
            objective="Add calculator functionality", description="Develop arithmetic behavior")
        renamed["tasks"][2].update(
            key="test_calculator_functionality", operations=["test_calculator_functionality"],
            objective="Verify calculator operation", description="Validate arithmetic behavior")
        renamed["tasks"][1]["depends_on"] = ["create_calculator_files"]
        renamed["tasks"][2]["depends_on"] = ["implement_calculator_functionality"]

        self.assertEqual(_semantic_plan_structure_fingerprint(original),
                         _semantic_plan_structure_fingerprint(renamed))
        registry = RejectedSemanticPlanRegistry("orchestration-test")
        rejected, added = registry.register(
            original, rejection_error={"type": "OverfragmentedPlan", "message": "rejected"},
            required_plan_delta={"must_change": ["task_count"]},
            planner_attempt=1, compiler_attempt=1, rejection_kind="compiler")
        match = registry.match(renamed)
        self.assertTrue(added)
        self.assertEqual(rejected["task_count"], 3)
        self.assertIsNotNone(match)
        self.assertEqual(match["match_kind"], "semantic_structure")

    def test_repeated_semantic_plan_error_remains_secondary_defense(self):
        self.assertTrue(issubclass(RepeatedSemanticPlanError, ValueError))
        self.assertTrue(issubclass(
            PlannerUnableToProduceAcceptablePlan,
            PlannerUnableToProduceMateriallyDifferentPlan,
        ))

    def test_seven_multi_step_tasks_share_one_worker_without_repair(self):
        spec = deterministic_task_spec(
            "Build a web calculator that adds, subtracts, multiplies and divides")
        semantic = plan(self.calculator_steps(), complexity="multi_step", reason=(
            "One worker can complete the seven dependent calculator steps in order."))
        catalog = RuntimeResourceCatalog.build()
        compiled = compile_semantic_plan(semantic, spec, resource_catalog=catalog)
        self.assertEqual(compiled["task_count"], 7)
        self.assertEqual(compiled["worker_count"], 1)
        self.assertEqual(compiled["worker_assignments"], [{
            "worker_id": "worker-1",
            "task_ids": [f"task-{number}" for number in range(1, 8)],
        }])
        self.assertEqual(compiled["tasks"][1]["depends_on"], ["task-1"])
        events = [event for event in catalog.compiler_events
                  if event["event_type"] == "plan_compiler.worker_assignment_created"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["worker_count"], 1)
        calls = []
        def model(prompt, context):
            calls.append(bool(context.get("_freya_repair")))
            return semantic
        result = Planner(model).create_plan_for_spec(spec)
        self.assertEqual(calls, [False])
        self.assertEqual((result["task_count"], result["worker_count"]), (7, 1))

    def test_sequential_tasks_become_ready_after_same_worker_acceptance(self):
        spec = deterministic_task_spec(
            "Build a web calculator that adds, subtracts, multiplies and divides")
        compiled = compile_semantic_plan(plan(self.calculator_steps(), complexity="multi_step"), spec)
        graph = ExecutionGraph(compiled)
        self.assertEqual([task["id"] for task in graph.ready_tasks()], ["task-1"])
        graph.mark_selected("task-1", "agent-1", "selection-1")
        graph.mark_running("task-1", "runtime-1", "delegation-1")
        self.assertEqual(graph.ready_tasks(), [])
        graph.apply_runtime_status("task-1", "Success")
        graph.apply_evaluation("task-1", "evaluation-1", "accepted", "Done")
        graph.refresh_dependencies()
        self.assertEqual([task["id"] for task in graph.ready_tasks()], ["task-2"])
        self.assertEqual(compiled["worker_assignments"][0]["task_ids"][:2],
                         ["task-1", "task-2"])

    def test_independent_tasks_in_one_worker_reserve_one_slot(self):
        spec = deterministic_task_spec("Build a frontend and backend application")
        compiled = compile_semantic_plan(plan([
            item("frontend", "front/app.js"), item("backend", "back/app.py")]), spec)
        graph = ExecutionGraph(compiled)
        self.assertEqual(len(graph.ready_tasks()), 2)
        worker_id = worker_id_for_task(compiled, "task-1")
        self.assertEqual(worker_id_for_task(compiled, "task-2"), worker_id)
        graph.mark_selected("task-1", "agent-1", "selection-1")
        self.assertIn(worker_id, occupied_workers(compiled, graph.serialize()))
        graph.mark_running("task-1", "runtime-1", "delegation-1")
        self.assertIn(worker_id, occupied_workers(
            compiled, graph.active_nodes(), include_selected_ready=False))
        graph.apply_runtime_status("task-1", "Success")
        graph.apply_evaluation("task-1", "evaluation-1", "accepted", "Done")
        self.assertNotIn(worker_id, occupied_workers(compiled, graph.serialize()))
        self.assertEqual([task["id"] for task in graph.ready_tasks()], ["task-2"])

    def test_multi_worker_groups_dependent_steps(self):
        spec = deterministic_task_spec("Build a large frontend and backend application")
        steps = [item("front-a", "front/a.js"),
                 item("front-b", "front/b.js", depends=["front-a"]),
                 item("front-c", "front/c.js", depends=["front-b"]),
                 item("back-a", "back/a.py"),
                 item("back-b", "back/b.py", depends=["back-a"]),
                 item("back-c", "back/c.py", depends=["back-b"]),
                 item("audit", "front/a.js", kind="review",
                      depends=["front-c", "back-c"], operation="read_file")]
        steps[-1]["success_criteria"] = ["The application is reviewed."]
        semantic = plan(steps, complexity="complex", strategy="multi_worker", reason=(
            "Independent frontend and backend subsystems can run in parallel, "
            "then a separate read-only audit checks both results."))
        compiled = compile_semantic_plan(semantic, spec)
        self.assertEqual(compiled["task_count"], 7)
        self.assertEqual(compiled["worker_count"], 3)
        self.assertEqual([group["task_ids"] for group in compiled["worker_assignments"]], [
            ["task-1", "task-2", "task-3"],
            ["task-4", "task-5", "task-6"], ["task-7"]])
        self.assertEqual(compiled["tasks"][6]["depends_on"], ["task-3", "task-6"])

    def test_multi_worker_strategy_keeps_two_slots_for_complex_chain(self):
        spec = deterministic_task_spec("Build a large frontend and backend application")
        names = ["interface", "service", "adapter", "verification"]
        steps = [item(name, f"src/{name}.py",
                      depends=[names[index - 1]] if index else [])
                 for index, name in enumerate(names)]
        compiled = compile_semantic_plan(plan(
            steps, complexity="complex", strategy="multi_worker", reason=(
                "A distinct specialist can take the downstream integration steps "
                "after the first implementation phase completes.")), spec)
        self.assertEqual(compiled["worker_count"], 2)
        self.assertEqual([group["task_ids"] for group in compiled["worker_assignments"]],
                         [["task-1", "task-2"], ["task-3", "task-4"]])

    def test_hello_txt_uses_one_worker(self):
        spec = deterministic_task_spec("Create hello.txt")
        result = compile_semantic_plan(plan([item("hello", "hello.txt", kind="file_creation")]), spec)
        self.assertEqual(result["execution_strategy"], "single_worker")
        self.assertEqual(result["task_complexity"], "simple")
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual((result["task_count"], result["worker_count"]), (1, 1))

    def test_web_calculator_can_keep_html_css_js_in_one_worker(self):
        spec = deterministic_task_spec(
            "Build a web calculator that adds, subtracts, multiplies and divides")
        work = item("calculator", "calculator.html")
        work["owned_paths"] = ["calculator.html", "calculator.css", "calculator.js"]
        work["write_targets"] = list(work["owned_paths"])
        work["semantic_needs"] = ["Implement the four arithmetic operations and interface."]
        work["success_criteria"] = [f"{path} exists." for path in work["write_targets"]]
        semantic = plan([work], complexity="multi_step", reason=(
            "The web calculator is one cohesive feature with compatible file and local checking tools."))
        result = compile_semantic_plan(semantic, spec)
        self.assertEqual(result["execution_strategy"], "single_worker")
        self.assertEqual(result["task_complexity"], "multi_step")
        self.assertEqual(len(result["tasks"][0]["write_targets"]), 3)

    def test_multiple_files_do_not_justify_delegation(self):
        spec = deterministic_task_spec("Build a frontend and backend application")
        semantic = plan([item("html", "index.html"), item("css", "style.css", depends=["html"]),
                         item("js", "app.js", depends=["css"])], complexity="multi_step",
                        strategy="multi_worker",
                        reason="Different files and HTML CSS JS steps require separate workers.")
        with self.assertRaises(OverfragmentedPlan):
            compile_semantic_plan(semantic, spec)

    def test_two_small_parts_of_same_feature_are_rejected(self):
        spec = deterministic_task_spec("Build a web calculator that adds and subtracts")
        semantic = plan([item("calculator markup", "calculator.html"),
                         item("calculator logic", "calculator.js",
                              depends=["calculator markup"])],
                        complexity="multi_step", strategy="multi_worker", reason=(
                            "Markup and program logic should be assigned to separate workers."))
        with self.assertRaises(OverfragmentedPlan):
            compile_semantic_plan(semantic, spec)

    def test_large_separable_subsystems_can_use_two_workers(self):
        spec = deterministic_task_spec("Build a large frontend and backend application")
        semantic = plan([item("frontend", "frontend/app.js"),
                         item("backend", "backend/server.py")], complexity="complex",
                        strategy="multi_worker", reason=(
                            "The frontend and backend have separate interfaces and substantial "
                            "implementation work that can proceed independently."))
        result = compile_semantic_plan(semantic, spec)
        self.assertEqual(len(result["tasks"]), 2)
        self.assertEqual(result["task_complexity"], "complex")

    def test_independent_read_only_audit_is_justified(self):
        spec = deterministic_task_spec("Create hello.txt")
        review = item("audit", "hello.txt", kind="review", depends=["hello"],
                      operation="read_file")
        review["success_criteria"] = ["The file content is inspected and recorded."]
        semantic = plan([item("hello", "hello.txt", kind="file_creation"), review],
                        complexity="multi_step", strategy="multi_worker", reason=(
                            "A dependent read-only audit provides an independent inspection of "
                            "the created artifact after implementation."))
        result = compile_semantic_plan(semantic, spec)
        self.assertEqual(result["execution_strategy"], "multi_worker")
        self.assertEqual(result["tasks"][1]["task_kind"], "review")

    def test_one_worker_can_have_several_operations(self):
        spec = deterministic_task_spec("Create hello.py")
        work = item("hello", "hello.py", kind="program_creation")
        work["operations"] = ["create_file", "modify_file", "read_file", "run_python_script"]
        result = compile_semantic_plan(plan([work], complexity="multi_step"), spec)
        self.assertEqual(result["task_complexity"], "multi_step")
        self.assertEqual(len(result["tasks"]), 1)
        self.assertIn("execution.python_script", result["tasks"][0]["required_capabilities"])

    def test_simple_calculator_split_is_rejected(self):
        spec = deterministic_task_spec("Create hello.txt")
        semantic = plan([item("scaffold", "calculator.py"),
                         item("logic", "calculator.py", depends=["scaffold"], operation="modify_file")],
                        strategy="multi_worker", reason=(
                            "The scaffold and arithmetic logic are separate implementation steps."))
        with self.assertRaises(OverfragmentedPlan):
            compile_semantic_plan(semantic, spec)

    def test_planner_repair_can_consolidate_rejected_graph(self):
        spec = deterministic_task_spec("Create hello.txt")
        fragmented = plan([item("scaffold", "hello.txt", kind="file_creation"),
                           item("finish", "hello.txt", kind="file_creation",
                                depends=["scaffold"], operation="modify_file")],
                          strategy="multi_worker", reason=(
                              "Separate scaffold and finish steps should use separate workers."))
        consolidated = plan([item("scaffold", "hello.txt", kind="file_creation")])
        calls = []
        def model(prompt, context):
            calls.append(context.get("_freya_repair", False))
            return consolidated if len(calls) == 2 else fragmented
        result = Planner(model).create_plan_for_spec(spec)
        self.assertEqual(calls, [False, True])
        self.assertEqual(len(result["tasks"]), 1)


if __name__ == "__main__":
    unittest.main()
