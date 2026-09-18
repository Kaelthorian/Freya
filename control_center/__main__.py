"""Run locally: python -m control_center --port 8765."""

from __future__ import annotations

import argparse
import multiprocessing
import os
from pathlib import Path

from .api import Application
from .http import ControlServer
from .runtime import Runtime
from .storage import Store
from .orchestrator import Orchestrator
from .planner import (DEFAULT_PLANNER_ENDPOINT, DEFAULT_PLANNER_MODEL,
                      DEFAULT_PLANNER_TIMEOUT_SECONDS, OllamaPlanner, Planner)
from .evaluator import (DEFAULT_EVALUATOR_ENDPOINT, DEFAULT_EVALUATOR_MODEL,
                        DEFAULT_EVALUATOR_TIMEOUT_SECONDS, Evaluator, OllamaEvaluator)
from .recovery import (DEFAULT_RECOVERY_ENDPOINT, DEFAULT_RECOVERY_MODEL,
                       DEFAULT_RECOVERY_TIMEOUT_SECONDS, OllamaRecoveryAdvisor,
                       RecoveryController, Replanner)

ROOT = Path(__file__).resolve().parent.parent


class InstanceLock:
    """Prevent a second scheduler from recovering/claiming the same database."""

    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / "server.lock").open("a+b")
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("Another server is already using this data directory.") from exc

    def close(self):
        self.file.close()


def main():
    parser = argparse.ArgumentParser(description="Agent Control Center - local Ollama platform")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--workers", type=int, default=2, help="Concurrent worker processes (1-8)")
    parser.add_argument("--planner-model", default=DEFAULT_PLANNER_MODEL,
                        help="Ollama model used for structured planning")
    parser.add_argument("--planner-endpoint", default=DEFAULT_PLANNER_ENDPOINT,
                        help="Loopback Ollama base URL used by the planner")
    parser.add_argument("--planner-timeout", type=float, default=DEFAULT_PLANNER_TIMEOUT_SECONDS,
                        help="Planner Ollama request timeout in seconds (0.1-120)")
    parser.add_argument("--planner-offline", action="store_true",
                        help="Explicitly use deterministic one-task fallback planning")
    parser.add_argument("--evaluator-model", default=DEFAULT_EVALUATOR_MODEL,
                        help="Ollama model used for semantic evaluation")
    parser.add_argument("--evaluator-endpoint", default=DEFAULT_EVALUATOR_ENDPOINT,
                        help="Loopback Ollama base URL used by the evaluator")
    parser.add_argument("--evaluator-timeout", type=float,
                        default=DEFAULT_EVALUATOR_TIMEOUT_SECONDS,
                        help="Evaluator Ollama request timeout in seconds (0.1-120)")
    parser.add_argument("--evaluator-offline", action="store_true",
                        help="Explicitly use deterministic evidence-only evaluation")
    parser.add_argument("--max-parallel-tasks", type=int, default=4,
                        help="Maximum concurrently active orchestration graph nodes (1-20)")
    parser.add_argument("--recovery-model", default=DEFAULT_RECOVERY_MODEL,
                        help="Ollama model used for bounded semantic recovery")
    parser.add_argument("--recovery-endpoint", default=DEFAULT_RECOVERY_ENDPOINT,
                        help="Loopback Ollama base URL used by the recovery advisor")
    parser.add_argument("--recovery-timeout", type=float,
                        default=DEFAULT_RECOVERY_TIMEOUT_SECONDS,
                        help="Recovery Ollama request timeout in seconds (0.1-120)")
    parser.add_argument("--recovery-offline", action="store_true",
                        help="Fail conservatively instead of calling a recovery model")
    parser.add_argument("--max-semantic-attempts", type=int, default=3,
                        help="Maximum execution/evaluation attempts per planned task (1-10)")
    parser.add_argument("--max-plan-revisions", type=int, default=2,
                        help="Maximum effective-plan revisions per orchestration (1-10)")
    parser.add_argument("--max-recovery-actions", type=int, default=8,
                        help="Maximum recovery decisions per orchestration (1-50)")
    parser.add_argument("--max-recovery-model-calls", type=int, default=16,
                        help="Maximum recovery and replanning model calls per orchestration (1-100)")
    parser.add_argument("--max-delegated-tasks", type=int, default=20,
                        help="Maximum planned tasks accepted by one orchestration (1-20)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.workers <= 8:
        parser.error("Use a port from 1 to 65535 and between 1 and 8 workers.")
    if (not 1 <= args.max_parallel_tasks <= 20
            or not 1 <= args.max_delegated_tasks <= 20):
        parser.error("Orchestration task limits must be between 1 and 20.")
    data_dir = args.data_dir.resolve()
    if (not 1 <= args.max_semantic_attempts <= 10
            or not 1 <= args.max_plan_revisions <= 10
            or not 1 <= args.max_recovery_actions <= 50
            or not 1 <= args.max_recovery_model_calls <= 100):
        parser.error("Recovery limits are outside their supported ranges.")
    lock = InstanceLock(data_dir)
    runtime = None
    server = None
    try:
        store = Store(data_dir / "control_center.sqlite3")
        store.recover_interrupted_orchestrations()
        runtime = Runtime(store, data_dir, ROOT, max_workers=args.workers)
        planner = (Planner(offline=True) if args.planner_offline else
                   Planner(OllamaPlanner(args.planner_model, args.planner_endpoint,
                                         args.planner_timeout)))
        evaluator = (Evaluator(offline=True) if args.evaluator_offline else
                     Evaluator(OllamaEvaluator(args.evaluator_model, args.evaluator_endpoint,
                                               args.evaluator_timeout)))
        recovery_adapter = None if args.recovery_offline else OllamaRecoveryAdvisor(
            args.recovery_model, args.recovery_endpoint, args.recovery_timeout,
        )
        recovery = (RecoveryController(offline=True) if recovery_adapter is None else
                    RecoveryController(recovery_adapter))
        replanner = Replanner(recovery_adapter)
        orchestrator = Orchestrator(store, runtime, planner=planner, evaluator=evaluator,
                                    recovery=recovery, replanner=replanner, config={
            "max_parallel_tasks": args.max_parallel_tasks,
            "max_delegated_tasks": args.max_delegated_tasks,
            "max_semantic_attempts_per_task": args.max_semantic_attempts,
            "max_plan_revisions": args.max_plan_revisions,
            "max_recovery_actions": args.max_recovery_actions,
            "max_recovery_model_calls": args.max_recovery_model_calls,
        })
        application = Application(store, runtime, data_dir, orchestrator)
        server = ControlServer(("127.0.0.1", args.port), application)
        runtime.start()
        print(f"Agent Control Center: http://127.0.0.1:{args.port}", flush=True)
        print(f"Data: {data_dir} | Workers: {args.workers} | "
              f"Graph parallelism: {orchestrator.config['max_parallel_tasks']} | Ctrl+C to stop",
              flush=True)
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nStopping executions and shutting down the server...", flush=True)
    finally:
        if server:
            server.server_close()
        if runtime:
            runtime.shutdown()
        lock.close()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
