"""Runtime-derived Planner resources and safe reference resolution."""
import unittest

from control_center.capabilities import CAPABILITIES
from control_center.agent_factory import AgentFactory
from control_center.plan_compiler import compile_semantic_plan
from control_center.runtime_resources import (
    AmbiguousToolCapability, RuntimeResourceCatalog, ToolCapabilityMismatch,
    UnknownCapability, UnknownSkill, UnknownTool,
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
        self.assertEqual([item["id"] for item in catalog.skills], ["freya-core"])
        core = catalog.skills[0]
        self.assertEqual(set(core["tools"]), schema_ids)
        self.assertEqual(core["required_capabilities"], [])
        self.assertTrue(core["use_when"])

    def test_compiler_marks_incompatible_preference_before_graph(self):
        catalog = RuntimeResourceCatalog.build()
        spec = deterministic_task_spec("Crea un programa Python que imprima hola")
        semantic = {"summary": "Create a Python file.", "tasks": [{
            "key": "implement", "objective": "Create calculator.py",
            "description": "Create the Python file.", "depends_on": [],
            "semantic_needs": ["Create calculator.py."],
            "required_capabilities": ["filesystem.create"],
            "required_tools": ["write_file"],
            "preferred_skills": ["python-development"],
            "success_criteria": ["The file exists."],
        }]}
        compiled = compile_semantic_plan(semantic, spec, resource_catalog=catalog)
        self.assertEqual(compiled["tasks"][0]["required_capabilities"], ["filesystem.create"])
        self.assertEqual(compiled["tasks"][0]["preferred_skills"], ["freya-core"])
        self.assertIn("planner skill preference ignored",
                      catalog.preferred_skill_warnings[0]["message"])

    def test_unknown_skill_and_capability_remain_typed_errors(self):
        catalog = RuntimeResourceCatalog.build()
        for field, reference, error in (
            ("required_capabilities", "filesystem.magic", UnknownCapability),
        ):
            with self.subTest(field=field), self.assertRaises(error):
                catalog.validate_semantic_plan({"tasks": [{field: [reference]}]})
        normalized = catalog.validate_semantic_plan({"tasks": [{"preferred_skills": ["nonexistent-skill"]}]})
        self.assertEqual(normalized["tasks"][0]["preferred_skills"], ["freya-core"])

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
        self.assertEqual(task["required_capabilities"], ["execution.python_script"])
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
        with self.assertRaises(ToolCapabilityMismatch) as other:
            catalog.validate_semantic_plan({"tasks": [{
                "required_tools": ["run_command"],
                "required_capabilities": ["filesystem.read"],
                "semantic_needs": ["Run an arbitrary command."],
            }]})
        self.assertEqual(other.exception.tool_id, "run_command")

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
            "tasks": [{"required_tools": ["run_command"],
                       "semantic_needs": ["Execute a Python script."]}],
        })
        self.assertEqual(resolved["tasks"][0]["required_capabilities"],
                         ["execution.python_script"])

    def test_ambiguous_tool_does_not_add_every_registered_capability(self):
        catalog = RuntimeResourceCatalog.build()
        with self.assertRaises(AmbiguousToolCapability):
            catalog.validate_semantic_plan({"tasks": [{"required_tools": ["run_command"]}]})
        with self.assertRaises(AmbiguousToolCapability):
            catalog.validate_semantic_plan({"tasks": [{"required_tools": ["write_file"]}]})

    def test_semantic_need_derives_missing_tool_capability_without_skill_grant(self):
        catalog = RuntimeResourceCatalog.build()
        resolved = catalog.validate_semantic_plan({"tasks": [{
            "semantic_needs": ["Create calculator.py.", "Execute a Python script."],
            "required_capabilities": ["filesystem.create"],
            "required_tools": ["write_file", "run_command"],
            "preferred_skills": ["python-development"],
        }]})
        self.assertEqual(resolved["tasks"][0]["required_capabilities"],
                         ["filesystem.create", "execution.python_script"])
        self.assertIn("planner skill preference ignored",
                      catalog.preferred_skill_warnings[0]["message"])


if __name__ == "__main__":
    unittest.main()
