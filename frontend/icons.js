const paths = {
 dashboard: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
 agents: '<rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 3v4M2 12v4m20-4v4M9 16h6"/><path d="M8 11h.01M16 11h.01" stroke-width="3"/>',
 tasks: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V2h6v2M9 9h6m-6 4h6m-6 4h3"/>',
 logs: '<path d="m4 6 4 4-4 4m8-1h8M3 20h18"/>',
 metrics: '<path d="M4 3v17h17M8 15v-4m5 4V7m5 8V4"/>',
 settings: '<path d="m9 3-1 3-3 1 1 4-2 2 2 3v3l4 1 2 2 3-2h3l2-4 2-2-2-3V7l-4-1-2-3z"/><circle cx="12" cy="12" r="3"/>',
 plus: '<path d="M12 5v14M5 12h14"/>', arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>', chevron: '<path d="m9 5 7 7-7 7"/>', close: '<path d="m6 6 12 12M6 18 18 6"/>',
 play: '<path d="m8 5 11 7-11 7z"/>', pause: '<path d="M8 5v14M16 5v14" stroke-width="3"/>', stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>', refresh: '<path d="M20 10a8 8 0 1 0-2 8M20 4v6h-6"/>',
 check: '<path d="m5 12 4 4L19 6"/>', clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', activity: '<path d="M2 12h5l3-8 4 16 3-8h5"/>',
 cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3"/>',
 bolt: '<path d="m13 2-9 12h7l-1 8 10-13h-8z"/>', tool: '<path d="M14 6a5 5 0 0 0-6 6L3 17a3 3 0 0 0 4 4l5-5a5 5 0 0 0 6-6l-3 3-4-4z"/>',
 alert: '<path d="m12 3 10 18H2zM12 9v5m0 3h.01"/>', edit: '<path d="m15 4 5 5M4 20l5-1L21 7a2 2 0 0 0-5-5L4 14z"/>', copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M15 8V4H4v11h4"/>', trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
 shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>', globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c-5 6-5 12 0 18 5-6 5-12 0-18"/>', database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>', filter: '<path d="M3 5h18l-7 8v6l-4 2v-8z"/>', search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>', folder: '<path d="M3 6h7l2 3h9v11H3z"/>', external: '<path d="M14 3h7v7m0-7L10 14M10 4H4v16h16v-6"/>', upload: '<path d="M12 16V4m0 0-4 4m4-4 4 4M4 14v5h16v-5"/>', download: '<path d="M12 4v12m0 0-4-4m4 4 4-4M4 20h16"/>',
 terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/>', info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>', spark: '<path d="m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3z"/>', back: '<path d="M19 12H5m5-5-5 5 5 5"/>'
};
export const icon = (name, extra = '') => `<svg class="icon ${extra}" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.activity}</svg>`;
