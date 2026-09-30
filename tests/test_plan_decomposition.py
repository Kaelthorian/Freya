"""Planner strategy must reflect delegation benefit, not file or step count."""
import unittest

from control_center.plan_compiler import OverfragmentedPlan, compile_semantic_plan
from control_center.planner import Planner
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

    def test_seven_calculator_steps_share_one_worker_without_repair(self):
        spec = deterministic_task_spec(
            "Build a web calculator that adds, subtracts, multiplies and divides")
        semantic = plan(self.calculator_steps(), complexity="simple", reason=(
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
        compiled = compile_semantic_plan(plan(self.calculator_steps()), spec)
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
