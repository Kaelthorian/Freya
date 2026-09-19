export const state = { agents: [], skills: [], tools: [], tasks: [], approvals: [], health: null, config: null, metrics: null, orchestrations: [], freyaDraft: { prompt: '', workspace_path: '' }, filters: { tasks: {}, logs: {}, skills: {} }, tab: 'timeline', chart: 'tasks', connected: false };

export async function api(path, method = 'GET', body) {
  const response = await fetch(`/api${path}`, { method, headers: { 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}
export function esc(value) { return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char])); }
export const number = value => new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 }).format(Number(value) || 0);
export const compact = value => new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(Number(value) || 0);
export function duration(value) { const secs = Number(value) || 0; return secs < 60 ? `${number(secs)} s` : `${Math.floor(secs / 60)} min ${Math.round(secs % 60)} s`; }
export function date(value, full = false) { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('en-US', { day: '2-digit', month: 'short', ...(full ? { year: 'numeric' } : {}), hour: '2-digit', minute: '2-digit' }); }
export function relative(value) { if (!value) return 'No activity'; const secs = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000); return secs < 60 ? 'Just now' : secs < 3600 ? `${Math.floor(secs / 60)} min ago` : secs < 86400 ? `${Math.floor(secs / 3600)} hr ago` : date(value); }
export function bytes(value) { return `${number((Number(value) || 0) / 1024 ** 3)} GB`; }
export function query(values) { return new URLSearchParams(Object.entries(values).filter(([, value]) => value !== '' && value !== undefined && value !== false)).toString(); }
export function toast(message, error = false) { const element = document.createElement('div'); element.className = `toast ${error ? 'toast-error' : ''}`; element.textContent = message; document.querySelector('#toasts').append(element); setTimeout(() => element.remove(), error ? 9000 : 4500); }
export function route() { const [path, search = ''] = location.hash.replace(/^#\/?/, '').split('?'); const parts = path.split('/').filter(Boolean); return { page: parts[0] || 'dashboard', id: parts[1], query: new URLSearchParams(search) }; }
export function serialize(value) { return typeof value === 'string' ? value : JSON.stringify(value, null, 2); }
const LEGACY_COPY = {
  'Esperar un trabajador y un workspace disponibles; cada agente ejecuta una tarea a la vez.': 'Waiting for an available worker and workspace; each agent runs one task at a time.',
  'Esperar un trabajador disponible; cada agente ejecuta una tarea a la vez.': 'Waiting for an available worker; each agent runs one task at a time.',
  'Pausa solicitada: la llamada en curso termina antes de pausar; el tiempo límite sigue avanzando.': 'Pause requested: the current call must finish before pausing, and the time limit continues to run.',
  'Proceso local iniciado con presupuesto de tiempo y herramientas del agente.': 'Local process started with the agent time budget and tools.',
  'Inspeccionar los archivos del espacio de trabajo.': 'Inspect the workspace files.',
  'Consultar el contenido de un archivo permitido.': 'Read the contents of an allowed file.',
  'Guardar el archivo solicitado dentro del espacio de trabajo.': 'Save the requested file in the workspace.',
  'Pausa cooperativa entre llamadas.': 'Paused between calls.',
  'Ejecución reanudada.': 'Run resumed.',
  'Solicitar la siguiente acción al modelo.': "Request the model's next action.",
  'Validar la herramienta solicitada contra los permisos del agente.': "Validate the requested tool against the agent's permissions.",
  'La solicitud falló': 'Request failed',
};
export const uiText = value => LEGACY_COPY[String(value ?? '')] || String(value ?? '');
export function statusClass(status) { return ({ Planning: 'running', Planned: 'waiting', Running: 'running', Integrating: 'running', Idle: 'idle', Waiting: 'waiting', WaitingForApproval: 'waiting', Paused: 'waiting', Queued: 'waiting', Pending: 'muted', Success: 'success', Error: 'error', Failed: 'error', Cancelled: 'muted', Offline: 'muted', Warning: 'waiting' })[status] || 'muted'; }
export const liveTask = task => ['Queued', 'Running', 'WaitingForApproval', 'Paused'].includes(task?.status);
