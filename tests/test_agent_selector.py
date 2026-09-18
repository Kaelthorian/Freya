import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from control_center.agent_selector import AGENT_SELECTOR_VERSION, AgentSelector
from control_center.config import normalize_agent
from control_center.orchestrator import Orchestrator
from control_center.planner import Planner
from control_center.storage import Store


def capability_policy(**modes):
    capabilities = {}
    for capability, mode in modes.items():
        category, action = capability.replace("__", ".").split(".", 1)
        capabilities.setdefault(category, {})[action] = {"mode": mode}
    return {"capabilities": capabilities}


def planned_task(**changes):
    value = {
        "id": "fix-auth",
        "objective": "Fix the Python authentication bug",
        "description": "Diagnose and implement a safe backend API fix.",
        "depends_on": [],
        "required_capabilities": ["filesystem.read"],
        "preferred_skills": ["python-development", "debugging"],
        "success_criteria": ["The bug is fixed and verified."],
    }
    value.update(changes)
    return value


def single_task_plan(task):
    return {
        "goal": task["objective"],
        "summary": "Complete one bounded task.",
        "complexity": "simple",
        "tasks": [task],
        "success_criteria": list(task["success_criteria"]),
    }


class ImmediateRuntime:
    def __init__(self, store):
        self.store = store

    def submit(self, agent_id, objective, workspace_path=None):
        task = self.store.create_task(agent_id, objective, workspace_path or "workspace")
        return self.store.update_task(
            task["id"], status="Success", result="done",
            finished_at="2026-09-18T00:00:00+00:00",
        )

    def cancel(self, task_id):
        return self.store.get_task(task_id)


class RecordingSelector(AgentSelector):
    def __init__(self):
        self.calls = []

    def select_agent(self, task, agents, context=None):
        self.calls.append((copy.deepcopy(task), len(agents), copy.deepcopy(context)))
        return super().select_agent(task, agents, context)


class AgentSelectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.selector = AgentSelector()

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, name="Agent", role="General Agent", *, modes=None, skills=None,
              enabled=True, description=""):
        policy = capability_policy(**(modes or {}))
        return self.store.create_agent(normalize_agent({
            "name": name, "role": role, "description": description,
            "enabled": enabled, "skills": skills or [],
            "capability_policy": policy,
        }))

    @staticmethod
    def candidate(result, agent_id):
        return next(item for item in result["candidates"] if item["agent_id"] == agent_id)

    def test_required_capabilities_allow_is_eligible(self):
        agent = self.agent(modes={"filesystem__read": "allow", "filesystem__modify": "allow"})
        result = self.selector.select_agent(
            planned_task(required_capabilities=["filesystem.read", "filesystem.modify"]), [agent],
        )
        candidate = self.candidate(result, agent["id"])
        self.assertEqual(candidate["classification"], "eligible")
        self.assertEqual(candidate["capability_status"]["allowed"],
                         ["filesystem.read", "filesystem.modify"])

    def test_deny_is_ineligible(self):
        agent = self.agent(modes={"filesystem__read": "deny"})
        result = self.selector.select_agent(planned_task(), [agent])
        candidate = self.candidate(result, agent["id"])
        self.assertEqual(candidate["classification"], "ineligible")
        self.assertIsNone(candidate["score"])
        self.assertEqual(candidate["capability_status"]["denied"], ["filesystem.read"])

    def test_ask_is_conditional_and_explicit(self):
        agent = self.agent(modes={"filesystem__read": "ask"})
        result = self.selector.select_agent(planned_task(), [agent])
        candidate = self.candidate(result, agent["id"])
        self.assertEqual(candidate["classification"], "conditional")
        self.assertEqual(candidate["capability_status"]["approval_required"],
                         ["filesystem.read"])
        self.assertTrue(result["approval_required"])

    def test_eligible_wins_over_higher_scoring_conditional(self):
        eligible = self.agent("Eligible", modes={"filesystem__read": "allow"})
        conditional = self.agent(
            "Conditional", "Backend Python Developer",
            modes={"filesystem__read": "ask"},
            skills=[{"id": "python-development"}, {"id": "debugging"}],
        )
        result = self.selector.select_agent(planned_task(), [conditional, eligible])
        self.assertEqual(result["selected_agent_id"], eligible["id"])
        self.assertEqual(result["classification"], "eligible")

    def test_preferred_skill_increases_score(self):
        plain = self.agent("Plain", modes={"filesystem__read": "allow"})
        skilled = self.agent("Skilled", modes={"filesystem__read": "allow"},
                             skills=[{"id": "python-development"}])
        result = self.selector.select_agent(
            planned_task(preferred_skills=["python-development"]), [plain, skilled],
        )
        self.assertGreater(self.candidate(result, skilled["id"])["score"],
                           self.candidate(result, plain["id"])["score"])

    def test_operational_skill_scores_more_than_non_operational(self):
        operational = self.agent("Operational", modes={"filesystem__read": "allow"},
                                 skills=[{"id": "python-development"}])
        non_operational = self.agent("Non operational", modes={"filesystem__read": "ask"},
                                     skills=[{"id": "python-development"}])
        task = planned_task(required_capabilities=[], preferred_skills=["python-development"])
        result = self.selector.select_agent(task, [non_operational, operational])
        self.assertGreater(self.candidate(result, operational["id"])["score"],
                           self.candidate(result, non_operational["id"])["score"])
        self.assertEqual(self.candidate(result, non_operational["id"])
                         ["non_operational_preferred_skills"], ["python-development"])

    def test_role_identity_match_increases_score(self):
        backend = self.agent("Backend", "Backend Developer", modes={"filesystem__read": "allow"})
        network = self.agent("Network", "Network Engineer", modes={"filesystem__read": "allow"})
        result = self.selector.select_agent(planned_task(preferred_skills=[]), [network, backend])
        self.assertGreater(self.candidate(result, backend["id"])["score"],
                           self.candidate(result, network["id"])["score"])

    def test_workload_penalizes_otherwise_equal_agent(self):
        first = self.agent("First", modes={"filesystem__read": "allow"})
        second = self.agent("Second", modes={"filesystem__read": "allow"})
        result = self.selector.select_agent(planned_task(preferred_skills=[]), [first, second], {
            "workloads": {first["id"]: 3, second["id"]: 0},
        })
        self.assertEqual(result["selected_agent_id"], second["id"])
        self.assertLess(self.candidate(result, first["id"])["score"],
                        self.candidate(result, second["id"])["score"])

    def test_disabled_and_unusable_status_are_ineligible(self):
        disabled = self.agent("Disabled", modes={"filesystem__read": "allow"}, enabled=False)
        paused = self.agent("Paused", modes={"filesystem__read": "allow"})
        self.store.set_agent_state(paused["id"], "Paused")
        result = self.selector.select_agent(planned_task(),
                                            [disabled, self.store.get_agent(paused["id"])])
        self.assertTrue(all(item["classification"] == "ineligible"
                            for item in result["candidates"]))

    def test_missing_archived_and_invalid_agents_are_ineligible(self):
        archived = self.agent("Archived", modes={"filesystem__read": "allow"})
        archived["archived"] = True
        invalid = self.agent("Invalid", modes={"filesystem__read": "allow"})
        invalid["config"]["identity"] = []
        result = self.selector.select_agent(planned_task(), [{"enabled": True}, archived, invalid])
        self.assertTrue(all(item["classification"] == "ineligible"
                            for item in result["candidates"]))
        rendered = " ".join(warning for item in result["candidates"]
                            for warning in item["warnings"])
        self.assertIn("valid id", rendered)
        self.assertIn("archived", rendered)
        self.assertIn("configuration is invalid", rendered)

    def test_no_eligible_agent_returns_no_selection(self):
        agent = self.agent(modes={"filesystem__read": "deny"})
        result = self.selector.select_agent(planned_task(), [agent])
        self.assertIsNone(result["selected_agent_id"])
        self.assertEqual(result["status"], "no_eligible_agent")

    def test_ranking_and_tie_break_are_deterministic(self):
        first = self.agent("First", modes={"filesystem__read": "allow"})
        second = self.agent("Second", modes={"filesystem__read": "allow"})
        task = planned_task(preferred_skills=[], objective="Read a file", description="Read safely")
        forward = self.selector.select_agent(task, [first, second])
        reverse = self.selector.select_agent(task, [second, first])
        expected = min(first["id"], second["id"])
        self.assertEqual(forward["selected_agent_id"], expected)
        self.assertEqual(reverse["selected_agent_id"], expected)
        self.assertEqual([item["agent_id"] for item in forward["candidates"]],
                         [item["agent_id"] for item in reverse["candidates"]])

    def test_required_capability_gate_dominates_relevance_score(self):
        relevant_denied = self.agent(
            "Relevant", "Backend Python Developer", modes={"filesystem__read": "deny"},
            skills=[{"id": "python-development"}, {"id": "debugging"}],
        )
        general_allowed = self.agent("General", modes={"filesystem__read": "allow"})
        result = self.selector.select_agent(planned_task(), [relevant_denied, general_allowed])
        self.assertEqual(result["selected_agent_id"], general_allowed["id"])
        self.assertIsNone(self.candidate(result, relevant_denied["id"])["score"])

    def test_runtime_tool_and_workspace_are_hard_filters(self):
        runtime_missing = self.agent("Runtime", modes={"filesystem__read": "allow"})
        workspace_mismatch = self.agent("Workspace", modes={"filesystem__read": "allow"})
        workspace_mismatch["workspace_compatible"] = False
        result = self.selector.select_agent(planned_task(), [runtime_missing], {"runtime_tools": []})
        self.assertEqual(self.candidate(result, runtime_missing["id"])["classification"],
                         "ineligible")
        result = self.selector.select_agent(planned_task(), [workspace_mismatch])
        self.assertEqual(self.candidate(result, workspace_mismatch["id"])["classification"],
                         "ineligible")

    def test_selector_is_pure_and_never_grants_policy_or_skills(self):
        agent = self.agent("Pure", modes={"filesystem__read": "ask"},
                           skills=[{"id": "python-development"}])
        task = planned_task()
        context = {"workloads": {agent["id"]: 2}}
        before_agent, before_task, before_context = (copy.deepcopy(agent), copy.deepcopy(task),
                                                     copy.deepcopy(context))
        self.selector.select_agent(task, [agent], context)
        self.assertEqual(agent, before_agent)
        self.assertEqual(task, before_task)
        self.assertEqual(context, before_context)
        self.assertEqual(agent["config"]["capability_policy"],
                         before_agent["config"]["capability_policy"])
        self.assertEqual(agent["skills"], before_agent["skills"])


class AgentSelectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def agent(self, mode="allow"):
        return self.store.create_agent(normalize_agent({
            "name": "Backend", "role": "Backend Developer",
            "capability_policy": capability_policy(filesystem__read=mode),
            "skills": [{"id": "python-development"}],
        }))

    def run_orchestration(self, mode="allow", selector=None):
        agent = self.agent(mode)
        task = planned_task(preferred_skills=["python-development"])
        orchestrator = Orchestrator(
            self.store, ImmediateRuntime(self.store),
            planner=Planner(lambda prompt, context: single_task_plan(task)),
            selector=selector,
        )
        run = self.store.create_orchestration(task["objective"])
        orchestrator._run(run["id"])
        return agent, self.store.get_orchestration(run["id"])

    def test_selection_snapshot_and_version_are_persisted(self):
        agent, run = self.run_orchestration()
        self.assertEqual(run["status"], "Success")
        self.assertEqual(len(run["selections"]), 1)
        selection = run["selections"][0]
        self.assertEqual(selection["selected_agent_id"], agent["id"])
        self.assertEqual(selection["selector_version"], AGENT_SELECTOR_VERSION)
        self.assertEqual(selection["snapshot"]["selected_agent_id"], agent["id"])
        reopened = Store(self.path).get_orchestration(run["id"])
        self.assertEqual(reopened["selections"][0]["snapshot"], selection["snapshot"])

    def test_selection_events_are_emitted(self):
        agent, run = self.run_orchestration()
        event_types = [event["event_type"] for event in run["events"]]
        self.assertIn("freya.agent_selection.started", event_types)
        self.assertIn("freya.agent_selected", event_types)
        selected = next(event for event in run["events"]
                        if event["event_type"] == "freya.agent_selected")
        payload = json.loads(selected["payload_json"])
        self.assertEqual(payload["agent_id"], agent["id"])
        self.assertEqual(payload["selector_version"], AGENT_SELECTOR_VERSION)

    def test_failed_selection_is_persisted_and_emits_failed_event(self):
        _, run = self.run_orchestration("deny")
        self.assertEqual(run["status"], "Failed")
        self.assertEqual(run["selections"][0]["status"], "no_eligible_agent")
        self.assertIsNone(run["selections"][0]["selected_agent_id"])
        event_types = [event["event_type"] for event in run["events"]]
        self.assertIn("freya.agent_selection.started", event_types)
        self.assertIn("freya.agent_selection.failed", event_types)
        self.assertNotIn("freya.agent_selected", event_types)

    def test_orchestrator_uses_injected_agent_selector(self):
        selector = RecordingSelector()
        _, run = self.run_orchestration(selector=selector)
        self.assertEqual(run["status"], "Success")
        self.assertEqual(len(selector.calls), 1)
        self.assertEqual(selector.calls[0][0]["id"], "fix-auth")

    def test_cancellation_during_selection_prevents_late_snapshot_or_event(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingSelector(AgentSelector):
            def select_agent(inner_self, task, agents, context=None):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("selector was not released")
                return super().select_agent(task, agents, context)

        self.agent("allow")
        task = planned_task(preferred_skills=[])
        orchestrator = Orchestrator(
            self.store, ImmediateRuntime(self.store),
            planner=Planner(lambda prompt, context: single_task_plan(task)),
            selector=BlockingSelector(),
        )
        run = self.store.create_orchestration(task["objective"])
        thread = threading.Thread(target=orchestrator._run, args=(run["id"],))
        thread.start()
        self.assertTrue(entered.wait(2))
        cancelled = orchestrator.cancel(run["id"])
        events_at_cancel = list(cancelled["events"])
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        final = self.store.get_orchestration(run["id"])
        self.assertEqual(final["status"], "Cancelled")
        self.assertEqual(final["selections"], [])
        self.assertEqual(final["delegations"], [])
        self.assertEqual(final["events"], events_at_cancel)


if __name__ == "__main__":
    unittest.main()
