"""Runtime-derived Planner resources and safe reference resolution."""
import unittest

from control_center.capabilities import CAPABILITIES
from control_center.agent_factory import AgentFactory
from control_center.plan_compiler import compile_semantic_plan
from control_center.runtime_resources import (
    RuntimeResourceCatalog, ToolCapabilityMismatch, UnknownTool,
    UnsupportedResourceRequirement,
)
from control_center.task_spec import deterministic_task_spec
from control_center.tools import Toolbox


class RuntimeResourceCatalogTests(unittest.TestCase):
    def test_catalog_matches_capability_registry_and_worker_schemas(self):
        catalog = RuntimeResourceCatalog.build()
        self.assertEqual(set(catalog.ids("capability")), {item.id for item in CAPABILITIES})
        schema_ids = {item["function"]["name"] for item in Toolbox.schema_catalog()}
        self.assertEqual(set(catalog.ids("tool")), schema_ids)
        command = next(item for item in catalog.tools if item["id"] == "run_command")
        self.assertIn("execution.python_script", command["capabilities"])
        self.assertIn("execution.python_script", catalog.capabilities_for_tool("run_command"))
        self.assertIn("bounded stdin", command["description"])
        capability = next(item for item in catalog.capabilities
                          if item["id"] == "execution.python_script")
        self.assertTrue(any("controlled stdin" in operation
                            for operation in capability["operations"]))

    def test_only_exact_ids_and_unique_declared_aliases_resolve(self):
        catalog = RuntimeResourceCatalog.build()
        self.assertEqual(catalog.resolve("capability", "run_python_script"),
                         "execution.python_script")
        self.assertEqual(catalog.resolve("tool", "run_workspace_command"), "run_command")
        self.assertIsNone(catalog.resolve("capability", "system"))
        ambiguous = RuntimeResourceCatalog(
            [{"id": "capability.one", "aliases": ["shared"]},
             {"id": "capability.two", "aliases": ["shared"]}], [], [])
        self.assertIsNone(ambiguous.resolve("capability", "shared"))

    def test_explicit_unsupported_need_produces_typed_error(self):
        catalog = RuntimeResourceCatalog.build()
        with self.assertRaises(UnsupportedResourceRequirement) as caught:
            catalog.validate_semantic_plan({
                "tasks": [],
                "unsupported_requirements": [{
                    "semantic_need": "Capture a screen image",
                    "resource_type": "tool",
                    "resource_id": "screen_capture",
                    "reason": "No screenshot tool is registered.",
                }],
            })
        self.assertEqual(caught.exception.resource_type, "tool")
        self.assertEqual(caught.exception.unknown_resource_id, "screen_capture")
        self.assertEqual(caught.exception.semantic_need, "Capture a screen image")

    def test_selected_tool_derives_compatible_capabilities_before_compilation(self):
        task_spec = deterministic_task_spec("Crea un programa Python que imprima hola")
        semantic = {
            "summary": "Create and run a Python script.",
            "success_criteria": [],
            "unsupported_requirements": [],
            "tasks": [{
                "key": "run_script",
                "objective": "Run the Python script",
                "description": "Execute a Python script from the workspace.",
                "depends_on": [],
                "semantic_needs": ["Execute a Python script from the workspace."],
                "required_capabilities": [],
                "required_tools": ["run_command"],
                "preferred_skills": [],
                "success_criteria": ["The script exits successfully."],
            }],
        }

        compiled = compile_semantic_plan(semantic, task_spec)

        task = compiled["tasks"][0]
        self.assertIn("execution.python_script", task["required_capabilities"])
        self.assertEqual(task["required_tools"], ["run_command"])
        policy = AgentFactory.capability_policy(task["required_capabilities"])
        self.assertEqual(policy["capabilities"]["execution"]["python_script"]["mode"], "ask")

    def test_known_tool_with_incompatible_capability_has_typed_mismatch(self):
        catalog = RuntimeResourceCatalog.build()
        with self.assertRaises(ToolCapabilityMismatch) as caught:
            catalog.validate_semantic_plan({
                "tasks": [{
                    "semantic_needs": ["Run a Python script."],
                    "required_capabilities": ["execution.python_script"],
                    "required_tools": ["read_file"],
                }],
            })
        self.assertEqual(caught.exception.error_type, "ToolCapabilityMismatch")
        self.assertEqual(caught.exception.tool_id, "read_file")
        self.assertEqual(caught.exception.compatible_capabilities, ["filesystem.read"])

    def test_context_cannot_add_tools_outside_global_catalog(self):
        registered = RuntimeResourceCatalog.build()
        catalog = RuntimeResourceCatalog.from_context({
            **registered.as_dict(),
            "tools": [*registered.tools, {"id": "invented_tool", "capabilities": []}],
        })
        with self.assertRaises(UnknownTool) as caught:
            catalog.validate_semantic_plan({
                "tasks": [{"required_tools": ["invented_tool"]}],
            })
        self.assertEqual(caught.exception.unknown_resource_id, "invented_tool")

    def test_custom_catalog_cannot_make_a_non_global_tool_valid(self):
        catalog = RuntimeResourceCatalog(
            [], [{"id": "invented_tool", "capabilities": []}], [],
        )
        with self.assertRaises(UnknownTool):
            catalog.validate_semantic_plan({
                "tasks": [{"required_tools": ["invented_tool"]}],
            })

    def test_custom_catalog_still_resolves_tools_and_capabilities_globally(self):
        catalog = RuntimeResourceCatalog([], [], [])
        resolved = catalog.validate_semantic_plan({
            "tasks": [{"required_tools": ["run_command"]}],
        })
        self.assertIn("execution.python_script",
                      resolved["tasks"][0]["required_capabilities"])


if __name__ == "__main__":
    unittest.main()
