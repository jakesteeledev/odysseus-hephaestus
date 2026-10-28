/**
 * Projections (Insights): net worth history continuing into a month-by-month
 * projection worked out by the server, with a horizon picker, year-by-year
 * table, events, income per tax year, editable assumptions and saved
 * scenarios compared on the same chart. Figures come from the engine; this
 * view never recalculates them.
 */

import { formatPence, longDate } from '../domain/money.js';
import { emptyHtml, esc, optionsHtml } from './dom.js';
import { monthShort, projectionChart, tableDetails } from './charts.js';

const HORIZONS = [1, 3, 5, 10, 25];
const GROWTH_LABELS = { property: 'Property', pension: 'Pensions', investment: 'Investments', vehicle: 'Vehicles', other: 'Other assets' };
const CHANGE_TYPES = [
  ['extra_income', 'Extra income a month'], ['reduce_spending', 'Spend less a month'], ['one_off', 'One-off income or expense'],
  ['overpay', 'Overpay a loan'], ['sell', 'Sell an asset'], ['balloon', 'Car finance balloon'], ['growth', 'Different growth rate'],
  ['salary', 'Salary change'],
];
// Inputs each change type needs (see the form below).
const CHANGE_FIELDS = {
  extra_income: ['amount', 'from', 'until'], reduce_spending: ['amount', 'from', 'until'], one_off: ['amount', 'on', 'direction'],
  overpay: ['liability', 'amount', 'monthly', 'on'], sell: ['asset', 'on', 'price', 'clear'], balloon: ['liability', 'choice', 'months', 'rate', 'asset'],
  growth: ['growth_target', 'rate'], salary: ['action', 'from', 'amount', 'percent', 'method'],
};
const SALARY_ACTIONS = [['raise', 'Pay rise'], ['salary', 'New salary'], ['bonus', 'Bonus'], ['pension_percent', 'Pension %'],
  ['pension_method', 'Pension method'], ['hours', 'Reduced hours (% of pay)'], ['stop', 'Stop working']];
const MAX_COMPARE = 3;

const YEARS_KEY = 'odysseus-finance-projection-years';

function rememberedYears() {
  try {
    const value = Number(globalThis.localStorage?.getItem(YEARS_KEY));
    return HORIZONS.includes(value) ? value : 5;
  } catch { return 5; }
}

export function createProjectionsSection({ app, ui }) {
  let years = rememberedYears();
  let compare = [];
  let data = null;
  let errorText = '';
  let editingScenario = null; // id, 'new' or null
  let draftChanges = [];
  let showAssumptions = false;
  let generation = 0;
  let mounted = null;

  async function load() {
    const mine = ++generation;
    try {
      const next = await app.projection(years, compare);
      if (mine !== generation) return false;
      data = next;
      compare = compare.filter((id) => data.scenario_list.some((s) => s.id === id));
      errorText = '';
    } catch (err) {
      if (mine !== generation) return false;
      errorText = err.message;
    }
    return true;
  }

  const names = () => data?.names || {};
  const assets = () => app.state.assets.filter((a) => !a.archived);
  const liabilities = () => app.state.liabilities.filter((l) => !l.archived);

  function fixTab(w) {
    if (w.item_kind === 'job') return 'salary';
    if (w.item_kind === 'recurring') return 'upcoming';
    if (w.item_kind === 'asset' || w.item_kind === 'liability') return 'overview';
    return null;
  }

  function warningsHtml(list) {
    if (!list?.length) return '';
    const one = (w) => {
      const tab = fixTab(w);
      return `<div class="fin-warning">${esc(w.text)}${tab ? ` <button type="button" class="fin-link-btn fin-inline-link" data-fin-go="${tab}">Fix</button>` : ''}</div>`;
    };
    const shown = list.slice(0, 2).map(one).join('');
    const rest = list.slice(2);
    return `<div class="fin-warnings">${shown}${rest.length ? `
      <details class="fin-more-warnings"><summary>${rest.length} more warning${rest.length === 1 ? '' : 's'}</summary>
        <div class="fin-warnings">${rest.map(one).join('')}</div></details>` : ''}</div>`;
  }

  function eventsHtml(events) {
    if (!events.length) return emptyHtml('No loans ending, balloons or cash shortfalls in this period.');
    return `<ol class="fin-timeline">${events.map((e) => `
      <li class="fin-timeline-item${e.kind === 'cash_runs_out' ? ' fin-timeline-alert' : ''}">
        <span class="fin-timeline-date">${esc(monthShort(e.month, true))}</span>
        <span class="fin-timeline-text">${esc(e.text)}${e.followup ? `<span class="fin-row-sub">${esc(e.followup)}</span>` : ''}</span>
      </li>`).join('')}</ol>`;
  }

  function summaryHtml(base) {
    const end = base.end;
    const sf = base.shortfall;
    const stats = [
      ['Net worth today', base.start.net_worth_pence], [`In ${years} year${years === 1 ? '' : 's'}`, end.net_worth_pence],
      ['Cash then', end.cash_pence],
    ];
    if (sf.peak_pence) stats.push(['Peak shortfall', sf.peak_pence]);
    return `<div class="fin-stats">${stats.map(([label, v]) => `
      <div class="fin-stat"><span class="fin-stat-label">${esc(label)}</span><span class="fin-amount${v < 0 ? ' fin-amount-negative' : ''}">${esc(formatPence(v))}</span></div>`).join('')}</div>
      ${sf.runs_out ? `<div class="fin-warning">Cash runs out in ${esc(monthShort(sf.runs_out_month, true))}. The gap is shown as "${esc(sf.name)}"${sf.rate_bp ? ` at ${sf.rate_bp / 100}% interest` : ''}, peaking at ${esc(formatPence(sf.peak_pence))}.</div>` : ''}`;
  }

  function scenarioListHtml() {
    const list = data.scenario_list || [];
    const rows = list.map((s) => {
      if (editingScenario === s.id) return `<div class="memory-item fin-edit-card">${scenarioForm(s)}</div>`;
      const on = compare.includes(s.id);
      return `
        <div class="memory-item fin-row fin-static-row">
          <label class="fin-check-label"><input type="checkbox" class="fin-check" data-compare="${esc(s.id)}"${on ? ' checked' : ''}${!on && compare.length >= MAX_COMPARE ? ' disabled' : ''}> Compare</label>
          <div class="fin-row-main"><span class="memory-item-title">${esc(s.name)}</span>
            <span class="fin-row-sub">${esc(s.descriptions.join(' · '))}</span></div>
          <button type="button" class="fin-link-btn" data-edit-scenario="${esc(s.id)}">Edit</button>
        </div>`;
    }).join('');
    return `
      <div class="fin-card-head"><div class="fin-section-title">Scenarios</div><span style="flex:1"></span>
        <button type="button" class="memory-toolbar-btn" data-action="new-scenario">+ New scenario</button></div>
      ${editingScenario === 'new' ? `<div class="memory-item fin-edit-card">${scenarioForm(null)}</div>` : ''}
      <div class="memory-list fin-list">${rows || (editingScenario === 'new' ? '' : emptyHtml('Try a what-if: sell the car, overpay the mortgage, a pay rise, or what to do at the PCP balloon.'))}</div>
      ${list.length ? `<div class="fin-hint">Compare up to ${MAX_COMPARE} scenarios against the base case on the chart.</div>` : ''}`;
  }

  function changeRowHtml(c, i) {
    const desc = c._description || c.type;
    return `<div class="fin-scenario-change"><span>${esc(desc)}</span><button type="button" class="fin-link-btn" data-remove-change="${i}">Remove</button></div>`;
  }

  function scenarioForm(s) {
    const opts = (items) => items.map((x) => ({ value: x.id, label: x.name }));
    const field = (name, html) => `<div class="fin-form-col" data-cfield="${name}">${html}</div>`;
    return `
      <form class="task-form fin-inline-form" data-scenario-form novalidate>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label" for="fin-scen-name">Scenario name</label>
            <input id="fin-scen-name" class="task-form-input" name="scenario_name" maxlength="80" autocomplete="off" value="${esc(s?.name || '')}"></div>
        </div>
        <div class="fin-scenario-changes">${draftChanges.map(changeRowHtml).join('') || '<div class="fin-hint">Add one or more changes.</div>'}</div>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label" for="fin-change-type">Change</label>
            <select id="fin-change-type" class="task-form-input" name="change_type">${optionsHtml(CHANGE_TYPES.map(([value, label]) => ({ value, label })), 'extra_income')}</select></div>
          ${field('action', `<label class="task-form-label" for="fin-c-action">Salary change</label>
            <select id="fin-c-action" class="task-form-input" name="salary_action">${optionsHtml(SALARY_ACTIONS.map(([value, label]) => ({ value, label })), 'raise')}</select>`)}
          ${field('liability', `<label class="task-form-label" for="fin-c-liab">Loan</label>
            <select id="fin-c-liab" class="task-form-input" name="liability_id">${optionsHtml(opts(liabilities()), '')}</select>`)}
          ${field('asset', `<label class="task-form-label" for="fin-c-asset">Asset</label>
            <select id="fin-c-asset" class="task-form-input" name="asset_id">${optionsHtml(opts(assets()), '', { blank: 'Choose' })}</select>`)}
          ${field('growth_target', `<label class="task-form-label" for="fin-c-target">For</label>
            <select id="fin-c-target" class="task-form-input" name="growth_target">${optionsHtml([
              ...Object.entries(GROWTH_LABELS).map(([value, label]) => ({ value: `type:${value}`, label: `All ${label.toLowerCase()}` })),
              ...assets().map((a) => ({ value: `asset:${a.id}`, label: a.name }))], 'type:property')}</select>`)}
          ${field('choice', `<label class="task-form-label" for="fin-c-choice">At the balloon</label>
            <select id="fin-c-choice" class="task-form-input" name="choice">${optionsHtml([
              { value: 'pay', label: 'Pay it' }, { value: 'hand_back', label: 'Hand the car back' }, { value: 'refinance', label: 'Refinance it' }], 'pay')}</select>`)}
        </div>
        <div class="fin-form-row">
          ${field('amount', '<label class="task-form-label" for="fin-c-amount">Amount (£)</label><input id="fin-c-amount" class="task-form-input" name="amount" inputmode="decimal" autocomplete="off">')}
          ${field('percent', '<label class="task-form-label" for="fin-c-percent">Percentage</label><input id="fin-c-percent" class="task-form-input" name="percent" inputmode="decimal" autocomplete="off">')}
          ${field('rate', '<label class="task-form-label" for="fin-c-rate">Rate (% a year)</label><input id="fin-c-rate" class="task-form-input" name="rate" inputmode="decimal" autocomplete="off">')}
          ${field('price', '<label class="task-form-label" for="fin-c-price">Sale price (£, blank = value)</label><input id="fin-c-price" class="task-form-input" name="price" inputmode="decimal" autocomplete="off">')}
          ${field('months', '<label class="task-form-label" for="fin-c-months">Over months</label><input id="fin-c-months" class="task-form-input" name="months" inputmode="numeric" autocomplete="off" value="36">')}
          ${field('method', `<label class="task-form-label" for="fin-c-method">Pension method</label>
            <select id="fin-c-method" class="task-form-input" name="pension_method">${optionsHtml([
              { value: 'salary_sacrifice', label: 'Salary sacrifice' }, { value: 'net_pay', label: 'Net pay arrangement' },
              { value: 'relief_at_source', label: 'Relief at source' }], 'salary_sacrifice')}</select>`)}
        </div>
        <div class="fin-form-row">
          ${field('from', '<label class="task-form-label" for="fin-c-from">From</label><input id="fin-c-from" class="task-form-input" type="date" name="from">')}
          ${field('until', '<label class="task-form-label" for="fin-c-until">Until <span class="fin-row-sub">(optional)</span></label><input id="fin-c-until" class="task-form-input" type="date" name="until">')}
          ${field('on', '<label class="task-form-label" for="fin-c-on">On</label><input id="fin-c-on" class="task-form-input" type="date" name="on">')}
          ${field('direction', `<label class="task-form-label" for="fin-c-dir">Type</label><select id="fin-c-dir" class="task-form-input" name="direction">${optionsHtml([{ value: 'out', label: 'Expense' }, { value: 'in', label: 'Income' }], 'out')}</select>`)}
          ${field('clear', `<label class="task-form-label" for="fin-c-clear">Proceeds clear</label><select id="fin-c-clear" class="task-form-input" name="clear_liability_id">${optionsHtml(opts(liabilities()), '', { blank: 'Nothing (to cash)' })}</select>`)}
        </div>
        <label class="fin-check-label" data-cfield="monthly"><input type="checkbox" class="fin-check" name="monthly"> Every month (otherwise once, on the date)</label>
        <div class="task-form-actions"><button type="button" class="task-btn" data-action="add-change">Add this change</button></div>
        <div class="fin-form-error" role="alert" hidden></div>
        <div class="task-form-actions">
          ${s ? '<button type="button" class="task-btn task-btn-danger" data-action="delete-scenario">Delete</button>' : ''}
          <span style="flex:1"></span>
          <button type="button" class="task-btn" data-action="cancel-scenario">Cancel</button>
          <button type="submit" class="task-btn task-btn-primary">${s ? 'Save' : 'Save scenario'}</button>
        </div>
      </form>`;
  }

  function assumptionsHtml() {
    const a = data.assumption_settings;
    const rows = data.base.assumptions.map((r) => `<li><strong>${esc(r.label)}:</strong> ${esc(r.value)}</li>`).join('');
    if (!showAssumptions) {
      return `<div class="fin-card-head"><div class="fin-section-title">Assumptions</div><span style="flex:1"></span>
        <button type="button" class="memory-toolbar-btn" data-action="edit-assumptions">Edit</button></div>
        <ul class="fin-assumptions">${rows}</ul>`;
    }
    const growth = Object.entries(GROWTH_LABELS).map(([key, label]) => `
      <div class="fin-form-col"><label class="task-form-label" for="fin-g-${key}">${esc(label)} (% a year)</label>
        <input id="fin-g-${key}" class="task-form-input" data-assume="growth.${key}" inputmode="decimal" value="${esc(a.growth_percent[key])}"></div>`).join('');
    const perAsset = assets().map((x) => `
      <div class="fin-form-col"><label class="task-form-label" for="fin-ga-${esc(x.id)}">${esc(x.name)}</label>
        <input id="fin-ga-${esc(x.id)}" class="task-form-input" data-assume="asset.${esc(x.id)}" inputmode="decimal"
          placeholder="${esc(a.growth_percent[x.type] ?? 0)} (type default)" value="${esc(a.asset_growth_percent[x.id] ?? '')}"></div>`).join('');
    return `
      <div class="fin-card-head"><div class="fin-section-title">Assumptions</div><span style="flex:1"></span>
        <button type="button" class="memory-toolbar-btn" data-action="done-assumptions">Done</button></div>
      <div class="fin-hint">Changes are saved and the projection re-runs straight away.</div>
      <div class="fin-form-row fin-form-grid">${growth}</div>
      ${perAsset ? `<details class="fin-chart-table"><summary>Growth for individual assets</summary><div class="fin-form-row fin-form-grid">${perAsset}</div></details>` : ''}
      <div class="fin-form-row fin-form-grid">
        <div class="fin-form-col"><label class="task-form-label" for="fin-a-spend">Day-to-day spending (£ a month)</label>
          <input id="fin-a-spend" class="task-form-input" data-assume="spending_override" inputmode="decimal"
            placeholder="${esc((data.base.assumptions.find((r) => r.key === 'spending') || {}).value || '')}" value="${a.spending_override_pence !== null ? esc((a.spending_override_pence / 100).toFixed(2)) : ''}"></div>
        <div class="fin-form-col"><label class="task-form-label" for="fin-a-months">Average over (months)</label>
          <input id="fin-a-months" class="task-form-input" data-assume="spending_months" inputmode="numeric" value="${esc(a.spending_months)}"></div>
        <div class="fin-form-col"><label class="task-form-label" for="fin-a-infl">Inflation on spending (%)</label>
          <input id="fin-a-infl" class="task-form-input" data-assume="inflation" inputmode="decimal" value="${esc(a.inflation_percent)}"></div>
        <div class="fin-form-col"><label class="task-form-label" for="fin-a-sf">Shortfall interest (%)</label>
          <input id="fin-a-sf" class="task-form-input" data-assume="shortfall_rate" inputmode="decimal" value="${esc(a.shortfall_rate_percent)}"></div>
        <div class="fin-form-col"><label class="task-form-label" for="fin-a-cash">Cash interest (%)</label>
          <input id="fin-a-cash" class="task-form-input" data-assume="cash_interest" inputmode="decimal" value="${esc(a.cash_interest_percent)}"></div>
      </div>
      <div class="fin-form-error" role="alert" data-assume-error hidden></div>`;
  }

  function incomeHtml(rows) {
    if (!rows?.length) return '';
    return `<div data-income-table></div>`;
  }

  function html() {
    if (!data) {
      return `<div class="admin-card fin-card"><h2 class="fin-card-title">Projections</h2>
        ${errorText ? `<div class="fin-form-error" role="alert">${esc(errorText)}</div>` : emptyHtml('Working out your projection…')}</div>`;
    }
    const base = data.base;
    const scenarioLegend = data.scenarios.map((s, n) => `
      <span class="fin-legend-item"><span class="fin-chart-key fin-chart-key-line fin-member-line fin-color-${['blue', 'green', 'purple'][n % 3]}"></span>${esc(s.name)}</span>`).join('');
    return `
      <div class="admin-card fin-card fin-chart-card fin-projections">
        <div class="fin-card-head"><h2 class="fin-card-title">Projections</h2><span class="fin-row-sub">Whole household</span><span style="flex:1"></span>
          <div class="task-form-toggle fin-range-toggle" role="radiogroup" aria-label="Projection horizon">
            ${HORIZONS.map((y) => `<button type="button" class="task-toggle-btn${y === years ? ' active' : ''}" data-years="${y}">${y} yr${y === 1 ? '' : 's'}</button>`).join('')}
          </div></div>
        ${errorText ? `<div class="fin-form-error" role="alert">${esc(errorText)}</div>` : ''}
        ${warningsHtml(base.warnings)}
        ${summaryHtml(base)}
        <div class="fin-legend">
          <span class="fin-legend-item"><span class="fin-chart-key fin-chart-key-line fin-c-accent"></span>Net worth (dashed = projected)</span>
          <span class="fin-legend-item"><span class="fin-chart-key fin-chart-key-line fin-c-neutral"></span>Assets (above) and liabilities (below)</span>
          ${scenarioLegend}
        </div>
        <div class="fin-chart" data-chart="projection"></div>
        <div data-table="projection"></div>
        <div class="fin-section-title">What happens when</div>
        ${eventsHtml(base.events)}
        ${data.scenarios.length ? `<div class="fin-section-title">Compared in ${years} year${years === 1 ? '' : 's'}</div>
          <div class="fin-stats">${data.scenarios.map((s) => `<div class="fin-stat"><span class="fin-stat-label">${esc(s.name)}</span>
            <span class="fin-amount">${esc(formatPence(s.end.net_worth_pence))}</span>
            <span class="fin-row-sub">${esc((s.end.net_worth_pence >= base.end.net_worth_pence ? '+' : '−') + formatPence(Math.abs(s.end.net_worth_pence - base.end.net_worth_pence)))} vs base</span></div>`).join('')}</div>` : ''}
        ${incomeHtml(base.income_by_tax_year)}
        ${assumptionsHtml()}
        ${scenarioListHtml()}
        <div class="fin-hint">Projections are estimates from your recorded data and the assumptions above, not advice.</div>
      </div>`;
  }

  function drawTables(container, history) {
    const base = data.base;
    const chart = container.querySelector('[data-chart="projection"]');
    // Without a history (a person is selected), today's household figures start the line.
    const past = history.length ? history : [{ ...base.start, as_of: base.start.date }];
    if (chart) projectionChart(chart, past, base.points, data.scenarios);
    const assetNames = base.years[0]?.assets.map((a) => a.name) || [];
    const liabNames = base.years[0]?.liabilities.map((l) => l.name) || [];
    const equityNames = base.years[0]?.property_equity.map((e) => `${e.name} equity`) || [];
    const headers = ['Year', 'Net worth', 'Cash', 'Shortfall', ...assetNames, ...liabNames, ...equityNames];
    const rows = base.years.map((y) => [`${y.label} (${y.end_label})`, formatPence(y.net_worth_pence), formatPence(y.cash_pence),
      formatPence(y.shortfall_pence), ...y.assets.map((a) => formatPence(a.value_pence)),
      ...y.liabilities.map((l) => formatPence(l.balance_pence)), ...y.property_equity.map((e) => formatPence(e.equity_pence))]);
    const table = tableDetails(headers, rows);
    table.open = true;
    table.querySelector('summary').textContent = 'Year by year';
    container.querySelector('[data-table="projection"]')?.replaceChildren(table);
    const income = container.querySelector('[data-income-table]');
    if (income) {
      const t = tableDetails(['Tax year', 'Gross', 'Tax', 'NI', 'Student loan', 'Pension (you)', 'Pension (employer)', 'Net pay'],
        base.income_by_tax_year.map((r) => [`${r.tax_year}${r.assumed ? ' (rates assumed)' : ''} · ${r.periods} pay${r.periods === 1 ? '' : 's'}`,
          formatPence(r.gross_pence), formatPence(r.tax_pence), formatPence(r.ni_pence), formatPence(r.student_loan_pence),
          formatPence(r.pension_employee_pence), formatPence(r.pension_employer_pence), formatPence(r.net_pence)]));
      t.querySelector('summary').textContent = 'Income per tax year';
      income.replaceChildren(t);
    }
  }

  function readChange(f) {
    const type = f.change_type.value;
    const wanted = CHANGE_FIELDS[type];
    const c = { type };
    const v = (name) => f[name]?.value?.trim?.() ?? '';
    if (wanted.includes('amount') && !(type === 'salary' && !['salary', 'bonus', 'raise'].includes(v('salary_action')))) c.amount = v('amount');
    if (wanted.includes('from') && v('from')) c.from = v('from');
    if (wanted.includes('until') && v('until')) c.until = v('until');
    if (wanted.includes('on') && v('on')) c.on = v('on');
    if (wanted.includes('direction')) c.direction = v('direction');
    if (wanted.includes('liability')) c.liability_id = v('liability_id');
    if (wanted.includes('asset') && v('asset_id')) c.asset_id = v('asset_id');
    if (wanted.includes('monthly')) c.monthly = f.monthly.checked;
    if (wanted.includes('price') && v('price')) c.price = v('price');
    if (wanted.includes('clear') && v('clear_liability_id')) c.clear_liability_id = v('clear_liability_id');
    if (wanted.includes('choice')) c.choice = v('choice');
    if (wanted.includes('months') && c.choice === 'refinance') c.months = v('months');
    if (wanted.includes('rate') && (type === 'growth' || c.choice === 'refinance')) c.rate = v('rate') || '0';
    if (type === 'growth') {
      const [kind, id] = v('growth_target').split(':');
      if (kind === 'asset') c.asset_id = id; else c.asset_type = id;
    }
    if (type === 'salary') {
      c.action = v('salary_action');
      if (['raise', 'pension_percent', 'hours'].includes(c.action) && v('percent')) c.percent = v('percent');
      if (c.action === 'raise' && c.percent) delete c.amount;
      if (c.action === 'pension_method') c.label = v('pension_method');
    }
    if (type === 'overpay' && c.monthly) { c.from = c.on; delete c.on; }
    return c;
  }

  function describe(c) {
    const n = names();
    const money = c.amount ? `£${c.amount}` : '';
    const label = Object.fromEntries(CHANGE_TYPES)[c.type];
    const target = c.liability_id ? n[c.liability_id] : c.asset_id ? n[c.asset_id] : '';
    return [label, target, money, c.action, c.percent && `${c.percent}%`, c.choice, c.on || c.from].filter(Boolean).join(' · ');
  }

  function showFields(form) {
    const type = form.change_type.value;
    const wanted = new Set(CHANGE_FIELDS[type]);
    if (type === 'balloon' && form.choice.value !== 'refinance') { wanted.delete('months'); wanted.delete('rate'); }
    if (type === 'balloon' && form.choice.value !== 'hand_back') wanted.delete('asset');
    if (type === 'salary') {
      const action = form.salary_action.value;
      if (!['salary', 'bonus', 'raise'].includes(action)) wanted.delete('amount');
      if (!['raise', 'pension_percent', 'hours'].includes(action)) wanted.delete('percent');
      if (action !== 'pension_method') wanted.delete('method');
    }
    form.querySelectorAll('[data-cfield]').forEach((el) => { el.hidden = !wanted.has(el.dataset.cfield); });
  }

  async function rerun(container, history) {
    container.classList.add('fin-refreshing');
    if (await load()) draw(container, history);
    container.classList.remove('fin-refreshing');
  }

  function wire(container, history) {
    container.querySelectorAll('[data-years]').forEach((b) => b.addEventListener('click', async () => {
      years = Number(b.dataset.years);
      try { globalThis.localStorage?.setItem(YEARS_KEY, String(years)); } catch { /* storage unavailable */ }
      await rerun(container, history);
    }));
    container.querySelectorAll('[data-compare]').forEach((b) => b.addEventListener('change', async () => {
      const id = b.dataset.compare;
      compare = b.checked ? [...compare, id].slice(0, MAX_COMPARE) : compare.filter((x) => x !== id);
      await rerun(container, history);
    }));
    container.querySelector('[data-action="edit-assumptions"]')?.addEventListener('click', () => { showAssumptions = true; draw(container, history); });
    container.querySelector('[data-action="done-assumptions"]')?.addEventListener('click', () => { showAssumptions = false; draw(container, history); });
    container.querySelectorAll('[data-assume]').forEach((input) => input.addEventListener('change', async () => {
      const [group, key] = input.dataset.assume.split('.');
      const value = input.value.trim();
      const body = group === 'growth' ? { growth: { [key]: value } } : group === 'asset' ? { asset_growth: { [key]: value || null } }
        : { [group]: value };
      try {
        await app.updateAssumptions(body);
        await rerun(container, history);
      } catch (err) {
        const box = container.querySelector('[data-assume-error]');
        if (box) { box.textContent = err.message; box.hidden = false; }
      }
    }));
    container.querySelector('[data-action="new-scenario"]')?.addEventListener('click', () => {
      editingScenario = 'new';
      draftChanges = [];
      draw(container, history);
    });
    container.querySelectorAll('[data-edit-scenario]').forEach((b) => b.addEventListener('click', () => {
      const s = data.scenario_list.find((x) => x.id === b.dataset.editScenario);
      editingScenario = s.id;
      draftChanges = s.changes.map((c, i) => ({ ...c, _description: s.descriptions[i] }));
      draw(container, history);
    }));
    const form = container.querySelector('[data-scenario-form]');
    if (!form) return;
    const errorBox = form.querySelector('.fin-form-error');
    const fail = (msg) => { errorBox.textContent = msg; errorBox.hidden = !msg; };
    showFields(form);
    for (const name of ['change_type', 'choice', 'salary_action']) form[name].addEventListener('change', () => showFields(form));
    form.querySelectorAll('[data-remove-change]').forEach((b) => b.addEventListener('click', () => {
      const name = form.scenario_name.value;
      draftChanges.splice(Number(b.dataset.removeChange), 1);
      draw(container, history);
      container.querySelector('[data-scenario-form]').scenario_name.value = name;
    }));
    form.querySelector('[data-action="add-change"]').addEventListener('click', () => {
      const c = readChange(form);
      c._description = describe(c);
      const name = form.scenario_name.value;
      draftChanges.push(c);
      draw(container, history);
      container.querySelector('[data-scenario-form]').scenario_name.value = name;
    });
    form.querySelector('[data-action="cancel-scenario"]').addEventListener('click', () => { editingScenario = null; draftChanges = []; draw(container, history); });
    form.querySelector('[data-action="delete-scenario"]')?.addEventListener('click', async () => {
      const ok = await ui.styledConfirm('Delete this scenario?', { title: 'Delete scenario', confirmText: 'Delete', danger: true });
      if (!ok) return;
      try {
        await app.deleteScenario(editingScenario);
        compare = compare.filter((x) => x !== editingScenario);
        editingScenario = null;
        ui.showToast('Scenario deleted');
        await rerun(container, history);
      } catch (err) { fail(err.message); }
    });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const name = form.scenario_name.value.trim();
      if (!name) { fail('Name the scenario'); return; }
      if (!draftChanges.length) { fail('Add at least one change'); return; }
      const body = { name, changes: draftChanges.map(({ _description, ...c }) => c) };
      try {
        const saved = editingScenario === 'new' ? await app.createScenario(body) : await app.updateScenario(editingScenario, body);
        if (!compare.includes(saved.id) && compare.length < MAX_COMPARE) compare.push(saved.id);
        editingScenario = null;
        draftChanges = [];
        ui.showToast('Scenario saved');
        await rerun(container, history);
      } catch (err) { fail(err.message); }
    });
  }

  function draw(container, history) {
    mounted = container;
    container.innerHTML = html();
    if (data) drawTables(container, history);
    wire(container, history);
  }

  return {
    /** Draw into *container*; *history* is the net worth history (month-end points). */
    async render(container, history, { reload = true } = {}) {
      draw(container, history);
      if (reload || !data) {
        if (await load() && container.isConnected) draw(container, history);
      }
    },
    redraw(container, history) { if (data) drawTables(container, history); },
    isEditing: () => editingScenario !== null || showAssumptions,
    get mounted() { return mounted; },
  };
}
