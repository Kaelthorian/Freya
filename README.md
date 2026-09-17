# Freya Agent Control Center

Freya is a local Ollama agent platform with a loopback web UI, bounded worker
runtime, SQLite history and workspace-scoped tools. The runtime flow is:

```text
frontend → API → scheduler → worker → Ollama → Capability Resolver → Policy Engine → tools
```

Agents combine structured Identity, Behavior, Autonomy, Verification, Output,
Skills, model settings and a Capability Policy. Skills are reusable knowledge:

```text
Skill ≠ Tool
Skill ≠ Capability
Skill ≠ Permission
```

A Skill can provide instructions, adaptable procedures and required or
recommended capability diagnostics. It never grants access or executes code;
the Capability Policy remains authoritative. Tasks snapshot the effective Skill
definitions and versions so later edits do not change historical runs.

Plataforma web local para crear agentes de programación con Ollama, asignarles
tareas y observar sus herramientas, logs y métricas en tiempo real. Usa Python
3.10+, SQLite y JavaScript sin proceso de build. No crea datos de demostración
ni descarga modelos automáticamente.

Run from the repository root in PowerShell:

```powershell
ollama list
python -m control_center --port 8765 --workers 2 --data-dir .\data
python -m unittest discover -s tests -v
python -m compileall -q control_center
```

Open `http://127.0.0.1:8765`. Ollama must be running locally with an installed
model for live execution. See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md),
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/API.md](docs/API.md) and
[docs/REPOSITORY_MAP.md](docs/REPOSITORY_MAP.md) for operations, routes and
change locations.

Cada agente puede tener una carpeta predeterminada y el formulario de cada tarea
permite elegir otra carpeta para esa ejecución. Si queda vacía, la tarea obtiene
un workspace nuevo bajo `data/workspaces/`. Las rutas de las tools permanecen
limitadas a la raíz elegida y a los subdirectorios relativos permitidos. Si
varios agentes comparten una carpeta, el runtime serializa sus tareas para evitar
escrituras simultáneas.

Para cambiar la concurrencia o la ubicación de los datos generados:

```powershell
python -m control_center --port 8765 --workers 2 --data-dir .\data
```

## Funciones

- CRUD y duplicación de agentes con workspace, modelo, Skills y límites propios.
- CRUD de Skills con IDs estables, versión, instrucciones y procedures adaptables.
- Diagnósticos de compatibilidad entre Skills y Capability Policy.
- Procesos independientes con cola, cancelación y pausa cooperativa.
- Actualizaciones SSE, timeline, logs, métricas, approvals e historial persistente.
- Límites de pasos, tiempo, tokens y llamadas al modelo o a tools.
- Tools de archivos, búsqueda, Git y comandos locales explícitamente permitidos.
- Sanitización de credenciales mediante referencias `ACC_SECRET_...`.

El permiso `execute` permite iniciar Python, tests, Ruff y consultas Git
permitidas desde el workspace. Es una concesión de confianza local, no un
sandbox del sistema operativo.

El preset Programmer crea un agente genérico con Skills, Capability Policy, Autonomy y Verification. Capability Policy deriva las tools efectivas desde allow/ask; Autonomy nunca otorga capabilities y las reglas ask se resuelven en Approvals.

## Validar

```powershell
python -m unittest discover -s tests -v
python -m compileall -q control_center
node --check frontend\app.js
node --check frontend\dialogs.js
python -m control_center --help
```

## Documentación

- [Mapa del repositorio](docs/REPOSITORY_MAP.md)
- [Arquitectura y límites](docs/ARCHITECTURE.md)
- [Desarrollo y operación](docs/DEVELOPMENT.md)
- [API](docs/API.md)
- [Reglas del backend](control_center/AGENTS.md)
