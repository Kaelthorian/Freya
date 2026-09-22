import json
import unittest

from control_center.config import normalize_agent
from control_center.agent_selector import AgentSelector
from control_center.orchestrator import Orchestrator
from control_center.task_analyst import (
    OllamaTaskAnalyst,
    TaskAnalyst,
    deterministic_task_analysis,
    reconcile_task_analysis,
    select_task_analyst,
    validate_task_analysis,
)


class AnalysisStore:
    def __init__(self, agents):
        self.agents = agents
        self.events = []
        self.status = "Planning"

    def list_agents(self):
        return list(self.agents)

    def get_orchestration(self, _oid):
        return {"status": self.status}

    def add_orchestration_event(self, _oid, event):
        self.events.append(event)


class StubAnalysisAdapter:
    def __init__(self):
        self.metrics = {"model_calls": 1, "prompt_tokens": 12,
                        "generated_tokens": 34, "total_tokens": 46,
                        "duration_seconds": 0.01}
        self.prompts = []

    def analyze(self, prompt, agent):
        self.prompts.append((prompt, agent["id"]))
        return deterministic_task_analysis(prompt)


class TaskAnalystTests(unittest.TestCase):
    def test_explicit_role_wins_and_disabled_agents_are_ignored(self):
        legacy = normalize_agent({"name": "Task Analyst", "role": "Task Analyst Planner"})
        explicit = normalize_agent({
            "name": "Prompt Interpreter", "config": {"orchestration_role": "task_analyst"},
        })
        disabled = normalize_agent({
            "name": "Disabled Analyst", "config": {"orchestration_role": "task_analyst"},
            "enabled": False,
        })
        for index, agent in enumerate((legacy, explicit, disabled), 1):
            agent["id"] = f"agent-{index}"
        self.assertEqual(select_task_analyst([legacy, disabled, explicit])["id"], explicit["id"])

    def test_deterministic_analysis_preserves_interactive_command_requirements(self):
        result = deterministic_task_analysis(
            "Crea un archivo CDM que ejecute una calculadora y no cierre la ventana; solicita input."
        )
        self.assertEqual(result["task_type"], "windows_command_script")
        self.assertTrue(result["task_characteristics"]["interactive"])
        self.assertTrue(result["validation"]["interactive_validation_required"])
        self.assertTrue(any("CMD/BAT" in item for item in result["validation"]["avoid"]))
        self.assertEqual(validate_task_analysis(result), result)

    def test_long_prompt_is_bounded_without_failing_preplanning(self):
        result = deterministic_task_analysis("A" * 6000 + " calculator input")
        self.assertLessEqual(len(result["objective"]), 4000)
        self.assertLessEqual(len(result["operational_prompt"]), 4000)
        self.assertTrue(result["task_characteristics"]["requires_user_input"])

    def test_task_analyst_is_not_selected_as_worker(self):
        agent = normalize_agent({
            "name": "Interpreter", "config": {"orchestration_role": "task_analyst"},
        })
        agent["id"] = "analyst-1"
        selection = AgentSelector().select_agent(
            {"id": "task-1", "objective": "Create a file", "description": "", 
             "required_capabilities": [], "preferred_skills": []},
            [agent],
        )
        self.assertEqual(selection["status"], "no_eligible_agent")

    def test_worker_prompt_contains_analyst_brief_not_human_source(self):
        rendered = Orchestrator._execution_prompt(
            "Precise operational brief",
            {"objective": "Implement the requested artifact", "success_criteria": ["It works"]},
        )
        self.assertIn("TASK ANALYST OPERATIONAL BRIEF", rendered)
        self.assertIn("Precise operational brief", rendered)
        self.assertNotIn("ORIGINAL USER REQUEST", rendered)

    def test_ollama_adapter_is_tool_free_and_validates_json(self):
        captured = {}

        def request(method, url, payload, timeout):
            captured.update(method=method, url=url, payload=payload, timeout=timeout)
            return {"message": {"content": json.dumps(deterministic_task_analysis("Create a file"))},
                    "prompt_eval_count": 4, "eval_count": 5}

        agent = normalize_agent({"name": "Task Analyst", "role": "Task Analyst Planner"})
        adapter = OllamaTaskAnalyst(request=request)
        result = adapter.analyze("Create a file", agent)
        self.assertEqual(captured["payload"]["tools"], [])
        self.assertIn("format", captured["payload"])
        self.assertIn("assume Python 3.10+", captured["payload"]["messages"][0]["content"])
        self.assertEqual(result["analysis_version"], 3)
        self.assertEqual(adapter.metrics["total_tokens"], 9)

    def test_missing_language_for_program_uses_explicit_python_assumption(self):
        prompt = "Create a program that prints Hello World."
        blocked = deterministic_task_analysis(prompt)
        blocked["ready_for_execution"] = False
        blocked["blocking_reason"] = "The user needs to specify the programming language."

        result, corrected_fields = reconcile_task_analysis(prompt, blocked)

        self.assertTrue(result["ready_for_execution"])
        self.assertIsNone(result["blocking_reason"])
        self.assertIn("Programming language: Python 3.10+", result["operational_prompt"])
        self.assertTrue(any("Python 3.10+" in item["description"]
                            for item in result["assumptions"]))
        self.assertIn("ready_for_execution", corrected_fields)
        self.assertIn("assumptions", corrected_fields)

    def test_explicit_language_is_preserved_when_analyst_misses_it(self):
        prompt = "Create a JavaScript program that prints Hello World."
        blocked = deterministic_task_analysis(prompt)
        blocked["ready_for_execution"] = False
        blocked["blocking_reason"] = "The user needs to specify the programming language."

        result, _ = reconcile_task_analysis(prompt, blocked)

        self.assertTrue(result["ready_for_execution"])
        self.assertIn("Programming language: JavaScript", result["operational_prompt"])
        self.assertFalse(any("Python 3.10+" in item["description"]
                             for item in result["assumptions"]))

    def test_go_is_detected_as_a_language_only_in_language_context(self):
        explicit, _ = reconcile_task_analysis(
            "Create a Go program that prints Hello World.",
            deterministic_task_analysis("Create a Go program that prints Hello World."),
        )
        ordinary_verb, _ = reconcile_task_analysis(
            "Create a program, go home, and print Hello World.",
            deterministic_task_analysis("Create a program, go home, and print Hello World."),
        )
        self.assertIn("Programming language: Go", explicit["operational_prompt"])
        self.assertIn("Programming language: Python 3.10+",
                      ordinary_verb["operational_prompt"])

    def test_code_change_without_language_uses_existing_project_stack(self):
        prompt = "Modify the existing project to add a route."
        blocked = deterministic_task_analysis(prompt)
        blocked["task_kind"] = "code_change"
        blocked["ready_for_execution"] = False
        blocked["blocking_reason"] = "The user needs to specify the programming language."

        result, _ = reconcile_task_analysis(prompt, blocked)

        self.assertTrue(result["ready_for_execution"])
        self.assertIn("preserve the existing project's language",
                      result["operational_prompt"])
        self.assertFalse(any("Use Python 3.10+" in item["description"]
                             for item in result["assumptions"]))
        self.assertTrue(any("existing project's language" in item["description"]
                            for item in result["assumptions"]))

    def test_language_default_does_not_clear_a_compound_blocker(self):
        prompt = "Create a program that prints Hello World."
        blocked = deterministic_task_analysis(prompt)
        blocked["ready_for_execution"] = False
        blocked["blocking_reason"] = (
            "The user needs to specify the programming language and the target database."
        )

        result, _ = reconcile_task_analysis(prompt, blocked)

        self.assertFalse(result["ready_for_execution"])
        self.assertIn("target database", result["blocking_reason"])

    def test_ollama_adapter_repairs_blocked_contract_once_before_failure(self):
        first = deterministic_task_analysis("Create a file")
        first["ready_for_execution"] = False
        first["blocking_reason"] = None
        second = deterministic_task_analysis("Create a file")
        responses = [
            {"message": {"content": json.dumps(first)}, "prompt_eval_count": 2, "eval_count": 3},
            {"message": {"content": json.dumps(second)}, "prompt_eval_count": 4, "eval_count": 5},
        ]

        def request(method, url, payload, timeout):
            self.assertEqual(payload["tools"], [])
            return responses.pop(0)

        agent = normalize_agent({"name": "Task Analyst", "role": "Task Analyst Planner"})
        adapter = OllamaTaskAnalyst(request=request)
        result = adapter.analyze("Create a file", agent)
        self.assertTrue(result["ready_for_execution"])
        self.assertEqual(adapter.metrics["model_calls"], 2)
        self.assertEqual(len(responses), 0)

    def test_orchestrator_runs_analysis_before_planning(self):
        analyst = normalize_agent({
            "name": "Interpreter", "config": {"orchestration_role": "task_analyst"},
        })
        analyst["id"] = "analyst-1"
        store = AnalysisStore([analyst])
        adapter = StubAnalysisAdapter()
        orchestrator = Orchestrator(store, None, task_analyst=TaskAnalyst(adapter))
        analysis, metrics = orchestrator._analyze_prompt("run-1", "Create a file")
        self.assertEqual(adapter.prompts, [("Create a file", analyst["id"])])
        self.assertEqual(analysis["analysis_version"], 3)
        self.assertEqual(metrics["mode"], "model")
        self.assertEqual([event["event_type"] for event in store.events], [
            "freya.task_analysis.started", "freya.task_analysis.completed",
        ])
        self.assertEqual(store.events[-1]["agent_id"], analyst["id"])

    def test_deterministic_modes_apply_the_language_default(self):
        prompt = "Create a program that prints Hello World."
        offline_analyst = TaskAnalyst(offline=True)
        analysis = offline_analyst.analyze(prompt, {})
        self.assertTrue(analysis["ready_for_execution"])
        self.assertIn("Programming language: Python 3.10+", analysis["operational_prompt"])
        self.assertIn("assumptions", offline_analyst.metrics["corrected_fields"])

        store = AnalysisStore([])
        orchestrator = Orchestrator(store, None)
        no_agent_analysis, metrics = orchestrator._analyze_prompt("run-1", prompt)
        self.assertIn("Programming language: Python 3.10+",
                      no_agent_analysis["operational_prompt"])
        self.assertIn("assumptions", metrics["corrected_fields"])
        self.assertEqual(store.events[-1]["corrected_fields"], metrics["corrected_fields"])

    def test_orchestrator_blocks_planning_when_analysis_requires_input(self):
        analyst = normalize_agent({
            "name": "Interpreter", "config": {"orchestration_role": "task_analyst"},
        })
        analyst["id"] = "analyst-1"
        store = AnalysisStore([analyst])
        blocked = deterministic_task_analysis("Crea un hola mundo")
        blocked["ready_for_execution"] = False
        blocked["blocking_reason"] = "The user needs to specify the programming language."
        orchestrator = Orchestrator(store, None)
        with self.assertRaisesRegex(ValueError, "programming language"):
            orchestrator._require_ready_analysis("run-1", blocked)
        self.assertEqual(store.events[-1]["event_type"], "freya.task_analysis.blocked")
        self.assertEqual(store.events[-1]["blocking_reason"], blocked["blocking_reason"])

    def test_model_analysis_is_semantically_corrected_before_becoming_operational(self):
        flawed = deterministic_task_analysis("Create a file")
        flawed["operational_prompt"] = "Create a Python file and run it normally."
        flawed["task_type"] = "general_task"
        flawed["task_characteristics"]["interactive"] = False
        flawed["task_characteristics"]["requires_user_input"] = False
        flawed["validation"]["interactive_validation_required"] = False

        class FlawedAdapter:
            metrics = {"model_calls": 1}
            def analyze(self, _prompt, _agent):
                return flawed

        wrapper = TaskAnalyst(FlawedAdapter())
        result = wrapper.analyze(
            "Crea una calculadora CMD que solicite input y no cierre la ventana.",
            {"id": "analyst"},
        )
        self.assertEqual(result["task_type"], "windows_command_script")
        self.assertTrue(result["validation"]["interactive_validation_required"])
        self.assertIn("controlled stdin", result["operational_prompt"])
        self.assertIn("operational_prompt", wrapper.metrics["corrected_fields"])

    def test_placeholder_operational_prompt_is_replaced_by_deterministic_brief(self):
        flawed = deterministic_task_analysis("Create hello.txt")
        flawed["operational_prompt"] = "{}"

        class PlaceholderAdapter:
            metrics = {"model_calls": 1}
            def analyze(self, _prompt, _agent):
                return flawed

        result = TaskAnalyst(PlaceholderAdapter()).analyze(
            "Create hello.txt", {"id": "analyst"},
        )
        self.assertNotEqual(result["operational_prompt"], "{}")
        self.assertIn("hello.txt", result["operational_prompt"])

    def test_model_program_evidence_wins_over_broad_file_fallback(self):
        analysis = deterministic_task_analysis("hace un hola mundo")
        analysis["task_kind"] = "file_creation"
        analysis["objective"] = "Execute a simple program that prints Hello World"
        analysis["operational_prompt"] = "Implement and execute a program; verify its stdout."
        analysis["task_characteristics"] = dict(analysis["task_characteristics"])
        analysis["task_characteristics"]["requires_code_execution"] = True

        class ProgramAdapter:
            metrics = {"model_calls": 1}
            def analyze(self, _prompt, _agent):
                return analysis

        result = TaskAnalyst(ProgramAdapter()).analyze("hace un hola mundo", {"id": "analyst"})
        self.assertEqual(result["task_kind"], "program_creation")

    def test_python_mentioned_in_bug_report_does_not_make_it_program_creation(self):
        prompt = "Fix the Python authentication bug."
        result, _ = reconcile_task_analysis(prompt, deterministic_task_analysis(prompt))
        self.assertEqual(result["task_kind"], "analysis")


if __name__ == "__main__":
    unittest.main()
