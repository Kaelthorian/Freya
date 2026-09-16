export const state = { agents: [], tools: [], tasks: [], health: null, config: null, metrics: null, filters: { tasks: {}, logs: {} }, tab: 'timeline', chart: 'tasks', connected: false };

export async function api(path, method = 'GET', body) {
  const response = await fetch(`/api${path}`, { method, headers: { 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `La solicitud falló (${response.status}).`);
  return data;
}
export function esc(value) { return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char])); }
export const number = value => new Intl.NumberFormat('es-AR', { maximumFractionDigits: 1 }).format(Number(value) || 0);
export const compact = value => new Intl.NumberFormat('es-AR', { notation: 'compact', maximumFractionDigits: 1 }).format(Number(value) || 0);
export function duration(value) { const secs = Number(value) || 0; return secs < 60 ? `${number(secs)} s` : `${Math.floor(secs / 60)} min ${Math.round(secs % 60)} s`; }
export function date(value, full = false) { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('es-AR', { day: '2-digit', month: 'short', ...(full ? { year: 'numeric' } : {}), hour: '2-digit', minute: '2-digit' }); }
export function relative(value) { if (!value) return 'Sin actividad'; const secs = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000); return secs < 60 ? 'Hace un momento' : secs < 3600 ? `Hace ${Math.floor(secs / 60)} min` : secs < 86400 ? `Hace ${Math.floor(secs / 3600)} h` : date(value); }
export function bytes(value) { return `${number((Number(value) || 0) / 1024 ** 3)} GB`; }
export function query(values) { return new URLSearchParams(Object.entries(values).filter(([, value]) => value !== '' && value !== undefined && value !== false)).toString(); }
export function toast(message, error = false) { const element = document.createElement('div'); element.className = `toast ${error ? 'toast-error' : ''}`; element.textContent = message; document.querySelector('#toasts').append(element); setTimeout(() => element.remove(), error ? 9000 : 4500); }
export function route() { const [path, search = ''] = location.hash.replace(/^#\/?/, '').split('?'); const parts = path.split('/').filter(Boolean); return { page: parts[0] || 'dashboard', id: parts[1], query: new URLSearchParams(search) }; }
export function serialize(value) { return typeof value === 'string' ? value : JSON.stringify(value, null, 2); }
export function statusClass(status) { return ({ Running: 'running', Idle: 'idle', Waiting: 'waiting', Paused: 'waiting', Queued: 'waiting', Pending: 'muted', Success: 'success', Error: 'error', Failed: 'error', Cancelled: 'muted', Offline: 'muted', Warning: 'waiting' })[status] || 'muted'; }
export const liveTask = task => ['Queued', 'Running', 'Paused'].includes(task?.status);
