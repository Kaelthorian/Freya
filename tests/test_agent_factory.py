import json
import copy
import tempfile
import unittest
from pathlib import Path

from control_center.agent_factory import AgentFactory
from control_center.capabilities import CAPABILITIES
from control_center.config import normalize_agent
from control_center.evaluator import Evaluator
from control_center.integration import GlobalVerifier
from control_center.orchestrator import Orchestrator
from control_center.planner import Planner
from control_center.runtime_resources import (
    ToolCapabilityMismatch, UnsupportedResourceRequirement,
)
from control_center.recovery import RecoveryController
from control_center.runtime import Runtime
from control_center.storage import Store
from control_center.task_analyst import TaskAnalyst
from control_center.worker import run_task


def planned_task(**changes):
    task = {
        "id": "task-a",
        "objective": "Read an existing Python file",
        "description": "Perform read-only analysis and report evidence.",
        "depends_on": [],
        "required_capabilities": ["filesystem.read"],
        "preferred_skills": ["python-development"],
        "success_criteria": ["a completes"],
    }
    task.update(changes)
    return task


def plan_for(task):
    return {
        "goal": task["objective"],
        "summary": "Complete one bounded task.",
        "complexity": "simple",
        "tasks": [task],
        "success_criteria": list(task["success_criteria"]),
    }


def evaluation(status):
    action = {
        "accepted": "accept",
        "needs_revision": "revise",
        "rejected": "reject",
        "blocked": "gather_evidence",
    }[status]
    return {
        "status": status,
        "confidence": 0.9,
        "summary": status + " in AgentFactory fixture.",
        "criteria": [{
            "criterion": "a completes",
            "status": "satisfied" if status == "accepted" else "unsatisfied",
            "reason": "Controlled fixture evidence.",
            "evidence": ["fixture"],
        }],
        "issues": [] if status == "accepted" else ["The result needs another attempt."],
        "missing_evidence": [] if status != "blocked" else ["a completes"],
        "recommended_action": action,
    }


class ImmediateRuntime:
    def __init__(self, store):
        self.store = store

    def submit(self, agent_id, prompt, workspace_path=None):
        task = self.store.create_task(agent_id, prompt, workspace_path or "workspace")
        return self.store.update_task(
            task["id"], status="Success", result="done",
            verification={
                "requested": True, "attempted": True, "passed": True,
                "failed": False, "unavailable": False,
                "skipped_with_reason": "",
                "evidence": [{
                    "check": "AgentFactory fixture",
                    "status": "passed",
                    "output": "verified",
                }],
            },
            finished_at="2026-09-21T00:00:00+00:00",
        )

    def cancel(self, task_id):
        return self.store.get_task(task_id)


class AgentFactoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "state.sqlite3")
        self.factory = AgentFactory(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def create(self, task, *, attempt=1, variant=0):
        return self.factory.create(
            task, orchestration_id="orch-1", attempt=attempt, variant=variant,
        )

    @staticmethod
    def active_modes(agent):
        return {
            capability.id: agent["config"]["capability_policy"]["capabilities"]
            [capability.category][capability.id.split(".", 1)[1]]["mode"]
            for capability in CAPABILITIES
            if agent["config"]["capability_policy"]["capabilities"]
            [capability.category][capability.id.split(".", 1)[1]]["mode"] != "deny"
        }

    def test_read_only_task_derives_only_read_file(self):
        created = self.create(planned_task(
            preferred_skills=[], required_capabilities=["filesystem.read"],
        ))
        agent = created["agent"]
        self.assertEqual(agent["tools"], ["read_file"])
        self.assertNotIn("write_file", agent["tools"])
        self.assertNotIn("run_command", agent["tools"])
        self.assertEqual(self.active_modes(agent), {"filesystem.read": "allow"})

    def test_generic_file_task_selects_simple_artifact_skill(self):
        created = self.create(planned_task(
            objective="Create hola_mundo.txt with hola mundo",
            description="Create one local text file and verify its contents.",
            required_capabilities=["filesystem.create", "filesystem.read"],
            preferred_skills=[],
        ))
        self.assertIn("simple-file-artifact", created["skill_ids"])
        self.assertIn("read_file", created["agent"]["tools"])
        self.assertIn("write_file", created["agent"]["tools"])

    def test_trivial_program_filters_irrelevant_preferred_skills(self):
        created = self.create(planned_task(
            objective="Create a Python program that prints Hello World",
            description="Implement and execute one non-interactive Python program.",
            task_kind="program_creation",
            task_characteristics={
                "interactive": False, "requires_user_input": False,
                "requires_code_execution": True,
            },
            required_capabilities=[
                "filesystem.create", "filesystem.read", "execution.python_script",
            ],
            preferred_skills=[
                "python-development", "simple-file-artifact",
                "interactive-testing", "debugging",
            ],
        ))
        self.assertEqual(created["skill_ids"], ["python-development"])
        self.assertEqual(created["agent"]["config"]["orchestration_role"], "worker")

    def test_hello_world_python_flow_uses_minimal_agent_and_real_evidence(self):
        built = self.create(planned_task(
            objective="Create a Python program that prints Hello World",
            description="Implement one non-interactive Python program and verify stdout.",
            task_kind="program_creation",
            task_characteristics={"interactive": False, "requires_code_execution": True},
            required_capabilities=[
                "filesystem.create", "filesystem.read", "execution.python_script",
            ],
            preferred_skills=[
                "python-development", "simple-file-artifact", "interactive-testing", "debugging",
            ],
            success_criteria=["The program outputs 'Hello World' when executed"],
        ))
        agent = built["agent"]
        self.assertEqual(built["skill_ids"], ["python-development"])
        self.assertEqual(agent["tools"], ["read_file", "write_file", "run_command"])

        def answer(content="Finished.", calls=None):
            value = {"message": {"role": "assistant", "content": content},
                     "prompt_eval_count": 1, "eval_count": 1}
            if calls:
                value["message"]["tool_calls"] = [
                    {"function": {"name": name, "arguments": args}}
                    for name, args in calls
                ]
            return value

        responses = iter([
            answer(calls=[("write_file", {"path": "hello.py", "content": "print('Hello World')\n"})]),
            answer(calls=[("read_file", {"path": "hello.py"})]),
            answer(calls=[("run_command", {"argv": ["python", "hello.py"]})]),
            answer("Created and verified hello.py."),
        ])
        events = []
        config = copy.deepcopy(agent["config"])
        config["verification"] = {
            "enabled": True, "inspect_changes": False, "run_available_tests": False,
            "require_tool_evidence": True,
            "completion_criteria": ["The program outputs 'Hello World' when executed"],
        }
        result = run_task(
            {"config": config, "tools": agent["tools"], "workspace": str(self.root / "workspace"),
             "prompt": "Create the Hello World Python program."},
            self.root, events.append, lambda: None,
            transport=lambda *args, **kwargs: next(responses),
            approval_handler=lambda request: "approved_task",
        )
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertTrue(result["verification"]["attempted"])
        self.assertTrue(result["verification"]["passed"])
        command = next(item for item in result["verification"]["evidence"]
                       if item.get("type") == "command_execution")
        self.assertEqual(command["exit_code"], 0)
        self.assertIn("Hello World", command["output"])
        self.assertFalse(any(event.get("event", {}).get("event_type") == "task.blocked"
                             for event in events))

    def test_hola_txt_flow_uses_file_skill_readback_and_no_execution_tool(self):
        built = self.create(planned_task(
            objective="Create hola.txt with hola mundo",
            description="Create one text artifact and verify its contents by reading it back.",
            task_kind="file_creation",
            required_capabilities=["filesystem.create", "filesystem.read"],
            preferred_skills=["simple-file-artifact", "python-development", "debugging", "interactive-testing"],
        ))
        self.assertEqual(built["skill_ids"], ["simple-file-artifact"])
        self.assertEqual(built["agent"]["tools"], ["read_file", "write_file"])
        responses = iter([
            {"message": {"role": "assistant", "content": "", "tool_calls": [{
                "function": {"name": "write_file", "arguments": {"path": "hola.txt", "content": "hola mundo"}}
            }]}, "prompt_eval_count": 1, "eval_count": 1},
            {"message": {"role": "assistant", "content": "", "tool_calls": [{
                "function": {"name": "read_file", "arguments": {"path": "hola.txt"}}
            }]}, "prompt_eval_count": 1, "eval_count": 1},
            {"message": {"role": "assistant", "content": "Created and verified hola.txt."},
             "prompt_eval_count": 1, "eval_count": 1},
        ])
        config = copy.deepcopy(built["agent"]["config"])
        config["verification"] = {
            "enabled": True, "inspect_changes": False, "run_available_tests": False,
            "require_tool_evidence": True, "completion_criteria": ["The file contains 'hola mundo'"],
        }
        result = run_task(
            {"config": config, "tools": built["agent"]["tools"], "workspace": str(self.root / "workspace"),
             "prompt": "Create hola.txt with hola mundo."},
            self.root, lambda event: None, lambda: None,
            transport=lambda *args, **kwargs: next(responses),
        )
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertTrue(result["verification"]["passed"])
        self.assertTrue((self.root / "workspace" / "hola.txt").read_text(encoding="utf-8") == "hola mundo")

    def test_trivial_file_filters_program_debug_and_interactive_skills(self):
        created = self.create(planned_task(
            objective="Create hola.txt with hola mundo",
            description="Create one text artifact and verify it by reading it back.",
            task_kind="file_creation",
            required_capabilities=["filesystem.create", "filesystem.read"],
            preferred_skills=[
                "simple-file-artifact", "python-development",
                "debugging", "interactive-testing",
            ],
        ))
        self.assertEqual(created["skill_ids"], ["simple-file-artifact"])

    def test_recovery_workspace_state_derives_safe_read_without_overwrite(self):
        created = self.create(planned_task(
            required_capabilities=["filesystem.create", "execution.python_script"],
            _recovery_workspace_state={
                "workspace_diffs": [{"path": "hello.py", "change_type": "created"}],
                "verification": {"attempted": False},
            },
        ))
        agent = created["agent"]
        self.assertIn("read_file", agent["tools"])
        self.assertIn("filesystem.read", created["required_capabilities"])
        self.assertNotIn("filesystem.overwrite", created["required_capabilities"])
        self.assertEqual(self.active_modes(agent)["filesystem.read"], "allow")
        self.assertEqual(self.active_modes(agent).get("filesystem.overwrite"), None)

    def test_recovery_state_selects_debugging_without_debug_words_in_objective(self):
        created = self.create(planned_task(
            objective="Create hola.txt with hola mundo",
            task_kind="file_creation",
            required_capabilities=["filesystem.create", "filesystem.read"],
            preferred_skills=["simple-file-artifact", "debugging"],
            _recovery_reason="The previous attempt did not produce the required artifact.",
        ))
        self.assertEqual(created["skill_ids"], ["debugging"])

    def test_exact_preferred_skill_is_assigned_when_compatible(self):
        agent = self.create(planned_task())["agent"]
        skill = next(item for item in agent["skills"]
                     if item["id"] == "python-development")
        self.assertTrue(skill["operational"])

    def test_unknown_preferred_skill_is_rejected(self):
        with self.assertRaises(UnsupportedResourceRequirement) as caught:
            self.create(planned_task(preferred_skills=["does-not-exist"]))
        self.assertEqual(caught.exception.resource_type, "skill")
        self.assertEqual(caught.exception.unknown_resource_id, "does-not-exist")

    def test_skill_requirement_never_expands_task_capabilities(self):
        skill = self.store.create_skill({
            "id": "write-guidance",
            "name": "Write Guidance",
            "category": "Engineering",
            "description": "Guidance that requires modification.",
            "instructions": ["Modify only when authorized."],
            "procedures": [],
            "recommended_capabilities": [],
            "required_capabilities": ["filesystem.modify"],
            "tags": ["write"],
            "source": "user",
            "enabled": True,
        })
        with self.assertRaisesRegex(ValueError, "Primary Skill.*filesystem.modify"):
            self.create(planned_task(
                preferred_skills=[skill["id"]],
                required_capabilities=["filesystem.read"],
            ))

    def test_incompatible_selected_skill_is_rejected_without_expanding_policy(self):
        skill = self.store.create_skill({
            "id": "requires-modify-after-unknown",
            "name": "Requires Modify After Unknown",
            "category": "Engineering",
            "description": "Guidance that requires modification.",
            "instructions": ["Modify only when authorized."],
            "procedures": [],
            "recommended_capabilities": [],
            "required_capabilities": ["filesystem.modify"],
            "tags": ["write"],
            "source": "user",
            "enabled": True,
        })
        with self.assertRaisesRegex(ValueError, "Primary Skill.*filesystem.modify"):
            self.create(planned_task(
                preferred_skills=[skill["id"]],
                required_capabilities=["filesystem.read"],
            ))

    def test_selected_tool_cannot_bypass_the_capability_policy(self):
        with self.assertRaises(ToolCapabilityMismatch) as caught:
            self.create(planned_task(
                required_capabilities=["filesystem.read"],
                required_tools=["write_file"],
            ))
        self.assertEqual(caught.exception.error_type, "ToolCapabilityMismatch")
        self.assertEqual(caught.exception.tool_id, "write_file")

    def test_qa_task_is_dynamic_and_has_no_write_surface(self):
        created = self.create(planned_task(
            id="qa-interactive-test",
            objective="QA-test interactive behavior",
            description="Act as independent QA with controlled input.",
            required_capabilities=["filesystem.read", "execution.python_script"],
            preferred_skills=["interactive-testing", "software-testing"],
        ))
        agent = created["agent"]
        self.assertEqual(agent["config"]["orchestration_role"], "qa")
        self.assertIn("read_file", agent["tools"])
        self.assertIn("run_command", agent["tools"])
        self.assertNotIn("write_file", agent["tools"])
        self.assertNotIn("edit_file", agent["tools"])
        self.assertEqual(self.active_modes(agent)["execution.python_script"], "ask")

    def test_single_case_qa_does_not_attach_multi_case_skill_guidance(self):
        created = self.create(planned_task(
            id="qa-single-case", task_kind="testing",
            objective="Run the one requested interactive QA case",
            description="Read once and run one bounded stdin case.",
            required_capabilities=["filesystem.read", "execution.python_script"],
            required_tools=["read_file", "run_command"], preferred_skills=[],
            task_characteristics={"interactive": True, "requires_user_input": True,
                                 "single_case_verification": True},
        ))
        self.assertEqual(created["role"], "qa")
        self.assertEqual(created["skill_ids"], [])
        self.assertEqual(created["effective_tools"], ["read_file", "run_command"])
        self.assertTrue(created["agent"]["config"]["verification"]["stop_after_acceptance_evidence"])

    def test_code_auditor_is_independent_and_read_only(self):
        implementation = self.create(planned_task(
            id="implement",
            objective="Implement the Python change",
            required_capabilities=["filesystem.read", "filesystem.modify"],
        ))
        audit = self.create(planned_task(
            id="code-audit",
            objective="Audit the code produced for the request",
            description="Perform a read-only code review.",
            preferred_skills=["code-review"],
            required_capabilities=["filesystem.read"],
        ))
        self.assertNotEqual(implementation["agent"]["id"], audit["agent"]["id"])
        self.assertEqual(audit["agent"]["config"]["orchestration_role"], "auditor")
        self.assertEqual(audit["agent"]["tools"], ["read_file"])

    def test_multiple_capabilities_receive_only_derived_tools(self):
        required = [
            "filesystem.list", "filesystem.read", "filesystem.search", "git.diff",
        ]
        agent = self.create(planned_task(
            preferred_skills=[], required_capabilities=required,
        ))["agent"]
        self.assertEqual(
            agent["tools"], ["list_files", "read_file", "search_code", "git_diff"]
        )
        self.assertEqual(set(self.active_modes(agent)), set(required))
        self.assertLessEqual(len(agent["skills"]), 8)

    def test_overlapping_capabilities_do_not_duplicate_tools(self):
        agent = self.create(planned_task(
            preferred_skills=[],
            required_capabilities=["filesystem.create", "filesystem.overwrite"],
        ))["agent"]
        self.assertEqual(agent["tools"], ["write_file"])

    def test_runtime_config_cannot_inject_authority(self):
        for field, value in (
            ("permissions", "execute"),
            ("capability_policy", {}),
            ("workspace_path", str(self.root)),
            ("allowed_directories", ["."]),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "authority or unsupported"
            ):
                AgentFactory(self.store, runtime_config={field: value})
    def test_qa_and_auditor_write_capabilities_fail_closed(self):
        for task in (
            planned_task(
                objective="QA interactive behavior",
                preferred_skills=["interactive-testing"],
                required_capabilities=["filesystem.modify"],
            ),
            planned_task(
                objective="Code audit",
                preferred_skills=["code-review"],
                required_capabilities=["filesystem.create"],
            ),
        ):
            with self.subTest(task=task["objective"]), self.assertRaisesRegex(
                ValueError, "cannot receive write capabilities"
            ):
                self.create(task)

    def test_manual_agent_cannot_spoof_factory_provenance(self):
        with self.assertRaisesRegex(ValueError, "reserved"):
            normalize_agent({
                "name": "Spoofed dynamic agent",
                "config": {"provenance": {
                    "generated_by_freya": True,
                    "orchestration_id": "orch-1",
                    "plan_task_id": "task-a",
                    "attempt": 1,
                    "factory_version": 1,
                    "ephemeral": True,
                }},
            })
    def test_provenance_is_persisted_and_validated(self):
        agent = self.create(planned_task(), attempt=2)["agent"]
        provenance = agent["config"]["provenance"]
        self.assertEqual(provenance["orchestration_id"], "orch-1")
        self.assertEqual(provenance["plan_task_id"], "task-a")
        self.assertEqual(provenance["attempt"], 2)
        self.assertTrue(provenance["generated_by_freya"])
        self.assertTrue(provenance["ephemeral"])

    def run_orchestration(self, statuses, *, with_task_analyst=False,
                          global_verifier=None):
        task = planned_task()
        calls = []
        analyst = None
        if with_task_analyst:
            analyst = self.store.create_agent(normalize_agent({
                "name": "Persistent Task Analyst",
                "config": {"orchestration_role": "task_analyst"},
            }))

        def evaluate(prompt, context):
            status = statuses[min(len(calls), len(statuses) - 1)]
            calls.append(status)
            result = evaluation(status)
            result["criteria"] = [{
                "criterion": criterion,
                "status": "satisfied" if status == "accepted" else "unsatisfied",
                "reason": "Controlled fixture evidence.",
                "evidence": ["fixture"],
            } for criterion in context["planned_task"]["success_criteria"]]
            return result

        orchestrator = Orchestrator(
            self.store, ImmediateRuntime(self.store),
            planner=Planner(lambda prompt, context: json.dumps(plan_for(task))),
            evaluator=Evaluator(evaluate),
            recovery=RecoveryController(offline=True),
            task_analyst=TaskAnalyst(offline=True),
            global_verifier=global_verifier,
            wait=lambda seconds: None,
            config={"max_wallclock_seconds": 10},
        )
        run = self.store.create_orchestration(task["objective"])
        orchestrator._run(run["id"])
        return self.store.get_orchestration(run["id"])

    def test_modern_graph_uses_dynamic_worker_with_only_task_analyst_persistent(self):
        final = self.run_orchestration(["accepted"], with_task_analyst=True)
        self.assertEqual(final["status"], "Success")
        roles = {
            str(agent.get("config", {}).get("orchestration_role"))
            for agent in self.store.list_agents()
        }
        self.assertEqual(roles, {"task_analyst"})
        events = final["events"]
        created = next(item for item in events if item["event_type"] == "freya.agent_created")
        created_payload = json.loads(created["payload_json"])
        self.assertEqual(created_payload["role"], "worker")
        self.assertIn("python-development", created_payload["skill_ids"])
        self.assertIn("freya.dynamic_agent.archived", [item["event_type"] for item in events])

    def test_retry_same_agent_reuses_exact_agent_id(self):
        final = self.run_orchestration(["needs_revision", "accepted"])
        selected = [item["selected_agent_id"] for item in final["attempts"]
                    if item["plan_task_id"] == "task-a"]
        self.assertEqual(final["status"], "Success")
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0], selected[1])

    def test_retry_different_agent_creates_new_id_with_same_policy_ceiling(self):
        final = self.run_orchestration(["rejected", "accepted"])
        selected = [item["selected_agent_id"] for item in final["attempts"]
                    if item["plan_task_id"] == "task-a"]
        self.assertEqual(final["status"], "Success")
        self.assertEqual(len(selected), 2)
        self.assertNotEqual(selected[0], selected[1])
        task_policies = [
            self.store.get_task(item["runtime_task_id"])["capability_policy"]
            for item in final["attempts"]
            if item["plan_task_id"] == "task-a"
        ]
        self.assertEqual(task_policies[0], task_policies[1])

    def test_orchestration_without_manual_workers_creates_and_archives_agent(self):
        self.assertEqual(self.store.list_agents(), [])
        final = self.run_orchestration(["accepted"])
        event_types = [item["event_type"] for item in final["events"]]
        self.assertEqual(final["status"], "Success")
        self.assertEqual(len([item for item in final["attempts"]
                              if item["plan_task_id"] == "task-a"]), 1)
        self.assertIn("freya.agent_factory.started", event_types)
        self.assertIn("freya.agent_created", event_types)
        self.assertIn("freya.agent_policy.validated", event_types)
        self.assertIn("freya.dynamic_agent.archived", event_types)
        self.assertEqual(self.store.list_agents(), [])

    def test_successful_agent_archival_does_not_inherit_global_verifier_failure(self):
        final = self.run_orchestration(["accepted"],
                                       global_verifier=GlobalVerifier(lambda prompt, context: "bad"))
        self.assertEqual(final["status"], "Failed")
        self.assertEqual(final["evaluations"][0]["status"], "accepted")
        archived = [json.loads(item["payload_json"]) for item in final["events"]
                    if item["event_type"] == "freya.dynamic_agent.archived"]
        self.assertTrue(archived)
        self.assertEqual(archived[0]["status"], "Success")
        self.assertEqual(archived[0]["orchestration_status"], "Failed")

    def test_manual_agent_still_supports_runtime_submit(self):
        manual = self.store.create_agent(normalize_agent({
            "name": "Manual Worker",
            "role": "Engineer",
        }))
        runtime = Runtime(self.store, self.root, Path.cwd())
        try:
            task = runtime.submit(manual["id"], "Queue a direct manual task")
            self.assertEqual(task["agent_id"], manual["id"])
            self.assertEqual(task["status"], "Queued")
        finally:
            runtime.shutdown()
