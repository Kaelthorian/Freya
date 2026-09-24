"""Scope reconciliation before resource authority and graph construction."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_center.orchestrator import Orchestrator
from control_center.plan_compiler import compile_semantic_plan
from control_center.plan_scope import PlannerScopeError, reconcile_plan_scope, semantic_categories
from control_center.planner import Planner
from control_center.runtime_resources import RuntimeResourceCatalog, ToolCapabilityMismatch
from control_center.storage import Store
from control_center.task_spec import TaskSpecAnalyst, deterministic_task_spec


def web_spec():
    prompt = "Hace una calculadora que pueda sumar restar multiplicar y dividir"
    pending = deterministic_task_spec(prompt)
    answers = {item["id"]: "web" if item["field"] == "interface"
               else "suma resta multiplicacion y division"
               for item in pending["clarification_questions"]}
    return deterministic_task_spec(prompt, pending, answers)


def web_plan():
    return {"summary": "Build a web calculator, then deploy it.",
            "success_criteria": ["The calculator performs four operations.",
                                 "The calculator is deployed to production."],
            "tasks": [{
                "key": "build_calculator", "objective": "Create the web calculator",
                "description": "Create index.html with four arithmetic operations.",
                "semantic_needs": ["Create index.html."], "depends_on": [],
                "required_capabilities": ["filesystem.create", "filesystem.read"],
                "required_tools": ["write_file", "read_file"],
                "preferred_skills": [],
                "success_criteria": ["The web calculator performs four operations."],
            }, {
                "key": "deploy_calculator", "objective": "Deploy calculator to production",
                "description": "Publish the application to hosting.",
                "semantic_needs": ["deploy_calculator"],
                "depends_on": ["build_calculator"],
                "required_capabilities": ["filesystem.read"],
                "required_tools": ["run_command"], "preferred_skills": [],
                "success_criteria": ["The calculator is deployed to production."],
            }]}


class PlanScopeTests(unittest.TestCase):
    def test_current_web_calculator_failure_is_omitted_before_resource_resolution(self):
        spec = web_spec()
        self.assertEqual(spec["status"], "READY_FOR_PLANNING")
        planner = Planner(lambda prompt, context: web_plan())
        compiled = planner.create_plan_for_spec(spec, RuntimeResourceCatalog.build().as_dict())
        self.assertEqual([task["id"] for task in compiled["tasks"]], ["task-1"])
        self.assertEqual(compiled["tasks"][0]["required_capabilities"],
                         ["filesystem.create", "filesystem.read"])
        self.assertNotIn("run_command", compiled["tasks"][0]["required_tools"])
        self.assertFalse(any("deploy" in item.lower() for item in compiled["success_criteria"]))
        self.assertNotIn("deploy", compiled["summary"].lower())
        self.assertEqual(planner.metrics["scope_adjustments"][0]["task_key"],
                         "deploy_calculator")
        self.assertEqual(planner.metrics["semantic_compiler"]["status"], "Success")
        raw = planner.metrics["planner_semantic_plan"]["tasks"][1]
        self.assertEqual(raw["semantic_needs"], ["deploy_calculator"])
        self.assertEqual(raw["required_tools"], ["run_command"])

        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            run = store.create_orchestration(spec["user_intent"])
            Orchestrator(store, None, planner=planner)._record_plan_compiler_activity(
                run["id"], planner.metrics)
            events = store.get_orchestration(run["id"])["events"]
            types = [event["event_type"] for event in events]
            self.assertIn("freya.plan.scope_adjusted", types)
            self.assertIn("freya.plan_compiler.completed", types)
            started = next(json.loads(event["payload_json"]) for event in events
                           if event["event_type"] == "freya.plan_compiler.started")
            self.assertEqual(started["planner_semantic_plan"]["tasks"][1]["key"],
                             "deploy_calculator")

    def test_clarified_calculator_reaches_durable_execution_graph(self):
        prompt = "Hace una calculadora que pueda sumar restar multiplicar y dividir"
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            planner = Planner(lambda request, context: web_plan())
            orchestrator = Orchestrator(
                store, None, planner=planner, task_analyst=TaskSpecAnalyst(offline=True))
            reached_runtime = []
            orchestrator._run_graph = lambda oid, run, deadline, brief: reached_runtime.append(oid)
            run = store.create_orchestration(prompt)
            orchestrator._run(run["id"])
            for _ in range(3):
                current = store.get_orchestration(run["id"])
                if current["status"] != "NeedsClarification":
                    break
                question = current["task_spec"]["clarification_questions"][0]
                answer = ("web" if question["field"] == "interface"
                          else "suma resta multiplicacion y division")
                replies = {question["id"]: answer}
                store.record_clarification_answers(run["id"], replies)
                orchestrator._run(run["id"], replies)
            final = store.get_orchestration(run["id"])
            event_types = [event["event_type"] for event in final["events"]]
            self.assertEqual(final["task_spec"]["status"], "READY_FOR_PLANNING")
            self.assertEqual(final["planning_metrics"]["semantic_compiler"]["status"], "Success")
            self.assertIn("freya.graph.initialized", event_types)
            self.assertIn("freya.plan.scope_adjusted", event_types)
            self.assertNotIn("freya.plan_compiler.failed", event_types)
            self.assertEqual(reached_runtime, [run["id"]])

    def test_direct_compiler_omits_external_work_and_rewires_dependencies(self):
        semantic = web_plan()
        semantic["tasks"].append({
            "key": "document", "objective": "Document the local calculator",
            "description": "Read index.html and describe its operations.",
            "semantic_needs": ["Read index.html."],
            "depends_on": ["deploy_calculator"],
            "required_capabilities": ["filesystem.read"],
            "required_tools": ["read_file"], "preferred_skills": [],
            "success_criteria": ["Local calculator documented."],
        })
        result = compile_semantic_plan(semantic, web_spec())
        self.assertEqual(len(result["tasks"]), 2)
        self.assertEqual(result["tasks"][1]["depends_on"], ["task-1"])

    def test_authoritative_criterion_cannot_be_dropped_silently(self):
        semantic = web_plan()
        criterion = web_spec()["validation_expectations"][0]
        semantic["tasks"][1]["success_criteria"].append(criterion)
        with self.assertRaises(PlannerScopeError):
            reconcile_plan_scope(web_spec(), semantic)

    def test_external_work_and_criteria_not_requested_are_removed(self):
        spec = deterministic_task_spec("Create a website")
        value, adjustments = reconcile_plan_scope(spec, web_plan())
        self.assertEqual(len(value["tasks"]), 1)
        self.assertTrue(adjustments)
        self.assertFalse(any("deployed" in item.lower() for item in value["success_criteria"]))

    def test_invented_local_criterion_and_unsupported_need_are_removed(self):
        semantic = web_plan()
        semantic["tasks"] = semantic["tasks"][:1]
        semantic["tasks"][0]["success_criteria"].append("The website is deployed.")
        semantic["unsupported_requirements"] = [{
            "resource_type": "tool", "resource_id": "deploy_tool",
            "semantic_need": "Deploy to hosting",
        }]
        value, adjustments = reconcile_plan_scope(web_spec(), semantic)
        self.assertEqual(value["unsupported_requirements"], [])
        self.assertFalse(any("deploy" in criterion.lower()
                             for criterion in value["tasks"][0]["success_criteria"]))
        self.assertIn("unsupported_need_removed", [item["action"] for item in adjustments])

    def test_semantic_categories_cover_local_and_external_operations(self):
        self.assertIn("artifact_creation", semantic_categories("Create index.html"))
        self.assertIn("local_testing", semantic_categories("Run pytest locally"))
        self.assertIn("deployment", semantic_categories("deploy_calculator"))
        self.assertIn("publication", semantic_categories("Publish the website"))
        self.assertIn("network_action", semantic_categories("Send an HTTP request"))
        self.assertNotIn("external_action", semantic_categories("Do not deploy the website"))

    def test_external_task_kind_without_specific_words_is_not_executed(self):
        semantic = web_plan()
        semantic["tasks"][1].update({
            "key": "distribution", "objective": "Handle distribution",
            "description": "Prepare external availability.",
            "semantic_needs": ["distribution"], "task_kind": "external_action",
        })
        value, adjustments = reconcile_plan_scope(web_spec(), semantic)
        self.assertEqual(len(value["tasks"]), 1)
        self.assertEqual(adjustments[0]["category"], "external_action")

    def test_explicit_deployment_remains_subject_to_resource_policy(self):
        spec = web_spec()
        spec["user_intent"] += " and deploy it to the configured local deployment target"
        spec["requirements"].append({"description": "Deploy to the configured local target.",
                                     "source": "explicit"})
        semantic = web_plan()
        semantic["tasks"][1]["description"] = "Deploy to the configured local target."
        value, adjustments = reconcile_plan_scope(spec, semantic)
        self.assertEqual(adjustments, [])
        self.assertEqual(len(value["tasks"]), 2)
        with self.assertRaises(ToolCapabilityMismatch):
            compile_semantic_plan(value, spec)

    def test_explicit_deployment_does_not_authorize_unrelated_email(self):
        spec = web_spec()
        spec["user_intent"] += " and deploy to the configured local target"
        semantic = web_plan()
        semantic["tasks"][1]["description"] = "Deploy to the configured local target."
        semantic["tasks"].append({
            "key": "email_customer", "objective": "Send an email to customers",
            "description": "Send an email after deployment.",
            "semantic_needs": ["Send email"], "depends_on": ["deploy_calculator"],
        })
        value, adjustments = reconcile_plan_scope(spec, semantic)
        self.assertEqual(len(value["tasks"]), 2)
        self.assertEqual(adjustments[0]["category"], "network_action")

    def test_requested_external_action_cannot_lose_unsupported_command(self):
        spec = web_spec()
        spec["user_intent"] += " and send an email"
        semantic = web_plan()
        semantic["tasks"][1].update({
            "key": "notify", "objective": "Send an email",
            "description": "Send an email with the calculator result.",
            "semantic_needs": ["Send email"],
        })
        semantic["tasks"][1]["success_criteria"] = ["An email is sent."]
        semantic["success_criteria"] = ["An email is sent."]
        with self.assertRaises(ToolCapabilityMismatch):
            compile_semantic_plan(semantic, spec)

    def test_mixed_artifact_and_external_task_fails_closed(self):
        semantic = web_plan()
        semantic["tasks"][0]["objective"] = "Create and deploy the web calculator"
        with self.assertRaises(PlannerScopeError):
            reconcile_plan_scope(web_spec(), semantic)

    def test_failed_compiler_keeps_sanitized_raw_task_for_diagnosis(self):
        semantic = web_plan()
        semantic["tasks"] = semantic["tasks"][:1]
        semantic["tasks"][0].update({
            "objective": "Run a command", "semantic_needs": ["Run arbitrary command"],
            "required_capabilities": ["filesystem.read"],
            "required_tools": ["run_command"],
        })
        semantic["tasks"][0]["description"] = "Run command. api_key=example-sensitive-value"
        planner = Planner(lambda prompt, context: semantic)
        with self.assertRaises(ToolCapabilityMismatch):
            planner.create_plan_for_spec(web_spec())
        raw = planner.metrics["semantic_compiler"]["planner_semantic_plan"]["tasks"][0]
        self.assertEqual(raw["required_tools"], ["run_command"])
        self.assertNotIn("example-sensitive-value", raw["description"])


class SemanticToolResolutionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = RuntimeResourceCatalog.build()

    def resolve(self, objective, need, capabilities):
        return self.catalog.validate_semantic_plan({"tasks": [{
            "key": "check", "objective": objective, "semantic_needs": [need],
            "required_tools": ["run_command"],
            "required_capabilities": capabilities,
        }]})["tasks"][0]

    def test_python_execution_follows_semantic_operation(self):
        task = self.resolve("Run calculator.py with inputs 3 and 5",
                            "Execute calculator.py with controlled stdin.",
                            ["filesystem.read"])
        self.assertEqual(task["required_capabilities"],
                         ["filesystem.read", "execution.python_script"])
        self.assertEqual(self.catalog.resource_resolutions[0]["resolved_capability"],
                         "execution.python_script")

    def test_pytest_is_selected_instead_of_python_script(self):
        task = self.resolve("Run pytest for the calculator Python script",
                            "Run pytest for calculator tests.", ["filesystem.read"])
        self.assertEqual(task["required_capabilities"],
                         ["filesystem.read", "execution.pytest"])

    def test_compile_check_selects_py_compile(self):
        task = self.resolve("Compile-check Python source",
                            "Compile-check Python calculator.py.", ["filesystem.read"])
        self.assertEqual(task["required_capabilities"],
                         ["filesystem.read", "execution.py_compile"])

    def test_ambiguous_command_still_fails(self):
        with self.assertRaises(ToolCapabilityMismatch):
            self.resolve("Run a command", "Run an arbitrary command.",
                         ["filesystem.read"])

    def test_unneeded_command_tool_is_removed_without_execution_authority(self):
        result = self.catalog.validate_semantic_plan({"tasks": [{
            "key": "create", "objective": "Create index.html",
            "semantic_needs": ["Create calculator UI."],
            "required_capabilities": ["filesystem.create"],
            "required_tools": ["write_file", "run_command"],
        }]})["tasks"][0]
        self.assertEqual(result["required_tools"], ["write_file"])
        self.assertEqual(result["required_capabilities"], ["filesystem.create"])

    def test_create_need_recovers_from_only_unneeded_command_proposal(self):
        semantic = {"tasks": [{
            "key": "create", "objective": "Create index.html",
            "semantic_needs": ["Create calculator UI."],
            "required_capabilities": [], "required_tools": ["run_command"],
        }]}
        task = self.catalog.validate_semantic_plan(semantic)["tasks"][0]
        self.assertEqual(task["required_capabilities"], ["filesystem.create"])
        self.assertEqual(task["required_tools"], [])
        compiled = compile_semantic_plan(semantic, web_spec())
        self.assertEqual(compiled["tasks"][0]["required_tools"], ["write_file"])

    def test_readback_wording_does_not_imply_python_execution(self):
        task = self.resolve("Verify Python source", "Read calculator.py to verify its content.",
                            ["filesystem.read"])
        self.assertEqual(task["required_capabilities"], ["filesystem.read"])
        self.assertEqual(task["required_tools"], [])


if __name__ == "__main__":
    unittest.main()
