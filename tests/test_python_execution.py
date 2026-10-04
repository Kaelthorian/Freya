"""Real venv/process coverage; dependency control tests never require PyPI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

from control_center.python_execution import (PythonExecutionManager, ExecutionFailure,
    cleanup_runtime_environments, controlled_process)
from control_center.tools import Toolbox
from control_center.final_state import final_verification_facts
from control_center.settings import Settings


TEMPERATURE_REQUEST = """Creá un archivo temperature_converter.py que convierta grados Celsius a Fahrenheit.
El programa debe pedir una temperatura por consola, mostrar el resultado con 2
decimales y manejar correctamente entradas inválidas. Después ejecutalo al menos
con 0, 100 y una entrada inválida para verificar que funciona."""
CONVERTER = '''import os
print("pid=" + str(os.getpid()))
try:
    value = float(input("Celsius: "))
    print(f"Fahrenheit: {value * 9 / 5 + 32:.2f}")
except ValueError:
    print("Invalid input. Please enter a valid number.")
'''


class PythonExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.events = []
        self.backend = PythonExecutionManager(self.root / "runtime_envs", "run-1", self.events.append)
        self.box = Toolbox(self.root, self.workspace, orchestration_id="run-1",
                           runtime_env_root=self.backend.root, execution_event=self.events.append)

    def test_lazy_creation_and_temperature_regression_three_real_processes(self):
        self.assertIn("0, 100", TEMPERATURE_REQUEST)
        result = self.box.invoke("write_file", {"path": "temperature_converter.py", "content": CONVERTER})
        self.assertTrue(result.success)
        self.assertFalse(self.backend.directory.exists())
        rows, pids = [], []
        with patch("control_center.tools.run_in_sandbox", side_effect=AssertionError("Docker used")):
            for case, input_value, expected in [("zero", "0", "32.00"), ("hundred", "100", "212.00"),
                                               ("invalid", "abc", "Invalid input.")]:
                result = self.box.invoke("run_command", {"argv": ["python", "temperature_converter.py"], "stdin": input_value})
                self.assertTrue(result.success, result.output)
                self.assertTrue(result.program_started)
                self.assertIn(expected, result.stdout)
                pids.append(result.stdout.splitlines()[0])
                rows.append({"tool": "run_command", "case_id": case,
                    "arguments": {"argv": ["python", "temperature_converter.py"]},
                    "exit_code": result.exit_code, "stdout": result.stdout, "success": result.success,
                    "program_started": result.program_started, "environment_available": result.environment_available})
        self.assertEqual(len(set(pids)), 3)
        facts = final_verification_facts([{"id": "qa", "result": {"actions": rows}}])
        self.assertEqual(len(facts), 3)
        self.assertTrue(all(row["status"] == "passed" for row in facts))
        self.assertEqual(sum(e["event_type"] == "python_env.created" for e in self.events), 1)
        self.assertFalse(any("dependencies" in e["event_type"] for e in self.events))
        state = self.backend.read_state()
        self.assertEqual(state["orchestration_id"], "run-1")
        self.assertTrue(Path(state["host_python"]).is_absolute())
        self.assertFalse((self.workspace / ".venv").exists())
        self.backend.cleanup()
        self.assertFalse(self.backend.directory.exists())

    def fake_environment(self):
        self.backend.directory.mkdir(parents=True)
        self.backend.python.parent.mkdir(parents=True)
        self.backend.python.touch()
        self.backend.save_state({"python_version": "3.11", "dependency_state": {}})

    def test_requirements_cache_change_and_recovery_reuse(self):
        self.fake_environment()
        manifest = self.workspace / "requirements.txt"
        manifest.write_text("declared-package==1.0\n")
        (self.workspace / "requirements-other.txt").write_text("never-install")
        invocations = []
        def process(argv, cwd, timeout, stdin=None):
            invocations.append(argv)
            return subprocess.CompletedProcess(argv, 0, "ok", "")
        with patch("control_center.python_execution.controlled_process", side_effect=process):
            for _ in range(2):
                self.backend.run(self.workspace, ["app.py"], 5)
            recovered = PythonExecutionManager(self.backend.root, "run-1")
            recovered.run(self.workspace, ["app.py"], 5)
            self.assertEqual(sum("pip" in argv for argv in invocations), 1)
            manifest.write_text("declared-package==2.0\n")
            recovered.run(self.workspace, ["app.py"], 5)
        self.assertEqual(sum("pip" in argv for argv in invocations), 2)
        self.assertTrue(all(argv[0] == str(self.backend.python) for argv in invocations))

    def test_missing_python_is_environment_evidence_not_exit_one(self):
        (self.workspace / "app.py").write_text("print('ok')")
        with patch("control_center.python_execution.resolve_host_python",
                   side_effect=ExecutionFailure("PYTHON_NOT_AVAILABLE", "missing Python")):
            result = self.box.invoke("run_command", {"argv": ["python", "app.py"]})
        self.assertFalse(result.success)
        self.assertIsNone(result.exit_code)
        self.assertFalse(result.program_started)
        self.assertEqual(result.error_class, "PYTHON_NOT_AVAILABLE")
        facts = final_verification_facts([{"id": "qa", "result": {"actions": [{
            "tool": "run_command", "arguments": {"argv": ["python", "app.py"]},
            "success": False, "exit_code": None, "program_started": False, "error_class": result.error_class}]}}])
        self.assertEqual(facts[0]["kind"], "execution_environment")
        self.assertEqual(facts[0]["status"], "unavailable")
        self.backend.cleanup()

    def test_dependency_failure_preserves_streams_and_never_starts_program(self):
        self.fake_environment()
        (self.workspace / "requirements.txt").write_text("nonexistent-package")
        with patch("control_center.python_execution.controlled_process", return_value=
                   subprocess.CompletedProcess([], 1, "resolver details", "wheel incompatible")) as process:
            with self.assertRaises(ExecutionFailure) as failure:
                self.backend.run(self.workspace, ["app.py"], 5)
        self.assertEqual(failure.exception.error_class, "DEPENDENCY_SETUP_FAILED")
        self.assertIn("requirements.txt", str(failure.exception))
        self.assertEqual(failure.exception.stderr, "wheel incompatible")
        self.assertFalse(failure.exception.program_started)
        self.assertEqual(process.call_count, 1)

    def test_real_timeout_kills_process_and_is_not_program_exit(self):
        (self.workspace / "sleep.py").write_text("import time; print('started', flush=True); time.sleep(30)")
        result = self.box.invoke("run_command", {"argv": ["python", "sleep.py"], "timeout_seconds": 1})
        self.assertEqual(result.error_class, "PROCESS_TIMEOUT")
        self.assertTrue(result.program_started)
        self.assertTrue(result.environment_available)
        self.assertIsNone(result.exit_code)
        self.assertIn("started", result.stdout)
        self.backend.cleanup()
        self.assertFalse(self.backend.directory.exists())

    def test_terminal_cleanup_and_old_active_environment_is_preserved(self):
        self.fake_environment()
        old = time.time() - 172800
        os.utime(self.backend.directory, (old, old))
        cleanup_runtime_environments(self.backend.root, lambda owner: "Running", self.events.append)
        self.assertTrue(self.backend.directory.exists())
        cleanup_runtime_environments(self.backend.root, lambda owner: "Failed", self.events.append)
        self.assertFalse(self.backend.directory.exists())
        self.assertTrue(any(e["event_type"] == "python_env.cleanup.completed" for e in self.events))

    def test_stale_orphan_cleanup_and_recent_orphan_retained(self):
        self.fake_environment()
        cleanup_runtime_environments(self.backend.root, lambda owner: None, self.events.append)
        self.assertTrue(self.backend.directory.exists())
        old = time.time() - 172800
        os.utime(self.backend.directory, (old, old))
        cleanup_runtime_environments(self.backend.root, lambda owner: None, self.events.append)
        self.assertFalse(self.backend.directory.exists())

    def test_manifest_selection_and_module_tool_dependencies(self):
        for name in ("requirements.txt", "requirements-test.txt", "requirements-dev.txt", "requirements-other.txt"):
            (self.workspace / name).write_text("package")
        sources = self.backend.dependency_sources(self.workspace, ["-m", "pytest"])
        self.assertEqual([item[0] for item in sources], ["requirements.txt", "requirements-test.txt",
                         "requirements-dev.txt", "execution-tool:pytest"])
        self.assertEqual([s[0] for s in self.backend.dependency_sources(self.workspace, ["app.py"])], ["requirements.txt"])

    def test_pyproject_legacy_and_explicit_dependencies(self):
        (self.workspace / "pyproject.toml").write_text('[tool.ruff]\nline-length = 88\n')
        self.assertEqual(self.backend.dependency_sources(self.workspace, ["app.py"]), [])
        (self.workspace / "pyproject.toml").write_text('[project]\nname="app"\nversion="1"\n')
        self.assertEqual(self.backend.dependency_sources(self.workspace, ["app.py"])[0][1], ["."])
        (self.workspace / "pyproject.toml").unlink()
        (self.workspace / "setup.cfg").write_text('[metadata]\nname=app\n')
        self.assertEqual(self.backend.dependency_sources(self.workspace, ["app.py"])[0][0], "project:.")
        self.backend.dependencies = ("requests==2.32.3",)
        self.assertEqual(self.backend.dependency_sources(self.workspace, ["app.py"])[-1][1], ["requests==2.32.3"])

    def test_missing_module_retry_once_only_with_declaration(self):
        self.fake_environment()
        failure = subprocess.CompletedProcess([], 1, "", "ModuleNotFoundError: missing")
        with patch("control_center.python_execution.controlled_process", return_value=failure) as process:
            self.backend.run(self.workspace, ["app.py"], 5)
            self.assertEqual(process.call_count, 1)
        (self.workspace / "requirements.txt").write_text("declared-package")
        success = subprocess.CompletedProcess([], 0, "ok", "")
        with patch("control_center.python_execution.controlled_process", side_effect=[success, failure, success, success]) as process:
            result = self.backend.run(self.workspace, ["app.py"], 5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(process.call_count, 4)

    def test_backend_setting_docker_never_selected_implicitly(self):
        self.assertEqual(Settings.from_environment({}).python_execution_backend, "venv")
        with self.assertRaises(ValueError):
            Settings.from_environment({"FREYA_PYTHON_EXECUTION_BACKEND": "docker"})

    def test_real_pip_requirements_local_wheel_and_cache(self):
        wheel = self.workspace / "freya_fixture-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("freya_fixture/__init__.py", "VALUE = 'dependency works'\n")
            archive.writestr("freya_fixture-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: freya-fixture\nVersion: 1.0\n")
            archive.writestr("freya_fixture-1.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nGenerator: freya-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr("freya_fixture-1.0.dist-info/RECORD", "")
        (self.workspace / "requirements.txt").write_text("--no-index\n./" + wheel.name + "\n")
        (self.workspace / "app.py").write_text("from freya_fixture import VALUE; print(VALUE)")
        for _ in range(2):
            result = self.box.invoke("run_command", {"argv": ["python", "app.py"]})
            self.assertTrue(result.success, result.output)
            self.assertIn("dependency works", result.stdout)
        self.assertEqual(sum(e["event_type"] == "python_env.dependencies.started" for e in self.events), 1)
        self.backend.cleanup()
        self.assertFalse(self.backend.directory.exists())

    def test_nested_manifest_change_and_declared_project_extras(self):
        (self.workspace / "requirements.txt").write_text("-r base.txt\n")
        nested = self.workspace / "base.txt"
        nested.write_text("one-package==1\n")
        before = self.backend.dependency_sources(self.workspace, ["app.py"])[0][2]
        nested.write_text("one-package==2\n")
        self.assertNotEqual(before, self.backend.dependency_sources(self.workspace, ["app.py"])[0][2])
        (self.workspace / "pyproject.toml").write_text('[project]\nname="example"\n[project.optional-dependencies]\ntest = ["pytest"]\ndev = ["ruff"]\n')
        sources = self.backend.dependency_sources(self.workspace, ["-m", "pytest"])
        self.assertIn(".[test,dev]", [args[0] for _, args, _ in sources])

    def test_real_unittest_and_compile_use_same_venv_without_pip(self):
        (self.workspace / "test_example.py").write_text(
            "import unittest\nclass Example(unittest.TestCase):\n    def test_add(self): self.assertEqual(1+1,2)\n")
        for argv in (["python", "-m", "unittest", "discover"],
                     ["python", "-m", "py_compile", "test_example.py"]):
            result = self.box.invoke("run_command", {"argv": argv})
            self.assertTrue(result.success, result.output)
        self.assertEqual(sum(e["event_type"] == "python_env.created" for e in self.events), 1)
        self.assertFalse(any("dependencies" in e["event_type"] for e in self.events))
        self.backend.cleanup()

    def test_ruff_normalizes_to_python_module_and_has_policy_capability(self):
        from control_center.capabilities import CapabilityResolver
        for argv in (["ruff", "check", "."], ["python", "-m", "ruff", "check", "."]):
            self.assertEqual(CapabilityResolver(self.workspace).resolve("run_command", {"argv": argv}), "execution.ruff")
            with patch.object(self.box.python_execution, "run", return_value=
                              subprocess.CompletedProcess([], 0, "All checks passed", "")) as execution:
                self.assertTrue(self.box.invoke("run_command", {"argv": argv}).success)
            self.assertEqual(execution.call_args.args[1], ["-m", "ruff", "check", "."])

    def test_backend_metadata_corruption_is_environment_failure(self):
        self.fake_environment()
        (self.backend.directory / "environment.json").write_text("broken")
        (self.workspace / "app.py").write_text("print('not run')")
        result = self.box.invoke("run_command", {"argv": ["python", "app.py"]})
        self.assertEqual(result.error_class, "ENVIRONMENT_UNAVAILABLE")
        self.assertFalse(result.program_started)

    def test_snapshot_does_not_include_runtime_infrastructure(self):
        from control_center.final_state import build_final_state
        snapshot = build_final_state({"owned_paths": ["data/runtime_envs/run-1/environment.json"]}, [], str(self.root))
        self.assertEqual(snapshot["files"], [])

    def test_orchestrator_finally_preserves_recovery_and_cleans_terminal_states(self):
        from types import SimpleNamespace
        from control_center.storage import Store
        from control_center.orchestrator import Orchestrator
        store = Store(self.root / "state.db")
        orchestrator = Orchestrator(store, SimpleNamespace(data_dir=self.root))
        for terminal in ("Success", "Failed", "Cancelled"):
            run = store.create_orchestration(TEMPERATURE_REQUEST)
            backend = PythonExecutionManager(self.backend.root, run["id"])
            backend.directory.mkdir(parents=True)
            (backend.directory / "sentinel").write_text("environment retained for Recovery")
            store.transition_orchestration(run["id"], ["Queued"], "Planning")
            with patch.object(orchestrator, "_run_impl", return_value=None):
                orchestrator._run(run["id"])
            self.assertTrue(backend.directory.exists())
            store.transition_orchestration(run["id"], ["Planning"], terminal, legacy_without_graph=True)
            with patch.object(orchestrator, "_run_impl", side_effect=RuntimeError("fatal")):
                with self.assertRaises(RuntimeError):
                    orchestrator._run(run["id"])
            self.assertFalse(backend.directory.exists())

    def test_real_worker_temperature_cases_and_evaluator_receive_program_facts(self):
        import copy
        from control_center.config import DEFAULT_CONFIG
        from control_center.worker import run_task
        from control_center.final_state import build_final_state
        from control_center.evaluator import Evaluator
        from tests.test_control_runtime import answer
        from tests.test_evaluator import planned, runtime, node, semantic
        (self.workspace / "temperature_converter.py").write_text(CONVERTER)
        criterion = "The script runs correctly for 0, 100 and invalid input."
        criterion_id = "lc-temperature"
        cases = [
            {"id": "zero", "input": "0", "supports_acceptance_criterion_ids": [criterion_id]},
            {"id": "hundred", "input": "100", "supports_acceptance_criterion_ids": [criterion_id]},
            {"id": "invalid", "input": "abc", "supports_acceptance_criterion_ids": [criterion_id]},
        ]
        config = {**copy.deepcopy(DEFAULT_CONFIG), "permissions": "execute",
                  "output": {"format": "structured", "include": ["summary", "actions", "artifacts", "verification", "limitations"]},
                  "verification_mode": "independent_cases", "verification_cases": cases,
                  "provenance": {"orchestration_id": "run-1"},
                  "runtime_context": {"python_runtime_env_root": str(self.backend.root)}}
        task = {"id": "qa", "prompt": TEMPERATURE_REQUEST, "workspace": str(self.workspace),
                "tools": ["run_command"], "config": config}
        result = run_task(task, self.root, self.events.append, lambda: None,
            transport=lambda *args, **kwargs: answer(calls=[("run_command", {"argv": ["python", "temperature_converter.py"]})]))
        self.assertEqual(result["status"], "Success", result["error"])
        self.assertEqual(len(result["result"]["actions"]), 3)
        plan = {**planned([criterion]), "required_capabilities": ["execution.python_script"], "required_tools": ["run_command"]}
        plan["acceptance_criteria"] = [{"id": criterion_id, "criterion": criterion}]
        run = runtime(result=result["result"], verification=result["verification"])
        run["final_state"] = build_final_state(plan, [run], str(self.workspace))
        seen = []
        def evaluate(prompt, context):
            seen.append(context)
            criterion_row = next(item for item in context["semantic_criteria"]
                                 if item["criterion"] == criterion)
            facts = [item for item in criterion_row["evidence"]
                     if item.get("type") == "command_execution"]
            self.assertEqual(len(facts), 3)
            self.assertTrue(all(fact["program_started"] and fact["environment_available"] for fact in facts))
            output_by_case = {fact["case_id"]: fact["stdout"] for fact in facts}
            self.assertIn("32.00", output_by_case["zero"])
            self.assertIn("212.00", output_by_case["hundred"])
            self.assertIn("Invalid input.", output_by_case["invalid"])
            response = semantic([criterion])
            response["criteria"][0]["evidence"] = [fact["evidence_id"] for fact in facts]
            return response
        outcome = Evaluator(evaluate).evaluate(planned_task=plan, runtime_task=run, execution_node=node())
        self.assertEqual(outcome["status"], "accepted")
        self.assertEqual(len(seen), 1)
        self.backend.cleanup()
        self.assertFalse(self.backend.directory.exists())


if __name__ == "__main__":
    unittest.main()
