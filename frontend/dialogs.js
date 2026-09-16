import { state, api, esc, toast, uiText } from './core.js';
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
  dialog.innerHTML = `<form id="dialog-form"><header class="dialog-header"><div><span class="eyebrow">AGENT CONTROL CENTER</span><h2 id="dialog-title">${esc(title)}</h2><p>${esc(subtitle)}</p></div><button type="button" class="icon-button" data-dialog-close aria-label="Close">${icon('close')}</button></header><div class="dialog-body">${content}</div><div class="dialog-error" role="alert" hidden></div><footer class="dialog-footer"><button type="button" class="button secondary" data-dialog-close>Cancel</button><button type="submit" class="button ${danger ? 'danger' : 'primary'}" id="dialog-submit">${esc(submitLabel)}</button></footer></form>`;
  dialog.showModal();
  dialog.querySelector('form').addEventListener('submit', async event => {
    event.preventDefault(); const form = event.currentTarget, errorBox = dialog.querySelector('.dialog-error'), submit = dialog.querySelector('#dialog-submit');
    if (form.dataset.pending === 'true') return;
    const data = new FormData(form); errorBox.hidden = true; form.dataset.pending = 'true'; submit.disabled = true; submit.innerHTML = `<span class="spinner"></span> Saving…`;
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
  return `<label class="form-field full-width"><span>Agent workspace</span><div class="workspace-input-row"><input type="text" name="workspace_path" value="${esc(value)}" placeholder="Empty: a new workspace will be created for each task"><button type="button" class="button secondary" id="select-workspace">${icon('folder')} Choose folder</button></div><small>The folder must already exist. The agent will work directly in it, and changes will persist between tasks. Clear this field to use a fresh, isolated workspace.</small></label>`;
}
async function browseWorkspace(path = '') {
  const result = await api(`/workspaces/browse?${new URLSearchParams(path ? { path } : {})}`);
  workspaceDialog.dataset.path = result.path;
  workspaceDialog.querySelector('#workspace-browser-path').value = result.path;
  const list = workspaceDialog.querySelector('#workspace-browser-list');
  list.innerHTML = result.directories.length ? result.directories.map(item => `<button type="button" class="workspace-folder" data-browse-path="${esc(item.path)}">${icon('folder')}<span>${esc(item.name)}</span>${icon('chevron')}</button>`).join('') : '<div class="workspace-empty">No accessible subfolders.</div>';
  const up = workspaceDialog.querySelector('#workspace-up'); up.disabled = !result.parent; up.dataset.browsePath = result.parent || '';
  workspaceDialog.querySelector('#workspace-current').textContent = result.writable ? 'The current user can write to this folder.' : 'This folder may be read-only.';
  workspaceDialog.querySelector('#workspace-truncated').hidden = !result.truncated;
}
async function openWorkspacePicker(target = dialog.querySelector('[name="workspace_path"]')) {
  workspaceDialog.innerHTML = `<div class="workspace-browser"><header class="dialog-header"><div><span class="eyebrow">LOCAL WORKSPACE</span><h2 id="workspace-dialog-title">Choose folder</h2><p>Choose the root folder where the agent can read and modify files.</p></div><button type="button" class="icon-button" data-workspace-close aria-label="Close">${icon('close')}</button></header><div class="workspace-browser-toolbar"><button type="button" class="button secondary" id="workspace-up">${icon('back')} Up</button><input id="workspace-browser-path" type="text" aria-label="Absolute path"><button type="button" class="button secondary" id="workspace-go">Go</button></div><div class="dialog-error" id="workspace-error" role="alert" hidden></div><div id="workspace-browser-list" class="workspace-browser-list"><span class="spinner"></span></div><p id="workspace-truncated" class="small muted" hidden>Showing the first 500 folders.</p><footer class="dialog-footer workspace-browser-footer"><span><strong id="workspace-current"></strong></span><button type="button" class="button secondary" data-workspace-close>Cancel</button><button type="button" class="button primary" id="workspace-select-current">Use this folder</button></footer></div>`;
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
export async function chooseWorkspace(target) { await openWorkspacePicker(target); if (target?.id === 'freya-workspace') { state.freyaDraft.workspace_path = target.value; target.dispatchEvent(new Event('input', { bubbles: true })); } }
export async function agentDialog(id) {
  const agent = id ? await api(`/agents/${encodeURIComponent(id)}`) : null;
  const c = { ...(state.config?.defaults || {}), ...(agent?.config || {}) }, selectedTools = agent?.tools || state.config?.default_tools || [];
  const content = `<section class="form-section"><div class="form-section-title">${icon('agents')}<div><h3>General</h3><p>Set the agent's identity and instructions.</p></div></div><div class="form-grid">${field('Name', 'name', agent?.name, { required: true, placeholder: 'e.g. Code reviewer' })}${field('Role', 'role', agent?.role, { placeholder: 'e.g. Code review' })}${field('Description', 'description', agent?.description, { span: true, placeholder: 'What is this agent responsible for?' })}${field('System prompt', 'system_prompt', uiText(c.system_prompt), { type: 'textarea', rows: 4, span: true, hint: 'Agent operating instructions. Do not include credentials or secrets.' })}<label class="checkbox-label full-width"><input type="checkbox" name="enabled" ${agent?.enabled !== false ? 'checked' : ''}>Agent enabled · can receive new tasks</label></div></section>
    <section class="form-section"><div class="form-section-title">${icon('cpu')}<div><h3>Model</h3><p>Connect a model available on your Ollama server.</p></div></div><div class="form-grid">${field('Ollama endpoint', 'endpoint', c.endpoint || 'http://127.0.0.1:11434', { required: true, type: 'url' })}${field('Model', 'model', c.model, { required: true, placeholder: 'qwen2.5-coder:3b' })}<datalist id="model-options"></datalist><div class="model-check full-width"><span id="model-status" class="muted small">Check installed models or enter a model name.</span><button type="button" class="button small-button" id="load-models">${icon('refresh')} Detect models</button></div>${field('Temperature', 'temperature', c.temperature ?? .2, { type: 'number', min: 0, max: 2, step: .05 })}${field('Context window', 'context_window', c.context_window ?? 8192, { type: 'number', min: 512, max: 131072, step: 1 })}${field('Token environment variable (optional)', 'secret_env', c.secret_env, { span: true, placeholder: 'ACC_SECRET_OLLAMA', hint: 'Enter only an ACC_SECRET_... variable name from the server environment, never the secret value.' })}</div></section>
    <section class="form-section"><div class="form-section-title">${icon('folder')}<div><h3>Workspace</h3><p>Choose the folder where this agent will work.</p></div></div><div class="form-grid">${workspaceField(c.workspace_path)}</div><div class="notice workspace-warning">${icon('shield')} Tools remain limited to this folder and its allowed relative directories. The Execute permission can start local code with your user permissions.</div></section>
    <section class="form-section"><div class="form-section-title">${icon('tool')}<div><h3>Tools</h3><p>Choose which tools are available for each run.</p></div></div><div class="tool-options">${state.tools.map(tool => `<label class="tool-option ${tool.available ? '' : 'unavailable'}"><input type="checkbox" name="tools" value="${esc(tool.name)}" ${selectedTools.includes(tool.name) && tool.available ? 'checked' : ''} ${tool.available ? '' : 'disabled'}><span><strong>${esc(tool.name)}${tool.dangerous ? `<span class="tool-warning">${icon('shield')}</span>` : ''}</strong><small>${esc(tool.available ? tool.description : 'Unavailable in this version')}</small></span></label>`).join('')}</div></section>
    <section class="form-section"><div class="form-section-title">${icon('shield')}<div><h3>Limits and permissions</h3><p>Set explicit limits for every task.</p></div></div><div class="form-grid three">${field('Maximum steps', 'max_steps', c.max_steps ?? 20, { type: 'number', min: 1, max: 100 })}${field('Maximum time (s)', 'max_seconds', c.max_seconds ?? 300, { type: 'number', min: 1, max: 86400 })}${field('Token budget', 'max_tokens', c.max_tokens ?? 16384, { type: 'number', min: 128, max: 1000000 })}${field('Model calls', 'max_model_calls', c.max_model_calls ?? 20, { type: 'number', min: 1, max: 100 })}${field('Tool calls', 'max_tool_calls', c.max_tool_calls ?? 40, { type: 'number', min: 0, max: 1000 })}${field('Retries per action', 'retries', c.retries ?? 1, { type: 'number', min: 0, max: 3 })}</div><div class="form-grid permission-grid"><label class="form-field full-width"><span>Agent permissions</span><select name="permissions">${[['read_only', 'Read only'], ['workspace', 'Read and write in the workspace'], ['execute', 'Read, write, and run allowed commands']].map(([value, label]) => `<option value="${value}" ${c.permissions === value ? 'selected' : ''}>${label}</option>`).join('')}</select><small>Running commands can start local processes and tests inside the workspace. Enable this only if you trust the model and task files.</small></label>${field('Allowed directories', 'allowed_directories', (c.allowed_directories || ['.']).join('\n'), { type: 'textarea', hint: 'One relative path per line. “.” includes the entire workspace; paths cannot escape it.' })}${field('Blocked commands', 'forbidden_commands', (c.forbidden_commands || []).join('\n'), { type: 'textarea', hint: 'One command name per line. This is applied in addition to the allowlist.' })}</div></section>`;
  open(id ? 'Configure agent' : 'Create a new agent', id ? 'Update the model, tools, and limits.' : 'A real agent with a defined model and scope.', content, id ? 'Save changes' : 'Create agent', async data => {
    const config = {};
    for (const key of ['model', 'endpoint', 'system_prompt', 'permissions', 'secret_env', 'workspace_path']) config[key] = String(data.get(key) || '').trim();
    for (const key of ['temperature', 'context_window', 'max_tokens', 'max_steps', 'max_seconds', 'max_model_calls', 'max_tool_calls', 'retries']) config[key] = Number(data.get(key));
    for (const key of ['allowed_directories', 'forbidden_commands']) config[key] = String(data.get(key) || '').split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    const saved = await api(id ? `/agents/${id}` : '/agents', id ? 'PATCH' : 'POST', { name: data.get('name'), description: data.get('description'), role: data.get('role'), enabled: data.has('enabled'), config, tools: data.getAll('tools') });
    toast(id ? 'Settings saved.' : 'Agent created.'); if (!id) location.hash = `#/agents/${saved.id}`;
  }, true);
  dialog.querySelector('#select-workspace').addEventListener('click', () => openWorkspacePicker());
  dialog.querySelector('#load-models').addEventListener('click', async event => {
    const button = event.currentTarget, status = dialog.querySelector('#model-status'); button.disabled = true; status.textContent = 'Checking Ollama…';
    try { const result = await api(`/models?${new URLSearchParams({ endpoint: dialog.querySelector('[name="endpoint"]').value })}`); dialog.querySelector('#model-options').innerHTML = (result.models || []).map(model => `<option value="${esc(model.name)}">`).join(''); status.textContent = result.error || `${result.models.length} models available. Choose one in the Model field.`; status.className = `small ${result.error ? 'text-red' : 'text-green'}`; }
    catch (error) { status.textContent = error.message; status.className = 'small text-red'; } finally { button.disabled = false; }
  });
}

export function assignDialog(id) {
  if (!state.agents.length) { toast('Create an agent before assigning a task.'); return agentDialog(); }
  const initialAgent = state.agents.find(agent => agent.id === id && agent.enabled) || state.agents.find(agent => agent.enabled) || state.agents[0];
  const content = `<div class="form-grid"><label class="form-field full-width"><span>Agent</span><select name="agent_id" required>${state.agents.map(agent => `<option value="${esc(agent.id)}" ${agent.id === initialAgent.id ? 'selected' : ''} ${agent.enabled ? '' : 'disabled'}>${esc(agent.name)} · ${esc(agent.config.model)}${agent.enabled ? '' : ' (disabled)'}</option>`).join('')}</select></label>${field('Task', 'prompt', '', { type: 'textarea', rows: 7, span: true, required: true, placeholder: 'Describe the goal, expected result, and task constraints…' })}<label class="form-field full-width"><span>Workspace for this task</span><div class="workspace-input-row"><input type="text" name="workspace_path" value="" placeholder="New automatic workspace"><button type="button" class="button secondary" id="select-task-workspace">${icon('folder')} Choose folder</button></div><small id="assigned-workspace-help"></small></label><div class="notice full-width workspace-warning">${icon('shield')} The agent's tools can read and modify files in the selected folder, subject to its permissions and allowed directories.</div></div>`;
  open('Assign a task', 'Choose the agent and workspace for this run.', content, 'Start run', async data => { const task = await api(`/agents/${data.get('agent_id')}/tasks`, 'POST', { prompt: data.get('prompt'), workspace_path: String(data.get('workspace_path') || '').trim() }); toast('Task created and queued.'); location.hash = `#/tasks/${task.id}`; });
  const select = dialog.querySelector('[name="agent_id"]'), workspace = dialog.querySelector('[name="workspace_path"]'), help = dialog.querySelector('#assigned-workspace-help');
  const refreshWorkspace = resetPath => {
    const agent = state.agents.find(item => item.id === select.value), path = agent?.config?.workspace_path || '';
    if (resetPath) workspace.value = path;
    help.textContent = path ? 'The configured folder is selected by default. You can change it for this task, or clear it to create a fresh workspace.' : 'Choose a folder for this task, or leave the field empty to create a fresh workspace.';
  };
  select.addEventListener('change', () => refreshWorkspace(true)); refreshWorkspace(true);
  dialog.querySelector('#select-task-workspace').addEventListener('click', () => openWorkspacePicker(workspace));
}

export function confirmAction({ title, description, label, action, danger = false }) {
  open(title, '', `<p class="confirm-description">${esc(description)}</p>`, label, async () => { await action(); }, false, danger);
}
