import { state, api, esc, number, route, toast, serialize } from './core.js';
import { icon } from './icons.js';
import { empty, button } from './components.js';
import { dashboard, agents, agentDetail, skills, skillDetail, tasks, taskDetail, logs, metricsView, settings, approvals } from './views.js';
import { agentDialog, skillDialog, assignDialog, confirmAction, setDialogRefresh, chooseWorkspace } from './dialogs.js';

const main = document.querySelector('#main-content');
const pages = [['dashboard', 'Dashboard'], ['logs', 'Logs'], ['skills', 'Skills']];
let renderSequence = 0, refreshing = false, refreshAgain = false, debounceTimer, currentKey = '';
let preparedAllLogsText = null;
const freyaTerminalStatuses = new Set(['Success', 'Failed', 'Cancelled']);
const freyaObservedStatuses = new Map();
const freyaObservedApprovalIds = new Set();
let freyaStatusSnapshotReady = false, freyaApprovalSnapshotReady = false;
let freyaAudioContext = null, freyaSoundQueueEnd = 0;
const pendingFreyaSounds = [];
const FREYA_SOUND_PEAK_GAIN = 0.72;

function scheduleFreyaSound(kind, startAt) {
  const context = freyaAudioContext;
  if (!context || context.state !== 'running') return;
  const patterns = {
    clarification: [
      { frequency: 880, offset: 0, duration: 0.14 },
      { frequency: 880, offset: 0.22, duration: 0.14 },
    ],
    complete: [
      { frequency: 523.25, offset: 0, duration: 0.18 },
      { frequency: 659.25, offset: 0.2, duration: 0.18 },
      { frequency: 783.99, offset: 0.4, duration: 0.24 },
    ],
    failure: [
      { frequency: 392, offset: 0, duration: 0.2 },
      { frequency: 329.63, offset: 0.24, duration: 0.2 },
      { frequency: 261.63, offset: 0.48, duration: 0.3 },
    ],
  };
  const notes = patterns[kind] || patterns.complete;
  for (const note of notes) {
    const start = startAt + note.offset;
    const oscillator = context.createOscillator(), volume = context.createGain();
    oscillator.type = 'sine';
    oscillator.frequency.setValueAtTime(note.frequency, start);
    volume.gain.setValueAtTime(0.0001, start);
    volume.gain.exponentialRampToValueAtTime(FREYA_SOUND_PEAK_GAIN, start + 0.02);
    volume.gain.exponentialRampToValueAtTime(0.0001, start + note.duration);
    oscillator.connect(volume);
    volume.connect(context.destination);
    oscillator.start(start);
    oscillator.stop(start + note.duration + 0.02);
  }
}

function flushFreyaSounds() {
  if (!freyaAudioContext || freyaAudioContext.state !== 'running') return;
  let startAt = Math.max(freyaAudioContext.currentTime, freyaSoundQueueEnd);
  for (const kind of pendingFreyaSounds.splice(0)) {
    scheduleFreyaSound(kind, startAt);
    startAt += kind === 'clarification' ? 0.5 : 0.9;
  }
  freyaSoundQueueEnd = startAt;
}

function unlockFreyaAudio() {
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext) return;
  try {
    freyaAudioContext ||= new AudioContext();
    if (freyaAudioContext.state === 'running') flushFreyaSounds();
    else freyaAudioContext.resume().then(flushFreyaSounds).catch(() => {});
  } catch { /* Audio is optional when the browser does not support Web Audio. */ }
}

function playFreyaSound(kind) {
  pendingFreyaSounds.push(kind);
  if (freyaAudioContext?.state === 'running') {
    flushFreyaSounds();
    return;
  }
  if (freyaAudioContext && freyaAudioContext.state !== 'running') {
    freyaAudioContext.resume().then(flushFreyaSounds).catch(() => {});
  }
}

function observeFreyaStatuses(orchestrations, approvals) {
  let needsResponse = false, taskFinished = false, taskFailed = false;
  if (!freyaStatusSnapshotReady) {
    for (const run of orchestrations) if (run.id) freyaObservedStatuses.set(run.id, run.status);
    freyaStatusSnapshotReady = true;
  } else {
    for (const run of orchestrations) {
      if (!run.id) continue;
      const previous = freyaObservedStatuses.get(run.id);
      if (run.status === 'NeedsClarification' && previous !== 'NeedsClarification') needsResponse = true;
      if (run.status === 'Success' && !freyaTerminalStatuses.has(previous)) taskFinished = true;
      if (run.status === 'Failed' && !freyaTerminalStatuses.has(previous)) taskFailed = true;
      freyaObservedStatuses.set(run.id, run.status);
    }
  }
  if (!freyaApprovalSnapshotReady) {
    for (const approval of approvals) if (approval.id) freyaObservedApprovalIds.add(approval.id);
    freyaApprovalSnapshotReady = true;
  } else {
    for (const approval of approvals) {
      if (!approval.id) continue;
      if (!freyaObservedApprovalIds.has(approval.id)) needsResponse = true;
      freyaObservedApprovalIds.add(approval.id);
    }
  }
  if (needsResponse) playFreyaSound('clarification');
  if (taskFinished) playFreyaSound('complete');
  if (taskFailed) playFreyaSound('failure');
}

document.addEventListener('pointerdown', unlockFreyaAudio, { passive: true });
document.addEventListener('keydown', unlockFreyaAudio);

function agentExportPayload(agent) {
  const config = agent.config && typeof agent.config === 'object' ? JSON.parse(JSON.stringify(agent.config)) : {};
  if (!config.capability_policy && agent.capability_policy) config.capability_policy = agent.capability_policy;
  return {
    name: agent.name,
    description: agent.description || '',
    role: agent.role || '',
    instructions: agent.instructions || '',
    enabled: agent.enabled !== false,
    skills: (agent.skills || []).map(skill => ({ skill_id: skill.id || skill.skill_id, priority: Number.isInteger(skill.priority) ? skill.priority : 0 })).filter(skill => skill.skill_id),
    config,
  };
}

function downloadAgentJson(agent) {
  const safeName = String(agent.name || 'agent').trim().replace(/[^a-z0-9._-]+/gi, '-').replace(/^-+|-+$/g, '') || 'agent';
  const blob = new Blob([JSON.stringify(agentExportPayload(agent), null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = `${safeName}.json`; document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function importAgentFromFile() {
  const input = document.createElement('input');
  input.type = 'file'; input.accept = '.json,application/json'; input.hidden = true;
  const cleanup = () => input.remove();
  input.addEventListener('cancel', cleanup, { once: true });
  input.addEventListener('change', async () => {
    const file = input.files?.[0]; cleanup();
    if (!file) return;
    try {
      const raw = JSON.parse(await file.text());
      const source = raw && typeof raw === 'object' && raw.agent && typeof raw.agent === 'object' && !Array.isArray(raw.agent) ? raw.agent : raw;
      if (!source || typeof source !== 'object' || Array.isArray(source)) throw new Error('The JSON must contain one agent object.');
      const name = String(source.name || '').trim();
      if (!name) throw new Error('The imported agent must have a name.');
      const skills = Array.isArray(source.skills) ? source.skills.map(item => {
        if (typeof item === 'string') return item;
        if (!item || typeof item !== 'object') throw new Error('Each imported skill must be an id or assignment object.');
        const skillId = item.skill_id || item.id;
        if (!skillId) throw new Error('Each imported skill assignment must have an id.');
        const priority = item.priority == null ? 0 : Number(item.priority);
        if (!Number.isInteger(priority)) throw new Error(`Invalid priority for skill ${skillId}.`);
        return { skill_id: skillId, priority };
      }) : [];
      const config = source.config && typeof source.config === 'object' && !Array.isArray(source.config) ? source.config : {};
      const endpoint = String(config.endpoint || 'http://127.0.0.1:11434').trim();
      const model = String(config.model || '').trim();
      if (!model) throw new Error('The imported agent must specify an Ollama model.');
      const modelCheck = await api('/models?' + new URLSearchParams({ endpoint }).toString());
      if (modelCheck.error) throw new Error(`Ollama model validation failed: ${modelCheck.error}`);
      const installedModels = new Set((modelCheck.models || []).map(item => String(item.name || '').trim()).filter(Boolean));
      if (!installedModels.has(model)) throw new Error(`Model "${model}" is not installed in Ollama.`);
      const payload = { name, description: source.description ?? '', role: source.role ?? '', instructions: source.instructions ?? '', enabled: source.enabled !== false, skills, config };
      if (Array.isArray(source.tools)) payload.tools = source.tools;
      for (const key of ['identity', 'behavior', 'autonomy', 'verification', 'output', 'purpose', 'responsibilities', 'constraints', 'capability_policy']) {
        if (Object.prototype.hasOwnProperty.call(source, key)) payload[key] = source[key];
      }
      const agent = await api('/agents', 'POST', payload);
      location.hash = `#/agents/${agent.id}`;
      toast('Agent imported.');
      await refresh();
    } catch (error) { toast(error.message || 'Could not import the agent JSON.', true); }
  }, { once: true });
  document.body.append(input); input.click();
}

const SKILL_EXPORT_FIELDS = ['id', 'name', 'description', 'category', 'version', 'instructions', 'procedures', 'recommended_capabilities', 'required_capabilities', 'tags', 'enabled', 'source', 'metadata'];

function skillExportPayload(skill) {
  return Object.fromEntries(SKILL_EXPORT_FIELDS.filter(key => Object.prototype.hasOwnProperty.call(skill, key)).map(key => [key, JSON.parse(JSON.stringify(skill[key]))]));
}

function downloadJson(filename, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

async function allRuntimeTaskLogs(taskId) {
  const events = [], pageSize = 1000;
  let after = 0;
  while (true) {
    const params = new URLSearchParams({ task_id: taskId, after: String(after), limit: String(pageSize) });
    const page = await api(`/logs?${params}`);
    if (!Array.isArray(page)) throw new Error('The server returned an invalid task log page.');
    events.push(...page);
    if (page.length < pageSize) return events;
    const lastId = Number(page[page.length - 1]?.id);
    if (!Number.isSafeInteger(lastId) || lastId <= after) throw new Error('Could not continue reading this task log.');
    after = lastId;
  }
}

async function orchestrationLogExport(id) {
  try {
    const events = await api(`/orchestrations/${encodeURIComponent(id)}/logs`);
    if (!Array.isArray(events)) throw new Error('The server returned an invalid task log export.');
    return events;
  } catch (error) {
    if (!/route not found/i.test(error.message || '')) throw error;
  }

  const encodedId = encodeURIComponent(id);
  const run = await api(`/orchestrations/${encodedId}`);
  const orchestrationEvents = run.events;
  if (!Array.isArray(run.delegations) || !Array.isArray(orchestrationEvents)) {
    throw new Error('The server does not expose all persisted logs for this task.');
  }

  const taskIds = [...new Set(run.delegations.map(item => item.task_id).filter(Boolean))];
  const runtimePages = await Promise.all(taskIds.map(taskId => allRuntimeTaskLogs(taskId)));
  const runtimeEvents = runtimePages.flat();
  const systemEvents = orchestrationEvents.map(event => {
    let payload = {};
    try {
      payload = typeof event.payload_json === 'string' ? JSON.parse(event.payload_json) : event.payload_json || {};
    } catch { /* Keep the stored payload_json below if it cannot be parsed. */ }
    return {
      ...payload,
      ...event,
      id: `orchestration:${event.id}`,
      log_id: `orchestration:${event.id}`,
      source: 'orchestration',
      orchestration_id: event.orchestration_id || id,
    };
  });
  return [...runtimeEvents, ...systemEvents].sort((left, right) =>
    String(left.timestamp || '').localeCompare(String(right.timestamp || '')) ||
    String(left.id || '').localeCompare(String(right.id || '')));
}

function importSkillsFromFile() {
  const input = document.createElement('input');
  input.type = 'file'; input.accept = '.json,application/json'; input.hidden = true;
  const cleanup = () => input.remove();
  input.addEventListener('cancel', cleanup, { once: true });
  input.addEventListener('change', async () => {
    const file = input.files?.[0]; cleanup();
    if (!file) return;
    try {
      const raw = JSON.parse(await file.text());
      const skills = Array.isArray(raw) ? raw : Array.isArray(raw?.skills) ? raw.skills : raw?.skill && typeof raw.skill === 'object' ? [raw.skill] : raw && typeof raw === 'object' ? [raw] : [];
      if (!skills.length) throw new Error('The JSON must contain one Skill object or a skills array.');
      const response = await api('/skills/import', 'POST', { skills });
      const importedCount = Number(response.count) || 0, skippedCount = Array.isArray(response.skipped) ? response.skipped.length : 0;
      toast(importedCount + (importedCount === 1 ? ' Skill imported.' : ' Skills imported.') + (skippedCount ? ' ' + skippedCount + ' already existed and were skipped.' : ''));
      await refresh();
    } catch (error) { toast(error.message || 'Could not import the Skill JSON.', true); }
  }, { once: true });
  document.body.append(input); input.click();
}
function updateChrome() {
  const current = route(), title = pages.find(([page]) => page === current.page)?.[1] || 'Workspace';
  document.querySelector('#breadcrumb').textContent = title;
  document.querySelector('#breadcrumb-icon').innerHTML = icon(current.page);
  document.title = `FREYA · ${title}`;
  document.querySelectorAll('[data-nav]').forEach(link => { const active = link.dataset.nav === current.page; link.classList.toggle('active', active); if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current'); });
  const m = state.metrics || {}, good = state.health?.status === 'ok';
  document.querySelector('#system-overview').innerHTML = `<span class="header-system-status"><span class="dot ${good ? 'success' : 'error'}"></span>${good ? 'System online' : 'API offline'}</span><span class="header-divider"></span><span>${icon('agents')}<strong>${number(m.active_agents)}</strong> active</span><span>${icon('activity')}<strong>${number(m.running_tasks)}</strong> running</span>`;
}

function preserveUI() {
  const active = document.activeElement, focused = main.contains(active) && active.name ? { name: active.name, filter: active.dataset.filter, selectionStart: active.selectionStart, selectionEnd: active.selectionEnd, value: active.value } : null;
  const consoleBody = main.querySelector('.mission-console-body');
  return { y: window.scrollY, focused, consoleScrollTop: consoleBody?.scrollTop, open: [...main.querySelectorAll('details[open][data-detail]')].map(element => element.dataset.detail) };
}
function restoreUI(saved) {
  saved.open.forEach(id => [...main.querySelectorAll('details[data-detail]')].find(item => item.dataset.detail === id)?.setAttribute('open', ''));
  const consoleBody = main.querySelector('.mission-console-body');
  if (consoleBody && saved.consoleScrollTop != null) consoleBody.scrollTop = saved.consoleScrollTop;
  if (saved.focused) {
    const target = [...main.querySelectorAll('[name]')].find(item => item.name === saved.focused.name && item.dataset.filter === saved.focused.filter);
    if (target) { target.value = saved.focused.value; target.focus({ preventScroll: true }); try { target.setSelectionRange(saved.focused.selectionStart, saved.focused.selectionEnd); } catch {} }
  }
  window.scrollTo({ top: saved.y, behavior: 'instant' });
}

async function render(navigation = false) {
  const current = route();
  if (current.page === 'freya') { location.hash = '#/dashboard'; return; }
  const sequence = ++renderSequence;
  updateChrome();
  try {
    let html;
    if (current.page === 'dashboard') html = await dashboard();
    else if (current.page === 'agents') html = current.id ? await agentDetail(current.id) : agents();
    else if (current.page === 'skills') html = current.id ? await skillDetail(current.id) : skills();
    else if (current.page === 'tasks') { location.hash = current.id ? `#/logs?task_id=${encodeURIComponent(current.id)}` : '#/logs'; return; }
    else if (current.page === 'approvals') html = approvals();
    else if (current.page === 'logs') html = await logs();
    else if (current.page === 'metrics') html = metricsView(state.metrics);
    else if (current.page === 'settings') html = settings();
    else html = empty('search', 'This page does not exist', 'Return to the dashboard to continue.', '<a class="button primary" href="#/dashboard">Open dashboard</a>');
    if (sequence !== renderSequence) return;
    const saved = navigation ? null : preserveUI();
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
    const [health, agents, skills, tasks, approvals, metrics, orchestrations] = await Promise.all([api('/health'), api('/agents'), api('/skills'), api('/tasks'), api('/approvals?status=pending'), api('/metrics'), api('/orchestrations')]);
    observeFreyaStatuses(orchestrations, approvals);
    Object.assign(state, { health, agents, skills, tasks, approvals, metrics, orchestrations });
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
  if (current.page === 'logs') { state.filters.logs.agent_id = current.query.get('agent_id') || ''; state.filters.logs.task_id = current.query.get('task_id') || ''; state.filters.logs.orchestration_id = current.query.get('orchestration_id') || ''; }
  render(true);
});

document.addEventListener('click', async event => {
  const target = event.target.closest('[data-action]');
  if (!target || target.disabled) return;
  const { action, id, value, orchestrationId } = target.dataset;
  try {
    if (action === 'import-agent') { importAgentFromFile(); return; }
    if (action === 'import-skills') { importSkillsFromFile(); return; }
    if (action === 'export-skills') { downloadJson('skills.json', { version: 1, skills: state.skills.map(skillExportPayload) }); toast('Skills exported as JSON.'); return; }
    if (action === 'export-skill') { const skill = await api('/skills/' + encodeURIComponent(id)); const safeName = String(skill.name || skill.id || 'skill').trim().replace(/[^a-z0-9._-]+/gi, '-').replace(/^-+|-+$/g, '') || 'skill'; downloadJson(safeName + '.json', skillExportPayload(skill)); toast('Skill exported as JSON.'); return; }
    if (action === 'export-agent') { const agent = await api(`/agents/${encodeURIComponent(id)}`); downloadAgentJson(agent); toast('Agent exported as JSON.'); return; }
    if (action === 'create-agent' || action === 'edit-agent') return await agentDialog(id);
    if (action === 'create-skill' || action === 'edit-skill') return await skillDialog(id);
    if (action === 'duplicate-skill') { const skill = await api(`/skills/${id}/duplicate`, 'POST', {}); location.hash = `#/skills/${skill.id}`; toast('Skill duplicated.'); await refresh(); return; }
    if (action === 'delete-skill') return confirmAction({ title: 'Delete skill', description: 'Assigned or historical skills are disabled so task snapshots remain readable.', label: 'Delete skill', danger: true, action: async () => { await api(`/skills/${id}`, 'DELETE', {}); location.hash = '#/skills'; toast('Skill deleted or disabled.'); } });
    if (action === 'freya-workspace') return await chooseWorkspace(document.querySelector('#freya-workspace'));
    if (action === 'assign') return await assignDialog(id);
    if (action === 'refresh') return await refresh();
    if (action === 'approve-once' || action === 'approve-task' || action === 'approve-file-intent' || action === 'deny-approval') {
      const endpoint = action === 'approve-once' ? 'approve-once' : action === 'approve-task' ? 'approve-task' : action === 'approve-file-intent' ? 'approve-file-intent' : 'deny';
      await api('/approvals/' + id + '/' + endpoint, 'POST', {});
      toast(endpoint === 'deny' ? 'Approval denied.' : 'Approval resolved.');
      await refresh();
      return;
    }
    if (action === 'create-programmer') {
      const agent = await api('/agent-presets/programmer', 'POST', {});
      location.hash = '#/agents/' + agent.id;
      toast('Programmer agent created.');
      await refresh();
      return;
    }
    if (action === 'chart') { state.chart = value; return render(); }
    if (action === 'agent-tab') { state.tab = value; return render(); }
    if (action === 'clear-logs') { state.filters.logs = {}; return render(); }
    if (action === 'show-freya-activity') { state.freyaRunId = id; return render(); }
    if (action === 'copy-logs') {
      event.preventDefault();
      if (preparedAllLogsText !== null) {
        const clipboardWrite = navigator.clipboard.writeText(preparedAllLogsText);
        await clipboardWrite;
        preparedAllLogsText = null;
        toast('All logs copied to the clipboard.');
        return;
      }
      const clipboardText = api('/logs?limit=10000').then(events => serialize({ event_count: events.length, events }));
      if (navigator.clipboard?.write && typeof ClipboardItem === 'function') {
        try {
          const item = new ClipboardItem({ 'text/plain': clipboardText.then(text => new Blob([text], { type: 'text/plain' })) });
          await navigator.clipboard.write([item]);
          toast('All logs copied to the clipboard.');
          return;
        } catch (clipboardError) {
          if (clipboardError?.name !== 'NotAllowedError' && !/activation/i.test(String(clipboardError?.message || ''))) throw clipboardError;
        }
      }
      preparedAllLogsText = await clipboardText;
      toast('Logs are ready. Click Copy all logs again to copy them.');
      return;
    }
    if (action === 'copy-orchestration-logs') {
      target.disabled = true;
      const events = await orchestrationLogExport(id);
      await navigator.clipboard.writeText(serialize({ orchestration_id: id, event_count: events.length, events }));
      toast(`${events.length} task logs copied for all agents.`);
      return;
    }
    if (action === 'copy-task-logs') { event.preventDefault(); const filter = orchestrationId ? `orchestration_id=${encodeURIComponent(orchestrationId)}` : `task_id=${encodeURIComponent(id)}`; const events = await api(`/logs?${filter}&limit=10000`); await navigator.clipboard.writeText(serialize(events)); toast('All details for this task were copied to the clipboard.'); return; }
    if (action === 'copy-task-details') { const task = await api(`/tasks/${encodeURIComponent(id)}`); await navigator.clipboard.writeText(serialize(task)); toast('Task details copied to the clipboard.'); return; }
    if (action === 'delete-agent') return confirmAction({ title: 'Delete agent', description: 'This agent will be removed from the workspace. Agents with active tasks cannot be deleted.', label: 'Delete agent', danger: true, action: async () => { await api(`/agents/${id}`, 'DELETE', {}); location.hash = '#/agents'; toast('Agent deleted.'); } });
    if (action === 'cancel-task') return confirmAction({ title: 'Cancel this run', description: 'The runtime will be asked to cancel this run, and the request will be recorded in its history. An action already in progress may finish before cancellation takes effect.', label: 'Cancel run', danger: true, action: async () => { await api(`/tasks/${id}/cancel`, 'POST', {}); toast('Cancellation requested.'); } });
    if (action === 'cancel-orchestration') return confirmAction({ title: 'Stop Freya', description: 'Cancel Freya and all active delegated tasks.', label: 'Stop Freya', danger: true, action: async () => { await api(`/orchestrations/${id}/cancel`, 'POST', {}); toast('Freya stopped.'); } });
    if (action === 'restart-agent') return confirmAction({ title: 'Restart agent', description: 'The runtime will cancel the active run and make the agent available for new tasks.', label: 'Restart agent', action: async () => { await api(`/agents/${id}/restart`, 'POST', {}); toast('Restart requested.'); } });
    target.disabled = true;
    if (action === 'toggle-agent') { await api(`/agents/${id}`, 'PATCH', { enabled: target.dataset.enabled === 'true' }); toast(target.dataset.enabled === 'true' ? 'Agent enabled.' : 'Agent disabled.'); }
    else if (action === 'duplicate-agent') { const agent = await api(`/agents/${id}/duplicate`, 'POST', {}); location.hash = `#/agents/${agent.id}`; toast('Agent duplicated.'); }
    else if (action === 'pause-agent' || action === 'resume-agent') { await api(`/agents/${id}/${action === 'pause-agent' ? 'pause' : 'resume'}`, 'POST', {}); toast(action === 'pause-agent' ? 'Pause requested. It takes effect between actions.' : 'Agent resumed.'); }
    else if (action === 'retry-task') { const task = await api(`/tasks/${id}/retry`, 'POST', {}); location.hash = `#/logs?task_id=${task.id}`; toast('New run created.'); }
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
document.addEventListener('submit', async event => {
  if (!event.target.classList.contains('freya-clarification-form')) return;
  event.preventDefault();
  const form = event.target;
  if (form.dataset.submitting === 'true') return;
  const answers = Object.fromEntries(new FormData(form).entries());
  form.dataset.submitting = 'true';
  const submitButton = form.querySelector('button[type="submit"]');
  if (submitButton) submitButton.disabled = true;
  try {
    await api('/orchestrations/' + encodeURIComponent(form.dataset.runId) + '/clarifications', 'POST', { answers });
    toast('Freya recibió tu respuesta.');
    await refresh();
  } catch (error) {
    toast(error.message, true);
    form.dataset.submitting = 'false';
    if (submitButton) submitButton.disabled = false;
  }
});
document.addEventListener('submit', async event => {
  if (event.target.id !== 'freya-form') return;
  event.preventDefault();
  const form = event.target;
  if (form.dataset.submitting === 'true') return;
  const prompt = form.prompt.value.trim(), workspace_path = form.workspace_path.value.trim();
  if (!prompt) return;
  form.dataset.submitting = 'true';
  const submitButton = form.querySelector('button[type="submit"]');
  if (submitButton) submitButton.disabled = true;
  try {
    const run = await api('/orchestrations', 'POST', { prompt, workspace_path });
    state.freyaHistory = [];
    state.freyaSessionStartedAt = run.created_at || new Date().toISOString();
    state.freyaRunId = run.id || null;
    toast('Freya started the orchestration.');
    await refresh();
  } catch (error) { toast(error.message, true); }
  finally {
    form.dataset.submitting = 'false';
    if (submitButton) submitButton.disabled = false;
  }
});
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
  try { const [tools, config, skills] = await Promise.all([api('/tools'), api('/config'), api('/skills')]); Object.assign(state, { tools, config, skills }); }
  catch (error) { toast(error.message, true); }
  const current = route();
  if (current.page === 'logs') { state.filters.logs.agent_id = current.query.get('agent_id') || ''; state.filters.logs.task_id = current.query.get('task_id') || ''; state.filters.logs.orchestration_id = current.query.get('orchestration_id') || ''; }
  await refresh(true); currentKey = `${current.page}/${current.id || ''}`;
  connectEvents(); setInterval(() => refresh(), 10000);
}
boot();
