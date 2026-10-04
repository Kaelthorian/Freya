import { state, api, esc, number, compact, duration, date, bytes, query, serialize, liveTask, uiText, taskTitle } from './core.js';
import { icon } from './icons.js';
import { badge, button, heading, panelHeading, empty, stat, progress, taskTable, agentCard, chart, barChart, timeline, logTable, groupedLogTable } from './components.js';

const agentOptions = selected => `<option value="">All agents</option>${state.agents.map(a => `<option value="${esc(a.id)}" ${selected === a.id ? 'selected' : ''}>${esc(a.name)}</option>`).join('')}`;
const tabs = (items, selected, action) => `<div class="tabs" role="tablist">${items.map(([value, label]) => `<button role="tab" aria-selected="${selected === value}" class="tab ${selected === value ? 'active' : ''}" data-action="${action}" data-value="${value}">${label}</button>`).join('')}</div>`;
const chartTabs = () => tabs([['tasks', 'Tasks'], ['tokens', 'Tokens'], ['errors', 'Errors'], ['duration_seconds', 'Duration']], state.chart, 'chart');
function skillContextPreview(skill) {
  const lines = [`## ${skill.name}`, `Version: ${skill.version}`, `Category: ${skill.category || 'General'}`];
  if (skill.description) lines.push('', 'Purpose:', skill.description);
  if (skill.instructions?.length) lines.push('', 'Instructions:', ...skill.instructions.map(item => `- ${item}`));
  if (skill.procedures?.length) {
    lines.push('', 'Relevant procedures:');
    skill.procedures.forEach(procedure => { lines.push(`### ${procedure.name}`); if (procedure.description) lines.push(procedure.description); (procedure.steps || []).forEach((step, index) => lines.push(`${index + 1}. ${step}`)); });
  }
  if (skill.required_capabilities?.length) lines.push('', 'Required capabilities: ' + skill.required_capabilities.join(', '));
  if (skill.recommended_capabilities?.length) lines.push('Recommended capabilities: ' + skill.recommended_capabilities.join(', '));
  return lines.join('\n');
}

const FREYA_ACTIVE_STATUSES = new Set(['Queued', 'Planning', 'Planned', 'Running', 'Integrating', 'WaitingForApproval', 'Paused']);
const FREYA_ORCHESTRATION_ACTIVE = new Set(['Queued', 'Analyzing', 'NeedsClarification', 'Planning', 'Planned', 'Running', 'Integrating']);

function activityClock(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleTimeString('en-US', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
}

function activityDetails(details = {}) {
  const rows = Object.entries(details).filter(([, value]) => value !== null && value !== undefined && value !== '');
  if (!rows.length) return '';
  return '<dl class="freya-activity-details">' + rows.map(([key, value]) =>
    '<dt>' + esc(key.replaceAll('_', ' ')) + '</dt><dd>' +
    (typeof value === 'object' ? '<pre>' + esc(serialize(value)) + '</pre>' : '<span>' + esc(value) + '</span>') +
    '</dd>').join('') + '</dl>';
}

function freyaActivityPanel(activity, run) {
  if (!activity) return '';
  const llm = activity.llm || {};
  const stats = [
    ['Total elapsed', activity.total_elapsed_seconds],
    ['Freya processing', activity.processing_seconds],
    ['Worker execution', activity.execution_seconds],
    ['Waiting for clarification', activity.waiting_for_user_seconds],
    ['Waiting for approvals', activity.waiting_for_approval_seconds],
    ['LLM time', llm.duration_seconds],
  ];
  const phaseRows = (activity.phases || []).map(phase => {
    const status = phase.fallback_used ? 'Success (fallback)' : phase.status;
    const extra = { ...phase };
    for (const key of ['name', 'label', 'actor_type', 'started_at', 'completed_at', 'duration_seconds', 'status']) delete extra[key];
    return '<tr><td><strong>' + esc(phase.label || phase.name) + '</strong>' +
      (phase.actor_name && phase.actor_name !== phase.label ? '<span class="table-sub">' + esc(phase.actor_name) + '</span>' : '') +
      '</td><td class="mono nowrap">' + activityClock(phase.started_at) + '</td><td>' + badge(status || 'Running') +
      '</td><td class="mono nowrap">' + duration(phase.duration_seconds) +
      '</td><td>' + (Object.keys(extra).length ? '<details><summary>Details</summary>' + activityDetails(extra) + '</details>' : '') + '</td></tr>';
  }).join('');
  const eventRows = (activity.events || []).map((event, index) => '<article class="freya-activity-event ' +
    (String(event.status || '').toLowerCase().includes('fail') ? 'error' : '') + '"><time class="mono">' +
    activityClock(event.timestamp) + '</time><div class="freya-activity-event-body"><div class="freya-activity-event-heading"><strong>' +
    esc(event.component || 'System') + '</strong><span>' + esc(event.event || event.event_type || 'event') + '</span>' +
    badge(event.status || 'Info') + (event.duration_seconds != null ? '<span class="mono">' + duration(event.duration_seconds) + '</span>' : '') +
    '</div>' + (event.message ? '<p>' + esc(uiText(event.message)) + '</p>' : '') +
    ((event.error || Object.keys(event.details || {}).length) ? '<details data-detail="freya-event-' + esc(event.id || index) + '"><summary>Details</summary>' +
      (event.error ? '<p class="event-error">' + esc(uiText(event.error)) + '</p>' : '') + activityDetails(event.details || {}) +
      '</details>' : '') + '</div></article>').join('');
  return '<section class="panel freya-orchestration-activity"><div class="freya-section-heading"><div><span class="eyebrow">ORCHESTRATION ACTIVITY</span><h2>' +
    esc(taskTitle(run?.prompt || 'Freya run')) + '</h2><p>Chronological system and worker events. Durations come from persisted timestamps.</p></div><span>' +
    badge(activity.status || run?.status || 'Running') + '</span></div>' +
    '<div class="freya-performance-grid">' + stats.map(([label, seconds]) => '<div class="freya-performance-card"><span>' + esc(label) +
      '</span><strong>' + duration(seconds) + '</strong></div>').join('') +
    '<div class="freya-performance-card"><span>LLM calls · tokens</span><strong>' + number(llm.calls) + ' · ' + compact(llm.total_tokens) + '</strong></div></div>' +
    '<p class="freya-duration-note">Processing excludes clarification and approval waits. Worker execution and LLM time are subsets of processing. Phase durations can overlap; do not add them to the elapsed total.</p>' +
    '<div class="table-wrap"><table class="freya-phase-table"><thead><tr><th>PHASE</th><th>STARTED</th><th>STATUS</th><th>DURATION</th><th></th></tr></thead><tbody>' +
    (phaseRows || '<tr><td colspan="5">No phase events recorded yet.</td></tr>') + '</tbody></table></div>' +
    '<div class="freya-event-timeline">' + (eventRows || '<p class="muted">No activity events have been recorded yet.</p>') + '</div></section>';
}

function freyaHistoryEntry(kind, id, data, agent) {
  const existing = state.freyaHistory.find(item => item.kind === kind && item.id === id);
  if (existing) {
    if (kind === 'task') existing.task = { ...existing.task, ...data };
    else existing.run = { ...existing.run, ...data };
    if (agent) existing.agent = agent;
    return existing;
  }
  const item = kind === 'task' ? { kind, id, task: data, agent: agent || null } : { kind, id, run: data };
  state.freyaHistory.push(item);
  return item;
}

function freyaStartedAt(item) {
  const value = item.kind === 'task' ? (item.task.started_at || item.task.created_at) : item.run.created_at;
  const timestamp = value ? new Date(value).getTime() : 0;
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function freyaIsInCurrentSession(item) {
  if (!state.freyaSessionStartedAt) return false;
  if (item.id === state.freyaRunId) return true;
  const start = new Date(state.freyaSessionStartedAt).getTime();
  return Number.isFinite(start) && freyaStartedAt(item) >= start - 1000;
}

function freyaTaskRow(item) {
  const task = item.task || {}, id = String(task.id || item.id), active = FREYA_ACTIVE_STATUSES.has(task.status);
  const agent = item.agent || {}, agentId = agent.id || task.agent_id, agentName = agent.name || task.agent_name || 'Agent';
  const startedAt = task.started_at || task.created_at;
  const elapsed = active ? (startedAt ? Math.max(0, (Date.now() - new Date(startedAt).getTime()) / 1000) : 0) : Number(task.duration_seconds) || 0;
  const title = taskTitle(task.prompt);
  return [
    '<tr>',
    '<td>' + badge(task.status || 'Pending') + '</td>',
    '<td><a class="table-title" title="' + esc(task.prompt || '') + '" href="#/logs?task_id=' + esc(id) + '">' + esc(title) + '</a><span class="table-sub mono">' + esc(id.slice(0, 12)) + '</span></td>',
    '<td><a class="table-agent" href="#/agents/' + esc(agentId || '') + '"><span class="avatar avatar-small">' + icon('agents') + '</span>' + esc(agentName) + '</a><span class="table-sub">' + esc(agent.role || task.agent_role || 'Agent') + '</span></td>',
    '<td class="nowrap muted">' + date(startedAt) + '</td>',
    '<td class="mono nowrap">' + duration(elapsed) + '</td>',
    '<td class="mono">' + compact(task.total_tokens) + '</td>',
    '<td class="mono">' + number(task.model_calls) + '</td>',
    '<td><a class="text-link" href="#/logs?task_id=' + esc(id) + '">Details ' + icon('arrow') + '</a></td>',
    '</tr>'
  ].join('');
}

function freyaRunRow(item) {
  const run = item.run || {}, active = FREYA_ORCHESTRATION_ACTIVE.has(run.status);
  const startedAt = run.created_at, elapsed = active && startedAt ? Math.max(0, (Date.now() - new Date(startedAt).getTime()) / 1000) : Number(run.duration_seconds) || 0;
  const failed = run.status === 'Failed', failure = failed ? taskTitle(uiText(run.error || run.response || 'Failure cause unavailable.'), 120) : '';
  const action = run.status === 'NeedsClarification' ? '<a class="text-link" href="#freya-clarification-' + esc(run.id) + '">Responder</a>' : active ? button('Stop', 'cancel-orchestration', 'stop', 'data-id="' + esc(run.id) + '"', 'small-button danger-quiet') : '<a class="text-link" href="#/logs?orchestration_id=' + encodeURIComponent(run.id || '') + '">' + (failed ? 'Diagnosis' : 'Logs') + ' ' + icon('arrow') + '</a>';
  const copyLogsAction = button('Copy all logs', 'copy-orchestration-logs', 'copy', 'type="button" data-id="' + esc(run.id) + '"', 'small-button');
  const activityAction = '<button type="button" class="text-link freya-view-activity" data-action="show-freya-activity" data-id="' + esc(run.id) + '">Activity</button>';
  return [
    '<tr>',
    '<td>' + badge(run.status || 'Pending') + '</td>',
    '<td><strong class="table-title">' + esc(taskTitle(run.prompt)) + '</strong><span class="table-sub" title="' + esc(failure) + '">' + (failed ? esc(failure) : active ? 'Waiting for a delegated task to start' : 'Orchestration completed') + '</span></td>',
    '<td><strong>Freya</strong><span class="table-sub">Orchestrator</span></td>',
    '<td class="nowrap muted">' + date(startedAt) + '</td>',
    '<td class="mono nowrap">' + duration(elapsed) + '</td>',
    '<td class="mono">—</td>',
    '<td class="mono">—</td>',
    '<td><div class="freya-row-actions">' + action + copyLogsAction + activityAction + '</div></td>',
    '</tr>'
  ].join('');
}

export async function freya() {
  const activeAgents = (await Promise.all(state.agents.filter(agent => agent.current_task).map(async agent => {
    try { return { agent, task: await api('/tasks/' + agent.current_task.id) }; } catch { return null; }
  }))).filter(item => item && FREYA_ACTIVE_STATUSES.has(item.task.status)).sort((a, b) => {
    const rank = { Running: 0, Integrating: 1, WaitingForApproval: 2, Paused: 3, Planning: 4, Planned: 5, Queued: 6 };
    const priority = (rank[a.task.status] ?? 99) - (rank[b.task.status] ?? 99);
    if (priority) return priority;
    return new Date(b.task.started_at || b.task.created_at || 0).getTime() - new Date(a.task.started_at || a.task.created_at || 0).getTime();
  });

  state.tasks.forEach(task => {
    const existing = state.freyaHistory.find(item => item.kind === 'task' && item.id === task.id);
    if (existing || freyaIsInCurrentSession({ kind: 'task', id: task.id, task })) freyaHistoryEntry('task', task.id, task);
  });
  state.orchestrations.forEach(run => {
    if (run.id === state.freyaRunId || freyaIsInCurrentSession({ kind: 'orchestration', id: run.id, run })) freyaHistoryEntry('orchestration', run.id, run);
  });
  activeAgents.forEach(item => freyaHistoryEntry('task', item.task.id, item.task, item.agent));

  const activeRuns = state.orchestrations.filter(run => FREYA_ORCHESTRATION_ACTIVE.has(run.status));
  const activeRun = (state.freyaRunId && activeRuns.find(run => run.id === state.freyaRunId)) || activeRuns[0];
  if (activeRun) freyaHistoryEntry('orchestration', activeRun.id, activeRun);

  const rows = state.freyaHistory.slice().sort((a, b) => {
    const activeA = a.kind === 'task' ? FREYA_ACTIVE_STATUSES.has(a.task.status) : FREYA_ORCHESTRATION_ACTIVE.has(a.run.status);
    const activeB = b.kind === 'task' ? FREYA_ACTIVE_STATUSES.has(b.task.status) : FREYA_ORCHESTRATION_ACTIVE.has(b.run.status);
    if (activeA !== activeB) return activeA ? -1 : 1;
    return freyaStartedAt(b) - freyaStartedAt(a);
  });
  const activeCount = rows.filter(item => item.kind === 'task' ? FREYA_ACTIVE_STATUSES.has(item.task.status) : FREYA_ORCHESTRATION_ACTIVE.has(item.run.status)).length;
  const activityRuns = state.orchestrations.filter(run => run.id === state.freyaRunId ||
    freyaIsInCurrentSession({ kind: 'orchestration', id: run.id, run })).sort((a, b) =>
    new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime());
  const selectedRun = activityRuns.find(run => run.id === state.freyaRunId) || activityRuns[0] || null;
  let activityPanel = '';
  if (selectedRun) {
    try {
      const activity = await api('/orchestrations/' + encodeURIComponent(selectedRun.id) + '/activity');
      activityPanel = freyaActivityPanel(activity, selectedRun);
    } catch (error) {
      activityPanel = '<section class="panel"><p class="event-error">Activity could not be loaded: ' + esc(error.message) + '</p></section>';
    }
  }
  const clarificationForms = state.orchestrations.filter(run => run.status === 'NeedsClarification' && run.task_spec?.clarification_questions?.length).map(run =>
    '<section class="panel" id="freya-clarification-' + esc(run.id) + '"><h2>Freya necesita aclarar</h2><p class="small muted">' + esc(taskTitle(run.prompt)) + '</p><form class="stack-form freya-clarification-form" data-run-id="' + esc(run.id) + '">' +
    run.task_spec.clarification_questions.map(question => '<label for="' + esc(run.id + '-' + question.id) + '">' + esc(question.question) + '</label><textarea id="' + esc(run.id + '-' + question.id) + '" name="' + esc(question.id) + '" rows="2" maxlength="4000" ' + (question.required ? 'required' : '') + '></textarea>').join('') +
    '<button class="button primary" type="submit">Responder y continuar</button></form></section>').join('');
  const table = rows.length ? '<div class="table-wrap"><table class="freya-current-table" aria-label="Freya task history"><thead><tr><th>STATUS</th><th>TASK</th><th>AGENT</th><th>STARTED</th><th>ELAPSED</th><th>TOKENS</th><th>MODEL CALLS</th><th></th></tr></thead><tbody>' + rows.map(item => item.kind === 'task' ? freyaTaskRow(item) : freyaRunRow(item)).join('') + '</tbody></table></div>' : '<div class="freya-empty-live">No task is running right now. Assign a task to start live activity.</div>';
  const label = activeCount ? activeCount + ' active · ' + rows.length + ' total' : rows.length ? rows.length + ' completed' : 'Idle';
  return heading('Freya', 'Tell Freya what you need and it will coordinate the available agents.', '', 'ORCHESTRATOR') +
    '<section class="panel"><form id="freya-form" class="stack-form"><label for="freya-prompt">What do you need?</label><textarea id="freya-prompt" name="prompt" rows="5" required placeholder="Describe the outcome you want...">' + esc(state.freyaDraft.prompt || '') + '</textarea><label for="freya-workspace">Workspace folder (existing files)</label><div class="workspace-input-row"><input id="freya-workspace" name="workspace_path" value="' + esc(state.freyaDraft.workspace_path || '') + '" placeholder="Select an existing folder, or leave empty for an isolated workspace"><button type="button" class="button secondary" data-action="freya-workspace">Choose folder</button></div><p class="small muted workspace-help">A selected folder is used directly, so Freya can act on its existing files. Leave it empty to create an isolated workspace.</p><button class="button primary" type="submit">Ask Freya</button></form></section>' +
    clarificationForms + '<section class="panel freya-overview"><div class="freya-section-heading"><div><span class="eyebrow">LIVE OVERVIEW</span><h2>Freya activity</h2><p>Live execution and completed rows remain visible until you submit a new task.</p></div><span class="subtle-tag">' + label + '</span></div>' + table + '</section>' + activityPanel;
}
const NODE_PRESENTATION = {
  success: ['✓', 'DONE', 'complete'], runtime_success: ['✓', 'RUNTIME DONE', 'complete'],
  running: ['●', 'RUNNING', 'running'], evaluating: ['●', 'EVALUATING', 'running'],
  ready: ['○', 'READY', 'waiting'], pending: ['○', 'PENDING', 'muted'],
  waiting_for_approval: ['!', 'WAITING APPROVAL', 'waiting'], recovery_pending: ['!', 'RECOVERY', 'waiting'],
  failed: ['!', 'FAILED', 'error'], blocked: ['×', 'BLOCKED', 'error'],
  cancelled: ['×', 'CANCELLED', 'muted'], skipped: ['×', 'SKIPPED', 'muted'],
  superseded: ['×', 'SUPERSEDED', 'muted'],
};

function nodePresentation(value) {
  const [symbol, label, tone] = NODE_PRESENTATION[value] || ['·', 'STATE UNAVAILABLE', 'muted'];
  return `<span class="mission-state ${tone}"><i aria-hidden="true">${symbol}</i>${label}</span>`;
}

function missionPercent(value) {
  if (value === null || value === undefined || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.min(100, Math.max(0, parsed)) : null;
}

function missionProgress(value, label = 'Worker progress', compactMode = false, tone = 'running') {
  const percent = missionPercent(value);
  if (percent === null) return '';
  return `<div class="mission-progress ${compactMode ? 'compact' : ''} ${tone}" title="Reported by the task runtime"><div><span>${esc(label)}</span><strong class="mono">${number(percent)}%</strong></div><span class="mission-progress-track"><i style="width:${percent}%"></i></span></div>`;
}

function missionRequestForm() {
  const draft = state.freyaDraft || {};
  return `<details class="mission-request" data-detail="mission-request"><summary><span class="mission-request-mark">+</span><strong>New request</strong><span>Send a task to Freya</span><span class="mission-request-expand">OPEN</span></summary><form id="freya-form" class="mission-request-form"><label for="freya-prompt">What do you need?</label><textarea id="freya-prompt" name="prompt" rows="2" required placeholder="Describe the outcome you want...">${esc(draft.prompt || '')}</textarea><label for="freya-workspace">Workspace folder</label><div class="workspace-input-row"><input id="freya-workspace" name="workspace_path" value="${esc(draft.workspace_path || '')}" placeholder="Select an existing folder, or leave empty for an isolated workspace"><button type="button" class="button secondary" data-action="freya-workspace">Choose folder</button></div><p class="small muted workspace-help">An empty path creates an isolated workspace for this orchestration.</p><button class="button primary" type="submit">Ask Freya</button></form></details>`;
}

function missionClarificationForm(run) {
  const questions = run?.task_spec?.clarification_questions || [];
  if (run?.status !== 'NeedsClarification' || !questions.length) return '';
  return `<section class="panel mission-clarification"><div class="mission-panel-heading"><div><span class="eyebrow">WAITING FOR YOUR INPUT</span><h2>Clarify this request</h2><p>${esc(taskTitle(run.prompt))}</p></div>${badge(run.status)}</div><form class="stack-form freya-clarification-form" data-run-id="${esc(run.id)}">${questions.map(question => `<label for="${esc(run.id + '-' + question.id)}">${esc(question.question)}</label><textarea id="${esc(run.id + '-' + question.id)}" name="${esc(question.id)}" rows="2" maxlength="4000" ${question.required ? 'required' : ''}></textarea>`).join('')}<button class="button primary" type="submit">Respond and continue</button></form></section>`;
}

function missionResource(label, value, detail = '') {
  const percent = missionPercent(value);
  if (percent === null) return `<div class="mission-resource unavailable"><div><span>${esc(label)}</span><strong>Unavailable</strong></div>${detail ? `<small>${esc(detail)}</small>` : ''}</div>`;
  return `<div class="mission-resource"><div><span>${esc(label)}</span><strong class="mono">${number(percent)}%</strong></div><span class="mission-resource-track"><i style="width:${percent}%"></i></span>${detail ? `<small>${esc(detail)}</small>` : ''}</div>`;
}

function missionSteps(planTasks, graphNodes, taskById, agentById) {
  if (!planTasks.length) return `<div class="mission-empty">${graphNodes.length ? 'No planned steps are available.' : 'The workflow graph is not available for this run yet.'}</div>`;
  const nodesById = new Map(graphNodes.map(node => [node.plan_task_id, node]));
  const plansById = new Map(planTasks.map(task => [task.id, task]));
  return `<ol class="mission-steps">${planTasks.map((task, index) => {
    const node = nodesById.get(task.id), currentState = node?.state || 'unknown';
    const title = taskTitle(task.objective || task.description || task.title || task.id, 100);
    const runtimeTask = node?.runtime_task_id ? taskById.get(node.runtime_task_id) : null;
    const agent = node?.selected_agent_id ? agentById.get(node.selected_agent_id) : null;
    const dependencies = (node?.depends_on || task.depends_on || []).map(id => plansById.get(id)?.objective || plansById.get(id)?.description || id);
    const progressValue = ['running', 'evaluating', 'runtime_success', 'success'].includes(currentState) ? runtimeTask?.progress : null;
    const progressTone = NODE_PRESENTATION[currentState]?.[2] || 'muted';
    return `<li class="mission-step ${progressTone}"><span class="mission-step-marker" aria-hidden="true">${NODE_PRESENTATION[currentState]?.[0] || '·'}</span><div class="mission-step-body"><div class="mission-step-main"><strong>${esc(title)}</strong><span class="mono">${esc(task.id || `STEP ${index + 1}`)}</span>${nodePresentation(currentState)}</div><div class="mission-step-meta">${dependencies.length ? `<span>After ${esc(dependencies.map(item => taskTitle(item, 48)).join(' · '))}</span>` : '<span>Workflow entry point</span>'}${agent ? `<span>Agent <a href="#/agents/${esc(agent.id)}">${esc(agent.name)}</a></span>` : ''}${node?.runtime_task_id ? `<a href="#/logs?task_id=${encodeURIComponent(node.runtime_task_id)}">Task logs</a>` : ''}</div>${progressValue != null ? missionProgress(progressValue, 'Runtime progress', true, progressTone) : ''}${node?.error ? `<p class="mission-inline-error">${esc(uiText(node.error))}</p>` : ''}</div></li>`;
  }).join('')}</ol>`;
}

function missionAgentsTable(agents) {
  if (!agents.length) return `<div class="mission-empty">No agents are configured. <a href="#/agents">Open agent settings</a>.</div>`;
  const ordered = agents.slice().sort((a, b) => Number(Boolean(b.current_task)) - Number(Boolean(a.current_task)) || String(a.name).localeCompare(String(b.name)));
  return `<div class="mission-table-scroll"><table class="mission-table"><thead><tr><th>ID</th><th>AGENT</th><th>ROLE</th><th>STATUS</th><th>CURRENT TASK</th><th>PROGRESS</th></tr></thead><tbody>${ordered.map(agent => {
    const current = agent.current_task, status = current?.status || agent.status || 'Unknown';
    const progress = status === 'Running' ? missionProgress(current?.progress, 'Runtime', true, 'running') : '';
    return `<tr><td class="mono muted">${esc(String(agent.id || '').slice(0, 8) || '—')}</td><td><a class="mission-agent-link" href="#/agents/${esc(agent.id)}">${esc(agent.name)}</a></td><td>${esc(agent.role || 'Agent')}</td><td>${badge(status)}</td><td>${current ? `<a class="mission-task-link" href="#/logs?task_id=${encodeURIComponent(current.id)}">${esc(taskTitle(current.prompt, 72))}</a>` : `<span class="muted">${agent.enabled ? 'Available' : 'Disabled'}</span>`}</td><td>${progress || '<span class="muted">—</span>'}</td></tr>`;
  }).join('')}</tbody></table></div>`;
}

function missionLogPreview(event) {
  const level = String(event.level || 'INFO').toLowerCase();
  const message = event.error || event.message || event.what || event.event_type || 'System event';
  return `<div class="mission-log-preview ${['error', 'critical'].includes(level) ? 'error' : level === 'warning' ? 'warning' : ''}"><time class="mono">${activityClock(event.timestamp)}</time><span class="mission-log-level">${esc(event.level || 'INFO')}</span><span class="mission-log-message">${esc(taskTitle(message, 112))}</span><span class="mission-log-agent">${esc(event.agent_name || event.agent_id?.slice(0, 8) || 'SYSTEM')}</span></div>`;
}

export async function dashboard() {
  const m = state.metrics || {}, h = state.health || {}, system = h.system || {};
  const runs = (state.orchestrations || []).slice().sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));
  const activeRun = runs.find(run => FREYA_ORCHESTRATION_ACTIVE.has(run.status));
  const selectedRun = activeRun || (state.freyaRunId && runs.find(run => run.id === state.freyaRunId)) || runs[0] || null;
  const plan = selectedRun?.effective_plan || selectedRun?.plan || null;
  let graph = { nodes: [], summary: selectedRun?.graph_summary || null };
  let logEvents = [];
  const requests = [];
  if (selectedRun) requests.push(api(`/orchestrations/${encodeURIComponent(selectedRun.id)}/graph`).then(value => { graph = value; }).catch(() => {}));
  requests.push(api('/logs?limit=24').then(value => { logEvents = Array.isArray(value) ? value : []; }).catch(() => {}));
  await Promise.all(requests);

  const graphNodes = graph.nodes || [], taskById = new Map((state.tasks || []).map(task => [task.id, task]));
  const agentById = new Map((state.agents || []).map(agent => [agent.id, agent]));
  const planTasks = Array.isArray(plan?.tasks) ? plan.tasks : [];
  const activeNode = graphNodes.find(node => ['running', 'waiting_for_approval', 'evaluating'].includes(node.state) && node.runtime_task_id);
  const latestNode = graphNodes.slice().reverse().find(node => node.runtime_task_id);
  const taskNode = activeNode || (!activeRun ? latestNode : null);
  let currentTask = taskNode?.runtime_task_id ? taskById.get(taskNode.runtime_task_id) : null;
  if (!currentTask && taskNode?.runtime_task_id) {
    try { currentTask = await api(`/tasks/${encodeURIComponent(taskNode.runtime_task_id)}`); taskById.set(currentTask.id, currentTask); } catch {}
  }
  const standaloneLiveTask = (state.tasks || []).find(liveTask);
  if (!activeRun && standaloneLiveTask && !liveTask(currentTask)) currentTask = standaloneLiveTask;
  if (!currentTask && !activeRun && !selectedRun) currentTask = state.tasks?.[0] || null;
  const taskIsLive = currentTask ? liveTask(currentTask) : false;
  const taskLabel = taskIsLive || (activeRun && Boolean(taskNode)) ? 'CURRENT TASK' : currentTask ? 'LATEST TASK' : 'CURRENT TASK';
  const currentTaskBelongsToRun = Boolean(selectedRun && currentTask && graphNodes.some(node => node.runtime_task_id === currentTask.id));
  const currentAgent = currentTask ? agentById.get(currentTask.agent_id) : null;
  const currentAgentName = currentAgent?.name || currentTask?.agent_name || '';
  const currentTitle = currentTask?.prompt || selectedRun?.prompt || '';
  const currentId = currentTask?.id || selectedRun?.id || '';
  const taskStarted = currentTask?.started_at || currentTask?.created_at || selectedRun?.created_at;
  const elapsedSeconds = taskIsLive && taskStarted ? Math.max(0, (Date.now() - new Date(taskStarted).getTime()) / 1000) : Number(currentTask?.duration_seconds ?? selectedRun?.duration_seconds) || 0;
  const currentStatus = currentTask?.status || selectedRun?.status || 'Idle';
  const taskProgress = currentTask && currentTask.status !== 'Queued' ? currentTask.progress : null;
  const taskDependencies = (taskNode?.depends_on || []).map(id => planTasks.find(task => task.id === id)?.objective || id);

  const summary = graph.summary || selectedRun?.graph_summary || null;
  const counts = summary?.counts || graphNodes.reduce((all, node) => { all[node.state] = (all[node.state] || 0) + 1; return all; }, {});
  const totalNodes = Number(summary?.total) || graphNodes.length;
  const completedNodes = (Number(counts.success) || 0) + (Number(counts.runtime_success) || 0);
  const runningNodes = (Number(counts.running) || 0) + (Number(counts.evaluating) || 0);
  const queuedNodes = graphNodes.filter(node => ['ready', 'pending'].includes(node.state));
  const runtimeQueuedTasks = graphNodes.length ? [] : (state.tasks || []).filter(task => task.status === 'Queued');
  const issueNodes = graphNodes.filter(node => ['failed', 'blocked', 'recovery_pending'].includes(node.state));
  const queueCount = queuedNodes.length + runtimeQueuedTasks.length;
  const activeRunTaskIds = new Set(graphNodes.map(node => node.runtime_task_id).filter(Boolean));
  const allPendingApprovals = Array.isArray(state.approvals) ? state.approvals : [];
  logEvents.sort((a, b) => new Date(b.timestamp || 0) - new Date(a.timestamp || 0));
  const runErrors = logEvents.filter(event => {
    const issueSignal = ['ERROR', 'CRITICAL'].includes(String(event.level || '').toUpperCase()) ||
      String(event.policy_decision || '').toLowerCase().includes('denied') ||
      /denied|timeout|validation\.failed|dependency\.failed/i.test(String(event.event_type || '')) ||
      String(event.status || '').toLowerCase() === 'denied';
    const belongsToSelectedRun = !selectedRun || event.orchestration_id === selectedRun.id || activeRunTaskIds.has(event.task_id);
    return issueSignal && belongsToSelectedRun;
  });
  const issues = [];
  issueNodes.slice(0, 4).forEach(node => {
    const step = planTasks.find(task => task.id === node.plan_task_id);
    issues.push(`<article class="mission-issue"><div>${nodePresentation(node.state)}<a href="${node.runtime_task_id ? `#/logs?task_id=${encodeURIComponent(node.runtime_task_id)}` : '#/logs'}">${esc(step?.objective || node.plan_task_id)}</a></div><p>${esc(uiText(node.error || node.waiting_reason || 'No additional detail recorded.'))}</p></article>`);
  });
  const currentTaskIssue = currentTask && (['Failed', 'Blocked'].includes(currentTask.status) || Boolean(currentTask.error));
  const currentTaskIssueRepresented = currentTaskIssue && (issueNodes.some(node => node.runtime_task_id === currentTask.id) || runErrors.some(event => event.task_id === currentTask.id));
  if (currentTaskIssue && !currentTaskIssueRepresented) issues.unshift(`<article class="mission-issue"><div>${badge(currentTask.status || 'Failed')}<a href="#/logs?task_id=${encodeURIComponent(currentTask.id)}">${esc(taskTitle(currentTask.prompt, 72))}</a></div><p>${esc(uiText(currentTask.error || currentTask.status || 'Task failed.'))}</p></article>`);
  if (selectedRun?.status === 'Failed' && !issueNodes.length && !currentTaskIssue && !runErrors.length) issues.push(`<article class="mission-issue"><div>${badge('Failed')}<a href="#/logs?orchestration_id=${encodeURIComponent(selectedRun.id)}">Freya orchestration</a></div><p>${esc(uiText(selectedRun.error || 'Failure recorded; no additional detail is available.'))}</p></article>`);
  const runFailureUnreported = selectedRun?.status === 'Failed' && !issueNodes.length && !currentTaskIssue && !runErrors.length;
  const existingIssueCount = issueNodes.length + runErrors.length + (currentTaskIssue && !currentTaskIssueRepresented ? 1 : 0) + Number(runFailureUnreported);
  runErrors.slice(0, Math.max(0, 3 - issues.length)).forEach(event => issues.push(`<article class="mission-issue"><div><span class="mission-state error"><i>!</i>${esc(event.level)}</span><a href="#/logs?${event.task_id ? `task_id=${encodeURIComponent(event.task_id)}` : selectedRun ? `orchestration_id=${encodeURIComponent(selectedRun.id)}` : ''}">${esc(event.event_type || 'Runtime error')}</a></div><p>${esc(uiText(event.error || event.message || event.what || 'Recorded error event.'))}</p></article>`));
  if (!issues.length) issues.push('<div class="mission-no-issues"><span>✓</span><strong>No active issues recorded</strong></div>');

  const workflowPct = totalNodes ? Math.min(100, completedNodes / totalNodes * 100) : null;
  const segment = (count, tone) => count && totalNodes ? `<i class="${tone}" style="width:${Math.min(100, count / totalNodes * 100)}%"></i>` : '';
  const waitingNodes = (Number(counts.waiting_for_approval) || 0) + (Number(counts.recovery_pending) || 0);
  const errorNodes = (Number(counts.failed) || 0) + (Number(counts.blocked) || 0);
  const otherNodes = Math.max(0, totalNodes - completedNodes - runningNodes - waitingNodes - queuedNodes.length - errorNodes);
  const workflowBar = workflowPct === null ? '<div class="mission-workflow-track empty"><i></i></div>' : `<div class="mission-workflow-track">${segment(completedNodes, 'complete')}${segment(runningNodes, 'running')}${segment(waitingNodes, 'waiting')}${segment(queuedNodes.length, 'queued')}${segment(errorNodes, 'error')}${segment(otherNodes, 'other')}</div>`;
  const workflowDetail = totalNodes ? `${number(completedNodes)} / ${number(totalNodes)} execution tasks complete · ${number(runningNodes)} running · ${number(queuedNodes.length)} queued · ${number(waitingNodes)} waiting · ${number(errorNodes)} failed or blocked` : selectedRun ? `${selectedRun.status} · Task graph not available yet` : currentTask ? `Standalone task · ${currentTask.status} · No workflow graph is available` : runtimeQueuedTasks.length ? `${number(runtimeQueuedTasks.length)} standalone tasks queued` : 'No workflow has been submitted';
  const latestEvents = logEvents.slice(0, 2);
  const eventList = logEvents.length ? logTable(logEvents.slice(0, 24)) : '<div class="mission-empty">No log events are available yet.</div>';
  const runtimeCapacity = Number(h.runtime?.max_workers);
  const resourceFact = runtimeCapacity > 0 ? `${number(m.running_tasks)} / ${number(runtimeCapacity)} worker slots in use` : `${number(m.running_tasks)} active runtime tasks`;
  const ramDetail = system.ram_total_bytes ? `${bytes(system.ram_used_bytes)} / ${bytes(system.ram_total_bytes)}` : '';
  const gpuDetail = system.gpu_memory_total_bytes
    ? `${system.gpu_temperature_c == null ? 'Temp unavailable' : `Temp ${number(system.gpu_temperature_c)}°C`} · VRAM ${bytes(system.gpu_memory_used_bytes)} / ${bytes(system.gpu_memory_total_bytes)} · ${system.gpu_name}`
    : 'Requires NVIDIA nvidia-smi';
  const selectedRunStatus = selectedRun?.status || (taskIsLive ? currentTask.status : 'Idle');
  const dashboardStatus = activeRun?.status || (taskIsLive ? currentTask.status : selectedRunStatus);
  const canStopWorkflow = Boolean(selectedRun && FREYA_ORCHESTRATION_ACTIVE.has(selectedRun.status));
  const workflowAction = canStopWorkflow
    ? button('Stop workflow', 'cancel-orchestration', 'stop', `data-id="${esc(selectedRun.id)}"`, 'small-button danger-quiet')
    : '';

  return `<div class="mission-control">
    <header class="mission-header"><div><span class="eyebrow">FREYA · MISSION CONTROL</span><h1>Operations dashboard</h1><p>Live workflow state, agents, queue, issues, resources and logs.</p></div><span class="mission-run-status">${badge(dashboardStatus)}</span></header>
    ${missionRequestForm()}
    ${missionClarificationForm(selectedRun)}
    <div class="mission-workflow-review-grid">
      <section class="panel mission-workflow"><div class="mission-panel-heading"><div><span class="eyebrow">GLOBAL WORKFLOW</span><h2>${esc(selectedRun ? taskTitle(selectedRun.prompt, 100) : 'Workflow progress')}</h2></div><div class="mission-workflow-actions">${workflowAction}<span class="subtle-tag">${esc(selectedRunStatus)}</span></div></div><div class="mission-workflow-body"><div class="mission-workflow-copy"><strong>${workflowPct === null ? '—' : `${number(workflowPct)}%`}</strong><span>${esc(workflowDetail)}</span></div>${workflowBar}</div></section>
      ${dashboardApprovals(allPendingApprovals)}
    </div>
    <div class="mission-primary-grid">
      <section class="panel mission-current"><div class="mission-panel-heading"><div><span class="eyebrow">${taskLabel}</span><h2>${currentTitle ? esc(taskTitle(currentTitle, 112)) : 'No task running'}</h2></div>${badge(currentStatus)}</div>
        <div class="mission-current-meta"><span class="mono">${currentId ? `ID ${esc(currentId)}` : 'Ready for a new request'}</span>${currentAgent ? `<a href="#/agents/${esc(currentAgent.id)}">Agent ${esc(currentAgentName)}</a>` : currentAgentName ? `<span>Agent ${esc(currentAgentName)}</span>` : selectedRun && !currentTask ? '<span>Agent Freya · Orchestrator</span>' : ''}${currentTask?.priority ? `<span>Priority ${esc(currentTask.priority)}</span>` : ''}${taskStarted ? `<span>Elapsed ${duration(elapsedSeconds)}</span>` : ''}</div>
        ${taskProgress !== null && taskProgress !== undefined ? missionProgress(taskProgress, 'Runtime progress', false, ['Failed', 'Error', 'Blocked'].includes(currentStatus) ? 'error' : currentStatus === 'Success' ? 'complete' : 'running') : '<div class="mission-no-progress">No task progress percentage is available.</div>'}
        ${currentTask ? `<div class="mission-current-facts"><span>${number(currentTask.steps)} steps used</span><span>${compact(currentTask.total_tokens)} tokens</span>${currentTask.model_calls != null ? `<span>${number(currentTask.model_calls)} model calls</span>` : ''}${taskDependencies.length ? `<span>Depends on ${esc(taskDependencies.map(item => taskTitle(item, 48)).join(' · '))}</span>` : ''}</div>` : selectedRun ? `<div class="mission-current-facts"><span>Orchestration ${esc(selectedRunStatus)}</span><a href="#/logs?orchestration_id=${encodeURIComponent(selectedRun.id)}">Open run logs</a></div>` : '<div class="mission-current-facts"><span>Use New request to start an orchestration.</span></div>'}
        ${taskIsLive && currentTask ? `<div class="mission-current-actions"><a class="text-link" href="#/logs?task_id=${encodeURIComponent(currentTask.id)}">Inspect task logs ${icon('arrow')}</a>${currentTaskBelongsToRun && canStopWorkflow ? '' : button('Cancel task', 'cancel-task', 'stop', `data-id="${esc(currentTask.id)}"`, 'small-button danger-quiet')}</div>` : ''}
      </section>
      <section class="panel mission-metrics"><div class="mission-panel-heading"><div><span class="eyebrow">SYSTEM RESOURCES</span><h2>Runtime telemetry</h2></div></div><div class="mission-resource-grid">${missionResource('GPU', system.gpu_available ? system.gpu_percent : null, gpuDetail)}${missionResource('RAM', system.available ? system.ram_percent : null, ramDetail)}<div class="mission-runtime-fact"><span>WORKERS</span><strong>${resourceFact}</strong></div><div class="mission-runtime-fact"><span>QUEUE</span><strong>${number(queueCount)} tasks</strong></div><div class="mission-runtime-fact"><span>ACTIVE AGENTS</span><strong>${number(m.active_agents)} / ${number(m.total_agents)}</strong></div><div class="mission-runtime-fact"><span>TOKENS · ALL RUNS</span><strong>${compact(m.total_tokens)}</strong></div></div></section>
    </div>
    <div class="mission-work-grid">
      <section class="panel mission-step-panel"><div class="mission-panel-heading"><div><span class="eyebrow">TASK STEPS</span><h2>Execution pipeline</h2><p>Step states come from the persisted workflow graph.</p></div><span class="subtle-tag">${number(planTasks.length)} planned</span></div>${missionSteps(planTasks, graphNodes, taskById, agentById)}</section>
      <div class="mission-side-stack">
        <section class="panel mission-queue"><div class="mission-panel-heading"><div><span class="eyebrow">WHAT'S NEXT</span><h2>Queue</h2></div><span class="subtle-tag">${number(queueCount)}</span></div>${queuedNodes.length ? `<ul class="mission-queue-list">${queuedNodes.slice(0, 5).map(node => { const step = planTasks.find(task => task.id === node.plan_task_id); const dependencies = (node.depends_on || []).map(id => planTasks.find(task => task.id === id)?.objective || id); const agent = node.selected_agent_id ? agentById.get(node.selected_agent_id) : null; return `<li><div>${nodePresentation(node.state)}<strong>${esc(taskTitle(step?.objective || node.plan_task_id, 72))}</strong></div><p>${dependencies.length ? `Waiting for ${esc(dependencies.map(item => taskTitle(item, 42)).join(' · '))}` : 'Ready to start'}${agent ? ` · ${esc(agent.name)}` : ''}</p></li>`; }).join('')}</ul>` : runtimeQueuedTasks.length ? `<ul class="mission-queue-list">${runtimeQueuedTasks.slice(0, 5).map(task => `<li><div>${badge(task.status)}<strong>${esc(taskTitle(task.prompt, 72))}</strong></div><p>${task.agent_name ? `Agent ${esc(task.agent_name)}` : 'Queued task'} · <a href="#/logs?task_id=${encodeURIComponent(task.id)}">${esc(String(task.id).slice(0, 12))}</a></p></li>`).join('')}</ul>` : `<div class="mission-empty">${totalNodes ? 'No queued steps.' : 'Queue details appear after the workflow plan is created.'}</div>`}</section>
        <section class="panel mission-issues"><div class="mission-panel-heading"><div><span class="eyebrow">ATTENTION</span><h2>Issues</h2></div><span class="subtle-tag ${existingIssueCount ? 'has-issues' : ''}">${number(existingIssueCount)}</span></div><div class="mission-issue-list">${issues.join('')}</div></section>
      </div>
    </div>
    <section class="panel mission-agents"><div class="mission-panel-heading"><div><span class="eyebrow">ACTIVE AGENTS</span><h2>Agent status</h2></div><a class="text-link" href="#/agents">Manage agents ${icon('arrow')}</a></div>${missionAgentsTable(state.agents || [])}</section>
    <details class="panel mission-console" data-detail="mission-console"><summary><span class="mission-console-title"><span class="eyebrow">LIVE LOGS</span><strong>Console</strong><span class="subtle-tag">${number(logEvents.length)} recent events</span></span><span class="mission-console-preview">${latestEvents.length ? latestEvents.map(missionLogPreview).join('') : '<span class="muted">Waiting for system events.</span>'}</span><span class="mission-console-summary-actions">${button('Copy all logs', 'copy-logs', 'copy', 'type="button"', 'small-button secondary')}<span class="mission-console-expand">EXPAND</span></span></summary><div class="mission-console-body"><div class="mission-console-toolbar"><span>Newest events first · live updates via SSE</span><a class="text-link" href="#/logs">Open full log explorer ${icon('arrow')}</a></div>${eventList}</div></details>
  </div>`;
}

export function skills() {
  const filter = state.filters.skills || {}, query = String(filter.q || '').toLowerCase();
  const items = (state.skills || []).filter(skill => !query || [skill.id, skill.name, skill.category, skill.description, ...(skill.tags || [])].join(' ').toLowerCase().includes(query)).filter(skill => filter.enabled === '' || filter.enabled == null || String(skill.enabled) === String(filter.enabled)).filter(skill => !filter.source || skill.source === filter.source);
  const filters = `<section class="panel skill-toolbar"><div class="filter-bar"><div class="filter-label">${icon('filter')} Filter</div><input name="q" data-filter="skills" value="${esc(filter.q || '')}" placeholder="Search name, category or tags"><select name="enabled" data-filter="skills"><option value="">All</option><option value="true" ${filter.enabled === 'true' ? 'selected' : ''}>Enabled</option><option value="false" ${filter.enabled === 'false' ? 'selected' : ''}>Disabled</option></select><select name="source" data-filter="skills"><option value="">All sources</option><option value="builtin" ${filter.source === 'builtin' ? 'selected' : ''}>Built-in</option><option value="user" ${filter.source === 'user' ? 'selected' : ''}>User</option></select></div></section>`;
  const cards = items.length ? `<div class="skill-grid">${items.map(skill => `<article class="skill-card"><div class="skill-card-top"><span class="avatar">${icon('spark')}</span><span class="badge ${skill.enabled ? 'success' : 'muted'}">${skill.enabled ? 'Enabled' : 'Disabled'}</span></div><a class="skill-name" href="#/skills/${esc(skill.id)}">${esc(skill.name)}</a><p>${esc(skill.category || 'General')} · v${number(skill.version)}</p><p class="skill-description">${esc(skill.description || 'No description')}</p><div class="skill-tags">${(skill.tags || []).slice(0, 6).map(tag => `<span>${esc(tag)}</span>`).join('')}</div><div class="skill-card-meta"><span>Assigned <b>${number(skill.assigned_agents)}</b></span><span>${esc(skill.source || 'user')}</span></div><div class="skill-card-actions"><button class="button small-button secondary" data-action="edit-skill" data-id="${esc(skill.id)}">Edit</button><button class="button small-button secondary" data-action="duplicate-skill" data-id="${esc(skill.id)}">Duplicate</button></div></article>`).join('')}</div>` : empty('spark', 'No skills match', 'Create or enable a reusable skill to specialize an agent.', button('New skill', 'create-skill', 'plus', '', 'primary'));
  return `${heading('Skills', 'Reusable knowledge and procedures that never grant permissions.', button('Import JSON', 'import-skills', 'upload', '', 'secondary') + button('Export JSON', 'export-skills', 'download', '', 'secondary') + button('New skill', 'create-skill', 'plus', '', 'primary'), 'WORKSPACE / SKILLS')}${filters}<div class="section-toolbar"><div class="counter-label">Available skills <span>${number(items.length)}</span></div><span class="muted small">Assigned skills remain snapshot based per task</span></div>${cards}`;
}

export async function skillDetail(id) {
  const skill = await api(`/skills/${encodeURIComponent(id)}`);
  const attrs = 'data-id="' + esc(id) + '"';
  return `<a class="back-link" href="#/skills">${icon('back')} All skills</a>${heading(skill.name, `${skill.category || 'General'} · version ${skill.version}`, button('Import JSON', 'import-skills', 'upload', '', 'secondary') + button('Export JSON', 'export-skill', 'download', attrs, 'secondary') + button('Edit skill', 'edit-skill', 'edit', `data-id="${esc(skill.id)}"`, 'secondary') + button('Duplicate', 'duplicate-skill', 'copy', `data-id="${esc(skill.id)}"`, 'secondary') + button('Delete', 'delete-skill', 'trash', `data-id="${esc(skill.id)}"`, 'small-button danger-quiet'), 'SKILL DETAIL')}<section class="panel skill-detail"><div class="key-values"><span>ID</span><code>${esc(skill.id)}</code><span>Source</span><span>${esc(skill.source || 'user')}</span><span>Assigned agents</span><span>${number(skill.assigned_agents)}${skill.assigned_agent_ids?.length ? ` · ${esc(skill.assigned_agent_ids.join(', '))}` : ''}</span><span>Required</span><code>${esc((skill.required_capabilities || []).join(', ') || 'None')}</code><span>Recommended</span><code>${esc((skill.recommended_capabilities || []).join(', ') || 'None')}</code></div><p>${esc(skill.description || '')}</p><h3>Instructions</h3><ul>${(skill.instructions || []).map(item => `<li>${esc(item)}</li>`).join('') || '<li>None specified</li>'}</ul><h3>Procedures</h3><pre>${esc(JSON.stringify(skill.procedures || [], null, 2))}</pre><details open><summary>Effective context preview</summary><pre>${esc(skillContextPreview(skill))}</pre></details></section>`;
}

export function agents() {
  return `${heading('Agents', 'Configure and coordinate your AI agents.', button('Import agent', 'import-agent', 'upload', '', 'secondary') + button('Create agent', 'create-agent', 'plus', '', 'primary'), 'WORKSPACE / AGENTS')}<div class="section-toolbar"><div class="counter-label">All agents <span>${number(state.agents.length)}</span></div><span class="muted small">${number(state.agents.filter(a => a.enabled).length)} active · local execution</span></div>${state.agents.length ? `<div class="agent-grid">${state.agents.map(agentCard).join('')}</div>` : `<section class="panel big-empty">${empty('agents', 'Create your first agent', 'Name it, connect your Ollama model, and choose the tools it can use.', button('Create agent', 'create-agent', 'plus', '', 'primary'))}<div class="setup-steps"><span><b>01</b> Configure a model</span><span><b>02</b> Choose its tools</span><span><b>03</b> Assign a task</span></div></section>`}`;
}

export async function agentDetail(id) {
  const [agent, tasks, metrics] = await Promise.all([api(`/agents/${id}`), api(`/tasks?agent_id=${encodeURIComponent(id)}`), api(`/metrics?agent_id=${encodeURIComponent(id)}`)]);
  const current = agent.current_task ? await api(`/tasks/${agent.current_task.id}`) : null;
  const attrs = `data-id="${esc(id)}"`, allTabs = [['timeline', 'Execution Timeline'], ['metrics', 'Metrics'], ['logs', 'Logs'], ['capabilities', 'Capabilities'], ['configuration', 'Configuration'], ];
  let content = '';
  if (state.tab === 'timeline') content = `<section class="panel">${panelHeading('Execution timeline', 'Actions and operational explanations. Private model reasoning is not shown.')}${current ? timeline(current.events || current.timeline) : tasks.length ? `<div class="panel-note">The agent has no active task. <a href="#/logs?task_id=${esc(tasks[0].id)}">View its latest run ${icon('arrow')}</a></div>${timeline((await api(`/tasks/${tasks[0].id}`)).events)}` : empty('activity', 'The agent is ready', 'Assign a task to see its actions in real time.', button('Assign task', 'assign', 'play', attrs, 'primary'))}</section>`;
  else if (state.tab === 'metrics') content = metricsView(metrics, false);
  else if (state.tab === 'history') content = `<section class="panel">${panelHeading('Task history', 'Saved runs for this agent')}${taskTable(tasks)}</section>`;
  else if (state.tab === 'logs') content = `<section class="panel">${panelHeading('Agent logs', 'Tasks and events for this agent', `<a class="text-link" href="#/logs?agent_id=${esc(id)}">Open filters ${icon('arrow')}</a>`)}${groupedLogTable(await api(`/logs?agent_id=${encodeURIComponent(id)}&limit=200`), tasks)}</section>`;
  else if (state.tab === 'capabilities' || state.tab === 'tools') { const policy = agent.capability_policy || agent.config.capability_policy || {}; const rows = Object.entries(policy.capabilities || {}).flatMap(([category, actions]) => Object.entries(actions).map(([action, rule]) => `<div class="tool-card ${rule.mode === 'allow' ? 'enabled' : ''}"><div><strong>${esc(category)}.${esc(action)}</strong><span class="badge ${rule.mode === 'allow' ? 'success' : rule.mode === 'ask' ? 'warning' : ''}">${esc(rule.mode || 'deny')}</span></div><p>${esc((rule.paths || []).join(', ') || 'Workspace scope')}${rule.extensions?.length ? ` · ${esc(rule.extensions.join(', '))}` : ''}${rule.max_bytes != null ? ` · max ${number(rule.max_bytes)} bytes` : ''}</p></div>`)); content = `<section class="panel">${panelHeading('Capabilities', 'The runtime resolves each tool request to one action before execution.', button('Configure capabilities', 'edit-agent', 'settings', attrs, 'secondary'))}<div class="tool-grid">${rows.join('')}</div></section>`; }
  else content = `<section class="panel">${panelHeading('Agent configuration', 'Changes apply to future tasks.', button('Edit configuration', 'edit-agent', 'edit', attrs, 'secondary'))}<div class="config-view"><div class="key-values"><span>ID</span><code>${esc(agent.id)}</code><span>Role</span><span>${esc(agent.role || '—')}</span><span>Purpose</span><span>${esc(agent.config.identity?.purpose || '—')}</span><span>Created</span><span>${date(agent.created_at, true)}</span>${Object.entries(agent.config).filter(([key]) => !['system_prompt', 'capability_policy', 'identity', 'behavior', 'autonomy', 'verification', 'output'].includes(key)).map(([key, value]) => `<span>${esc(key)}</span><code>${esc(Array.isArray(value) ? value.join(', ') : value)}</code>`).join('')}</div><h3>Additional instructions</h3><pre>${esc(uiText(agent.instructions || 'No additional instructions'))}</pre><details class="context-preview"><summary>Effective Agent Context</summary><pre>${esc(agent.context_preview || 'Preview unavailable')}</pre></details></div></section>`;
  return `<a class="back-link" href="#/agents">${icon('back')} All agents</a>${heading(agent.name, agent.description || agent.role || 'General-purpose local agent', button('Export JSON', 'export-agent', 'download', attrs, 'secondary') + button('Configure', 'edit-agent', 'settings', attrs, 'secondary') + button('Assign task', 'assign', 'play', attrs, 'primary'), 'AGENT DETAIL')}<div class="agent-detail-summary"><div class="agent-detail-identity"><div class="avatar">${icon('agents')}</div><div>${badge(agent.status)}<span class="mono muted">${esc(agent.config.model)}</span></div></div><div class="agent-detail-controls">${button(agent.enabled ? 'Disable' : 'Enable', 'toggle-agent', '', `${attrs} data-enabled="${!agent.enabled}"`, 'small-button')}${button(agent.status === 'Paused' ? 'Resume' : 'Pause', agent.status === 'Paused' ? 'resume-agent' : 'pause-agent', agent.status === 'Paused' ? 'play' : 'pause', attrs, 'small-button')}${button('Restart', 'restart-agent', 'refresh', attrs, 'small-button')}${button('Duplicate', 'duplicate-agent', 'copy', attrs, 'small-button')}${button('Delete', 'delete-agent', 'trash', attrs, 'small-button danger-quiet')}</div></div><div class="current-task-card"><div><span class="eyebrow">CURRENT TASK</span><h3>${current ? `<a href="#/logs?task_id=${esc(current.id)}">${esc(taskTitle(current.prompt))}</a>` : 'No task running'}</h3><p class="small muted">${current ? `${badge(current.status)} · ${duration(current.duration_seconds)} · ${number(current.tool_calls)} tool calls` : 'Assign a task whenever you want to start a new run.'}</p></div>${current ? `<div class="current-progress">${progress(current)}${button('Cancel task', 'cancel-task', 'stop', `data-id="${esc(current.id)}"`, 'small-button danger-quiet')}</div>` : ''}</div>${agent.status === 'Paused' ? '<div class="notice">Pausing takes effect between actions. An in-progress model or tool call may finish first.</div>' : ''}${tabs(allTabs, state.tab, 'agent-tab')}<div class="tab-content">${content}</div>`;
}

export async function tasks() {
  const filters = state.filters.tasks, items = await api(`/tasks?${query(filters)}`);
  return `${heading('Tasks', 'Every run, from start to result.', button('Assign task', 'assign', 'plus', '', 'primary'), 'WORKSPACE / TASKS')}<section class="panel"><div class="filter-bar"><div class="filter-label">${icon('filter')} Filter</div><select aria-label="Filter tasks by agent" data-filter="tasks" name="agent_id">${agentOptions(filters.agent_id)}</select><select aria-label="Filter tasks by status" data-filter="tasks" name="status"><option value="">All statuses</option>${['Queued', 'Running', 'WaitingForApproval', 'Paused', 'Success', 'Failed', 'Cancelled'].map(value => `<option ${filters.status === value ? 'selected' : ''}>${value}</option>`).join('')}</select><span class="filter-count">${number(items.length)} tasks</span></div>${taskTable(items)}</section>`;
}

export async function taskDetail(id) {
  const task = await api(`/tasks/${id}`), attrs = `data-id="${esc(id)}"`;
  const copyAction = button('Copy to clipboard', 'copy-task-details', 'copy', attrs, 'secondary');
  const actions = copyAction + (liveTask(task) ? button(task.status === 'Paused' ? 'Resume agent' : 'Pause agent', task.status === 'Paused' ? 'resume-agent' : 'pause-agent', task.status === 'Paused' ? 'play' : 'pause', `data-id="${esc(task.agent_id)}"`, 'secondary') + button('Cancel', 'cancel-task', 'stop', attrs, 'danger') : button('Run again', 'retry-task', 'refresh', attrs, 'primary'));
  return `<a class="back-link" href="#/logs">${icon('back')} Logs</a>${heading('Run details', task.id, actions, 'TASK EXECUTION')}<section class="panel task-overview"><div class="task-title-row"><h2>${esc(task.prompt)}</h2>${badge(task.status)}</div><div class="task-metadata"><span>${icon('agents')}<a href="#/agents/${esc(task.agent_id)}">${esc(task.agent_name)}</a></span><span>${icon('clock')}${date(task.started_at || task.created_at)}</span><span>${icon('cpu')}${esc(task.config.model)}</span></div>${progress(task)}<p class="small muted">The percentage shows the step budget used; it does not estimate the time remaining.</p><div class="task-stats"><div><span>Duration</span><strong>${duration(task.duration_seconds)}</strong></div><div><span>Steps</span><strong>${number(task.steps)}</strong></div><div><span>Tokens</span><strong>${compact(task.total_tokens)}</strong></div><div><span>Model / tools</span><strong>${number(task.model_calls)} / ${number(task.tool_calls)}</strong></div><div><span>Finished</span><strong class="small">${date(task.finished_at)}</strong></div></div><details data-detail="workspace-${esc(id)}" class="workspace-detail"><summary>${icon('folder')} Run workspace</summary><code>${esc(task.workspace)}</code></details></section>${task.status === 'Paused' ? '<div class="notice">Pause requested: the runtime waits between actions and keeps this run active.</div>' : task.status === 'WaitingForApproval' ? '<div class="notice">This run is waiting for a human approval. Resolve it from the Operations Dashboard.</div>' : ''}${task.error ? `<div class="error-banner">${icon('alert')}<div><strong>Run error</strong><p>${esc(uiText(task.error))}</p></div></div>` : ''}${task.result ? `<section class="panel task-result">${panelHeading('Result', 'Final response from the agent')}<pre>${esc(serialize(task.result))}</pre></section>` : ''}<section class="panel">${panelHeading('Execution Timeline', 'Persisted actions, tools, and results', `<span class="subtle-tag">${number((task.events || task.timeline || []).length)} events</span>`)}${timeline(task.events || task.timeline || [])}</section>`;
}

export async function logs() {
  const f = state.filters.logs, events = await api(`/logs?${query({ ...f, limit: 200 })}`);
  return `${heading('Logs', 'Runtime tasks and Freya orchestration events in one place.', button('Refresh', 'refresh', 'refresh', '', 'secondary') + button('Copy all logs', 'copy-logs', 'copy', '', 'secondary'), 'OBSERVABILITY / LOGS')}<section class="panel"><form class="log-filters" id="log-filters"><label>Agent<select name="agent_id" data-filter="logs">${agentOptions(f.agent_id)}</select></label><label>Task ID<input name="task_id" value="${esc(f.task_id || '')}" placeholder="Task ID" data-filter="logs"></label><label>Orchestration ID<input name="orchestration_id" value="${esc(f.orchestration_id || '')}" placeholder="Orchestration ID" data-filter="logs"></label><label>Level<select name="level" data-filter="logs"><option value="">All levels</option>${['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'].map(v => `<option ${f.level === v ? 'selected' : ''}>${v}</option>`).join('')}</select></label><label>Tool<select name="tool" data-filter="logs"><option value="">All tools</option>${state.tools.map(t => `<option ${f.tool === t.name ? 'selected' : ''}>${esc(t.name)}</option>`).join('')}</select></label><label>From<input type="datetime-local" name="date_from" value="${esc(f.date_from || '')}" data-filter="logs"></label><label>To<input type="datetime-local" name="date_to" value="${esc(f.date_to || '')}" data-filter="logs"></label><label class="checkbox-label"><input type="checkbox" name="error_only" data-filter="logs" ${f.error_only ? 'checked' : ''}>Errors only</label>${button('Clear filters', 'clear-logs', '', '', 'small-button')}</form><div class="log-list-meta"><span>${number(events.length)} events · runtime and orchestration record</span><span>${icon('shield')} Sensitive content is sanitized by the server</span></div><div class="log-table-header" aria-hidden="true"><span>Date and time</span><span>Level</span><span>Event / tool</span><span>Agent</span><span></span></div>${groupedLogTable(events, state.tasks)}</section>`;
}

function approvalCard(item) {
  const cross = item.cross_task_modification;
  const details = cross
    ? '<span>Target file</span><code>' + esc(cross.target_path) + '</code>'
      + '<span>File owner task</span><code>' + esc(cross.target_owner_plan_task_id) + '</code>'
      + '<span>Operation</span><code>' + esc(cross.requested_operation) + '</code>'
      + '<span>Requested change</span><span>' + esc(cross.requested_change) + '</span>'
      + '<span>Why</span><span>' + esc(cross.reason) + '</span>'
      + '<span>Needed for</span><span>' + esc(cross.needed_for) + '</span>'
      + '<span>Blocking</span><span>' + (cross.blocking ? 'Yes' : 'No') + '</span>'
    : '<span>Capability</span><code>' + esc(item.capability) + '</code><span>Tool</span><code>' + esc(item.tool) + '</code><span>Resource</span><code>' + esc(item.resource || '.') + '</code><span>Reason</span><span>' + esc(item.reason) + '</span><span>Arguments</span><pre>' + esc(serialize(item.arguments || {})) + '</pre>';
  const actions = cross
    ? '<button class="button primary" data-action="approve-once" data-id="' + esc(item.id) + '">Approve this change once</button><button class="button secondary" data-action="approve-file-intent" data-id="' + esc(item.id) + '">Approve similar purpose for this file</button><button class="button danger-quiet" data-action="deny-approval" data-id="' + esc(item.id) + '">Deny</button>'
    : '<button class="button primary" data-action="approve-once" data-id="' + esc(item.id) + '">Approve once</button><button class="button secondary" data-action="approve-task" data-id="' + esc(item.id) + '">Approve for task</button><button class="button danger-quiet" data-action="deny-approval" data-id="' + esc(item.id) + '">Deny</button>';
  return '<article class="panel approval-card"><div class="approval-card-header"><div><span class="eyebrow">' + (cross ? 'CROSS-TASK FILE CHANGE' : 'PENDING APPROVAL') + '</span><h2>' + esc(item.action_summary || item.capability) + '</h2></div>' + badge(item.status || 'pending') + '</div><div class="key-values">' + details + '<span>Created</span><span>' + date(item.created_at, true) + '</span></div><div class="approval-actions">' + actions + '</div></article>';
}

function dashboardApprovals(items) {
  const content = items.length
    ? '<div class="approval-list">' + items.map(approvalCard).join('') + '</div>'
    : '<div class="mission-no-approvals"><strong>No actions are waiting for review.</strong><span>Pending human decisions will appear here.</span></div>';
  return '<section class="panel mission-approvals" id="dashboard-approvals"><div class="mission-panel-heading"><div><span class="eyebrow">HUMAN REVIEW</span><h2>Pending approvals</h2><p>Review requests before continuing.</p></div><span class="subtle-tag">' + number(items.length) + ' waiting</span></div>' + content + '</section>';
}

export function approvals() {
  const items = state.approvals || [];
  const cards = items.length ? '<div class="approval-list">' + items.map(approvalCard).join('') + '</div>' : '<section class="panel big-empty">' + empty('check', 'No pending approvals', 'Tasks continue automatically when their configured capabilities and autonomy allow them.') + '</section>';
  return heading('Approvals', 'Review actions that are waiting for a human decision.', '', 'WORKSPACE / APPROVALS') + cards;
}

export function metricsView(m = {}, withHeading = true) {
  return `${withHeading ? heading('Metrics', 'Agent usage, performance, and results.', '<span class="subtle-tag">Live data · last 7 days</span>', 'OBSERVABILITY / METRICS') : ''}<div class="stats-grid">${stat('Tasks succeeded', number(m.successful_tasks), 'check', `${number(m.total_tasks)} total runs`)}${stat('Tasks failed', number(m.failed_tasks), 'alert', `${number(m.cancelled_tasks)} cancelled`)}${stat('Tokens used', compact(m.total_tokens), 'bolt', `${number(m.model_calls)} model calls`)}${stat('Average time', duration(m.avg_duration_seconds), 'clock', `${number(m.avg_steps)} steps per task`)}</div><div class="stats-grid secondary-stats">${stat('Tool calls', number(m.tool_calls), 'tool', 'Cumulative total')}${stat('Model calls', number(m.model_calls), 'cpu', 'Cumulative total')}${stat('Model response', duration(m.avg_model_seconds), 'activity', 'Average duration')}${stat('Running tasks', number(m.running_tasks), 'play', `${number(m.running_agents)} agents working`)}</div><section class="panel metrics-history">${panelHeading('Activity history', 'Daily values recorded by the runtime · UTC')}${chartTabs()}<div class="chart-body">${chart(m.history, state.chart, true)}</div></section><div class="two-column"><section class="panel">${panelHeading('Most-used tools', 'Total calls by tool')}${barChart(m.tools)}</section><section class="panel">${panelHeading('Errors by agent', 'Recorded error events')}${barChart(m.errors_by_agent, 'errors')}</section></div>`;
}

export function settings() {
  const config = state.config || {}, h = state.health || {};
  return `${heading('Settings', 'Infrastructure and workspace capability settings.', '', 'WORKSPACE / SETTINGS')}<div class="settings-grid"><section class="panel">${panelHeading('Local workspace', 'Running server configuration')}<div class="settings-rows"><div><span>${icon('database')} Persistence</span><strong>SQLite</strong></div><div><span>${icon('activity')} Live updates</span><strong>Server-Sent Events</strong></div><div><span>${icon('cpu')} Concurrent workers</span><strong>${number(h.runtime?.max_workers)}</strong></div><div><span>${icon('globe')} Model provider</span><strong>Ollama</strong></div><div><span>${icon('folder')} Persistent data</span><code>${esc(config.data_dir || '—')}</code></div><div><span>${icon('folder')} Automatic workspaces</span><code>${esc(config.workspaces_dir || '—')}</code></div></div><div class="panel-note">The model, its limits, and an optional workspace folder are configured per agent. Infrastructure settings are set when the server starts.</div></section><section class="panel">${panelHeading('Per-agent security', 'Explicit capabilities and a limited scope')}<div class="security-items"><article>${icon('shield')}<div><h3>Controlled workspace root</h3><p>Reads and writes are limited to the selected folder and its allowed relative directories.</p></div></article><article>${icon('terminal')}<div><h3>Capability policy</h3><p>Each filesystem, execution, and Git action is evaluated before its underlying tool runs.</p></div></article><article>${icon('logs')}<div><h3>Secrets stay out of configuration</h3><p>Set an environment variable name on the agent. Never enter secret values in prompts or forms.</p></div></article></div></section></div><section class="panel future-panel">${panelHeading('Platform capabilities', 'This version focuses on real local execution and monitoring.')}<div class="capabilities"><div><span>Local agents · Ollama</span><span class="badge success">Available</span></div><div><span>Local queue and concurrent execution</span><span class="badge success">Available</span></div><div><span>Persistent logs and metrics</span><span class="badge success">Available</span></div>${['Remote workers and containers', 'Automatic scheduling', 'Persistent memory and RAG', 'Agent teams and hierarchies', 'Web search, browser, and external connectors', 'Multi-user authentication'].map(label => `<div><span>${label}</span><span class="subtle-tag">Unavailable</span></div>`).join('')}</div></section>`;
}
