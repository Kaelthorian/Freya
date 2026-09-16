# Mini-Coder + Agent Control Center

Este repositorio contiene dos formas de ejecutar agentes locales con Ollama:

- `agent.py`: CLI de una sola tarea con evaluador fijo y registros JSONL.
- `python -m control_center`: panel web para crear agentes, asignar tareas,
  observar herramientas en tiempo real y consultar historial, logs y métricas.

El panel usa Python 3.10+, SQLite y JavaScript sin build. No crea agentes ni
datos de demostración. Ollama debe estar iniciado y el modelo elegido debe estar
instalado; la aplicación no descarga modelos automáticamente.

## Iniciar el panel

Desde la raíz, en PowerShell:

```powershell
ollama list
python -m control_center --port 8765
```

Abre `http://127.0.0.1:8765`. Los datos locales se guardan por defecto en
`data/control_center.sqlite3` y cada tarea obtiene un workspace nuevo bajo
`data/workspaces/`. Para cambiar la ubicación o concurrencia:

```powershell
python -m control_center --port 8765 --workers 2 --data-dir .\data
```

El servidor escucha sólo en loopback. El panel es una herramienta local de un
solo usuario; no es un servicio multiusuario ni debe exponerse directamente a
una red.

## Funciones del MVP

- CRUD y duplicación de agentes con configuración y herramientas propias.
- Ejecuciones reales en procesos independientes, con cola y una tarea activa
  por agente.
- Actualizaciones SSE, timeline operativo, cancelación y pausa cooperativa entre
  acciones.
- Límites de pasos, tiempo, tokens, llamadas al modelo y llamadas a tools.
- Logs y métricas persistentes en SQLite, filtros e historial reconstruible.
- Sanitización de credenciales; sólo se guarda el nombre de una variable
  `ACC_SECRET_...`, nunca su valor.

Las integraciones distribuidas, RAG, memoria, equipos, búsqueda web, navegador,
HTTP genérico y bases de datos aparecen como no disponibles. Una ejecución web
marcada `Success` significa que el modelo terminó normalmente; el panel no corre
el evaluador de calculadora salvo que el agente tenga `run_tests` habilitado y
lo invoque. Cada tarea web comienza con un workspace vacío.

## CLI y benchmark existentes

```powershell
python agent.py --model qwen2.5-coder:7b
python benchmark.py --models qwen2.5-coder:7b gemma4:latest --runs 5
```

El CLI escribe en `workspace/`, puede ejecutar el evaluador fijo de
`evaluator/` y guarda registros en `results/`. Para una tarea ajena a la
calculadora usa `--skip-final-tests` o define un evaluador adecuado.

## Validar

```powershell
python -m unittest discover -s tests -v
python -m py_compile agent.py benchmark.py hardware.py ollama_client.py tools.py
python -m compileall -q control_center
node --check frontend\app.js
```

## Documentación

- [Mapa del repositorio](docs/REPOSITORY_MAP.md)
- [Arquitectura y límites](docs/ARCHITECTURE.md)
- [Desarrollo y operación](docs/DEVELOPMENT.md)
- [API del panel](docs/API.md)
- [Reglas del subsistema web](control_center/AGENTS.md)
