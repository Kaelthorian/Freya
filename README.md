# Freya

Freya is a local Ollama multi-agent control center. The Freya orchestrator receives user requests, selects existing enabled agents, delegates through the protected runtime, and returns an integrated response. Agents have explicit identity, instructions, capabilities, workspace and limits.

Run from the repository root:

```powershell
python -m control_center --port 8765
```

Open `http://127.0.0.1:8765`. The **Freya** page is the primary flow; **Agents**, **Tasks**, **Logs**, **Metrics**, and **Settings** remain available.

Plataforma web local para crear agentes de programación con Ollama, asignarles
tareas y observar sus herramientas, logs y métricas en tiempo real. Usa Python
3.10+, SQLite y JavaScript sin proceso de build. No crea datos de demostración
ni descarga modelos automáticamente.

## Iniciar

Desde la raíz del repositorio, en PowerShell:

```powershell
ollama list
python -m control_center --port 8765
```

Abre `http://127.0.0.1:8765`. Los datos locales se guardan en
`data/control_center.sqlite3`. El servidor escucha solo en loopback y está
diseñado para un único usuario local.

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

- CRUD y duplicación de agentes con workspace, modelo, tools y límites propios.
- Procesos independientes con cola, cancelación y pausa cooperativa.
- Actualizaciones SSE, timeline, logs, métricas e historial persistente.
- Límites de pasos, tiempo, tokens y llamadas al modelo o a tools.
- Tools de archivos, búsqueda, Git y comandos locales explícitamente permitidos.
- Sanitización de credenciales mediante referencias `ACC_SECRET_...`.

El permiso `execute` permite iniciar Python, tests, Ruff y consultas Git
permitidas desde el workspace. Es una concesión de confianza local, no un
sandbox del sistema operativo.

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
