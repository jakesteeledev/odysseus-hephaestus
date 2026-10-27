/** Small DOM helpers shared by the finance views. */

import { formatDirected, formatPence } from '../domain/money.js';

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

/** Build an element from an HTML string (single root). */
export function fragment(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html.trim();
  return tpl.content.firstElementChild;
}

const SVG = (inner, size = 12) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;

export const ICONS = {
  // Finance mark: a wallet.
  finance: (size = 14) => SVG('<path d="M19 7V5a2 2 0 0 0-2-2H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>', size),
  accounts: () => SVG('<rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/>'),
  list: () => SVG('<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>'),
  summary: () => SVG('<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>'),
  add: () => SVG('<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/>'),
  upload: () => SVG('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>'),
  settings: () => SVG('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>'),
  prev: () => SVG('<polyline points="15 18 9 12 15 6"/>', 14),
  next: () => SVG('<polyline points="9 18 15 12 9 6"/>', 14),
  transfer: () => SVG('<polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>'),
  download: () => SVG('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>'),
};

/** Coloured dot for a category colour name (theme-aware, see finance.css). */
export function categoryDot(color) {
  const name = String(color || '');
  return `<span class="fin-dot${name ? ` fin-color-${esc(name)}` : ''}" aria-hidden="true"></span>`;
}

export function amountHtml(pence, direction) {
  const cls = direction === 'in' ? 'fin-amount fin-amount-in' : 'fin-amount';
  return `<span class="${cls}">${esc(formatDirected(pence, direction))}</span>`;
}

export function balanceHtml(pence) {
  const cls = pence < 0 ? 'fin-amount fin-amount-negative' : 'fin-amount';
  return `<span class="${cls}">${esc(formatPence(pence))}</span>`;
}

export function emptyHtml(text) {
  return `<div class="fin-empty">${esc(text)}</div>`;
}

export function optionsHtml(items, selected, { blank } = {}) {
  const opts = blank !== undefined ? [`<option value="">${esc(blank)}</option>`] : [];
  for (const item of items) {
    opts.push(`<option value="${esc(item.value)}"${item.value === selected ? ' selected' : ''}>${esc(item.label)}</option>`);
  }
  return opts.join('');
}
