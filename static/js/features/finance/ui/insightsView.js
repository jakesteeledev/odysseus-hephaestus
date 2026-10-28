/**
 * Insights: the month's totals and spending by category, budget vs actual,
 * net worth history, income vs spending and the spending trend. One filter
 * row scopes everything below it: the month (shared with Transactions),
 * the history range and the trend category.
 */

import { formatPence, monthLabel } from '../domain/money.js';
import { amountHtml, avatarHtml, esc, optionsHtml } from './dom.js';
import { hasHousehold, possessive } from './ownerChips.js';
import { createProjectionsSection } from './projectionsSection.js';
import {
  barList, budgetBullets, incomeSpendingChart, membersChart, monthShort, netWorthChart, tableDetails, trendChart,
} from './charts.js';

const RANGES = [{ value: '6m', label: '6 months' }, { value: '1y', label: '1 year' }, { value: 'all', label: 'All time' }];

export function createInsightsView({ app, ui, monthNav, storage }) {
  const projections = createProjectionsSection({ app, ui });
  let range = storage?.chartRange?.() || '1y';
  let trendCategory = '';
  let data = { history: null, monthly: null, budgets: null };
  let errorText = '';
  let generation = 0;
  let observer = null;
  let lastWidth = 0;

  async function load() {
    const mine = ++generation;
    try {
      const [history, monthly, budgets] = await Promise.all([
        app.netWorthHistory(range), app.monthlyTotals(range, trendCategory), app.budgetStatus(),
      ]);
      if (mine !== generation) return false;
      data = { history, monthly, budgets };
      errorText = '';
    } catch (err) {
      if (mine !== generation) return false;
      errorText = err.message;
    }
    return true;
  }

  function legend(items) {
    return `<div class="fin-legend">${items.map((i) => `
      <span class="fin-legend-item"><span class="fin-chart-key fin-chart-key-${i.key} ${i.cls}"></span>${esc(i.label)}</span>`).join('')}</div>`;
  }

  function card(id, title, subtitle, legendHtml = '') {
    return `
      <div class="admin-card fin-card fin-chart-card">
        <div class="fin-chart-head"><h2 class="fin-card-title">${esc(title)}</h2>${subtitle ? `<span class="fin-row-sub">${esc(subtitle)}</span>` : ''}</div>
        ${legendHtml}
        <div class="fin-chart" data-chart="${id}"></div>
        <div data-table="${id}"></div>
      </div>`;
  }

  /** Members with a line in the household history: anyone who has owned something in the range. */
  function chartMembers() {
    const seen = new Set((data.history || []).flatMap((p) => Object.keys(p.by_member || {})));
    return app.state.members.filter((m) => seen.has(m.id));
  }

  function memberLegend(members) {
    return `<div class="fin-legend">${members.map((m) => `
      <span class="fin-legend-item">${avatarHtml(m, { title: false })}${esc(m.display_name)}</span>`).join('')}</div>`;
  }

  function movedHtml(s) {
    if (!s?.moved_in_pence && !s?.moved_out_pence) return '';
    const parts = [];
    if (s.moved_in_pence) parts.push(`${formatPence(s.moved_in_pence)} moved in from other members`);
    if (s.moved_out_pence) parts.push(`${formatPence(s.moved_out_pence)} moved out to other members`);
    return `<div class="fin-moved">${esc(parts.join(' · '))}</div>`;
  }

  function shell(container) {
    const s = app.state.summary;
    const person = app.personMember();
    const members = !person && hasHousehold(app) ? chartMembers() : [];
    const categories = app.state.categories.filter((c) => c.kind === 'expense').map((c) => ({ value: c.id, label: c.name }));
    container.innerHTML = `
      <div class="fin-filters fin-insight-filters">
        ${monthNav.html()}
        <div class="task-form-toggle fin-range-toggle" role="radiogroup" aria-label="History range">
          ${RANGES.map((r) => `<button type="button" class="task-toggle-btn${r.value === range ? ' active' : ''}" data-range="${r.value}">${r.label}</button>`).join('')}
        </div>
        <select class="task-form-input" data-trend-category aria-label="Spending trend category">${optionsHtml(categories, trendCategory, { blank: 'Trend: all spending' })}</select>
      </div>
      ${errorText ? `<div class="fin-form-error" role="alert">${esc(errorText)}</div>` : ''}
      <div class="admin-card fin-card">
        <h2 class="fin-card-title">${esc(monthLabel(app.state.month))}${person ? ` · ${esc(possessive(person))} share` : ''}</h2>
        ${s ? `<div class="fin-stats">
          <div class="fin-stat"><span class="fin-stat-label">Money in</span>${amountHtml(s.total_in_pence, 'in')}</div>
          <div class="fin-stat"><span class="fin-stat-label">Money out</span>${amountHtml(s.total_out_pence, 'out')}</div>
          <div class="fin-stat"><span class="fin-stat-label">Net</span>${amountHtml(Math.abs(s.net_pence), s.net_pence >= 0 ? 'in' : 'out')}</div>
        </div>` : ''}
        ${movedHtml(s)}
        <div class="fin-hint">${person
          ? 'Joint accounts are split equally between their owners. Transfers between members show as moved, not as money in or out.'
          : 'Transfers between your accounts are not counted as money in or out.'}</div>
      </div>
      ${card('spending', 'Spending by category', monthLabel(app.state.month))}
      ${card('budgets', 'Budget vs actual', monthLabel(app.state.month), legend([
        { key: 'rect', cls: 'fin-c-accent', label: 'Spent' }, { key: 'tick', cls: 'fin-c-none', label: 'Limit' },
        { key: 'rect', cls: 'fin-c-over', label: 'Over budget' }]))}
      ${members.length > 1 ? card('members', 'Net worth by person', 'Month ends', memberLegend(members)) : ''}
      ${card('networth', person ? `${possessive(person)} net worth` : 'Net worth', 'Month ends', legend([
        { key: 'line', cls: 'fin-c-accent', label: 'Net worth' },
        { key: 'rect', cls: 'fin-c-neutral', label: 'Assets (above the line)' },
        { key: 'rect', cls: 'fin-c-neutral', label: 'Liabilities (below the line)' }]))}
      ${card('income', 'Income vs spending', 'Per month', legend([
        { key: 'rect', cls: 'fin-c-neutral', label: 'Money in (above)' },
        { key: 'rect', cls: 'fin-c-accent', label: 'Money out (below)' }]))}
      ${card('trend', trendCategory ? `Spending on ${app.categoryById(trendCategory)?.name || 'category'}` : 'Spending trend', 'Per month')}
      <div data-projections></div>`;
    monthNav.wire(container);
    container.querySelectorAll('[data-range]').forEach((b) => b.addEventListener('click', async () => {
      range = b.dataset.range;
      storage?.rememberChartRange?.(range);
      await refresh(container);
    }));
    container.querySelector('[data-trend-category]').addEventListener('change', async (e) => {
      trendCategory = e.target.value;
      await refresh(container);
    });
  }

  const slot = (container, id) => container.querySelector(`[data-chart="${id}"]`);
  const tableSlot = (container, id) => container.querySelector(`[data-table="${id}"]`);
  const empty = (node, text) => { node.innerHTML = `<div class="fin-empty">${esc(text)}</div>`; };

  function drawCharts(container) {
    const s = app.state.summary;
    const spend = s?.spend_by_category || [];
    const spendSlot = slot(container, 'spending');
    if (spend.length) {
      barList(spendSlot, spend.map((c) => ({ label: c.name, value: c.total_pence, note: `${c.count} transaction${c.count === 1 ? '' : 's'}` })));
      tableSlot(container, 'spending').replaceChildren(tableDetails(['Category', 'Spent', 'Transactions'],
        spend.map((c) => [c.name, formatPence(c.total_pence), String(c.count)])));
    } else empty(spendSlot, 'No spending this month.');

    const budgets = data.budgets?.budgets || [];
    const budgetSlot = slot(container, 'budgets');
    if (budgets.length) {
      budgetBullets(budgetSlot, budgets);
      tableSlot(container, 'budgets').replaceChildren(tableDetails(['Category', 'Spent', 'Limit', 'Left'],
        budgets.map((b) => [b.name, formatPence(b.spent_pence), formatPence(b.limit_pence), formatPence(b.remaining_pence)])));
    } else empty(budgetSlot, 'No budgets for this month. Add them in the Budgets tab.');

    const history = data.history || [];
    const memberSlot = slot(container, 'members');
    if (history.length && memberSlot) {
      const members = chartMembers();
      membersChart(memberSlot, history, members);
      tableSlot(container, 'members').replaceChildren(tableDetails(['Month', ...members.map((m) => m.display_name)],
        history.map((p) => [monthShort(p.month, true), ...members.map((m) => formatPence(p.by_member?.[m.id] ?? 0))])));
    }
    if (history.length) {
      netWorthChart(slot(container, 'networth'), history);
      tableSlot(container, 'networth').replaceChildren(tableDetails(['Month', 'Net worth', 'Assets', 'Liabilities'],
        history.map((p) => [monthShort(p.month, true), formatPence(p.net_worth_pence), formatPence(p.assets_pence), formatPence(p.liabilities_pence)])));
    }

    const monthly = data.monthly || [];
    if (monthly.length) {
      incomeSpendingChart(slot(container, 'income'), monthly);
      tableSlot(container, 'income').replaceChildren(tableDetails(['Month', 'Money in', 'Money out', 'Net'],
        monthly.map((p) => [monthShort(p.month, true), formatPence(p.in_pence), formatPence(p.out_pence), formatPence(p.net_pence)])));
      const valueOf = trendCategory ? (p) => p.category_out_pence || 0 : (p) => p.out_pence;
      const label = trendCategory ? (app.categoryById(trendCategory)?.name || 'Category') : 'Money out';
      trendChart(slot(container, 'trend'), monthly, valueOf, label);
      tableSlot(container, 'trend').replaceChildren(tableDetails(['Month', label],
        monthly.map((p) => [monthShort(p.month, true), formatPence(valueOf(p))])));
    }
  }

  /** History for the projection chart: the household's, or just today when a person is shown. */
  function projectionHistory() {
    return app.personMember() ? [] : (data.history || []);
  }

  async function refresh(container) {
    container.classList.add('fin-refreshing'); // keep the frame, dim while loading
    if (await load()) {
      shell(container);
      drawCharts(container);
      const slotEl = container.querySelector('[data-projections]');
      if (slotEl) projections.render(slotEl, projectionHistory());
    }
    container.classList.remove('fin-refreshing');
  }

  function watchResize(container) {
    observer?.disconnect();
    if (typeof ResizeObserver !== 'function') return;
    lastWidth = container.clientWidth;
    let timer = null;
    observer = new ResizeObserver(() => {
      if (!container.isConnected) { observer.disconnect(); return; }
      if (Math.abs(container.clientWidth - lastWidth) < 8) return;
      lastWidth = container.clientWidth;
      clearTimeout(timer);
      timer = setTimeout(() => {
        // Another tab may have replaced the Insights content since the resize.
        if (!container.querySelector('[data-chart="spending"]')) return;
        drawCharts(container);
        const slotEl = container.querySelector('[data-projections]');
        if (slotEl) projections.redraw(slotEl, projectionHistory());
      }, 120);
    });
    observer.observe(container);
  }

  return {
    async render(container) {
      shell(container);
      watchResize(container);
      await refresh(container);
    },
  };
}
