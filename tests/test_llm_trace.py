"""Prompt observability stays bounded and redacted across model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from control_center.llm_trace import bind_llm_trace, record_validation
from control_center.transport import model_request
from control_center.config import normalize_agent
from control_center.storage import Store
from control_center.config import DEFAULT_CONFIG
from control_center.evaluator import OllamaEvaluator
from control_center.planner import OllamaPlanner
from control_center.worker import run_task


class LlmTraceTests(unittest.TestCase):
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
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true"}):
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
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true"}):
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


if __name__ == "__main__":
    unittest.main()
