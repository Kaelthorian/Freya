"""HTTP-independent application API. The process runtime owns execution state."""

from __future__ import annotations

import threading
from pathlib import Path

from . import __version__
from .config import DEFAULT_CONFIG, DEFAULT_TOOLS, TOOL_CATALOG, normalize_agent, validate_endpoint
from .security import sanitize

LIVE = {"Queued", "Running", "Paused"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Application:
    def __init__(self, store, runtime, data_dir: Path):
        self.store = store
        self.runtime = runtime
        self.data_dir = data_dir.resolve()
        self.lock = threading.RLock()
        self._psutil = None
        try:
            import psutil
            psutil.cpu_percent()
            self._psutil = psutil
        except ImportError:
            pass

    def system(self) -> dict:
        if self._psutil is None:
            return {"available": False, "cpu_percent": None, "ram_percent": None,
                    "ram_used_bytes": None, "ram_total_bytes": None}
        memory = self._psutil.virtual_memory()
        return {"available": True, "cpu_percent": self._psutil.cpu_percent(),
                "ram_percent": memory.percent, "ram_used_bytes": memory.used,
                "ram_total_bytes": memory.total}

    def _idle_required(self, agent_id):
        tasks = self.store.list_tasks(agent_id=agent_id, limit=10000)
        if any(task["status"] in LIVE for task in tasks):
            raise ApiError(409, "Cancela o completa las tareas pendientes antes de editar o eliminar el agente.")

    def _agent_event(self, agent, event_type):
        self.store.append_event(None, {"agent_id": agent["id"], "event_type": event_type,
                                     "level": "INFO", "status": agent["status"],
                                     "output": {"name": agent["name"]}})

    def dispatch(self, method: str, path: str, query: dict, body: dict) -> tuple[int, object]:
        """Mutations serialize validation and dispatch; readers use SQLite snapshots."""
        if method == "GET":
            return 200, sanitize(self._get(path, query))
        with self.lock:
            status, value = self._mutate(method, path, body)
            return status, sanitize(value)

    def _get(self, path, query):
        parts = path.strip("/").split("/")[1:]
        if parts == ["health"]:
            return {"status": "ok", "version": __version__, "database": "sqlite", "sse": True,
                    "runtime": {"max_workers": self.runtime.max_workers}, "system": self.system()}
        if parts == ["config"]:
            return {"defaults": DEFAULT_CONFIG, "default_tools": DEFAULT_TOOLS,
                    "data_dir": str(self.data_dir), "workspaces_dir": str(self.data_dir / "workspaces"),
                    "capabilities": {"ollama": True, "sse": True, "pause": "between_actions",
                                     "cancel": True, "external_workers": False, "remote_models": False,
                                     "scheduling": False, "rag": False, "teams": False}}
        if parts == ["tools"]:
            return TOOL_CATALOG
        if parts == ["models"]:
            from .transport import request_json
            endpoint = validate_endpoint(query.get("endpoint", DEFAULT_CONFIG["endpoint"]))
            try:
                response = request_json("GET", endpoint + "/api/tags", timeout=4)
                models = response.get("models", [])
                if not isinstance(models, list):
                    raise ValueError("Respuesta de modelos inválida.")
                return {"models": models, "error": None}
            except Exception as exc:
                return {"models": [], "error": str(exc)}
        if parts == ["agents"]:
            return self.store.list_agents()
        if len(parts) == 2 and parts[0] == "agents":
            return self.store.get_agent(parts[1])
        if parts == ["tasks"]:
            return self.store.list_tasks(agent_id=query.get("agent_id") or None,
                                         status=query.get("status") or None, limit=self._limit(query))
        if len(parts) == 2 and parts[0] == "tasks":
            task = self.store.get_task(parts[1])
            task["timeline"] = self.store.list_steps(parts[1])
            task["events"] = self.store.list_events(task_id=parts[1], limit=10000)
            return task
        if parts == ["logs"]:
            filters = {k: v for k, v in query.items() if k in {"agent_id", "level", "tool", "date_from", "date_to"} and v}
            filters["error_only"] = query.get("error_only", "").lower() in {"true", "1"}
            filters["newest"] = "after" not in query
            return self.store.list_events(task_id=query.get("task_id") or None, after=int(query.get("after", 0)),
                                          limit=self._limit(query), **filters)
        if parts == ["metrics"]:
            return self.store.metrics(agent_id=query.get("agent_id") or None)
        raise ApiError(404, "Ruta no encontrada.")

    @staticmethod
    def _limit(query):
        limit = int(query.get("limit", 200))
        if limit < 1 or limit > 10000:
            raise ValueError("limit debe estar entre 1 y 10000.")
        return limit

    def _mutate(self, method, path, body):
        parts = path.strip("/").split("/")[1:]
        if method == "POST" and parts == ["agents"]:
            agent = self.store.create_agent(normalize_agent(body))
            self._agent_event(agent, "agent.created")
            return 201, agent
        if len(parts) >= 2 and parts[0] == "agents":
            agent_id = parts[1]
            agent = self.store.get_agent(agent_id)
            if method == "PATCH" and len(parts) == 2:
                self._idle_required(agent_id)
                updated = normalize_agent(body, agent)
                agent = self.store.update_agent(agent_id, updated)
                if "enabled" in body:
                    self.runtime.resume(agent_id)
                agent = self.store.get_agent(agent_id)
                self._agent_event(agent, "agent.updated")
                return 200, agent
            if method == "DELETE" and len(parts) == 2:
                self._idle_required(agent_id)
                self._agent_event(agent, "agent.deleted")
                self.store.delete_agent(agent_id)
                return 200, {"deleted": True, "id": agent_id}
            if method == "POST" and len(parts) == 3:
                action = parts[2]
                if action == "duplicate":
                    data = {k: agent[k] for k in ("name", "description", "role", "enabled", "config", "tools")}
                    data["name"] = data["name"][:92] + " (copia)"
                    new = self.store.create_agent(normalize_agent(data))
                    self._agent_event(new, "agent.created")
                    return 201, new
                if action in {"pause", "resume", "restart"}:
                    getattr(self.runtime, action)(agent_id)
                    agent = self.store.get_agent(agent_id)
                    self._agent_event(agent, "agent." + action)
                    return 200, agent
                if action == "tasks":
                    if set(body) != {"prompt"} or not isinstance(body["prompt"], str) or not body["prompt"].strip() or len(body["prompt"]) > 32000:
                        raise ValueError("Envía un prompt no vacío de hasta 32000 caracteres.")
                    if not agent["enabled"] or agent["status"] in {"Paused", "Offline"}:
                        raise ApiError(409, "Activa y reanuda el agente antes de asignar una tarea.")
                    return 201, self.runtime.submit(agent_id, body["prompt"].strip())
        if method == "POST" and len(parts) == 3 and parts[0] == "tasks":
            task = self.store.get_task(parts[1])
            if parts[2] == "cancel":
                if task["status"] not in LIVE:
                    raise ApiError(409, "La tarea ya terminó.")
                self.runtime.cancel(task["id"])
                return 200, self.store.get_task(task["id"])
            if parts[2] == "retry":
                if task["status"] in LIVE:
                    raise ApiError(409, "La tarea sigue activa.")
                agent = self.store.get_agent(task["agent_id"])
                if not agent["enabled"] or agent["status"] in {"Paused", "Offline"}:
                    raise ApiError(409, "Activa y reanuda el agente antes de reintentar.")
                return 201, self.runtime.submit(agent["id"], task["prompt"])
        raise ApiError(404, "Ruta o método no disponible.")
