"""Persistent task scheduler with independently cancellable worker processes."""

from __future__ import annotations

import multiprocessing
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_center.worker import process_main
from control_center.security import sanitize


TERMINAL = {"Success", "Failed", "Cancelled"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class _WindowsJob:
    """Kill-on-close Job Object also contains grandchildren after the worker exits."""

    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes
        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]
        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IOCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        self.api = api
        self.handle = api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("Could not create worker Job Object.")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = api.OpenProcess(0x0100 | 0x0001 | 0x0400, False, pid)
        try:
            if (not process or not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                    or not api.AssignProcessToJobObject(self.handle, process)):
                raise OSError("Could not contain worker process in a Job Object.")
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                api.CloseHandle(process)

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class Runtime:
    def __init__(self, store: Any, data_dir: Path, project_root: Path, max_workers: int = 2) -> None:
        if not 1 <= max_workers <= 16:
            raise ValueError("max_workers must be between 1 and 16.")
        self.store = store
        self.data_dir = Path(data_dir).resolve()
        self.project_root = Path(project_root).resolve()
        self.max_workers = max_workers
        self.context = multiprocessing.get_context("spawn")
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = False
        self.thread: threading.Thread | None = None
        self.pending: list[str] = []
        self.active: dict[str, dict[str, Any]] = {}
        self.paused: set[str] = set()
        self.last_error: str | None = None

    def start(self) -> None:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            if self.closed:
                raise RuntimeError("Runtime is shut down.")
            self.store.recover_interrupted()
            self.thread = threading.Thread(target=self._loop, name="control-center-scheduler", daemon=True)
            self.thread.start()

    def submit(self, agent_id: str, prompt: str) -> dict[str, Any]:
        with self.lock:
            if self.closed:
                raise ValueError("Runtime is shut down.")
            agent = self.store.get_agent(agent_id)
            if not agent:
                raise KeyError(agent_id)
            if not agent.get("enabled", True):
                raise ValueError("Agent is disabled.")
            if agent_id in self.paused or agent.get("status") == "Paused":
                raise ValueError("Resume the agent before submitting a task.")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
                raise ValueError("Task prompt must contain 1–32000 characters.")
            workspace = self.data_dir / "workspaces" / uuid.uuid4().hex
            workspace.mkdir(parents=True, exist_ok=False)
            task = self.store.create_task(agent_id, prompt, str(workspace))
            self.pending.append(task["id"])
            self.store.append_event(task["id"], {"event_type": "task.queued", "level": "info", "status": "Queued",
                                                   "reason": "Esperar un trabajador disponible; cada agente ejecuta una tarea a la vez."})
            self._refresh_agent(agent_id)
            self.wake.set()
            return task

    def pause(self, agent_id: str) -> dict[str, Any]:
        with self.lock:
            self.store.get_agent(agent_id)
            self.paused.add(agent_id)
            for task_id, worker in self.active.items():
                if worker["agent_id"] == agent_id:
                    worker["pause"].set()
                    self.store.append_event(task_id, {"event_type": "task.pause_requested", "level": "info",
                                                      "reason": "Pausa solicitada: la llamada en curso termina antes de pausar; el tiempo límite sigue avanzando."})
            self._refresh_agent(agent_id)
            return self.store.get_agent(agent_id)

    def resume(self, agent_id: str) -> dict[str, Any]:
        with self.lock:
            self.store.get_agent(agent_id)
            self.paused.discard(agent_id)
            for worker in self.active.values():
                if worker["agent_id"] == agent_id:
                    worker["pause"].clear()
            self._refresh_agent(agent_id)
            self.wake.set()
            return self.store.get_agent(agent_id)

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            task = self.store.get_task(task_id)
            if task["status"] in TERMINAL:
                return task
            self._finish(task_id, {"status": "Cancelled", "error": "Cancelled by operator."})
            self.wake.set()
            return self.store.get_task(task_id)

    def restart(self, agent_id: str) -> dict[str, Any]:
        with self.lock:
            self.store.get_agent(agent_id)
            for task_id in list(self.pending) + list(self.active):
                if self.store.get_task(task_id)["agent_id"] == agent_id:
                    self._finish(task_id, {"status": "Cancelled", "error": "Agent restarted by operator."})
            self.paused.discard(agent_id)
            self._refresh_agent(agent_id)
            self.wake.set()
            return self.store.get_agent(agent_id)

    def shutdown(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for task_id in list(self.pending) + list(self.active):
                self._finish(task_id, {"status": "Cancelled", "error": "Control Center stopped."})
            self.wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=10)

    def _refresh_agent(self, agent_id: str, failed: bool = False) -> None:
        agent = self.store.get_agent(agent_id)
        if not agent:
            return
        if not agent.get("enabled", True):
            state = "Offline"
        elif agent_id in self.paused:
            state = "Paused"
        elif any(worker["agent_id"] == agent_id for worker in self.active.values()):
            state = "Running"
        elif any(self.store.get_task(task_id)["agent_id"] == agent_id for task_id in self.pending):
            state = "Waiting"
        else:
            state = "Error" if failed else "Idle"
        self.store.set_agent_state(agent_id, state)

    def _stop_worker(self, worker: dict[str, Any]) -> None:
        process = worker["process"]
        job = worker.get("job")
        if job is not None:
            job.close()
        elif os.name == "nt" and process.is_alive():
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, shell=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if process.is_alive():
            process.kill()
        process.join(timeout=3)
        worker["queue"].close()
        worker["queue"].cancel_join_thread()
        if not process.is_alive():
            process.close()

    def _finish(self, task_id: str, fields: dict[str, Any]) -> None:
        task = self.store.get_task(task_id)
        worker = self.active.pop(task_id, None)
        if task_id in self.pending:
            self.pending.remove(task_id)
        if worker:
            # Capture already emitted counters/events before terminating an in-flight call.
            try:
                for _ in range(1000):
                    message = worker["queue"].get_nowait()
                    if message.get("kind") == "update":
                        self.store.update_task(task_id, **message["fields"])
                    elif message.get("kind") == "event":
                        self.store.append_event(task_id, message["event"])
            except queue.Empty:
                pass
            fields["duration_seconds"] = round(time.monotonic() - worker["started"], 3)
            self._stop_worker(worker)
        fields.update({"finished_at": now(), "progress": 100})
        self.store.update_task(task_id, **fields)
        self.store.append_event(task_id, {"event_type": "task." + fields["status"].lower(),
                                          "status": fields["status"], "level": "error" if fields["status"] == "Failed" else "info",
                                          "error": fields.get("error", ""), "output": fields.get("result", ""),
                                          "duration_seconds": fields.get("duration_seconds", 0)})
        self._refresh_agent(task["agent_id"], failed=fields["status"] == "Failed")

    def _spawn(self, task: dict[str, Any]) -> None:
        outbox = self.context.Queue()
        pause_event, ready_event, go_event = self.context.Event(), self.context.Event(), self.context.Event()
        process = self.context.Process(target=process_main,
                                       args=(task, str(self.project_root), outbox, pause_event, ready_event, go_event),
                                       name="agent-task-" + task["id"][:12], daemon=False)
        started = time.monotonic()
        process.start()
        worker = {"process": process, "queue": outbox, "pause": pause_event, "started": started,
                  "agent_id": task["agent_id"], "max_seconds": task["config"].get("max_seconds", 600), "job": None}
        self.active[task["id"]] = worker
        self.store.update_task(task["id"], status="Running", started_at=now())
        try:
            if not ready_event.wait(timeout=min(10, worker["max_seconds"])):
                raise RuntimeError("Worker failed to initialize within the time budget.")
            if os.name == "nt":
                worker["job"] = _WindowsJob(process.pid)
            go_event.set()
            self.store.append_event(task["id"], {"event_type": "task.started", "level": "info", "status": "Running",
                                                  "reason": "Proceso local iniciado con presupuesto de tiempo y herramientas del agente."})
            self._refresh_agent(task["agent_id"])
        except Exception as exc:
            self._finish(task["id"], {"status": "Failed", "error": "Worker initialization failed: " + str(exc)})

    def _loop(self) -> None:
        while not self.closed:
            try:
                self._schedule_until_error()
            except Exception as exc:
                # A malformed task or transient persistence failure cannot kill the
                # scheduler silently and leave uncontrolled workers behind.
                self.last_error = sanitize("{}: {}".format(type(exc).__name__, exc))
                with self.lock:
                    for task_id, worker in list(self.active.items()):
                        try:
                            self._finish(task_id, {"status": "Failed", "error": "Scheduler failure: " + self.last_error})
                        except Exception:
                            self.active.pop(task_id, None)
                            self._stop_worker(worker)
                self.wake.wait(.25)
                self.wake.clear()

    def _schedule_until_error(self) -> None:
        while True:
            with self.lock:
                if self.closed:
                    return
                for task_id, worker in list(self.active.items()):
                    # Enforce independently of the worker, even during blocked tools or pause.
                    if time.monotonic() - worker["started"] >= worker["max_seconds"]:
                        self._finish(task_id, {"status": "Failed", "error": "Maximum execution time reached (including paused time)."})
                        continue
                    try:
                        for _ in range(200):
                            message = worker["queue"].get_nowait()
                            kind = message.get("kind")
                            if kind == "done":
                                self._finish(task_id, message["fields"])
                                break
                            if kind == "event":
                                self.store.append_event(task_id, message["event"])
                            elif kind == "update":
                                fields = message["fields"]
                                fields["duration_seconds"] = round(time.monotonic() - worker["started"], 3)
                                self.store.update_task(task_id, **fields)
                            elif kind in {"paused", "resumed"}:
                                status = "Paused" if kind == "paused" else "Running"
                                self.store.update_task(task_id, status=status)
                                self.store.append_event(task_id, {"event_type": "task." + kind, "level": "info", "status": status,
                                                                  "reason": "Pausa cooperativa entre llamadas." if kind == "paused" else "Ejecución reanudada."})
                    except queue.Empty:
                        pass
                    if task_id in self.active and not worker["process"].is_alive():
                        # Queue feeder may finish just after process exit; allow one tick.
                        if worker.get("exited"):
                            self._finish(task_id, {"status": "Failed", "error": "Worker exited unexpectedly."})
                        else:
                            worker["exited"] = True
                busy = {worker["agent_id"] for worker in self.active.values()}
                for task_id in list(self.pending):
                    if len(self.active) >= self.max_workers:
                        break
                    task = self.store.get_task(task_id)
                    agent = self.store.get_agent(task["agent_id"])
                    if not agent.get("enabled", True):
                        self._finish(task_id, {"status": "Cancelled", "error": "Agent was disabled before execution."})
                        continue
                    if task["agent_id"] in busy or task["agent_id"] in self.paused:
                        continue
                    self.pending.remove(task_id)
                    try:
                        self._spawn(task)
                    except Exception as exc:
                        self._finish(task_id, {"status": "Failed", "error": "Worker launch failed: " + str(exc)})
                    busy.add(task["agent_id"])
            self.wake.wait(.05)
            self.wake.clear()
