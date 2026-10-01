"""Prompt observability stays bounded and redacted across model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from control_center.llm_trace import bind_llm_trace, record_validation
from control_center.transport import MODEL_PROFILES, model_request, request_json
from control_center.settings import Settings, initialize_settings
from control_center.config import normalize_agent
from control_center.storage import Store
from control_center.config import DEFAULT_CONFIG
from control_center.evaluator import OllamaEvaluator
from control_center.planner import OllamaPlanner
from control_center.worker import run_task


class LlmTraceTests(unittest.TestCase):
    def setUp(self):
        # Each test represents a fresh process, with an explicit environment fixture.
        self.settings_patch = patch("control_center.settings._settings", None)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    def _call(self):
        captured = []
        body = {"model": "fixture", "messages": [
            {"role": "system", "content": "Return JSON"},
            {"role": "user", "content": "Build the requested artifact. token=very-private-value"}],
            "tools": [], "format": {"type": "object"}}
        def transport(*_args, **_kwargs):
            return {"message": {"content": json.dumps({"result": "ok"})},
                    "prompt_eval_count": 8, "eval_count": 4}
        with bind_llm_trace(captured.append):
            response = model_request(transport, "planner", "POST", "http://127.0.0.1:11434/api/chat",
                                     body, timeout=1, stage="repair")
            record_validation("planner", "accepted", detail="Compiled one task.",
                              normalized_response={"tasks": ["one"]})
        return captured, response

    def test_default_mode_records_metadata_without_prompt_or_response(self):
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "false"}):
            events, response = self._call()
        self.assertEqual(events[0]["event_type"], "llm.call")
        self.assertEqual(events[0]["stage"], "repair")
        self.assertEqual(events[0]["prompt_tokens"], 8)
        self.assertNotIn("request_body", events[0])
        self.assertNotIn("raw_response", events[0])
        self.assertNotIn("normalized_response", events[1])
        self.assertNotIn("detail", events[1])
        self.assertEqual(events[0]["llm_call_id"], events[1]["llm_call_id"])
        self.assertEqual(response["_freya_llm_call_id"], events[0]["llm_call_id"])

    def test_debug_mode_records_redacted_structured_prompt_and_raw_response(self):
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true",
                                    "FREYA_DEBUG_PROMPT_MAX_CHARS": "20000"}):
            events, _ = self._call()
        call = events[0]
        self.assertEqual(call["request_body"]["messages"][0]["role"], "system")
        self.assertEqual(call["raw_response"], '{"result": "ok"}')
        self.assertEqual(call["parsed_response"], {"result": "ok"})
        self.assertEqual(events[1]["normalized_response"], {"tasks": ["one"]})
        self.assertNotIn("very-private-value", json.dumps(call))

    def test_debug_limit_is_reported(self):
        events = []
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true",
                                    "FREYA_DEBUG_PROMPT_MAX_CHARS": "1000"}):
            with bind_llm_trace(events.append):
                model_request(lambda *_a, **_k: {"message": {"content": "done"}},
                              "worker", "POST", "http://127.0.0.1:11434/api/chat",
                              {"model": "fixture", "messages": [{"role": "user", "content": "x" * 5000}]},
                              timeout=1)
        self.assertTrue(events[0]["request_truncation"]["truncated"])
        self.assertGreater(events[0]["request_truncation"]["original_size"],
                           events[0]["request_truncation"]["stored_size"])
        self.assertLessEqual(len(events[0]["request_body"]["messages"][0]["content"]), 1000)

    def test_metadata_counts_original_prompt_beyond_redaction_limit(self):
        events = []
        long_prompt = "x" * 50000
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true",
                                    "FREYA_DEBUG_PROMPT_MAX_CHARS": "20000"}):
            with bind_llm_trace(events.append):
                model_request(lambda *_a, **_k: {"message": {"content": "done"}},
                              "planner", "POST", "http://127.0.0.1:11434/api/chat",
                              {"model": "fixture", "messages": [{"role": "user", "content": long_prompt}]},
                              timeout=1)
        self.assertGreater(events[0]["prompt_chars"], 50000)
        self.assertGreater(events[0]["request_truncation"]["original_chars"], 50000)
        self.assertTrue(events[0]["request_truncation"]["truncated"])

    def test_long_prompt_redaction_remains_bounded(self):
        events = []
        long_prompt = "x" * 50000 + " token=very-private-value"
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true",
                                    "FREYA_DEBUG_PROMPT_MAX_CHARS": "20000"}):
            with bind_llm_trace(events.append):
                model_request(lambda *_a, **_k: {"message": {"content": "done"}},
                              "planner", "POST", "http://127.0.0.1:11434/api/chat",
                              {"model": "fixture", "messages": [{"role": "user", "content": long_prompt}]},
                              timeout=1)
        self.assertNotIn("very-private-value", json.dumps(events[0]))
        self.assertTrue(events[0]["request_truncation"]["truncated"])

    def test_cookie_and_database_credentials_are_redacted(self):
        events = []
        body = {"model": "fixture", "messages": [{"role": "user",
                "content": "Cookie: session=private-cookie; db=postgresql://user:private-pass@localhost/app"}]}
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true"}):
            with bind_llm_trace(events.append):
                model_request(lambda *_a, **_k: {"message": {"content": "done"}},
                              "planner", "POST", "http://127.0.0.1:11434/api/chat",
                              body, timeout=1)
        persisted = json.dumps(events[0])
        self.assertNotIn("private-cookie", persisted)
        self.assertNotIn("private-pass", persisted)

    def test_configured_limit_preserves_full_local_prompt(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            store = Store(Path(directory) / "state.sqlite3")
            agent = store.create_agent(normalize_agent({"name": "Long prompt fixture"}))
            task = store.create_task(agent["id"], "Inspect fixture", directory)
            prompt = "x" * 50000
            with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true",
                                        "FREYA_DEBUG_PROMPT_MAX_CHARS": "60000"}):
                with bind_llm_trace(lambda event: store.append_event(task["id"], event)):
                    model_request(lambda *_a, **_k: {"message": {"content": "done"}},
                                  "planner", "POST", "http://127.0.0.1:11434/api/chat",
                                  {"model": "fixture", "messages": [{"role": "user", "content": prompt}]},
                                  timeout=1)
            persisted = next(event for event in store.list_events(task["id"])
                             if event["event_type"] == "llm.call")
            self.assertEqual(persisted["request_body"]["messages"][0]["content"], prompt)
            self.assertFalse(persisted["request_truncation"]["truncated"])

    def test_failed_call_has_same_metadata_without_response(self):
        events = []
        def fail(*_args, **_kwargs):
            raise TimeoutError("model timed out")
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "false"}):
            with bind_llm_trace(events.append):
                with self.assertRaises(TimeoutError):
                    model_request(fail, "evaluator", "POST", "http://127.0.0.1:11434/api/chat",
                                  {"model": "fixture", "messages": []}, timeout=1)
        self.assertEqual(events[0]["status"], "Failed")
        self.assertEqual(events[0]["error_type"], "TimeoutError")
        self.assertNotIn("raw_response", events[0])

    def test_debug_event_persists_redacted_in_runtime_log(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            store = Store(Path(directory) / "state.sqlite3")
            agent = store.create_agent(normalize_agent({"name": "Trace fixture"}))
            task = store.create_task(agent["id"], "Inspect fixture", directory)
            with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true"}):
                with bind_llm_trace(lambda event: store.append_event(task["id"], event)):
                    model_request(lambda *_a, **_k: {"message": {"content": '{"ok": true}'},
                                                        "prompt_eval_count": 1, "eval_count": 2},
                                  "planner", "POST", "http://127.0.0.1:11434/api/chat",
                                  {"model": "fixture", "messages": [{"role": "user",
                                      "content": "token=very-private-value"}]}, timeout=1)
            persisted = [event for event in store.list_events(task["id"])
                         if event["event_type"] == "llm.call"]
            self.assertEqual(len(persisted), 1)
            self.assertIn("request_body", persisted[0])
            self.assertNotIn("very-private-value", json.dumps(persisted[0]))

    def test_orchestration_event_persists_metadata_only_by_default(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            store = Store(Path(directory) / "state.sqlite3")
            run = store.create_orchestration("Inspect fixture")
            with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "false"}):
                with bind_llm_trace(lambda event: store.add_orchestration_event(run["id"], event)):
                    model_request(lambda *_a, **_k: {"message": {"content": "done"}},
                                  "task_analyst", "POST", "http://127.0.0.1:11434/api/chat",
                                  {"model": "fixture", "messages": [{"role": "user",
                                      "content": "private fixture content"}]}, timeout=1)
            events = [json.loads(event["payload_json"])
                      for event in store.get_orchestration(run["id"])["events"]
                      if event["event_type"] == "llm.call"]
            self.assertEqual(len(events), 1)
            self.assertNotIn("request_body", events[0])
            self.assertNotIn("private fixture content", json.dumps(events[0]))

    def test_worker_event_contains_actual_plan_and_task_context(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            events = []
            prompt = "Current task: create hello.txt. FULL PLAN CONTEXT: sibling audit follows."
            task = {"config": {**DEFAULT_CONFIG, "verification": {"enabled": False}},
                    "tools": [], "workspace": directory, "prompt": prompt}
            answer = json.dumps({"summary": "Finished.", "actions": [], "artifacts": [],
                                 "verification": {}, "limitations": []})
            with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true"}):
                run_task(task, Path(directory), events.append, lambda: None,
                         transport=lambda *_a, **_k: {"message": {"role": "assistant", "content": answer},
                                                      "prompt_eval_count": 3, "eval_count": 2})
            calls = [item["event"] for item in events if item.get("kind") == "event"
                     and item["event"].get("event_type") == "llm.call"]
            self.assertTrue(calls)
            self.assertIn("FULL PLAN CONTEXT", calls[0]["request_body"]["messages"][1]["content"])
            self.assertEqual(calls[0]["structured_context"]["current_task"]["prompt"], prompt)
            self.assertIn("agent_context", calls[0]["structured_context"])

    def test_evaluator_event_contains_supplied_evidence_context(self):
        events = []
        evaluator = OllamaEvaluator(model="fixture", request=lambda *_a, **_k: {
            "message": {"content": '{"criteria": []}'}, "prompt_eval_count": 2, "eval_count": 1})
        evidence = {"criterion_ids": ["GC-1"], "evidence": [{"type": "readback", "path": "hello.txt"}]}
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true"}):
            with bind_llm_trace(events.append):
                evaluator("Review supplied evidence", evidence)
        self.assertEqual(events[0]["structured_context"], evidence)
        self.assertIn("Bounded evaluation data", events[0]["request_body"]["messages"][1]["content"])

    def test_repair_has_distinct_call_id_prompt_and_raw_response(self):
        events = []
        replies = iter(['{"invalid": true}', '{"summary": "repaired"}'])
        planner = OllamaPlanner(model="fixture", request=lambda *_a, **_k: {
            "message": {"content": next(replies)}, "prompt_eval_count": 2, "eval_count": 1})
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true"}):
            with bind_llm_trace(events.append):
                planner("Initial plan", {"_semantic_plan": True})
                planner("Repair the plan", {"_semantic_plan": True, "_freya_repair": True})
        self.assertEqual([event["stage"] for event in events], ["initial", "repair"])
        self.assertNotEqual(events[0]["llm_call_id"], events[1]["llm_call_id"])
        self.assertEqual(events[1]["repair_of_llm_call_id"], events[0]["llm_call_id"])
        self.assertIn("invalid", events[0]["raw_response"])
        self.assertIn("repaired", events[1]["raw_response"])
        self.assertIn("Repair the plan", events[1]["request_body"]["messages"][1]["content"])

    def test_all_registered_components_share_startup_setting_and_repairs(self):
        for enabled in (False, True):
            with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": str(enabled)}):
                initialize_settings()
            # Changing the environment after startup must not change any component.
            with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": str(not enabled)}):
                for component in MODEL_PROFILES:
                    with self.subTest(enabled=enabled, component=component):
                        events = []
                        with bind_llm_trace(events.append):
                            for stage in ("initial", "repair"):
                                model_request(lambda *_a, **_k: {"message": {"content": "Hola mundo\nFreya funciona"}},
                                              component, "POST", "http://127.0.0.1:11434/api/chat",
                                              {"model": "fixture", "messages": [{"role": "user", "content": stage}]},
                                              timeout=1, stage=stage)
                        self.assertEqual([event["debug_prompts_enabled"] for event in events], [enabled, enabled])
                        self.assertEqual(events[1]["repair_of_llm_call_id"], events[0]["llm_call_id"])
                        if enabled:
                            self.assertEqual(events[1]["request_body"]["messages"][0]["content"], "repair")
                            self.assertEqual(events[1]["raw_response"], "Hola mundo\nFreya funciona")
                        else:
                            self.assertNotIn("request_body", events[1])
                            self.assertNotIn("raw_response", events[1])

    def test_real_component_adapters_observe_shared_flag(self):
        from control_center.task_spec import TaskSpecAnalyst
        from control_center.recovery import OllamaFailureAnalyzer, OllamaRecoveryAdvisor
        from control_center.integration import (
            OllamaGlobalVerifier, OllamaIntegrationReplanner, OllamaResultIntegrator,
        )
        reply = lambda *_a, **_k: {"message": {"content": "{}"}}
        adapters = {
            "task_analyst": lambda: TaskSpecAnalyst(model="fixture", request=reply).analyze_spec("Create saludo.txt"),
            "planner": lambda: OllamaPlanner(model="fixture", request=reply)("Create saludo.txt", {"_semantic_plan": True}),
            "evaluator": lambda: OllamaEvaluator(model="fixture", request=reply)("Review saludo.txt", {}),
            "failure_analyzer": lambda: OllamaFailureAnalyzer(model="fixture", request=reply)([]),
            "recovery_replanner": lambda: OllamaRecoveryAdvisor(model="fixture", request=reply)("Inspect fixture", {}),
            "global_verifier": lambda: OllamaGlobalVerifier(model="fixture", request=reply)("Inspect fixture", {}),
            "integration_replanner": lambda: OllamaIntegrationReplanner(model="fixture", request=reply)("Inspect fixture", {}),
            "result_integrator": lambda: OllamaResultIntegrator(model="fixture", request=reply)("Inspect fixture", {}),
        }
        for enabled in (False, True):
            initialize_settings(Settings(debug_llm_prompts=enabled))
            with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": str(not enabled)}):
                for component, call in adapters.items():
                    with self.subTest(enabled=enabled, component=component):
                        events = []
                        with bind_llm_trace(events.append):
                            call()
                        calls = [event for event in events if event["event_type"] == "llm.call"]
                        self.assertTrue(calls)
                        self.assertTrue(all(event["component"] == component for event in calls))
                        self.assertTrue(all(event["debug_prompts_enabled"] == enabled for event in calls))
                with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
                    events = []
                    answer = json.dumps({"summary": "Fixture", "actions": [], "artifacts": [],
                                         "verification": {}, "limitations": []})
                    run_task({"config": {**DEFAULT_CONFIG, "verification": {"enabled": False}},
                              "tools": [], "workspace": directory, "prompt": "Inspect fixture"},
                             Path(directory), events.append, lambda: None,
                             transport=lambda *_a, **_k: {"message": {"content": answer}})
                    calls = [item["event"] for item in events if item.get("kind") == "event"
                             and item["event"].get("event_type") == "llm.call"]
                    self.assertTrue(calls)
                    self.assertTrue(all(event["debug_prompts_enabled"] == enabled for event in calls))
        self.assertEqual(set(adapters) | {"worker"}, set(MODEL_PROFILES))

    def test_debug_does_not_change_requests_or_responses(self):
        requests, responses = [], []
        body = {"model": "fixture", "messages": [{"role": "user", "content": "Hola mundo"}],
                "options": {"temperature": 0}, "tools": []}
        def transport(_method, _url, payload, **kwargs):
            requests.append((json.loads(json.dumps(payload)), kwargs))
            return {"message": {"content": "Freya funciona"}}
        for enabled in (False, True):
            initialize_settings(Settings(debug_llm_prompts=enabled))
            with bind_llm_trace(lambda _event: None):
                responses.append(model_request(transport, "worker", "POST", "http://127.0.0.1:11434/api/chat",
                                               body, timeout=2))
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(responses[0]["message"], responses[1]["message"])
        self.assertEqual(requests[0][0], body)

    def test_production_trace_matches_effective_streamed_request(self):
        from tests.test_transport import StreamingOllama, chunk
        server = StreamingOllama([(0, chunk("Hola mundo", done=True))])
        self.addCleanup(server.close)
        initialize_settings(Settings(debug_llm_prompts=True))
        events = []
        body = {"model": "fixture", "messages": [], "stream": False, "options": {"temperature": 0}}
        with bind_llm_trace(events.append):
            model_request(request_json, "planner", "POST", server.url + "/api/chat", body,
                          timeout=1, max_output_tokens=123)
        self.assertEqual(events[0]["request_body"], server.requests[0])
        self.assertTrue(events[0]["request_body"]["stream"])
        self.assertEqual(events[0]["request_body"]["options"]["num_predict"], 123)
        self.assertFalse(body["stream"])
        self.assertEqual(events[0]["raw_response"], "Hola mundo")

    def test_evaluator_validation_and_repair_explain_actual_contract_error(self):
        from control_center.evaluator import Evaluator, EvaluationValidationError
        initialize_settings(Settings(debug_llm_prompts=True))
        events = []
        replies = iter(['{"wrong": true}', '{"criteria": []}'])
        evaluator = OllamaEvaluator(model="fixture", request=lambda *_a, **_k: {
            "message": {"content": next(replies)}})
        with bind_llm_trace(events.append):
            for repair in (False, True):
                raw = evaluator("Review Hola mundo and Freya funciona", {"_freya_repair": repair})
                if not repair:
                    with self.assertRaises(EvaluationValidationError):
                        Evaluator._parse(raw, [])
                else:
                    self.assertEqual(Evaluator._parse(raw, []), {"criteria": []})
        initial, rejected, repaired, accepted = events
        self.assertEqual(rejected["llm_call_id"], initial["llm_call_id"])
        self.assertEqual(rejected["error_type"], "EvaluationValidationError")
        self.assertIn("only criteria", rejected["validation_message"])
        self.assertEqual(rejected["raw_response"], '{"wrong": true}')
        self.assertEqual(rejected["contract_name"], "evaluator")
        self.assertEqual(rejected["contract_version"], "evaluator-v1")
        self.assertEqual(rejected["validation_stage"], "output_contract")
        self.assertEqual(repaired["repair_of_llm_call_id"], initial["llm_call_id"])
        self.assertEqual(accepted["llm_call_id"], repaired["llm_call_id"])
        self.assertEqual(accepted["normalized_response"], {"criteria": []})

    def test_validation_redacts_response_and_rule_and_off_mode_keeps_type_only(self):
        for enabled in (False, True):
            initialize_settings(Settings(debug_llm_prompts=enabled))
            events = []
            with bind_llm_trace(events.append):
                model_request(lambda *_a, **_k: {"message": {"content": 'password="fixture-password"'}},
                              "worker", "POST", "http://127.0.0.1:11434/api/chat",
                              {"model": "fixture", "messages": []}, timeout=1)
                record_validation("worker", "rejected", detail="ValueError: token=fixture-token")
            self.assertEqual(events[1]["error_type"], "ValueError")
            serialized = json.dumps(events)
            self.assertNotIn("fixture-password", serialized)
            self.assertNotIn("fixture-token", serialized)
            self.assertEqual("raw_response" in events[1], enabled)
            self.assertEqual("validation_message" in events[1], enabled)

    def test_semantic_unknown_is_not_rejected_by_readback_rationale_regex(self):
        from control_center.evaluator import Evaluator
        initialize_settings(Settings(debug_llm_prompts=True))
        events = []
        decision = {"criteria": [{"criterion": "Both lines", "status": "unknown",
                                 "reason": "No sufficient evidence available", "evidence": [], "confidence": 0.5}]}
        with bind_llm_trace(events.append):
            model_request(lambda *_a, **_k: {"message": {"content": json.dumps(decision)}}, "evaluator", "POST",
                          "http://127.0.0.1:11434/api/chat", {}, timeout=1)
            parsed = Evaluator._parse(decision, ["Both lines"])
        self.assertEqual(parsed["criteria"][0]["status"], "unknown")
        self.assertEqual(events[1]["validation_stage"], "output_contract")
        self.assertEqual(events[1]["llm_call_id"], events[0]["llm_call_id"])
        self.assertEqual(events[1]["normalized_response"], parsed)

    def test_intent_matcher_uses_shared_debug_setting(self):
        from control_center.cross_task import CrossTaskIntentMatcher
        approved = {"requested_change": "Update the greeting file text", "reason": "Complete the greeting fixture",
                    "needed_for": "Verify the requested greeting output"}
        requested = {**approved, "requested_change": "Append a second greeting line"}
        reply = '{"same_intent": true, "reason": "Equivalent fixture purpose", "confidence": 1.0}'
        for enabled in (False, True):
            initialize_settings(Settings(debug_llm_prompts=enabled))
            events = []
            matcher = CrossTaskIntentMatcher(request=lambda *_a, **_k: {"message": {"content": reply}})
            with bind_llm_trace(events.append):
                matcher.match(approved, requested)
            self.assertEqual(events[0]["prompt_name"], "intent_matcher")
            self.assertEqual(events[0]["debug_prompts_enabled"], enabled)
            self.assertEqual(events[1]["llm_call_id"], events[0]["llm_call_id"])
            self.assertEqual("raw_response" in events[0], enabled)

    def test_saludo_regression_evaluator_capture_has_evidence_criteria_schema_and_repair(self):
        initialize_settings(Settings(debug_llm_prompts=True))
        prompt = ("Crea un archivo saludo.txt con el texto Hola mundo, después agregá una segunda línea "
                  "que diga Freya funciona. Finalmente verificá que el archivo contenga ambas líneas.")
        context = {"planned_task": {"success_criteria": ["saludo.txt contains both requested lines"]},
                   "evidence_by_criterion": [{"criterion": "saludo.txt contains both requested lines",
                       "evidence": [{"type": "file_readback", "path": "saludo.txt",
                                     "output": "Hola mundo\nFreya funciona"}]}]}
        replies = iter(['{"invalid": true}', '{"criteria": []}'])
        evaluator = OllamaEvaluator(model="fixture", request=lambda *_a, **_k: {
            "message": {"content": next(replies)}})
        events = []
        with bind_llm_trace(events.append):
            evaluator(prompt, context)
            evaluator(prompt + "\nRepair the invalid contract.", {**context, "_freya_repair": True})
        for event in events:
            self.assertEqual(event["structured_context"], context)
            self.assertIn("Hola mundo", event["request_body"]["messages"][1]["content"])
            self.assertIn("Freya funciona", event["request_body"]["messages"][1]["content"])
            self.assertIn("format", event["request_body"])
        self.assertEqual(events[0]["raw_response"], '{"invalid": true}')
        self.assertEqual(events[1]["raw_response"], '{"criteria": []}')
        self.assertEqual(events[1]["repair_of_llm_call_id"], events[0]["llm_call_id"])

    def test_debug_validator_observer_never_replaces_original_exception(self):
        from control_center.llm_trace import observe_validation
        error = ValueError("original rule failure")
        @observe_validation("evaluator", validation_stage="evidence_contract")
        def validate():
            raise error
        with patch("control_center.llm_trace.record_validation", side_effect=RuntimeError("logging failed")):
            with self.assertRaises(ValueError) as rejected:
                validate()
        self.assertIs(rejected.exception, error)

    def test_default_debug_preserves_large_prompt_through_persistence_and_api(self):
        initialize_settings(Settings(debug_llm_prompts=True))
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            store = Store(Path(directory) / "state.sqlite3")
            agent = store.create_agent(normalize_agent({"name": "Full debug fixture"}))
            task = store.create_task(agent["id"], "Inspect fixture", directory)
            prompt = "Hola mundo\nFreya funciona\n" + "x" * 250000
            with bind_llm_trace(lambda event: store.append_event(task["id"], event)):
                model_request(lambda *_a, **_k: {"message": {"content": prompt}}, "evaluator", "POST",
                              "http://127.0.0.1:11434/api/chat",
                              {"model": "fixture", "messages": [{"role": "user", "content": prompt}]}, timeout=1)
            from control_center.security import sanitize
            call = sanitize(next(event for event in store.list_events(task["id"])
                                 if event["event_type"] == "llm.call"))
            self.assertEqual(call["request_body"]["messages"][0]["content"], prompt)
            self.assertEqual(call["raw_response"], prompt)
            self.assertFalse(call["request_truncation"]["truncated"])

    def test_repair_does_not_link_another_component_and_nested_traces_restore_context(self):
        initialize_settings(Settings(debug_llm_prompts=True))
        events = []
        with bind_llm_trace(events.append):
            model_request(lambda *_a, **_k: {"message": {"content": "outer"}}, "planner", "POST",
                          "http://127.0.0.1:11434/api/chat", {}, timeout=1)
            with bind_llm_trace(events.append):
                model_request(lambda *_a, **_k: {"message": {"content": "inner"}}, "evaluator", "POST",
                              "http://127.0.0.1:11434/api/chat", {}, timeout=1, stage="repair")
            record_validation("planner", "rejected", detail="ValueError: invalid plan")
        self.assertNotIn("repair_of_llm_call_id", events[1])
        self.assertEqual(events[2]["raw_response"], "outer")
        self.assertEqual(events[2]["llm_call_id"], events[0]["llm_call_id"])


if __name__ == "__main__":
    unittest.main()
