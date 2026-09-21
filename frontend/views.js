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
const FREYA_ORCHESTRATION_ACTIVE = new Set(['Queued', 'Planning', 'Planned', 'Running', 'Integrating']);

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
  const action = active ? button('Stop', 'cancel-orchestration', 'stop', 'data-id="' + esc(run.id) + '"', 'small-button danger-quiet') : '<a class="text-link" href="#/logs?orchestration_id=' + encodeURIComponent(run.id || '') + '">' + (failed ? 'Diagnosis' : 'Logs') + ' ' + icon('arrow') + '</a>';
  return [
    '<tr>',
    '<td>' + badge(run.status || 'Pending') + '</td>',
    '<td><strong class="table-title">' + esc(taskTitle(run.prompt)) + '</strong><span class="table-sub" title="' + esc(failure) + '">' + (failed ? esc(failure) : active ? 'Waiting for a delegated task to start' : 'Orchestration completed') + '</span></td>',
    '<td><strong>Freya</strong><span class="table-sub">Orchestrator</span></td>',
    '<td class="nowrap muted">' + date(startedAt) + '</td>',
    '<td class="mono nowrap">' + duration(elapsed) + '</td>',
    '<td class="mono">—</td>',
    '<td class="mono">—</td>',
    '<td>' + action + '</td>',
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

  const taskHistory = state.freyaHistory.filter(item => item.kind === 'task');
  const rows = (taskHistory.length ? taskHistory : state.freyaHistory).slice().sort((a, b) => {
    const activeA = a.kind === 'task' ? FREYA_ACTIVE_STATUSES.has(a.task.status) : FREYA_ORCHESTRATION_ACTIVE.has(a.run.status);
    const activeB = b.kind === 'task' ? FREYA_ACTIVE_STATUSES.has(b.task.status) : FREYA_ORCHESTRATION_ACTIVE.has(b.run.status);
    if (activeA !== activeB) return activeA ? -1 : 1;
    return freyaStartedAt(b) - freyaStartedAt(a);
  });
  const activeCount = rows.filter(item => item.kind === 'task' ? FREYA_ACTIVE_STATUSES.has(item.task.status) : FREYA_ORCHESTRATION_ACTIVE.has(item.run.status)).length;
  const table = rows.length ? '<div class="table-wrap"><table class="freya-current-table" aria-label="Freya task history"><thead><tr><th>STATUS</th><th>TASK</th><th>AGENT</th><th>STARTED</th><th>ELAPSED</th><th>TOKENS</th><th>MODEL CALLS</th><th></th></tr></thead><tbody>' + rows.map(item => item.kind === 'task' ? freyaTaskRow(item) : freyaRunRow(item)).join('') + '</tbody></table></div>' : '<div class="freya-empty-live">No task is running right now. Assign a task to start live activity.</div>';
  const label = activeCount ? activeCount + ' active · ' + rows.length + ' total' : rows.length ? rows.length + ' completed' : 'Idle';
  return heading('Freya', 'Tell Freya what you need and it will coordinate the available agents.', '', 'ORCHESTRATOR') +
    '<section class="panel"><form id="freya-form" class="stack-form"><label for="freya-prompt">What do you need?</label><textarea id="freya-prompt" name="prompt" rows="5" required placeholder="Describe the outcome you want...">' + esc(state.freyaDraft.prompt || '') + '</textarea><label for="freya-workspace">Workspace folder (existing files)</label><div class="workspace-input-row"><input id="freya-workspace" name="workspace_path" value="' + esc(state.freyaDraft.workspace_path || '') + '" placeholder="Select an existing folder, or leave empty for an isolated workspace"><button type="button" class="button secondary" data-action="freya-workspace">Choose folder</button></div><p class="small muted workspace-help">A selected folder is used directly, so Freya can act on its existing files. Leave it empty to create an isolated workspace.</p><button class="button primary" type="submit">Ask Freya</button></form></section>' +
    '<section class="panel freya-overview"><div class="freya-section-heading"><div><span class="eyebrow">LIVE OVERVIEW</span><h2>Freya activity</h2><p>Live execution and completed rows remain visible until you submit a new task.</p></div><span class="subtle-tag">' + label + '</span></div>' + table + '</section>';
}
export function dashboard() {
  const m = state.metrics || {}, h = state.health || {}, system = h.system || {};
  return `${heading('Operations dashboard', 'A clear view of your agents, runs, and resources.', button('Assign task', 'assign', 'play', '', 'secondary') + button('Programmer preset', 'create-programmer', 'cpu', '', 'secondary') + button('Create agent', 'create-agent', 'plus', '', 'primary'), 'OVERVIEW')}
    <div class="stats-grid">${stat('Total agents', number(m.total_agents), 'agents', `<span class="mini-dot"></span> ${number(m.active_agents)} active · ${number(m.running_agents)} running`)}${stat('Runs completed', number(m.total_tasks), 'tasks', `<span class="text-green">${number(m.successful_tasks)} succeeded</span><span class="note-separator">·</span>${number(m.failed_tasks)} failed`)}${stat('Tokens used', compact(m.total_tokens), 'bolt', `${number(m.model_calls)} model calls`)}${stat('Average time', duration(m.avg_duration_seconds), 'clock', `Per completed task`)}</div>
    <div class="dashboard-grid"><section class="panel activity-panel">${panelHeading('Execution activity', 'Actual history for the last 7 days · UTC', '<span class="subtle-tag">Last 7 days</span>')}${chartTabs()}<div class="chart-body">${chart(m.history, state.chart)}</div></section>
    <section class="panel runtime-panel">${panelHeading('System status', 'Local infrastructure', `<span class="online-dot"></span>`)}<div class="runtime-status">${icon('cpu')}<div><strong>Agent Runtime</strong><span>${h.status === 'ok' ? 'Online' : 'Connecting'}</span></div><span class="badge success">Local</span></div><div class="resource"><div><span>CPU</span><strong class="mono">${system.cpu_percent == null ? 'N/A' : `${number(system.cpu_percent)}%`}</strong></div><div class="resource-track"><span style="width:${Math.min(100, Math.max(0, system.cpu_percent || 0))}%"></span></div></div><div class="resource"><div><span>Memory</span><strong class="mono">${system.ram_percent == null ? 'N/A' : `${number(system.ram_percent)}%`}</strong></div><div class="resource-track"><span style="width:${Math.min(100, Math.max(0, system.ram_percent || 0))}%"></span></div><p>${system.ram_total_bytes ? `${bytes(system.ram_used_bytes)} of ${bytes(system.ram_total_bytes)}` : 'Telemetry is unavailable on this system'}</p></div><div class="runtime-facts"><div><span>Concurrent workers</span><strong>${number(h.runtime?.max_workers)}</strong></div><div><span>Database</span><strong>${esc(h.database || '—')}</strong></div><div><span>Agents with errors</span><strong class="${m.error_agents ? 'text-red' : ''}">${number(m.error_agents)}</strong></div></div></section></div>
    <section class="panel recent-tasks">${panelHeading('Recent runs', 'From the first step to the final result', '<a class="text-link" href="#/logs">View all task logs ' + icon('arrow') + '</a>')}${taskTable(state.tasks.slice(0, 5), { small: true })}</section>
    <section class="agents-preview">${panelHeading('Your agents', `${number(m.total_agents)} agents in this workspace`, '<a class="text-link" href="#/agents">Manage agents ' + icon('arrow') + '</a>')}${state.agents.length ? `<div class="agent-grid">${state.agents.slice(0, 3).map(agentCard).join('')}</div>` : `<div class="onboarding-strip"><div class="onboarding-icon">${icon('agents')}</div><div><h3>Your agent team starts here</h3><p>Connect a model, choose its tools, and assign its first task.</p></div>${button('Create your first agent', 'create-agent', 'plus', '', 'secondary')}</div>`}</section>`;
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
  return `<a class="back-link" href="#/logs">${icon('back')} Logs</a>${heading('Run details', task.id, actions, 'TASK EXECUTION')}<section class="panel task-overview"><div class="task-title-row"><h2>${esc(task.prompt)}</h2>${badge(task.status)}</div><div class="task-metadata"><span>${icon('agents')}<a href="#/agents/${esc(task.agent_id)}">${esc(task.agent_name)}</a></span><span>${icon('clock')}${date(task.started_at || task.created_at)}</span><span>${icon('cpu')}${esc(task.config.model)}</span></div>${progress(task)}<p class="small muted">The percentage shows the step budget used; it does not estimate the time remaining.</p><div class="task-stats"><div><span>Duration</span><strong>${duration(task.duration_seconds)}</strong></div><div><span>Steps</span><strong>${number(task.steps)}</strong></div><div><span>Tokens</span><strong>${compact(task.total_tokens)}</strong></div><div><span>Model / tools</span><strong>${number(task.model_calls)} / ${number(task.tool_calls)}</strong></div><div><span>Finished</span><strong class="small">${date(task.finished_at)}</strong></div></div><details data-detail="workspace-${esc(id)}" class="workspace-detail"><summary>${icon('folder')} Run workspace</summary><code>${esc(task.workspace)}</code></details></section>${task.status === 'Paused' ? '<div class="notice">Pause requested: the runtime waits between actions and keeps this run active.</div>' : task.status === 'WaitingForApproval' ? '<div class="notice">This run is waiting for a human approval. Open Approvals to resolve it.</div>' : ''}${task.error ? `<div class="error-banner">${icon('alert')}<div><strong>Run error</strong><p>${esc(uiText(task.error))}</p></div></div>` : ''}${task.result ? `<section class="panel task-result">${panelHeading('Result', 'Final response from the agent')}<pre>${esc(serialize(task.result))}</pre></section>` : ''}<section class="panel">${panelHeading('Execution Timeline', 'Persisted actions, tools, and results', `<span class="subtle-tag">${number((task.events || task.timeline || []).length)} events</span>`)}${timeline(task.events || task.timeline || [])}</section>`;
}

export async function logs() {
  const f = state.filters.logs, events = await api(`/logs?${query({ ...f, limit: 200 })}`);
  return `${heading('Logs', 'Tasks and their complete execution record in one place.', button('Refresh', 'refresh', 'refresh', '', 'secondary') + button('Copy all logs', 'copy-logs', 'copy', '', 'secondary'), 'OBSERVABILITY / LOGS')}<section class="panel"><form class="log-filters" id="log-filters"><label>Agent<select name="agent_id" data-filter="logs">${agentOptions(f.agent_id)}</select></label><label>Task ID<input name="task_id" value="${esc(f.task_id || '')}" placeholder="Task ID" data-filter="logs"></label><label>Level<select name="level" data-filter="logs"><option value="">All levels</option>${['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'].map(v => `<option ${f.level === v ? 'selected' : ''}>${v}</option>`).join('')}</select></label><label>Tool<select name="tool" data-filter="logs"><option value="">All tools</option>${state.tools.map(t => `<option ${f.tool === t.name ? 'selected' : ''}>${esc(t.name)}</option>`).join('')}</select></label><label>From<input type="datetime-local" name="date_from" value="${esc(f.date_from || '')}" data-filter="logs"></label><label>To<input type="datetime-local" name="date_to" value="${esc(f.date_to || '')}" data-filter="logs"></label><label class="checkbox-label"><input type="checkbox" name="error_only" data-filter="logs" ${f.error_only ? 'checked' : ''}>Errors only</label>${button('Clear filters', 'clear-logs', '', '', 'small-button')}</form><div class="log-list-meta"><span>${number(events.length)} events · tasks and execution details</span><span>${icon('shield')} Sensitive content is sanitized by the server</span></div><div class="log-table-header" aria-hidden="true"><span>Date and time</span><span>Level</span><span>Event / tool</span><span>Agent</span><span></span></div>${groupedLogTable(events, state.tasks)}</section>`;
}

export function approvals() {
  const items = state.approvals || [];
  const cards = items.length ? '<div class="approval-list">' + items.map(item => '<article class="panel approval-card"><div class="approval-card-header"><div><span class="eyebrow">PENDING APPROVAL</span><h2>' + esc(item.action_summary || item.capability) + '</h2></div>' + badge(item.status || 'pending') + '</div><div class="key-values"><span>Capability</span><code>' + esc(item.capability) + '</code><span>Tool</span><code>' + esc(item.tool) + '</code><span>Resource</span><code>' + esc(item.resource || '.') + '</code><span>Reason</span><span>' + esc(item.reason) + '</span><span>Arguments</span><pre>' + esc(serialize(item.arguments || {})) + '</pre><span>Created</span><span>' + date(item.created_at, true) + '</span></div><div class="approval-actions"><button class="button primary" data-action="approve-once" data-id="' + esc(item.id) + '">Approve once</button><button class="button secondary" data-action="approve-task" data-id="' + esc(item.id) + '">Approve for task</button><button class="button danger-quiet" data-action="deny-approval" data-id="' + esc(item.id) + '">Deny</button></div></article>').join('') + '</div>' : '<section class="panel big-empty">' + empty('check', 'No pending approvals', 'Tasks continue automatically when their configured capabilities and autonomy allow them.') + '</section>';
  return heading('Approvals', 'Review actions that are waiting for a human decision.', '', 'WORKSPACE / APPROVALS') + cards;
}

export function metricsView(m = {}, withHeading = true) {
  return `${withHeading ? heading('Metrics', 'Agent usage, performance, and results.', '<span class="subtle-tag">Live data · last 7 days</span>', 'OBSERVABILITY / METRICS') : ''}<div class="stats-grid">${stat('Tasks succeeded', number(m.successful_tasks), 'check', `${number(m.total_tasks)} total runs`)}${stat('Tasks failed', number(m.failed_tasks), 'alert', `${number(m.cancelled_tasks)} cancelled`)}${stat('Tokens used', compact(m.total_tokens), 'bolt', `${number(m.model_calls)} model calls`)}${stat('Average time', duration(m.avg_duration_seconds), 'clock', `${number(m.avg_steps)} steps per task`)}</div><div class="stats-grid secondary-stats">${stat('Tool calls', number(m.tool_calls), 'tool', 'Cumulative total')}${stat('Model calls', number(m.model_calls), 'cpu', 'Cumulative total')}${stat('Model response', duration(m.avg_model_seconds), 'activity', 'Average duration')}${stat('Running tasks', number(m.running_tasks), 'play', `${number(m.running_agents)} agents working`)}</div><section class="panel metrics-history">${panelHeading('Activity history', 'Daily values recorded by the runtime · UTC')}${chartTabs()}<div class="chart-body">${chart(m.history, state.chart, true)}</div></section><div class="two-column"><section class="panel">${panelHeading('Most-used tools', 'Total calls by tool')}${barChart(m.tools)}</section><section class="panel">${panelHeading('Errors by agent', 'Recorded error events')}${barChart(m.errors_by_agent, 'errors')}</section></div>`;
}

export function settings() {
  const config = state.config || {}, h = state.health || {};
  return `${heading('Settings', 'Infrastructure and workspace capability settings.', '', 'WORKSPACE / SETTINGS')}<div class="settings-grid"><section class="panel">${panelHeading('Local workspace', 'Running server configuration')}<div class="settings-rows"><div><span>${icon('database')} Persistence</span><strong>SQLite</strong></div><div><span>${icon('activity')} Live updates</span><strong>Server-Sent Events</strong></div><div><span>${icon('cpu')} Concurrent workers</span><strong>${number(h.runtime?.max_workers)}</strong></div><div><span>${icon('globe')} Model provider</span><strong>Ollama</strong></div><div><span>${icon('folder')} Persistent data</span><code>${esc(config.data_dir || '—')}</code></div><div><span>${icon('folder')} Automatic workspaces</span><code>${esc(config.workspaces_dir || '—')}</code></div></div><div class="panel-note">The model, its limits, and an optional workspace folder are configured per agent. Infrastructure settings are set when the server starts.</div></section><section class="panel">${panelHeading('Per-agent security', 'Explicit capabilities and a limited scope')}<div class="security-items"><article>${icon('shield')}<div><h3>Controlled workspace root</h3><p>Reads and writes are limited to the selected folder and its allowed relative directories.</p></div></article><article>${icon('terminal')}<div><h3>Capability policy</h3><p>Each filesystem, execution, and Git action is evaluated before its underlying tool runs.</p></div></article><article>${icon('logs')}<div><h3>Secrets stay out of configuration</h3><p>Set an environment variable name on the agent. Never enter secret values in prompts or forms.</p></div></article></div></section></div><section class="panel future-panel">${panelHeading('Platform capabilities', 'This version focuses on real local execution and monitoring.')}<div class="capabilities"><div><span>Local agents · Ollama</span><span class="badge success">Available</span></div><div><span>Local queue and concurrent execution</span><span class="badge success">Available</span></div><div><span>Persistent logs and metrics</span><span class="badge success">Available</span></div>${['Remote workers and containers', 'Automatic scheduling', 'Persistent memory and RAG', 'Agent teams and hierarchies', 'Web search, browser, and external connectors', 'Multi-user authentication'].map(label => `<div><span>${label}</span><span class="subtle-tag">Unavailable</span></div>`).join('')}</div></section>`;
}
