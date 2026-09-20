# Análisis de migración a escritorio

Fecha: 2026-09-20  
Repositorio: W:\\K-Tools\\Freya  
Alcance: relevamiento previo a la migración; no modifica el flujo funcional existente.

## 1. Arquitectura actual confirmada

Freya es una aplicación local de un solo producto con este flujo:

```text
frontend/ (HTML + CSS + JavaScript sin bundler)
        ↓ same-origin HTTP
control_center/http.py (ThreadingHTTPServer loopback)
        ↓
control_center/api.py → storage.py (SQLite) + runtime.py
        ↓
orchestrator/planner/evaluator/recovery/integration
        ↓
Runtime scheduler → procesos worker (multiprocessing spawn)
        ↓
Ollama loopback + policy/capability engine + herramientas allowlisted
        ↓
workspace seleccionado o data/workspaces/<uuid>
```

### Backend y servidor

- El arranque documentado es `python -m control_center`.
- `control_center/__main__.py` calcula el root desde el archivo del módulo, acepta `--port`, `--data-dir` y límites de workers/orquestación, crea `InstanceLock`, abre SQLite, recupera ejecuciones interrumpidas, inicia `Runtime` y sirve con `ControlServer`.
- El servidor escucha exclusivamente en `127.0.0.1`. La implementación es `ThreadingHTTPServer`; valida Host/Origin y rechaza tráfico cross-site.
- El frontend se sirve desde la carpeta `frontend/` por el mismo servidor. No existe un servidor web separado.
- La ruta de salud actual es `GET /api/health`. No existe una ruta `/health` independiente. Devuelve estado, versión, SQLite, SSE, workers y métricas de sistema.
- SSE está implementado en `/api/events` y `/api/tasks/<id>/events`; la interfaz usa polling/EventSource contra el backend local.

### Persistencia y rutas

- El `data-dir` por defecto es `<repo>/data`, calculado a partir del módulo y no de `os.getcwd()`.
- SQLite vive en `<data-dir>/control_center.sqlite3`, con WAL, foreign keys, conexiones por operación y migraciones in-place.
- El lock de instancia vive en `<data-dir>/server.lock` y evita que dos schedulers usen la misma base.
- Los workspaces generados viven en `<data-dir>/workspaces/<uuid>`.
- Un usuario también puede seleccionar una carpeta absoluta existente; se valida que sea un directorio accesible y escribible. Esa ruta queda persistida en la configuración del agente/tarea.
- La base conserva tareas, ejecuciones, steps, eventos, métricas, approvals, agentes, Skills y snapshots de orquestación; el historial no debe moverse ni regenerarse durante la migración.

### Ejecución

- `Runtime.start()` inicia un hilo scheduler y recupera ejecuciones abandonadas.
- Cada worker se crea con `multiprocessing.get_context("spawn")`, importante en Windows.
- El runtime dispone de un Job Object de Windows para contener procesos descendientes y tiene apagado explícito que cancela tareas activas y espera el scheduler.
- Ollama se consume únicamente por HTTP loopback (por defecto `http://127.0.0.1:11434`) mediante un transporte que desactiva proxies y redirects.

### Frontend

- `frontend/index.html`, `app.js`, `core.js`, `components.js`, `views.js`, `dialogs.js`, `icons.js` y `styles.css` forman una SPA ligera.
- No hay `package.json`, React, Electron, Tauri, Vite ni otro bundler.
- La interfaz depende de rutas relativas same-origin (`/api/*`, SSE y assets estáticos). Por ello el primer MVP puede reutilizarla sin reescribirla.

## 2. Componentes reutilizables

Se pueden conservar sin cambios funcionales en el primer MVP:

- Todo el dominio Python de agentes, tareas, logs, orquestación, approvals, Skills y policy.
- SQLite, migraciones, recuperación de ejecuciones y almacenamiento de workspaces.
- El frontend actual y sus rutas same-origin.
- El endpoint `/api/health`, SSE y el control de una única instancia mediante `server.lock`.
- El cierre coordinado de `ControlServer` y `Runtime`.

La capa nueva debe limitarse inicialmente a:

1. resolver rutas de instalación/datos/config/logs/workspaces/temp;
2. iniciar el proceso Python como hijo;
3. esperar salud real con timeout y backoff;
4. abrir la ventana de escritorio contra la URL loopback;
5. capturar stdout/stderr del backend en un log de escritorio;
6. apagar el proceso hijo limpiamente cuando se cierre la ventana.

## 3. Riesgos y decisiones necesarias

### Salud y startup

- El contrato correcto es `/api/health`; usar `/health` produciría falsos negativos.
- No se deben usar sleeps fijos: el launcher debe sondear con timeout, detectar conexión rechazada, proceso terminado y respuesta HTTP inválida, y mostrar el último diagnóstico.
- Un error de startup debe ofrecer reintentar, abrir logs, copiar el error y cerrar.

### Instancia duplicada y puertos

- El lock de datos ya evita dos schedulers sobre la misma base, pero un segundo proceso puede fallar luego al bindear el puerto.
- El launcher deberá comprobar una instancia existente de forma explícita y, en una fase posterior, enfocar su ventana en lugar de crear otra.
- El puerto no debe quedar fijo como única estrategia: conviene reservar un puerto configurable y pasar el valor al backend.

### Datos de usuario

- El instalador no debe escribir la base dentro de la carpeta de instalación.
- Para producción de escritorio conviene separar aplicación instalada, datos persistentes, configuración, logs y temporales.
- La migración debe aceptar una instalación existente y conservar `control_center.sqlite3`, `server.lock` y workspaces; cualquier movimiento debe ser explícito, verificable y reversible.

### Herramientas de build

- En el repositorio no hay toolchain ni configuración de Tauri/Electron.
- En la máquina actual no se encontraron `cargo` ni `rustc`; sí existe Node, pero no hay proyecto Node configurado.
- Instalar toolchains o dependencias requiere una decisión explícita porque cambia el entorno y puede necesitar red. Por eso la elección de framework queda documentada antes de instalar.

### Seguridad

- Se debe conservar loopback-only, aislamiento del renderer, CSP, validación de Host/Origin y el motor de capabilities/policy.
- No se debe exponer Ollama ni una API remota para resolver la migración.
- El launcher no debe habilitar shell arbitrario ni IPC irrestricto.

## 4. Alcance recomendado del MVP

El MVP de escritorio debe ser un shell sobre la aplicación actual:

1. Tauri como shell objetivo, cargando la URL del backend local.
2. Bootstrap que resuelva rutas absolutas y pase `--data-dir`.
3. Proceso Python hijo con stdout/stderr a `logs/freya-desktop.log`.
4. Health check contra `GET /api/health` antes de mostrar la ventana.
5. Mensaje de startup fallido con reintento, logs, copiar diagnóstico y cierre.
6. Cierre de ventana que ordene server/runtime y espere al hijo.
7. Verificación de base existente y lock para evitar corrupción/duplicación.
8. Reutilización total de la UI actual; la migración visual a React queda fuera de este MVP.

## 5. Validación pendiente

Este análisis se basa en lectura estática del repositorio y comprobación de herramientas locales. Todavía no prueba un binario empaquetado, una ventana nativa, la interacción con Ollama instalado ni un instalador Windows. Esas validaciones requieren implementar y construir el shell.
