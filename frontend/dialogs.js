import { state, api, esc, toast } from './core.js';
import { icon } from './icons.js';
import { button } from './components.js';

const dialog = document.querySelector('#app-dialog');
const workspaceDialog = document.querySelector('#workspace-dialog');
let onSaved = () => {};
export function setDialogRefresh(callback) { onSaved = callback; }
export function closeDialog() { if (dialog.querySelector('[data-pending="true"]')) return; dialog.close(); }
dialog.addEventListener('click', event => { if (event.target.closest('[data-dialog-close]')) closeDialog(); });
dialog.addEventListener('cancel', event => { if (dialog.querySelector('[data-pending="true"]')) event.preventDefault(); });
function open(title, subtitle, content, submitLabel, handler, wide = false, danger = false) {
  dialog.className = wide ? 'wide-dialog' : '';
  dialog.innerHTML = `<form id="dialog-form"><header class="dialog-header"><div><span class="eyebrow">AGENT CONTROL CENTER</span><h2 id="dialog-title">${esc(title)}</h2><p>${esc(subtitle)}</p></div><button type="button" class="icon-button" data-dialog-close aria-label="Cerrar">${icon('close')}</button></header><div class="dialog-body">${content}</div><div class="dialog-error" role="alert" hidden></div><footer class="dialog-footer"><button type="button" class="button secondary" data-dialog-close>Cancelar</button><button type="submit" class="button ${danger ? 'danger' : 'primary'}" id="dialog-submit">${esc(submitLabel)}</button></footer></form>`;
  dialog.showModal();
  dialog.querySelector('form').addEventListener('submit', async event => {
    event.preventDefault(); const form = event.currentTarget, errorBox = dialog.querySelector('.dialog-error'), submit = dialog.querySelector('#dialog-submit');
    if (form.dataset.pending === 'true') return;
    const data = new FormData(form); errorBox.hidden = true; form.dataset.pending = 'true'; submit.disabled = true; submit.innerHTML = `<span class="spinner"></span> Guardando…`;
    try { await handler(data, form); dialog.close(); await onSaved(); }
    catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
    finally { form.dataset.pending = 'false'; submit.disabled = false; submit.textContent = submitLabel; }
  });
}
function field(label, name, value = '', options = {}) {
  const { type = 'text', min, max, step, required, hint, placeholder, span, rows = 3 } = options;
  return `<label class="form-field ${span ? 'full-width' : ''}"><span>${esc(label)}${required ? ' <b>*</b>' : ''}</span>${type === 'textarea' ? `<textarea name="${name}" rows="${rows}" ${required ? 'required' : ''} placeholder="${esc(placeholder || '')}">${esc(value)}</textarea>` : `<input type="${type}" name="${name}" value="${esc(value)}" ${required ? 'required' : ''} ${min == null ? '' : `min="${min}"`} ${max == null ? '' : `max="${max}"`} ${step == null ? '' : `step="${step}"`} placeholder="${esc(placeholder || '')}" ${name === 'model' ? 'list="model-options"' : ''}>`}${hint ? `<small>${esc(hint)}</small>` : ''}</label>`;
}
function workspaceField(value = '') {
  return `<label class="form-field full-width"><span>Workspace del agente</span><div class="workspace-input-row"><input type="text" name="workspace_path" value="${esc(value)}" placeholder="Vacío: workspace nuevo para cada tarea"><button type="button" class="button secondary" id="select-workspace">${icon('folder')} Seleccionar carpeta</button></div><small>La carpeta debe existir. El agente trabajará directamente allí y sus cambios se conservarán entre tareas. Vacía el campo para usar un workspace aislado nuevo.</small></label>`;
}
async function browseWorkspace(path = '') {
  const result = await api(`/workspaces/browse?${new URLSearchParams(path ? { path } : {})}`);
  workspaceDialog.dataset.path = result.path;
  workspaceDialog.querySelector('#workspace-browser-path').value = result.path;
  const list = workspaceDialog.querySelector('#workspace-browser-list');
  list.innerHTML = result.directories.length ? result.directories.map(item => `<button type="button" class="workspace-folder" data-browse-path="${esc(item.path)}">${icon('folder')}<span>${esc(item.name)}</span>${icon('chevron')}</button>`).join('') : '<div class="workspace-empty">No hay subcarpetas accesibles.</div>';
  const up = workspaceDialog.querySelector('#workspace-up'); up.disabled = !result.parent; up.dataset.browsePath = result.parent || '';
  workspaceDialog.querySelector('#workspace-current').textContent = result.writable ? 'La carpeta permite escritura al usuario actual.' : 'La carpeta puede ser de solo lectura.';
  workspaceDialog.querySelector('#workspace-truncated').hidden = !result.truncated;
}
async function openWorkspacePicker(target = dialog.querySelector('[name="workspace_path"]')) {
  workspaceDialog.innerHTML = `<div class="workspace-browser"><header class="dialog-header"><div><span class="eyebrow">WORKSPACE LOCAL</span><h2 id="workspace-dialog-title">Seleccionar carpeta</h2><p>Elige la raíz donde el agente podrá leer y modificar archivos.</p></div><button type="button" class="icon-button" data-workspace-close aria-label="Cerrar">${icon('close')}</button></header><div class="workspace-browser-toolbar"><button type="button" class="button secondary" id="workspace-up">${icon('back')} Subir</button><input id="workspace-browser-path" type="text" aria-label="Ruta absoluta"><button type="button" class="button secondary" id="workspace-go">Ir</button></div><div class="dialog-error" id="workspace-error" role="alert" hidden></div><div id="workspace-browser-list" class="workspace-browser-list"><span class="spinner"></span></div><p id="workspace-truncated" class="small muted" hidden>Se muestran las primeras 500 carpetas.</p><footer class="dialog-footer workspace-browser-footer"><span><strong id="workspace-current"></strong></span><button type="button" class="button secondary" data-workspace-close>Cancelar</button><button type="button" class="button primary" id="workspace-select-current">Usar esta carpeta</button></footer></div>`;
  workspaceDialog.showModal();
  const showError = error => { const box = workspaceDialog.querySelector('#workspace-error'); box.textContent = error.message; box.hidden = false; };
  const navigate = async path => { workspaceDialog.querySelector('#workspace-error').hidden = true; try { await browseWorkspace(path); } catch (error) { showError(error); } };
  workspaceDialog.onclick = event => {
    if (event.target.closest('[data-workspace-close]')) return workspaceDialog.close();
    const folder = event.target.closest('[data-browse-path]'); if (folder) return navigate(folder.dataset.browsePath);
    if (event.target.closest('#workspace-go')) return navigate(workspaceDialog.querySelector('#workspace-browser-path').value.trim());
    if (event.target.closest('#workspace-select-current')) { target.value = workspaceDialog.dataset.path; workspaceDialog.close(); }
  };
  workspaceDialog.querySelector('#workspace-browser-path').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); navigate(event.currentTarget.value.trim()); } });
  await navigate(target.value.trim());
}
export async function agentDialog(id) {
  const agent = id ? await api(`/agents/${encodeURIComponent(id)}`) : null;
  const c = { ...(state.config?.defaults || {}), ...(agent?.config || {}) }, selectedTools = agent?.tools || state.config?.default_tools || [];
  const content = `<section class="form-section"><div class="form-section-title">${icon('agents')}<div><h3>General</h3><p>Define la identidad y las instrucciones del agente.</p></div></div><div class="form-grid">${field('Nombre', 'name', agent?.name, { required: true, placeholder: 'Ej. Code reviewer' })}${field('Rol', 'role', agent?.role, { placeholder: 'Ej. Revisión de código' })}${field('Descripción', 'description', agent?.description, { span: true, placeholder: '¿Qué responsabilidad tiene este agente?' })}${field('System prompt', 'system_prompt', c.system_prompt, { type: 'textarea', rows: 4, span: true, hint: 'Instrucciones operativas del agente. No incluyas credenciales ni secrets.' })}<label class="checkbox-label full-width"><input type="checkbox" name="enabled" ${agent?.enabled !== false ? 'checked' : ''}>Agente activo · puede recibir nuevas tareas</label></div></section>
    <section class="form-section"><div class="form-section-title">${icon('cpu')}<div><h3>Modelo</h3><p>Conecta un modelo disponible en tu servidor Ollama.</p></div></div><div class="form-grid">${field('Endpoint de Ollama', 'endpoint', c.endpoint || 'http://127.0.0.1:11434', { required: true, type: 'url' })}${field('Modelo', 'model', c.model, { required: true, placeholder: 'qwen2.5-coder:3b' })}<datalist id="model-options"></datalist><div class="model-check full-width"><span id="model-status" class="muted small">Consulta los modelos instalados o ingresa su nombre.</span><button type="button" class="button small-button" id="load-models">${icon('refresh')} Detectar modelos</button></div>${field('Temperatura', 'temperature', c.temperature ?? .2, { type: 'number', min: 0, max: 2, step: .05 })}${field('Context window', 'context_window', c.context_window ?? 8192, { type: 'number', min: 512, max: 131072, step: 1 })}${field('Variable de entorno del token (opcional)', 'secret_env', c.secret_env, { span: true, placeholder: 'ACC_SECRET_OLLAMA', hint: 'Solo un nombre ACC_SECRET_... del entorno del servidor; nunca el valor del secret.' })}</div></section>
    <section class="form-section"><div class="form-section-title">${icon('folder')}<div><h3>Workspace</h3><p>Selecciona la carpeta donde este agente hará su trabajo.</p></div></div><div class="form-grid">${workspaceField(c.workspace_path)}</div><div class="notice workspace-warning">${icon('shield')} Las tools siguen limitadas a esta carpeta y a los directorios relativos permitidos. El permiso «ejecutar» puede iniciar código local con los permisos de tu usuario.</div></section>
    <section class="form-section"><div class="form-section-title">${icon('tool')}<div><h3>Tools</h3><p>Selecciona las herramientas disponibles en cada ejecución.</p></div></div><div class="tool-options">${state.tools.map(tool => `<label class="tool-option ${tool.available ? '' : 'unavailable'}"><input type="checkbox" name="tools" value="${esc(tool.name)}" ${selectedTools.includes(tool.name) && tool.available ? 'checked' : ''} ${tool.available ? '' : 'disabled'}><span><strong>${esc(tool.name)}${tool.dangerous ? `<span class="tool-warning">${icon('shield')}</span>` : ''}</strong><small>${esc(tool.available ? tool.description : 'No disponible en esta versión')}</small></span></label>`).join('')}</div></section>
    <section class="form-section"><div class="form-section-title">${icon('shield')}<div><h3>Límites y permisos</h3><p>Establece un presupuesto explícito para cada tarea.</p></div></div><div class="form-grid three">${field('Máximo de pasos', 'max_steps', c.max_steps ?? 20, { type: 'number', min: 1, max: 100 })}${field('Tiempo máximo (s)', 'max_seconds', c.max_seconds ?? 300, { type: 'number', min: 1, max: 86400 })}${field('Presupuesto de tokens', 'max_tokens', c.max_tokens ?? 16384, { type: 'number', min: 128, max: 1000000 })}${field('Llamadas al modelo', 'max_model_calls', c.max_model_calls ?? 20, { type: 'number', min: 1, max: 100 })}${field('Llamadas a tools', 'max_tool_calls', c.max_tool_calls ?? 40, { type: 'number', min: 0, max: 1000 })}${field('Reintentos por acción', 'retries', c.retries ?? 1, { type: 'number', min: 0, max: 3 })}</div><div class="form-grid permission-grid"><label class="form-field full-width"><span>Permisos del agente</span><select name="permissions">${[['read_only', 'Solo lectura'], ['workspace', 'Leer y escribir en el workspace'], ['execute', 'Leer, escribir y ejecutar comandos permitidos']].map(([value, label]) => `<option value="${value}" ${c.permissions === value ? 'selected' : ''}>${label}</option>`).join('')}</select><small>Ejecutar comandos permite iniciar procesos locales y tests dentro del workspace. Habilítalo solo si confías en el modelo y en los archivos de la tarea.</small></label>${field('Directorios permitidos', 'allowed_directories', (c.allowed_directories || ['.']).join('\n'), { type: 'textarea', hint: 'Una ruta relativa por línea. “.” incluye el workspace completo; no permite salir de él.' })}${field('Comandos prohibidos', 'forbidden_commands', (c.forbidden_commands || []).join('\n'), { type: 'textarea', hint: 'Un nombre de comando por línea. Se aplica además de la lista de comandos permitidos.' })}</div></section>`;
  open(id ? 'Configurar agente' : 'Crear nuevo agente', id ? 'Actualiza el modelo, las herramientas y los límites.' : 'Un agente real, con un modelo y un alcance definidos.', content, id ? 'Guardar cambios' : 'Crear agente', async data => {
    const config = {};
    for (const key of ['model', 'endpoint', 'system_prompt', 'permissions', 'secret_env', 'workspace_path']) config[key] = String(data.get(key) || '').trim();
    for (const key of ['temperature', 'context_window', 'max_tokens', 'max_steps', 'max_seconds', 'max_model_calls', 'max_tool_calls', 'retries']) config[key] = Number(data.get(key));
    for (const key of ['allowed_directories', 'forbidden_commands']) config[key] = String(data.get(key) || '').split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    const saved = await api(id ? `/agents/${id}` : '/agents', id ? 'PATCH' : 'POST', { name: data.get('name'), description: data.get('description'), role: data.get('role'), enabled: data.has('enabled'), config, tools: data.getAll('tools') });
    toast(id ? 'Configuración guardada.' : 'Agente creado.'); if (!id) location.hash = `#/agents/${saved.id}`;
  }, true);
  dialog.querySelector('#select-workspace').addEventListener('click', () => openWorkspacePicker());
  dialog.querySelector('#load-models').addEventListener('click', async event => {
    const button = event.currentTarget, status = dialog.querySelector('#model-status'); button.disabled = true; status.textContent = 'Consultando Ollama…';
    try { const result = await api(`/models?${new URLSearchParams({ endpoint: dialog.querySelector('[name="endpoint"]').value })}`); dialog.querySelector('#model-options').innerHTML = (result.models || []).map(model => `<option value="${esc(model.name)}">`).join(''); status.textContent = result.error || `${result.models.length} modelos disponibles. Elige uno en el campo Modelo.`; status.className = `small ${result.error ? 'text-red' : 'text-green'}`; }
    catch (error) { status.textContent = error.message; status.className = 'small text-red'; } finally { button.disabled = false; }
  });
}

export function assignDialog(id) {
  if (!state.agents.length) { toast('Primero crea un agente para asignarle una tarea.'); return agentDialog(); }
  const initialAgent = state.agents.find(agent => agent.id === id && agent.enabled) || state.agents.find(agent => agent.enabled) || state.agents[0];
  const content = `<div class="form-grid"><label class="form-field full-width"><span>Agente</span><select name="agent_id" required>${state.agents.map(agent => `<option value="${esc(agent.id)}" ${agent.id === initialAgent.id ? 'selected' : ''} ${agent.enabled ? '' : 'disabled'}>${esc(agent.name)} · ${esc(agent.config.model)}${agent.enabled ? '' : ' (desactivado)'}</option>`).join('')}</select></label>${field('Tarea', 'prompt', '', { type: 'textarea', rows: 7, span: true, required: true, placeholder: 'Describe el objetivo, el resultado esperado y las restricciones de la tarea…' })}<label class="form-field full-width"><span>Workspace para esta tarea</span><div class="workspace-input-row"><input type="text" name="workspace_path" value="" placeholder="Workspace nuevo automático"><button type="button" class="button secondary" id="select-task-workspace">${icon('folder')} Seleccionar carpeta</button></div><small id="assigned-workspace-help"></small></label><div class="notice full-width workspace-warning">${icon('shield')} Las herramientas del agente podrán leer y modificar archivos dentro de la carpeta elegida, según sus permisos y directorios autorizados.</div></div>`;
  open('Asignar una tarea', 'Elige el agente y dónde trabajará esta ejecución.', content, 'Iniciar ejecución', async data => { const task = await api(`/agents/${data.get('agent_id')}/tasks`, 'POST', { prompt: data.get('prompt'), workspace_path: String(data.get('workspace_path') || '').trim() }); toast('Tarea creada y enviada a la cola.'); location.hash = `#/tasks/${task.id}`; });
  const select = dialog.querySelector('[name="agent_id"]'), workspace = dialog.querySelector('[name="workspace_path"]'), help = dialog.querySelector('#assigned-workspace-help');
  const refreshWorkspace = resetPath => {
    const agent = state.agents.find(item => item.id === select.value), path = agent?.config?.workspace_path || '';
    if (resetPath) workspace.value = path;
    help.textContent = path ? 'Se propone la carpeta configurada para este agente. Puedes cambiarla para esta tarea; si la vacías, se creará un workspace nuevo.' : 'Elige una carpeta para esta tarea o deja el campo vacío para crear un workspace nuevo.';
  };
  select.addEventListener('change', () => refreshWorkspace(true)); refreshWorkspace(true);
  dialog.querySelector('#select-task-workspace').addEventListener('click', () => openWorkspacePicker(workspace));
}

export function confirmAction({ title, description, label, action, danger = false }) {
  open(title, '', `<p class="confirm-description">${esc(description)}</p>`, label, async () => { await action(); }, false, danger);
}
