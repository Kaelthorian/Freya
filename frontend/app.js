import { state, api, esc, number, route, toast, serialize } from './core.js';
import { icon } from './icons.js';
import { empty, button } from './components.js';
import { freya, dashboard, agents, agentDetail, tasks, taskDetail, logs, metricsView, settings } from './views.js';
import { agentDialog, assignDialog, confirmAction, setDialogRefresh, chooseWorkspace } from './dialogs.js';

const main = document.querySelector('#main-content');
const pages = [['freya', 'Freya'], ['agents', 'Agents'], ['tasks', 'Tasks'], ['logs', 'Logs'], ['metrics', 'Metrics'], ['settings', 'Settings']];
document.querySelector('#navigation').innerHTML = pages.map(([key, title], index) => `${index === 3 ? '<div class="nav-label secondary-nav-label">OBSERVABILITY</div>' : ''}${index === 5 ? '<div class="nav-divider"></div>' : ''}<a href="#/${key}" class="nav-item" data-nav="${key}">${icon(key)}<span>${title}</span>${key === 'agents' ? '<span class="nav-count" id="agent-count">0</span>' : ''}${key === 'dashboard' ? '<span class="nav-active-dot"></span>' : ''}</a>`).join('');
let renderSequence = 0, refreshing = false, refreshAgain = false, debounceTimer, currentKey = '';

function updateChrome() {
  const current = route(), title = pages.find(([page]) => page === current.page)?.[1] || 'Workspace';
  document.querySelector('#breadcrumb').textContent = title;
  document.querySelector('#breadcrumb-icon').innerHTML = icon(current.page);
  document.title = `${title} · Agent Control Center`;
  document.querySelectorAll('[data-nav]').forEach(link => { const active = link.dataset.nav === current.page; link.classList.toggle('active', active); if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current'); });
  document.querySelector('#agent-count').textContent = number(state.agents.length);
  const m = state.metrics || {}, good = state.health?.status === 'ok';
  document.querySelector('#system-overview').innerHTML = `<span class="header-system-status"><span class="dot ${good ? 'success' : 'error'}"></span>${good ? 'System online' : 'API offline'}</span><span class="header-divider"></span><span>${icon('agents')}<strong>${number(m.active_agents)}</strong> active</span><span>${icon('activity')}<strong>${number(m.running_tasks)}</strong> running</span>`;
}

function preserveUI() {
  const active = document.activeElement, focused = main.contains(active) && active.name ? { name: active.name, filter: active.dataset.filter, selectionStart: active.selectionStart, selectionEnd: active.selectionEnd, value: active.value } : null;
  return { y: window.scrollY, focused, open: [...main.querySelectorAll('details[open][data-detail]')].map(element => element.dataset.detail) };
}
function restoreUI(saved) {
  saved.open.forEach(id => [...main.querySelectorAll('details[data-detail]')].find(item => item.dataset.detail === id)?.setAttribute('open', ''));
  if (saved.focused) {
    const target = [...main.querySelectorAll('[name]')].find(item => item.name === saved.focused.name && item.dataset.filter === saved.focused.filter);
    if (target) { target.value = saved.focused.value; target.focus({ preventScroll: true }); try { target.setSelectionRange(saved.focused.selectionStart, saved.focused.selectionEnd); } catch {} }
  }
  window.scrollTo({ top: saved.y, behavior: 'instant' });
}

async function render(navigation = false) {
  const sequence = ++renderSequence, current = route(), saved = preserveUI();
  updateChrome();
  try {
    let html;
    if (current.page === 'freya') html = await freya();
    else if (current.page === 'dashboard') html = dashboard();
    else if (current.page === 'agents') html = current.id ? await agentDetail(current.id) : agents();
    else if (current.page === 'tasks') html = current.id ? await taskDetail(current.id) : await tasks();
    else if (current.page === 'logs') html = await logs();
    else if (current.page === 'metrics') html = metricsView(state.metrics);
    else if (current.page === 'settings') html = settings();
    else html = empty('search', 'This page does not exist', 'Return to the dashboard to continue.', '<a class="button primary" href="#/dashboard">Open dashboard</a>');
    if (sequence !== renderSequence) return;
    main.innerHTML = html;
    if (navigation) window.scrollTo({ top: 0, behavior: 'instant' }); else restoreUI(saved);
  } catch (error) {
    if (sequence !== renderSequence) return;
    main.innerHTML = `<section class="panel error-page">${empty('alert', 'Could not load this page', error.message, button('Retry', 'refresh', 'refresh', '', 'primary'))}</section>`;
  }
}

async function refresh(navigation = false) {
  if (refreshing) { refreshAgain = true; return; }
  refreshing = true;
  try {
    const [health, agents, tasks, metrics, orchestrations] = await Promise.all([api('/health'), api('/agents'), api('/tasks'), api('/metrics'), api('/orchestrations')]);
    Object.assign(state, { health, agents, tasks, metrics, orchestrations });
    await render(navigation);
  } catch (error) {
    state.health = null; updateChrome();
    if (!currentKey) main.innerHTML = `<section class="panel error-page">${empty('alert', 'Could not connect to the server', error.message, button('Retry', 'refresh', 'refresh', '', 'primary'))}</section>`;
  } finally { refreshing = false; if (refreshAgain) { refreshAgain = false; queueRefresh(); } }
}

function queueRefresh() { clearTimeout(debounceTimer); debounceTimer = setTimeout(() => refresh(), 350); }
setDialogRefresh(() => refresh());
window.addEventListener('hashchange', () => {
  const current = route(), key = `${current.page}/${current.id || ''}`;
  if (key !== currentKey) state.tab = 'timeline';
  currentKey = key;
  if (current.page === 'logs' && current.query.has('agent_id')) state.filters.logs.agent_id = current.query.get('agent_id');
  render(true);
});

document.addEventListener('click', async event => {
  const target = event.target.closest('[data-action]');
  if (!target || target.disabled) return;
  const { action, id, value } = target.dataset;
  try {
    if (action === 'create-agent' || action === 'edit-agent') return await agentDialog(id);
    if (action === 'freya-workspace') return await chooseWorkspace(document.querySelector('#freya-workspace'));
    if (action === 'assign') return await assignDialog(id);
    if (action === 'refresh') return await refresh();
    if (action === 'chart') { state.chart = value; return render(); }
    if (action === 'agent-tab') { state.tab = value; return render(); }
    if (action === 'clear-logs') { state.filters.logs = {}; return render(); }
    if (action === 'copy-logs') { const events = await api('/logs?limit=10000'); await navigator.clipboard.writeText(events.map(e => `${e.timestamp} [${e.level}] Task ${e.task_id || 'system'} ${e.event_type}${e.tool ? ` · ${e.tool}` : ''}${e.error ? ` · ${e.error}` : ''}`).join('\n')); toast('All logs copied to the clipboard.'); return; }
    if (action === 'copy-task-logs') { event.preventDefault(); const events = await api(`/logs?task_id=${encodeURIComponent(id)}&limit=10000`); await navigator.clipboard.writeText(serialize(events)); toast('All details for this task were copied to the clipboard.'); return; }
    if (action === 'delete-agent') return confirmAction({ title: 'Delete agent', description: 'This agent will be removed from the workspace. Agents with active tasks cannot be deleted.', label: 'Delete agent', danger: true, action: async () => { await api(`/agents/${id}`, 'DELETE', {}); location.hash = '#/agents'; toast('Agent deleted.'); } });
    if (action === 'cancel-task') return confirmAction({ title: 'Cancel this run', description: 'The runtime will be asked to cancel this run, and the request will be recorded in its history. An action already in progress may finish before cancellation takes effect.', label: 'Cancel run', danger: true, action: async () => { await api(`/tasks/${id}/cancel`, 'POST', {}); toast('Cancellation requested.'); } });
    if (action === 'cancel-orchestration') return confirmAction({ title: 'Stop Freya', description: 'Cancel Freya and all active delegated tasks.', label: 'Stop Freya', danger: true, action: async () => { await api(`/orchestrations/${id}/cancel`, 'POST', {}); toast('Freya stopped.'); } });
    if (action === 'restart-agent') return confirmAction({ title: 'Restart agent', description: 'The runtime will cancel the active run and make the agent available for new tasks.', label: 'Restart agent', action: async () => { await api(`/agents/${id}/restart`, 'POST', {}); toast('Restart requested.'); } });
    target.disabled = true;
    if (action === 'toggle-agent') { await api(`/agents/${id}`, 'PATCH', { enabled: target.dataset.enabled === 'true' }); toast(target.dataset.enabled === 'true' ? 'Agent enabled.' : 'Agent disabled.'); }
    else if (action === 'duplicate-agent') { const agent = await api(`/agents/${id}/duplicate`, 'POST', {}); location.hash = `#/agents/${agent.id}`; toast('Agent duplicated.'); }
    else if (action === 'pause-agent' || action === 'resume-agent') { await api(`/agents/${id}/${action === 'pause-agent' ? 'pause' : 'resume'}`, 'POST', {}); toast(action === 'pause-agent' ? 'Pause requested. It takes effect between actions.' : 'Agent resumed.'); }
    else if (action === 'retry-task') { const task = await api(`/tasks/${id}/retry`, 'POST', {}); location.hash = `#/tasks/${task.id}`; toast('New run created.'); }
    await refresh();
  } catch (error) { toast(error.message, true); }
  finally { target.disabled = false; }
});

document.addEventListener('change', event => {
  const target = event.target;
  if (!target.dataset.filter) return;
  state.filters[target.dataset.filter][target.name] = target.type === 'checkbox' ? target.checked : target.value;
  render();
});
document.addEventListener('submit', event => { if (event.target.id === 'log-filters') event.preventDefault(); });
document.addEventListener('submit', async event => { if (event.target.id === 'freya-form') { event.preventDefault(); const prompt=event.target.prompt.value.trim(), workspace_path=event.target.workspace_path.value.trim(); if (!prompt) return; try { await api('/orchestrations','POST',{prompt,workspace_path}); toast('Freya started the orchestration.'); await refresh(); } catch (error) { toast(error.message,true); } } });
document.addEventListener('input', event => { if (event.target.form?.id === 'freya-form' && event.target.name in state.freyaDraft) state.freyaDraft[event.target.name] = event.target.value; });
document.addEventListener('change', event => { if (event.target.form?.id === 'freya-form' && event.target.name in state.freyaDraft) state.freyaDraft[event.target.name] = event.target.value; });

function connectEvents() {
  const source = new EventSource('/api/events');
  const setConnection = connected => {
    state.connected = connected;
    document.querySelector('#connection-label').textContent = connected ? 'Connected in real time' : 'Reconnecting · polling active';
    document.querySelector('#connection-dot').className = `dot ${connected ? 'success' : 'waiting'}`;
  };
  source.addEventListener('open', () => setConnection(true));
  source.addEventListener('error', () => setConnection(false));
  source.addEventListener('update', queueRefresh);
  window.addEventListener('beforeunload', () => source.close(), { once: true });
}

async function boot() {
  try { const [tools, config] = await Promise.all([api('/tools'), api('/config')]); Object.assign(state, { tools, config }); }
  catch (error) { toast(error.message, true); }
  const current = route();
  if (current.page === 'logs' && current.query.has('agent_id')) state.filters.logs.agent_id = current.query.get('agent_id');
  await refresh(true); currentKey = `${current.page}/${current.id || ''}`;
  connectEvents(); setInterval(() => refresh(), 10000);
}
boot();
