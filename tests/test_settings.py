"""Startup-only, fail-closed prompt debug configuration and spawn propagation."""
import io
import json
import multiprocessing
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from control_center.settings import (
    DEFAULT_DEBUG_MAX_CHARS, Settings, get_settings, initialize_settings,
    parse_bool, report_startup_config,
)


def _spawn_trace(settings, outbox):
    # Exercise the real worker bootstrap while avoiding tools and provider access.
    from control_center.llm_trace import bind_llm_trace
    from control_center.transport import model_request
    from control_center.worker import process_main
    def run_fixture(_task, _root, emit, _checkpoint, **_kwargs):
        with bind_llm_trace(lambda event: emit({"kind": "event", "event": event})):
            model_request(lambda *_a, **_k: {"message": {"content": "Freya funciona"}},
                          "worker", "POST", "http://127.0.0.1:11434/api/chat",
                          {"model": "fixture", "messages": [{"role": "user", "content": "Hola mundo"}]}, timeout=1)
        return {"status": "Success"}
    with patch("control_center.worker.run_task", run_fixture):
        process_main({"config": {}}, str(Path(__file__).parent), outbox,
                     Mock(), Mock(), Mock(), settings=settings)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("control_center.settings._settings", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_boolean_environment_values(self):
        for value in ("true", "TRUE", "True", "1", "yes", "YES", "on", "ON", " true "):
            with self.subTest(value=value):
                self.assertTrue(parse_bool(value))
                settings = Settings.from_environment({"FREYA_DEBUG_LLM_PROMPTS": value})
                self.assertTrue(settings.debug_llm_prompts)
                self.assertEqual(settings.debug_llm_prompts_source, "environment")
        for value in (None, "", " ", "false", "FALSE", "False", "0", "no", "off", "unexpected"):
            with self.subTest(value=value):
                self.assertFalse(parse_bool(value))
                env = {} if value is None else {"FREYA_DEBUG_LLM_PROMPTS": value}
                settings = Settings.from_environment(env)
                self.assertFalse(settings.debug_llm_prompts)
                self.assertEqual(settings.debug_llm_prompts_source, "default" if value is None else "environment")

    def test_limits_are_central_and_bounded(self):
        for value, expected in (("invalid", DEFAULT_DEBUG_MAX_CHARS), ("", DEFAULT_DEBUG_MAX_CHARS),
                                ("0", 1000), ("60000", 60000), ("999999999", DEFAULT_DEBUG_MAX_CHARS)):
            with self.subTest(value=value):
                self.assertEqual(Settings.from_environment({"FREYA_DEBUG_PROMPT_MAX_CHARS": value}).debug_prompt_max_chars,
                                 expected)
        self.assertEqual(Settings.from_environment({}).debug_prompt_max_chars, DEFAULT_DEBUG_MAX_CHARS)

    def test_environment_changes_require_restart(self):
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true"}):
            first = initialize_settings()
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "false"}):
            self.assertIs(get_settings(), first)
            self.assertTrue(get_settings().debug_llm_prompts)
            # Simulate the explicit next process bootstrap.
            self.assertFalse(initialize_settings().debug_llm_prompts)

    def test_startup_event_contains_only_known_non_sensitive_fields(self):
        output = io.StringIO()
        settings = Settings.from_environment({"FREYA_DEBUG_LLM_PROMPTS": "true", "OTHER_PRIVATE_VALUE": "fixture"})
        with patch("sys.stdout", output):
            report_startup_config(settings)
        lines = output.getvalue().splitlines()
        self.assertEqual(json.loads(lines[0]), {"event_type": "freya.config.loaded", "debug_llm_prompts": True,
                                              "source": "environment", "llm_provider": "ollama",
                                              "debug_prompt_max_chars": DEFAULT_DEBUG_MAX_CHARS})
        self.assertEqual(lines[1], "Debug LLM prompts: ENABLED")
        self.assertNotIn("fixture", output.getvalue())

    def test_runtime_uses_same_snapshot(self):
        from control_center.runtime import Runtime
        snapshot = initialize_settings(Settings(debug_llm_prompts=True))
        with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "false"}):
            runtime = Runtime(Mock(), Path(__file__).parent, Path(__file__).parent)
        self.assertIs(runtime.settings, snapshot)
        task = {"id": "fixture", "agent_id": "fixture", "workspace": str(Path(__file__).parent), "config": {}}
        with patch.object(runtime, "context") as context, patch.object(runtime, "_finish"), \
                patch.object(runtime, "_refresh_agent"), patch("control_center.runtime._WindowsJob"):
            runtime._spawn(task)
        self.assertIs(context.Process.call_args.kwargs["args"][-1], snapshot)

    def test_main_reports_configuration_before_runtime_creation(self):
        from contextlib import ExitStack
        from control_center.__main__ import main
        output = io.StringIO()
        def runtime_fixture(*_args, **_kwargs):
            self.assertTrue(get_settings().debug_llm_prompts)
            self.assertIn("freya.config.loaded", output.getvalue())
            return Mock()
        with ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": "true"}))
            stack.enter_context(patch("sys.stdout", output))
            stack.enter_context(patch("sys.argv", ["freya", "--data-dir", str(Path(__file__).parent),
                "--planner-offline", "--task-analyst-offline", "--evaluator-offline", "--recovery-offline",
                "--integration-offline"]))
            for name in ("InstanceLock", "Store", "Orchestrator", "Application", "ControlServer"):
                stack.enter_context(patch("control_center.__main__." + name))
            runtime = stack.enter_context(patch("control_center.__main__.Runtime", side_effect=runtime_fixture))
            main()
        runtime.assert_called_once()
        self.assertTrue(json.loads(output.getvalue().splitlines()[0])["debug_llm_prompts"])

    def test_spawned_worker_keeps_parent_snapshot_despite_different_environment(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                context = multiprocessing.get_context("spawn")
                outbox = context.Queue()
                worker = context.Process(target=_spawn_trace, args=(Settings(debug_llm_prompts=enabled), outbox))
                try:
                    with patch.dict("os.environ", {"FREYA_DEBUG_LLM_PROMPTS": str(not enabled)}):
                        worker.start()
                    message = outbox.get(timeout=15)
                    worker.join(15)
                    self.assertEqual(worker.exitcode, 0)
                    event = message["event"]
                    self.assertEqual(event["debug_prompts_enabled"], enabled)
                    self.assertEqual("request_body" in event, enabled)
                    self.assertEqual("raw_response" in event, enabled)
                finally:
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(5)
                    outbox.close()
                    outbox.join_thread()
