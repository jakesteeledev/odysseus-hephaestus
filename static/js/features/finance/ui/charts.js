/**
 * Small dependency-free charts for the finance Insights tab.
 *
 * Colour comes from the theme: the accent (--red) marks the series a chart
 * is about, a light neutral step of the text colour marks supporting
 * series, and position (above / below the zero line) separates assets from
 * liabilities and income from spending, so identity never rests on colour
 * alone. Text always uses text colours. Every chart has hover/keyboard
 * tooltips and a table view (rendered by the caller).
 */

import { formatPence } from '../domain/money.js';

const NS = 'http://www.w3.org/2000/svg';
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const PAD = { top: 14, right: 16, bottom: 26, left: 52 };
const HEIGHT = 220;
const MAX_COLUMN = 24;

export function monthShort(ym, withYear = false) {
  const [y, m] = String(ym).split('-').map(Number);
  return withYear ? `${MONTHS[m - 1]} ${String(y).slice(2)}` : MONTHS[m - 1];
}

/** Axis money: £0, £850, £1.2K, £15K, £1.2M, -£2K. */
export function compactPence(pence) {
  const pounds = Math.abs(pence) / 100;
  const sign = pence < 0 ? '-' : '';
  if (pounds >= 1e6) return `${sign}£${(pounds / 1e6).toFixed(pounds >= 1e7 ? 0 : 1).replace(/\.0$/, '')}M`;
  if (pounds >= 1e3) return `${sign}£${(pounds / 1e3).toFixed(pounds >= 1e4 ? 0 : 1).replace(/\.0$/, '')}K`;
  return `${sign}£${Math.round(pounds)}`;
}

/** Clean axis ticks covering [lo, hi] (pence), always including 0. */
export function niceTicks(lo, hi, count = 4) {
  lo = Math.min(lo, 0);
  hi = Math.max(hi, 0);
  if (lo === hi) hi = lo + 10000;
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((f) => f * mag).find((s) => s >= raw) || 10 * mag;
  const start = Math.floor(lo / step) * step;
  const ticks = [];
  for (let v = start; v <= hi + step * 0.001; v += step) ticks.push(Math.round(v));
  if (ticks[ticks.length - 1] < hi) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

function el(name, attrs = {}, parent) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (parent) parent.appendChild(node);
  return node;
}

/** Column path with a 4px rounded data end and a square baseline end. */
function columnPath(x, w, base, end) {
  const up = end < base;
  const h = Math.abs(base - end);
  const r = Math.min(4, h, w / 2);
  if (h < 0.5) return '';
  if (up) {
    return `M${x},${base}V${end + r}Q${x},${end} ${x + r},${end}H${x + w - r}Q${x + w},${end} ${x + w},${end + r}V${base}Z`;
  }
  return `M${x},${base}V${end - r}Q${x},${end} ${x + r},${end}H${x + w - r}Q${x + w},${end} ${x + w},${end - r}V${base}Z`;
}

function frame(container, points, values, { mirror = false } = {}) {
  container.replaceChildren();
  const width = Math.max(280, Math.floor(container.clientWidth || 560));
  const svg = el('svg', { viewBox: `0 0 ${width} ${HEIGHT}`, width: '100%', height: HEIGHT, class: 'fin-chart-svg', 'aria-hidden': 'true' });
  container.appendChild(svg);
  const ticks = niceTicks(Math.min(...values), Math.max(...values));
  const plotW = width - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const lo = ticks[0];
  const hi = ticks[ticks.length - 1];
  const y = (v) => PAD.top + plotH - ((v - lo) / (hi - lo)) * plotH;
  const band = plotW / points.length;
  const cx = (i) => PAD.left + band * i + band / 2;
  // Grid and y axis: solid hairlines, recessive; the zero line slightly stronger.
  for (const t of ticks) {
    el('line', { x1: PAD.left, x2: width - PAD.right, y1: y(t), y2: y(t), class: t === 0 ? 'fin-chart-zero' : 'fin-chart-grid' }, svg);
    const label = el('text', { x: PAD.left - 6, y: y(t) + 3, class: 'fin-chart-axis', 'text-anchor': 'end' }, svg);
    // Mirrored charts draw positive amounts downwards (liabilities, spending): label magnitudes.
    label.textContent = compactPence(mirror ? Math.abs(t) : t);
  }
  // X labels: thinned so they never collide.
  const every = Math.max(1, Math.ceil(points.length / Math.max(2, Math.floor(plotW / 46))));
  points.forEach((p, i) => {
    if (i % every !== 0) return;
    const first = i === 0 || p.month.endsWith('-01');
    const label = el('text', { x: cx(i), y: HEIGHT - 8, class: 'fin-chart-axis', 'text-anchor': 'middle' }, svg);
    label.textContent = monthShort(p.month, first);
  });
  return { svg, width, y, band, cx, plotTop: PAD.top, plotBottom: PAD.top + plotH, zero: y(0) };
}

/**
 * Hover and keyboard layer for band charts: a crosshair snaps to the nearest
 * month and one tooltip lists every series. `rows(i)` returns
 * [{ label, value, key: 'line'|'rect', cls }].
 */
function attachTooltip(container, f, count, rows, title) {
  const tip = document.createElement('div');
  tip.className = 'fin-chart-tip';
  tip.hidden = true;
  container.appendChild(tip);
  const cross = el('line', { y1: f.plotTop, y2: f.plotBottom, class: 'fin-chart-cross', visibility: 'hidden' }, f.svg);
  let active = -1;
  function show(i) {
    active = Math.max(0, Math.min(count - 1, i));
    cross.setAttribute('x1', f.cx(active));
    cross.setAttribute('x2', f.cx(active));
    cross.setAttribute('visibility', 'visible');
    tip.replaceChildren();
    const head = document.createElement('div');
    head.className = 'fin-chart-tip-title';
    head.textContent = title(active);
    tip.appendChild(head);
    for (const row of rows(active)) {
      const line = document.createElement('div');
      line.className = 'fin-chart-tip-row';
      const key = document.createElement('span');
      key.className = `fin-chart-key fin-chart-key-${row.key} ${row.cls}`;
      const value = document.createElement('strong');
      value.textContent = formatPence(row.value);
      const label = document.createElement('span');
      label.className = 'fin-chart-tip-label';
      label.textContent = row.label;
      line.append(key, value, label);
      tip.appendChild(line);
    }
    tip.hidden = false;
    const x = (f.cx(active) / f.width) * container.clientWidth;
    const left = x + 12 + tip.offsetWidth > container.clientWidth ? x - 12 - tip.offsetWidth : x + 12;
    tip.style.left = `${Math.max(0, left)}px`;
    tip.style.top = `${f.plotTop}px`;
  }
  function hide() {
    active = -1;
    tip.hidden = true;
    cross.setAttribute('visibility', 'hidden');
  }
  const index = (clientX) => {
    const rect = f.svg.getBoundingClientRect();
    const x = ((clientX - rect.left) / rect.width) * f.width;
    return Math.floor((x - PAD.left) / f.band);
  };
  f.svg.addEventListener('pointermove', (e) => {
    const i = index(e.clientX);
    if (i >= 0 && i < count) show(i); else hide();
  });
  f.svg.addEventListener('pointerleave', hide);
  container.tabIndex = 0;
  container.addEventListener('focus', () => show(active < 0 ? count - 1 : active));
  container.addEventListener('blur', hide);
  container.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      show((active < 0 ? count - 1 : active) + (e.key === 'ArrowRight' ? 1 : -1));
    } else if (e.key === 'Escape') hide();
  });
}

function endLabel(f, x, yValue, text) {
  const label = el('text', { x: Math.min(x, f.width - PAD.right), y: Math.max(f.plotTop + 10, yValue - 10), class: 'fin-chart-label', 'text-anchor': 'end' }, f.svg);
  label.textContent = text;
}

/** Net worth history: assets above and liabilities below zero, net worth as the accent line. */
export function netWorthChart(container, points) {
  if (!points.length) return;
  const values = points.flatMap((p) => [p.assets_pence, -p.liabilities_pence, p.net_worth_pence]);
  const f = frame(container, points, values, { mirror: true });
  const w = Math.min(MAX_COLUMN, Math.max(4, f.band * 0.55));
  const cols = el('g', { class: 'fin-c-neutral' }, f.svg);
  points.forEach((p, i) => {
    const x = f.cx(i) - w / 2;
    // A 2px surface gap at zero keeps the column above apart from the one below.
    const up = columnPath(x, w, f.zero - 1, Math.min(f.zero - 1, f.y(p.assets_pence)));
    const down = columnPath(x, w, f.zero + 1, Math.max(f.zero + 1, f.y(-p.liabilities_pence)));
    if (up) el('path', { d: up }, cols);
    if (down) el('path', { d: down }, cols);
  });
  const line = el('g', { class: 'fin-c-accent' }, f.svg);
  el('polyline', { points: points.map((p, i) => `${f.cx(i)},${f.y(p.net_worth_pence)}`).join(' '), class: 'fin-chart-line' }, line);
  const last = points.length - 1;
  if (points.length <= 24) points.forEach((p, i) => el('circle', { cx: f.cx(i), cy: f.y(p.net_worth_pence), r: i === last ? 4 : 3, class: 'fin-chart-dot' }, line));
  else el('circle', { cx: f.cx(last), cy: f.y(points[last].net_worth_pence), r: 4, class: 'fin-chart-dot' }, line);
  endLabel(f, f.cx(last) + 4, f.y(points[last].net_worth_pence), compactPence(points[last].net_worth_pence));
  attachTooltip(container, f, points.length, (i) => [
    { label: 'Net worth', value: points[i].net_worth_pence, key: 'line', cls: 'fin-c-accent' },
    { label: 'Assets', value: points[i].assets_pence, key: 'rect', cls: 'fin-c-neutral' },
    { label: 'Liabilities', value: points[i].liabilities_pence, key: 'rect', cls: 'fin-c-neutral' },
  ], (i) => `${monthShort(points[i].month, true)} · as of ${points[i].as_of}`);
}

/**
 * Net worth per household member: one line each in the member's colour.
 * Lines also differ by dash pattern and the tooltip names everyone, so
 * telling them apart never rests on colour alone.
 */
export function membersChart(container, points, members) {
  if (!points.length || !members.length) return;
  const series = members.map((member, index) => ({
    member, index, cls: `fin-member-line${member.color ? ` fin-color-${member.color}` : ''}`,
    values: points.map((p) => p.by_member?.[member.id] ?? 0),
  }));
  const f = frame(container, points, series.flatMap((s) => s.values));
  const last = points.length - 1;
  for (const s of series) {
    const g = el('g', { class: s.cls }, f.svg);
    const dash = s.index % 4 ? ` fin-chart-dash-${s.index % 4}` : '';
    el('polyline', { points: s.values.map((v, i) => `${f.cx(i)},${f.y(v)}`).join(' '), class: `fin-chart-line${dash}` }, g);
    el('circle', { cx: f.cx(last), cy: f.y(s.values[last]), r: 3.5, class: 'fin-chart-dot' }, g);
  }
  attachTooltip(container, f, points.length, (i) => series.map((s) => (
    { label: s.member.display_name, value: s.values[i], key: 'line', cls: s.cls })),
  (i) => `${monthShort(points[i].month, true)} · as of ${points[i].as_of}`);
}

/** Income above and spending below zero, per month. */
export function incomeSpendingChart(container, points) {
  if (!points.length) return;
  const values = points.flatMap((p) => [p.in_pence, -p.out_pence]);
  const f = frame(container, points, values, { mirror: true });
  const w = Math.min(MAX_COLUMN, Math.max(4, f.band * 0.55));
  const income = el('g', { class: 'fin-c-neutral' }, f.svg);
  const spending = el('g', { class: 'fin-c-accent' }, f.svg);
  points.forEach((p, i) => {
    const x = f.cx(i) - w / 2;
    const up = columnPath(x, w, f.zero - 1, Math.min(f.zero - 1, f.y(p.in_pence)));
    const down = columnPath(x, w, f.zero + 1, Math.max(f.zero + 1, f.y(-p.out_pence)));
    if (up) el('path', { d: up }, income);
    if (down) el('path', { d: down }, spending);
  });
  attachTooltip(container, f, points.length, (i) => [
    { label: 'Money in', value: points[i].in_pence, key: 'rect', cls: 'fin-c-neutral' },
    { label: 'Money out', value: points[i].out_pence, key: 'rect', cls: 'fin-c-accent' },
    { label: 'Net', value: points[i].net_pence, key: 'line', cls: 'fin-c-none' },
  ], (i) => monthShort(points[i].month, true));
}

/** One series of monthly columns (spending trend). */
export function trendChart(container, points, valueOf, label) {
  if (!points.length) return;
  const values = points.map(valueOf);
  const f = frame(container, points, values);
  const w = Math.min(MAX_COLUMN, Math.max(4, f.band * 0.55));
  const g = el('g', { class: 'fin-c-accent' }, f.svg);
  points.forEach((p, i) => {
    const d = columnPath(f.cx(i) - w / 2, w, f.zero, f.y(valueOf(p)));
    if (d) el('path', { d }, g);
  });
  const last = points.length - 1;
  if (valueOf(points[last]) > 0) {
    const t = el('text', { x: f.cx(last), y: f.y(valueOf(points[last])) - 5, class: 'fin-chart-label', 'text-anchor': 'middle' }, f.svg);
    t.textContent = compactPence(valueOf(points[last]));
  }
  attachTooltip(container, f, points.length, (i) => [
    { label, value: valueOf(points[i]), key: 'rect', cls: 'fin-c-accent' },
  ], (i) => monthShort(points[i].month, true));
}

/** Horizontal bars (HTML): one series, value at the tip, tooltip on hover/focus. */
export function barList(container, rows) {
  container.replaceChildren();
  const max = Math.max(1, ...rows.map((r) => r.value));
  const list = document.createElement('div');
  list.className = 'fin-hbars';
  for (const row of rows) {
    const item = document.createElement('div');
    item.className = 'fin-hbar';
    item.tabIndex = 0;
    item.title = `${row.label}: ${formatPence(row.value)}${row.note ? ` (${row.note})` : ''}`;
    const name = document.createElement('span');
    name.className = 'fin-hbar-name';
    name.textContent = row.label;
    const track = document.createElement('span');
    track.className = 'fin-hbar-track';
    const fill = document.createElement('span');
    fill.className = 'fin-hbar-fill fin-c-accent';
    fill.style.width = `${Math.max(1, Math.round((row.value / max) * 100))}%`;
    track.appendChild(fill);
    const value = document.createElement('span');
    value.className = 'fin-hbar-value';
    value.textContent = formatPence(row.value);
    item.append(name, track, value);
    list.appendChild(item);
  }
  container.appendChild(list);
}

/** Budget vs actual (HTML): spent bar against a limit marker; over budget uses status colour + icon + label. */
export function budgetBullets(container, rows) {
  container.replaceChildren();
  const max = Math.max(1, ...rows.map((r) => Math.max(r.limit_pence, r.spent_pence)));
  const list = document.createElement('div');
  list.className = 'fin-hbars';
  for (const row of rows) {
    const item = document.createElement('div');
    item.className = 'fin-hbar';
    item.tabIndex = 0;
    const over = row.spent_pence > row.limit_pence;
    item.title = `${row.name}: ${formatPence(row.spent_pence)} of ${formatPence(row.limit_pence)}${over ? ' (over budget)' : ''}`;
    const name = document.createElement('span');
    name.className = 'fin-hbar-name';
    name.textContent = row.name;
    const track = document.createElement('span');
    track.className = 'fin-hbar-track';
    const fill = document.createElement('span');
    fill.className = `fin-hbar-fill ${over ? 'fin-c-over' : 'fin-c-accent'}`;
    fill.style.width = `${Math.max(1, Math.round((row.spent_pence / max) * 100))}%`;
    const marker = document.createElement('span');
    marker.className = 'fin-hbar-limit';
    marker.style.left = `${Math.round((row.limit_pence / max) * 100)}%`;
    track.append(fill, marker);
    const value = document.createElement('span');
    value.className = 'fin-hbar-value';
    value.textContent = `${formatPence(row.spent_pence)} / ${formatPence(row.limit_pence)}`;
    item.append(name, track, value);
    if (over) {
      const flag = document.createElement('span');
      flag.className = 'fin-hbar-flag';
      flag.textContent = '! Over';
      item.appendChild(flag);
    }
    list.appendChild(item);
  }
  container.appendChild(list);
}

/** A <details> table twin for a chart: rows of [label, ...values]. */
export function tableDetails(headers, rows) {
  const details = document.createElement('details');
  details.className = 'fin-chart-table';
  const summary = document.createElement('summary');
  summary.textContent = 'Show as table';
  const table = document.createElement('table');
  const head = table.createTHead().insertRow();
  for (const h of headers) {
    const th = document.createElement('th');
    th.textContent = h;
    head.appendChild(th);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    row.forEach((cell) => { tr.insertCell().textContent = cell; });
  }
  details.append(summary, table);
  return details;
}
