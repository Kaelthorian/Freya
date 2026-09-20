# Arquitectura propuesta para Freya Desktop

Fecha: 2026-09-20  
Estado: propuesta para el MVP; no se modificó aún la arquitectura funcional Python.

## 1. Decisión de framework

### Recomendación: Tauri

Se recomienda Tauri como shell de escritorio porque:

- permite conservar el frontend HTML/CSS/JavaScript actual sin reescribir el dominio;
- mantiene un renderer con aislamiento y una superficie nativa pequeña;
- puede iniciar y supervisar un proceso Python externo;
- encaja con el objetivo de un `Freya.exe` liviano y con lifecycle explícito.

La migración a React no es requisito del shell. Si más adelante se decide React, debe ser una fase separada del MVP para no mezclar una reescritura visual con el problema de instalación, persistencia y procesos.

### Fallback: Electron

Electron queda como fallback solamente si el build de Tauri exige una instalación/toolchain que no se puede mantener o si una integración nativa necesaria requiere una reescritura sustancial. La elección no se hace por disponibilidad accidental de Node: el repositorio hoy no tiene package manifest ni runtime de desktop.

En el entorno verificado no están disponibles `cargo` ni `rustc`. Por lo tanto, el siguiente paso de implementación deberá instalar/preparar el toolchain de Tauri con aprobación explícita o adoptar Electron como decisión consciente. No se instalaron dependencias automáticamente.

## 2. Flujo de procesos

```text
Freya.exe (Tauri)
  ├─ resuelve directorios absolutos
  ├─ comprueba/coordina instancia única
  ├─ inicia python -m control_center --data-dir <data> --port <port>
  ├─ captura stdout/stderr → logs/freya-desktop.log
  ├─ sondea http://127.0.0.1:<port>/api/health con timeout/backoff
  ├─ abre la ventana cuando health.status == "ok"
  └─ ante cierre: solicita apagado y espera al proceso hijo

Python Control Center
  ├─ InstanceLock(<data>/server.lock)
  ├─ SQLite <data>/control_center.sqlite3
  ├─ Runtime scheduler + workers spawn
  └─ HTTP loopback + frontend actual
```

El shell nunca debe llamar Ollama directamente. Todas las operaciones de tareas, logs, approvals y herramientas siguen pasando por el backend y su policy engine.

## 3. Directorios de producción

La ubicación concreta debe resolverse con una sola función de paths del launcher. La propuesta es:

- instalación: carpeta administrada por el instalador, sin datos mutables;
- datos: `%LOCALAPPDATA%\\Freya\\data`;
- configuración: `%LOCALAPPDATA%\\Freya\\config`;
- logs: `%LOCALAPPDATA%\\Freya\\logs\\freya-desktop.log`;
- temporales: `%LOCALAPPDATA%\\Freya\\temp`;
- workspaces generados: `%LOCALAPPDATA%\\Freya\\data\\workspaces`.

En desarrollo se debe poder conservar el comportamiento actual pasando explícitamente `W:\\K-Tools\\Freya\\data` como `--data-dir`. Nunca se debe inferir una ruta persistente desde el directorio de trabajo actual.

La primera ejecución debe crear solamente los directorios propios que falten, detectar una base existente, ejecutar las migraciones ya soportadas por `Store`, no sobrescribir ni borrar una base o workspace y registrar la ruta efectiva en el diagnóstico de startup sin imprimir secretos.

## 4. Contratos del launcher

### Health check

- endpoint: `GET /api/health`;
- éxito: HTTP 200 y JSON con `status: "ok"`;
- timeout total acotado y backoff incremental;
- errores diferenciados: proceso terminado, conexión rechazada, timeout, HTTP no esperado, JSON inválido;
- el diagnóstico debe indicar puerto, data-dir y último error sin incluir prompts ni credenciales.

### Startup failure UI

La ventana de error debe mostrar título, mensaje legible, último estado del proceso/health check y botones `Retry`, `Open logs`, `Copy error`, `Close`. No se debe mostrar la UI principal antes de un health check exitoso.

### Shutdown

- al cerrar la ventana, detener nuevas asignaciones;
- solicitar cierre del backend;
- esperar el cierre normal;
- como último recurso y con registro, terminar el proceso hijo que pertenezca a esta instancia;
- conservar la recuperación existente para un cierre inesperado;
- no matar procesos no hijos ni el Ollama del usuario.

### Single instance

El MVP puede apoyarse en el lock de datos y en un puerto coordinado. La versión siguiente debe añadir un mutex/IPC de instancia de Tauri que enfoque la ventana existente y devuelva un diagnóstico si la instancia está iniciando o bloqueada. Nunca se deben abrir dos schedulers contra la misma base.

## 5. Archivos previstos

### Nuevos

- `docs/DESKTOP_MIGRATION_ANALYSIS.md` — evidencia y riesgos del estado actual.
- `docs/DESKTOP_ARCHITECTURE.md` — decisión Tauri, contratos y fases.
- `desktop/` — proyecto Tauri y código de lifecycle, cuando el toolchain esté aprobado/disponible.
- tests de paths, health check, startup failure, shutdown y single-instance.

### A modificar en fases posteriores

- `control_center/__main__.py` o un entrypoint auxiliar para recibir paths/puerto resueltos por el shell y permitir logging estructurado.
- documentación de desarrollo, mapa del repositorio e instrucciones de empaquetado.
- no se debe alterar el dominio de tareas, la policy ni el esquema SQLite salvo que una prueba de migración lo justifique.

## 6. Fases

1. **MVP shell:** Tauri + backend Python hijo + paths + health + logs + shutdown; UI actual sin rediseño.
2. **Persistencia y recuperación:** migración de data-dir existente, lock/puerto/single-instance, pruebas de interrupción.
3. **Distribución:** build Windows, instalador, shortcuts/tray si corresponde, exclusión de Ollama.
4. **Experiencia desktop:** error de startup, logs navegables, menú/tray, actualización y diagnóstico.
5. **Opcional:** React/renderer moderno y sidebar desktop, manteniendo los contratos del backend.

## 7. Riesgos no resueltos

- Falta validar el toolchain Tauri y el método de distribución firmado en esta máquina.
- Falta definir si la aplicación debe usar siempre `%LOCALAPPDATA%\\Freya` o permitir una ubicación configurable de datos.
- Falta implementar el canal seguro de apagado del backend desde el shell.
- La presencia de Ollama y de los modelos locales no se resolverá ni se empaquetará en el MVP; el diagnóstico deberá explicar si no está disponible.

## 8. Criterio de aceptación del MVP

Se considerará listo solamente cuando un build Windows verificable:

1. arranque desde un acceso directo sin una consola visible;
2. cree/reutilice datos sin perder tareas, logs ni workspaces;
3. espere `/api/health` real antes de mostrar la UI;
4. muestre un error accionable si backend/Ollama/puerto falla;
5. cierre el backend y workers propios sin dejar procesos huérfanos;
6. mantenga los controles de loopback, CSP, capabilities y approvals actuales.
