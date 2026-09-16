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
            raise RuntimeError("Ya hay un servidor usando este directorio de datos.") from exc

    def close(self):
        self.file.close()


def main():
    parser = argparse.ArgumentParser(description="Agent Control Center · panel local para Ollama")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--workers", type=int, default=2, help="Procesos de ejecución simultáneos (1–8)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.workers <= 8:
        parser.error("Usa un puerto de 1 a 65535 y entre 1 y 8 workers.")
    data_dir = args.data_dir.resolve()
    lock = InstanceLock(data_dir)
    runtime = None
    server = None
    try:
        store = Store(data_dir / "control_center.sqlite3")
        runtime = Runtime(store, data_dir, ROOT, max_workers=args.workers)
        application = Application(store, runtime, data_dir)
        server = ControlServer(("127.0.0.1", args.port), application)
        runtime.start()
        print(f"Agent Control Center: http://127.0.0.1:{args.port}", flush=True)
        print(f"Datos: {data_dir} | Workers: {args.workers} | Ctrl+C para detener", flush=True)
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nDeteniendo ejecuciones y cerrando el servidor...", flush=True)
    finally:
        if server:
            server.server_close()
        if runtime:
            runtime.shutdown()
        lock.close()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
