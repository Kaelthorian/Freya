"""Cross-task file ownership, approval scope and intent matching tests."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from uuid import uuid4

from control_center.agent_factory import AgentFactory
from control_center.config import normalize_agent
from control_center.cross_task import (
    CrossTaskIntentMatcher, CrossTaskRequestError, deterministic_intent_match,
    normalize_owned_path,
)
from control_center.orchestrator import Orchestrator
from control_center.execution_graph import ExecutionGraph
from control_center.planner import PlanValidationError, validate_plan
from control_center.storage import Store
from control_center.worker import PolicyToolbox


class CrossTaskIntentTests(unittest.TestCase):
    def setUp(self):
        self.approved = {
            "requested_change": "Add an explicit cache expiry setting",
            "reason": "Prevent stale cache entries from persisting",
            "needed_for": "The consumer configuration requires bounded retention",
        }

    def test_paths_are_exact_workspace_relative_files(self):
        self.assertEqual(normalize_owned_path("src\\cache.py"), "src/cache.py")
        for unsafe in ("../secrets.txt", "C:/private.txt", "/root/file", "src/*.py", "a/./b.py"):
            with self.subTest(path=unsafe), self.assertRaises(CrossTaskRequestError):
                normalize_owned_path(unsafe)

    def test_plan_rejects_two_owners_for_the_same_exact_file(self):
        plan = {
            "goal": "Create a shared artifact", "summary": "Two conflicting writers.",
            "complexity": "multi_step", "success_criteria": ["Artifact is complete."],
            "tasks": [
                {"id": "writer-a", "objective": "Write A", "description": "Write A.",
                 "depends_on": [], "required_capabilities": ["filesystem.create"],
                 "preferred_skills": [], "success_criteria": ["A is written."],
                 "owned_paths": ["shared/output.txt"]},
                {"id": "writer-b", "objective": "Write B", "description": "Write B.",
                 "depends_on": [], "required_capabilities": ["filesystem.modify"],
                 "preferred_skills": [], "success_criteria": ["B is written."],
                 "owned_paths": ["shared/output.txt"]},
            ],
        }
        with self.assertRaisesRegex(PlanValidationError, "Conflicting write ownership"):
            validate_plan(plan)

    def test_exact_same_intent_matches_deterministically(self):
        requested = {key: "  ".join(value.split()) for key, value in self.approved.items()}
        self.assertTrue(deterministic_intent_match(self.approved, requested))
        result = CrossTaskIntentMatcher(model=lambda *_: self.fail("exact matches need no model call")).match(
            self.approved, requested,
        )
        self.assertEqual(result["method"], "deterministic")
        self.assertTrue(result["same_intent"])

    def test_changed_sensitive_purpose_never_reuses_grant(self):
        requested = dict(self.approved)
        requested["requested_change"] = "Delete authentication checks from the cache layer"
        result = CrossTaskIntentMatcher(model=lambda *_: self.fail("sensitive scope guard must be deterministic")).match(
            self.approved, requested,
        )
        self.assertFalse(result["same_intent"])
        self.assertEqual(result["method"], "deterministic_scope_guard")

    def test_semantic_matcher_is_bounded_to_validated_json_decisions(self):
        matcher = CrossTaskIntentMatcher(
            model=lambda _approved, _requested: {
                "same_intent": True, "reason": "Both request bounded cache retention.",
                "confidence": 0.93,
            },
        )
        requested = dict(self.approved)
        requested["requested_change"] = "Add a configured cache retention limit"
        result = matcher.match(self.approved, requested)
        self.assertTrue(result["same_intent"])
        self.assertEqual(result["method"], "injected_model")
        invalid = CrossTaskIntentMatcher(model=lambda *_: {"same_intent": "yes"})
        with self.assertRaises(CrossTaskRequestError):
            invalid.match(self.approved, requested)


class CrossTaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        self.requester = self.store.create_agent(normalize_agent({"name": "Requester"}))
        self.owner = self.store.create_agent(normalize_agent({"name": "Owner"}))
        self.run = self._run()

    def _run(self):
        run = self.store.create_orchestration("Coordinate a scoped file change")
        self.store.transition_orchestration(run["id"], "Queued", "Running")
        return run["id"]

    def _request(self, orchestration_id=None, *, requester_task="task-requester",
                 owner_task="task-owner", path="config/cache.toml",
                 requested_change="Add an explicit cache expiry setting",
                 operation="modify"):
        runtime_task = self.store.create_task(
            self.requester["id"], "Request the cache change", "workspace",
        )
        approval_id = "approval-" + uuid4().hex
        self.store.create_approval(
            runtime_task["id"], self.requester["id"], "cross_task.modify",
            "cross_task_modification", {}, "Request an owner-scoped file change",
            path, "The task does not own this file.", approval_id=approval_id,
            mark_waiting=True,
        )
        request = {
            "request_id": approval_id,
            "orchestration_id": orchestration_id or self.run,
            "requester_plan_task_id": requester_task,
            "requester_runtime_task_id": runtime_task["id"],
            "target_owner_plan_task_id": owner_task,
            "target_path": path,
            "requested_operation": operation,
            "requested_change": requested_change,
            "reason": "Prevent stale cache entries from persisting",
            "needed_for": "The consumer configuration requires bounded retention",
            "blocking": True,
        }
        self.store.create_cross_task_modification_request(request, approval_id)
        return request

    def test_approve_once_does_not_create_reusable_grant(self):
        request = self._request()
        self.store.resolve_cross_task_modification_approval(request["request_id"], "approved_once")
        self.assertEqual(self.store.find_cross_task_intent_grants(request), [])

    def test_orchestration_cancel_closes_pending_cross_task_approval(self):
        request = self._request()
        self.store.cancel_cross_task_modification_requests(self.run, "Cancelled by operator.")
        self.assertEqual(self.store.get_cross_task_modification_request(request["request_id"])["status"], "cancelled")
        self.assertEqual(self.store.get_approval(request["request_id"])["status"], "denied")

    def test_restart_recovery_closes_pending_cross_task_approval(self):
        request = self._request()
        self.assertEqual(self.store.recover_interrupted_orchestrations(), 1)
        self.assertEqual(self.store.get_orchestration(self.run)["status"], "Failed")
        self.assertEqual(
            self.store.get_cross_task_modification_request(request["request_id"])["status"],
            "cancelled",
        )
        self.assertEqual(self.store.get_approval(request["request_id"])["status"], "denied")

    def test_same_file_intent_grant_is_scoped_to_run_requester_owner_and_path(self):
        first = self._request()
        resolved = self.store.resolve_cross_task_modification_approval(
            first["request_id"], "approved_file_intent",
        )
        self.assertIsNotNone(resolved["grant"])
        same_scope = self._request()
        self.assertEqual(len(self.store.find_cross_task_intent_grants(same_scope)), 1)
        changed_path = self._request(path="config/auth.toml")
        other_requester = self._request(requester_task="task-other-requester")
        other_owner = self._request(owner_task="task-other-owner")
        other_operation = self._request(operation="overwrite")
        other_run = self._run()
        other_orchestration = self._request(orchestration_id=other_run)
        for request in (changed_path, other_requester, other_owner, other_operation, other_orchestration):
            with self.subTest(scope=request):
                self.assertEqual(self.store.find_cross_task_intent_grants(request), [])

    def test_plan_cycle_is_detected_before_cross_task_dispatch(self):
        request = self._request()
        plan = {"tasks": [
            {"id": "task-requester", "depends_on": []},
            {"id": "task-owner", "depends_on": ["task-requester"]},
        ]}
        orchestrator = object.__new__(Orchestrator)
        orchestrator.store = self.store
        self.assertTrue(orchestrator._cross_task_cycle_would_form(plan, request))

    def test_owner_agent_capabilities_and_scope_cannot_expand(self):
        plan = {"tasks": [
            {"id": "task-requester", "owned_paths": ["request.py"]},
            {"id": "task-owner", "owned_paths": ["config/cache.toml"],
             "required_capabilities": ["filesystem.read", "filesystem.modify", "execution.python_script"]},
        ]}
        request = self._request()
        task = Orchestrator._cross_task_change_task(plan, plan["tasks"][1], request)
        self.assertEqual(task["owned_paths"], ["config/cache.toml"])
        self.assertEqual(task["required_capabilities"], ["filesystem.read", "filesystem.modify"])
        self.assertNotIn("execution.python_script", task["required_capabilities"])
        with self.assertRaisesRegex(ValueError, "declare filesystem.read"):
            Orchestrator._cross_task_change_task(
                plan, {**plan["tasks"][1], "required_capabilities": ["filesystem.modify"]}, request,
            )
        with self.assertRaisesRegex(ValueError, "requested filesystem write capability"):
            Orchestrator._cross_task_change_task(
                plan, plan["tasks"][1], {**request, "requested_operation": "overwrite"},
            )


class WorkerOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "owner.txt").write_text("before", encoding="utf-8")
        self.config = {
            "capability_policy": AgentFactory.capability_policy([
                "filesystem.read", "filesystem.create", "filesystem.modify",
            ]),
            "permissions": "workspace", "allowed_directories": ["."],
            "provenance": {"plan_task_id": "requester"},
            "task_owned_paths": ["requester.txt"],
            "task_write_owners": {"requester.txt": "requester", "owner.txt": "owner"},
            "task_write_scope_enforced": True,
            "autonomy": {"create_files": "automatic", "modify_files": "automatic"},
        }
        self.toolbox = PolicyToolbox(
            self.root, self.workspace, self.config,
            ["read_file", "write_file", "edit_file"],
        )

    def test_owned_write_is_allowed_and_foreign_read_remains_allowed(self):
        own = self.toolbox.invoke("write_file", {"path": "requester.txt", "content": "owned"})
        self.assertTrue(own.success)
        foreign_read = self.toolbox.invoke("read_file", {"path": "owner.txt"})
        self.assertTrue(foreign_read.success)
        self.assertIn("before", foreign_read.output)

    def test_foreign_write_returns_handoff_without_mutating_file(self):
        arguments = {
            "path": "owner.txt", "old": "before", "new": "after",
            "requested_change": "Add an explicit cache expiry setting",
            "reason": "Prevent stale cache entries from persisting",
            "needed_for": "The consumer configuration requires bounded retention",
            "blocking": True,
        }
        result = self.toolbox.invoke("edit_file", arguments)
        self.assertEqual(result.error_class, "cross_task_modification_required")
        self.assertFalse(result.executed)
        self.assertEqual((self.workspace / "owner.txt").read_text(encoding="utf-8"), "before")

    def test_hard_policy_deny_precedes_ownership_handoff_even_with_action_grant(self):
        arguments = {
            "path": "owner.txt", "old": "before", "new": "after",
            "requested_change": "Add an explicit cache expiry setting",
            "reason": "Prevent stale cache entries from persisting",
            "needed_for": "The consumer configuration requires bounded retention",
            "blocking": True,
        }
        self.toolbox.policy.evaluate = lambda *_: SimpleNamespace(
            outcome="deny", reason="Hard filesystem policy denial.",
        )
        self.toolbox.grant_approval("filesystem.modify", arguments, task=True)
        result = self.toolbox.invoke("edit_file", arguments)
        self.assertEqual(result.error_class, "policy_denied")
        self.assertNotEqual(result.error_class, "cross_task_modification_required")
        self.assertEqual((self.workspace / "owner.txt").read_text(encoding="utf-8"), "before")


class _CoordinatorStore:
    def __init__(self, plan, request, *, grants=None, runtime_task=None):
        self.plan = plan
        self.request = dict(request)
        self.grants = list(grants or [])
        self.runtime_task = runtime_task or {"id": "owner-runtime", "status": "Success"}
        self.events = []
        self.graph = ExecutionGraph(plan)
        self.graph.mark_selected("requester", "requester-agent", "selection")
        self.graph.mark_running("requester", "requester-runtime", "delegation")
        self.graph.apply_runtime_status("requester", "WaitingForApproval")

    def get_orchestration(self, _oid):
        return {"id": "run", "status": "Running", "plan": self.plan,
                "effective_plan": self.plan, "config": {"workspace_path": "workspace"}}

    def list_cross_task_modification_requests(self, _oid, statuses=None):
        if statuses and self.request.get("status") not in statuses:
            return []
        return [dict(self.request)]

    def find_cross_task_intent_grants(self, _request):
        return list(self.grants)

    def update_cross_task_modification_request(self, request_id, *, expected_statuses,
                                               status, **fields):
        if request_id != self.request["id"] or self.request["status"] not in expected_statuses:
            return None
        self.request.update(fields)
        self.request["status"] = status
        return dict(self.request)

    def auto_approve_cross_task_modification_request(self, request_id, *, grant_id, intent_match):
        if request_id != self.request["id"] or self.request["status"] != "pending":
            return None
        self.request.update(status="auto_approved", grant_id=grant_id,
                            intent_match=intent_match, approval_source="automatic_reuse")
        return dict(self.request)

    def block_cross_task_modification_request(self, request_id, *, status, reason,
                                              expected_statuses=None):
        if request_id != self.request["id"] or (expected_statuses and
                                                 self.request["status"] not in expected_statuses):
            return None
        self.request.update(status=status, human_resolution=reason)
        return dict(self.request)

    def add_orchestration_event(self, _oid, event):
        self.events.append(dict(event))

    def get_execution_graph(self, _oid):
        return {"nodes": self.graph.serialize()}

    def save_execution_graph(self, _oid, nodes):
        self.graph = ExecutionGraph(self.plan, nodes)
        return {"nodes": self.graph.serialize()}

    def get_task(self, _task_id):
        return dict(self.runtime_task)


def _coordinator_fixture(status="pending"):
    plan = {"tasks": [
        {"id": "requester", "objective": "Complete the consuming task", "depends_on": [],
         "required_capabilities": ["filesystem.read"], "owned_paths": ["consumer.py"]},
        {"id": "owner", "objective": "Maintain cache configuration", "depends_on": [],
         "required_capabilities": ["filesystem.read", "filesystem.modify"],
         "owned_paths": ["config/cache.toml"], "success_criteria": ["Owner task completes"]},
    ]}
    request = {
        "id": "cross-request", "approval_id": "approval", "orchestration_id": "run",
        "requester_plan_task_id": "requester", "requester_runtime_task_id": "requester-runtime",
        "target_owner_plan_task_id": "owner", "target_path": "config/cache.toml",
        "requested_operation": "modify",
        "requested_change": "Add an explicit cache expiry setting",
        "reason": "Prevent stale cache entries from persisting",
        "needed_for": "The consumer configuration requires bounded retention",
        "blocking": True, "status": status,
    }
    return plan, request


class CrossTaskCoordinatorTests(unittest.TestCase):
    def orchestrator(self, store, matcher=None, evaluator=None):
        value = object.__new__(Orchestrator)
        value.store = store
        value.cross_task_intent_matcher = matcher
        value.evaluator = evaluator
        value.evaluator_lock = threading.Lock()
        value.clock = lambda: 1.0
        return value

    def test_matcher_failure_leaves_request_for_human(self):
        plan, request = _coordinator_fixture()
        store = _CoordinatorStore(plan, request, grants=[{
            "id": "grant", "approved_intent": dict(request),
        }])
        class Unavailable:
            def match(self, *_):
                raise RuntimeError("local matcher unavailable")
        orchestrator = self.orchestrator(store, Unavailable())
        orchestrator._resolve_pending_cross_task_intents("run")
        self.assertEqual(store.request["status"], "awaiting_human")
        self.assertEqual(store.request["intent_match"]["method"], "unavailable")

    def test_high_confidence_same_purpose_match_auto_approves_only_current_request(self):
        plan, request = _coordinator_fixture()
        store = _CoordinatorStore(plan, request, grants=[{
            "id": "grant", "approved_intent": dict(request),
        }])
        class SamePurpose:
            def match(self, *_):
                return {"same_intent": True, "reason": "Same bounded cache purpose.",
                        "confidence": 0.94, "method": "test"}
        orchestrator = self.orchestrator(store, SamePurpose())
        orchestrator._resolve_pending_cross_task_intents("run")
        self.assertEqual(store.request["status"], "auto_approved")
        self.assertEqual(store.request["grant_id"], "grant")

    def test_different_or_uncertain_purpose_stays_with_human(self):
        for decision in (
            {"same_intent": False, "reason": "Purpose differs.", "confidence": 0.98},
            {"same_intent": True, "reason": "Evidence is uncertain.", "confidence": 0.89},
        ):
            with self.subTest(decision=decision):
                plan, request = _coordinator_fixture()
                store = _CoordinatorStore(plan, request, grants=[{
                    "id": "grant", "approved_intent": dict(request),
                }])
                class Matcher:
                    def match(self, *_):
                        return dict(decision)
                self.orchestrator(store, Matcher())._resolve_pending_cross_task_intents("run")
                self.assertEqual(store.request["status"], "awaiting_human")

    def test_denial_resumes_requester_without_agent_to_agent_message(self):
        plan, request = _coordinator_fixture("denied")
        store = _CoordinatorStore(plan, request)
        orchestrator = self.orchestrator(store)
        orchestrator._advance_cross_task_requests("run", 10.0, "Original task brief")
        self.assertEqual(store.graph.node("requester")["state"], "ready")
        self.assertIn("did not proceed", store.graph.node("requester")["attempt_prompt"])
        self.assertTrue(any(item["event_type"] == "cross_task_modification.requester_resumed"
                            for item in store.events))

    def test_dependency_cycle_is_blocked_and_does_not_resume_twice(self):
        plan, request = _coordinator_fixture()
        plan["tasks"][1]["depends_on"] = ["requester"]
        store = _CoordinatorStore(plan, request)
        orchestrator = self.orchestrator(store)
        orchestrator._resolve_pending_cross_task_intents("run")
        self.assertEqual(store.request["status"], "cycle_detected")
        orchestrator._advance_cross_task_requests("run", 10.0, "Original task brief")
        self.assertEqual(store.graph.node("requester")["state"], "ready")
        resumed_count = sum(item["event_type"] == "cross_task_modification.requester_resumed"
                             for item in store.events)
        orchestrator._advance_cross_task_requests("run", 10.0, "Original task brief")
        self.assertEqual(sum(item["event_type"] == "cross_task_modification.requester_resumed"
                             for item in store.events), resumed_count)

    def test_owner_success_must_pass_evaluator_before_requester_resumes(self):
        plan, request = _coordinator_fixture("owner_evaluating")
        request.update(owner_runtime_task_id="owner-runtime", owner_agent_id="owner-agent")
        store = _CoordinatorStore(plan, request)
        class AcceptingEvaluator:
            def evaluate(self, **_):
                return {"status": "accepted", "summary": "Approved file change verified."}
        orchestrator = self.orchestrator(store, evaluator=AcceptingEvaluator())
        orchestrator._evaluate_cross_task_owner_change("run", request, 10.0)
        self.assertEqual(store.request["status"], "completed")
        self.assertEqual(store.graph.node("requester")["state"], "ready")
        self.assertIn("do not repeat", store.graph.node("requester")["attempt_prompt"])
        event_types = [item["event_type"] for item in store.events]
        self.assertLess(event_types.index("cross_task_modification.completed"),
                        event_types.index("cross_task_modification.requester_resumed"))


if __name__ == "__main__":
    unittest.main()
