"""Validated configuration shared by the API, tool registry and workers."""

from __future__ import annotations

import copy
import ipaddress
import math
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

TOOL_CATALOG = [
    {"name": name, "description": description, "available": available, "dangerous": dangerous}
    for name, description, available, dangerous in (
        ("list_files", "Listar archivos del workspace permitido", True, False),
        ("read_file", "Leer un archivo de texto", True, False),
        ("write_file", "Crear o sobrescribir un archivo", True, False),
        ("edit_file", "Reemplazar una coincidencia exacta", True, False),
        ("search_code", "Buscar texto en el workspace", True, False),
        ("git_diff", "Consultar cambios de Git en el workspace", True, False),
        ("run_command", "Ejecutar Python, tests, Ruff o consultas Git permitidas", True, True),
        ("web_search", "Búsqueda web · integración pendiente", False, False),
        ("browser", "Navegador · integración pendiente", False, False),
        ("http_request", "HTTP genérico · integración pendiente", False, False),
        ("database", "Consultas a bases de datos · integración pendiente", False, False),
    )
]
DEFAULT_TOOLS = ["list_files", "read_file", "write_file", "edit_file", "search_code"]
DEFAULT_CONFIG = {
    "model": "qwen2.5-coder:7b",
    "endpoint": "http://127.0.0.1:11434",
    "temperature": 0.0,
    "context_window": 8192,
    "max_tokens": 32000,
    "max_steps": 20,
    "max_seconds": 600,
    "max_model_calls": 20,
    "max_tool_calls": 40,
    "retries": 1,
    "system_prompt": "Eres un agente de programación. Inspecciona antes de editar, usa las herramientas disponibles y resume los resultados con precisión.",
    "permissions": "workspace",
    "allowed_directories": ["."],
    "forbidden_commands": [],
    "secret_env": "",
    "workspace_path": "",
}
LIMITS = {
    "context_window": (512, 131072), "max_tokens": (128, 1000000),
    "max_steps": (1, 100), "max_seconds": (1, 86400),
    "max_model_calls": (1, 100), "max_tool_calls": (0, 1000), "retries": (0, 3),
}


def validate_endpoint(value: str) -> str:
    """MVP inference may contact only local Ollama, never arbitrary remote URLs."""
    if not isinstance(value, str):
        raise ValueError("endpoint debe ser una URL local de Ollama.")
    parsed = urlsplit(value.strip())
    try:
        hostname = parsed.hostname or ""
        local = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        local = False
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Puerto del endpoint inválido.") from exc
    if (parsed.scheme not in {"http", "https"} or not local or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or port == 0):
        raise ValueError("Usa la URL base local de Ollama, sin credenciales ni ruta (http://127.0.0.1:11434).")
    return value.strip().rstrip("/")


def _text(value, name: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"{name}: texto {'no vacío ' if required else ''}de hasta {maximum} caracteres.")
    return value.strip()


def normalize_agent(data: dict, existing: dict | None = None) -> dict:
    if not isinstance(data, dict):
        raise ValueError("La configuración debe ser un objeto JSON.")
    allowed = {"name", "description", "role", "enabled", "config", "tools"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("Campos desconocidos: " + ", ".join(sorted(unknown)))
    baseline = existing or {}
    result = {key: copy.deepcopy(baseline.get(key, default)) for key, default in (
        ("name", ""), ("description", ""), ("role", "Developer"),
        ("enabled", True), ("tools", DEFAULT_TOOLS),
    )}
    result.update({key: value for key, value in data.items() if key != "config"})
    for name, maximum, required in (("name", 100, True), ("description", 2000, False), ("role", 100, False)):
        result[name] = _text(result[name], name, maximum, required)
    if not isinstance(result["enabled"], bool):
        raise ValueError("enabled debe ser booleano.")
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(baseline.get("config", {}))
    incoming = data.get("config", {})
    if not isinstance(incoming, dict) or set(incoming) - set(DEFAULT_CONFIG):
        raise ValueError("config contiene campos desconocidos. Los secretos se referencian con secret_env.")
    config.update(incoming)
    config["model"] = _text(config["model"], "model", 200, True)
    if re.search(r"[\s\x00-\x1f]", config["model"]):
        raise ValueError("model no debe contener espacios ni caracteres de control.")
    config["system_prompt"] = _text(config["system_prompt"], "system_prompt", 16000)
    workspace_path = _text(config["workspace_path"], "workspace_path", 2048)
    if workspace_path:
        candidate = Path(workspace_path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("workspace_path debe ser una ruta absoluta.")
        try:
            candidate = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("La carpeta seleccionada como workspace no existe o no es accesible.") from exc
        if not candidate.is_dir():
            raise ValueError("workspace_path debe apuntar a una carpeta existente.")
        config["workspace_path"] = str(candidate)
    else:
        config["workspace_path"] = ""
    config["endpoint"] = validate_endpoint(config["endpoint"])
    temp = config["temperature"]
    if isinstance(temp, bool) or not isinstance(temp, (int, float)) or not math.isfinite(temp) or not 0 <= temp <= 2:
        raise ValueError("temperature debe estar entre 0 y 2.")
    for key, (lower, upper) in LIMITS.items():
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError(f"{key} debe ser un entero entre {lower} y {upper}.")
    if config["permissions"] not in {"read_only", "workspace", "execute"}:
        raise ValueError("permissions debe ser read_only, workspace o execute.")
    for key in ("allowed_directories", "forbidden_commands"):
        values = config[key]
        if not isinstance(values, list) or len(values) > 40 or any(not isinstance(x, str) or not x.strip() or len(x) > 240 for x in values):
            raise ValueError(f"{key} debe ser una lista de textos no vacíos (máximo 40).")
        config[key] = list(dict.fromkeys(x.strip() for x in values))
    if not config["allowed_directories"]:
        raise ValueError("Debe existir al menos un directorio permitido.")
    normalized_dirs = []
    for directory in config["allowed_directories"]:
        path = PurePosixPath(directory.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or ":" in directory or "\x00" in directory:
            raise ValueError("Los directorios permitidos deben ser relativos al workspace, sin '..'.")
        normalized_dirs.append(str(path))
    config["allowed_directories"] = list(dict.fromkeys(normalized_dirs))
    env_name = config["secret_env"]
    if not isinstance(env_name, str) or (env_name and not re.fullmatch(r"ACC_SECRET_[A-Z0-9_]{1,100}", env_name)):
        raise ValueError("secret_env debe ser vacío o un nombre de variable ACC_SECRET_...; nunca una clave real.")
    selected = result["tools"]
    available = {tool["name"] for tool in TOOL_CATALOG if tool["available"]}
    if not isinstance(selected, list) or any(not isinstance(x, str) or x not in available for x in selected):
        raise ValueError("tools debe contener solamente nombres de herramientas disponibles.")
    result["tools"] = list(dict.fromkeys(selected))
    if config["permissions"] == "read_only" and set(selected) & {"write_file", "edit_file", "run_command"}:
        raise ValueError("El permiso read_only no admite escritura ni ejecución.")
    if config["permissions"] != "execute" and "run_command" in selected:
        raise ValueError("run_command requiere permiso execute.")
    result["config"] = config
    return result
