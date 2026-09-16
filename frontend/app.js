import { state, api, esc, number, route, toast } from './core.js';
import { icon } from './icons.js';
import { empty, button } from './components.js';
import { dashboard, agents, agentDetail, tasks, taskDetail, logs, metricsView, settings } from './views.js';
import { agentDialog, assignDialog, confirmAction, setDialogRefresh } from './dialogs.js';

const main = document.querySelector('#main-content');
const pages = [['dashboard', 'Dashboard'], ['agents', 'Agents'], ['tasks', 'Tasks'], ['logs', 'Logs'], ['metrics', 'Metrics'], ['settings', 'Settings']];
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
  document.querySelector('#system-overview').innerHTML = `<span class="header-system-status"><span class="dot ${good ? 'success' : 'error'}"></span>${good ? 'Sistema operativo' : 'API sin conexión'}</span><span class="header-divider"></span><span>${icon('agents')}<strong>${number(m.active_agents)}</strong> activos</span><span>${icon('activity')}<strong>${number(m.running_tasks)}</strong> ejecutando</span>`;
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
    if (current.page === 'dashboard') html = dashboard();
    else if (current.page === 'agents') html = current.id ? await agentDetail(current.id) : agents();
    else if (current.page === 'tasks') html = current.id ? await taskDetail(current.id) : await tasks();
    else if (current.page === 'logs') html = await logs();
    else if (current.page === 'metrics') html = metricsView(state.metrics);
    else if (current.page === 'settings') html = settings();
    else html = empty('search', 'Esta vista no existe', 'Vuelve al dashboard para continuar.', '<a class="button primary" href="#/dashboard">Abrir dashboard</a>');
    if (sequence !== renderSequence) return;
    main.innerHTML = html;
    if (navigation) window.scrollTo({ top: 0, behavior: 'instant' }); else restoreUI(saved);
  } catch (error) {
    if (sequence !== renderSequence) return;
    main.innerHTML = `<section class="panel error-page">${empty('alert', 'No se pudo cargar esta vista', error.message, button('Reintentar', 'refresh', 'refresh', '', 'primary'))}</section>`;
  }
}

async function refresh(navigation = false) {
  if (refreshing) { refreshAgain = true; return; }
  refreshing = true;
  try {
    const [health, agents, tasks, metrics] = await Promise.all([api('/health'), api('/agents'), api('/tasks'), api('/metrics')]);
    Object.assign(state, { health, agents, tasks, metrics });
    await render(navigation);
  } catch (error) {
    state.health = null; updateChrome();
    if (!currentKey) main.innerHTML = `<section class="panel error-page">${empty('alert', 'No se pudo conectar con el servidor', error.message, button('Reintentar', 'refresh', 'refresh', '', 'primary'))}</section>`;
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
    if (action === 'assign') return await assignDialog(id);
    if (action === 'refresh') return await refresh();
    if (action === 'chart') { state.chart = value; return render(); }
    if (action === 'agent-tab') { state.tab = value; return render(); }
    if (action === 'clear-logs') { state.filters.logs = {}; return render(); }
    if (action === 'delete-agent') return confirmAction({ title: 'Eliminar agente', description: 'El agente se eliminará del workspace. No se puede eliminar mientras tiene tareas activas.', label: 'Eliminar agente', danger: true, action: async () => { await api(`/agents/${id}`, 'DELETE', {}); location.hash = '#/agents'; toast('Agente eliminado.'); } });
    if (action === 'cancel-task') return confirmAction({ title: 'Cancelar esta ejecución', description: 'Se solicitará la cancelación al runtime y quedará registrada en el historial. Una acción en curso puede terminar antes de que se aplique.', label: 'Cancelar ejecución', danger: true, action: async () => { await api(`/tasks/${id}/cancel`, 'POST', {}); toast('Cancelación solicitada.'); } });
    if (action === 'restart-agent') return confirmAction({ title: 'Reiniciar agente', description: 'El runtime cancelará la ejecución activa y dejará al agente disponible para recibir nuevas tareas.', label: 'Reiniciar agente', action: async () => { await api(`/agents/${id}/restart`, 'POST', {}); toast('Reinicio solicitado.'); } });
    target.disabled = true;
    if (action === 'toggle-agent') { await api(`/agents/${id}`, 'PATCH', { enabled: target.dataset.enabled === 'true' }); toast(target.dataset.enabled === 'true' ? 'Agente activado.' : 'Agente desactivado.'); }
    else if (action === 'duplicate-agent') { const agent = await api(`/agents/${id}/duplicate`, 'POST', {}); location.hash = `#/agents/${agent.id}`; toast('Agente duplicado.'); }
    else if (action === 'pause-agent' || action === 'resume-agent') { await api(`/agents/${id}/${action === 'pause-agent' ? 'pause' : 'resume'}`, 'POST', {}); toast(action === 'pause-agent' ? 'Pausa solicitada. Se aplica entre acciones.' : 'Agente reanudado.'); }
    else if (action === 'retry-task') { const task = await api(`/tasks/${id}/retry`, 'POST', {}); location.hash = `#/tasks/${task.id}`; toast('Nueva ejecución creada.'); }
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

function connectEvents() {
  const source = new EventSource('/api/events');
  const setConnection = connected => {
    state.connected = connected;
    document.querySelector('#connection-label').textContent = connected ? 'Conectado en tiempo real' : 'Reconectando · sondeo activo';
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
