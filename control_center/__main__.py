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
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.workers <= 8:
        parser.error("Use a port from 1 to 65535 and between 1 and 8 workers.")
    data_dir = args.data_dir.resolve()
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
        orchestrator = Orchestrator(store, runtime, planner=planner)
        application = Application(store, runtime, data_dir, orchestrator)
        server = ControlServer(("127.0.0.1", args.port), application)
        runtime.start()
        print(f"Agent Control Center: http://127.0.0.1:{args.port}", flush=True)
        print(f"Data: {data_dir} | Workers: {args.workers} | Ctrl+C to stop", flush=True)
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
