/**
 * Budgets: a monthly limit per spending category with spent, committed
 * (recurring items still expected) and what is left. Uses the shared month
 * navigation; a change applies from the shown month onwards.
 */

import { formatPence, monthLabel, penceToInput } from '../domain/money.js';
import { categoryDot, emptyHtml, esc, optionsHtml } from './dom.js';

export function createBudgetsView({ app, ui, monthNav }) {
  let status = null;
  let editing = null; // category id or 'new'
  let errorText = '';
  let mounted = null;
  let generation = 0;

  async function load() {
    const mine = ++generation;
    try {
      const next = await app.budgetStatus();
      if (mine !== generation) return false;
      status = next;
      errorText = '';
    } catch (err) {
      if (mine !== generation) return false;
      errorText = err.message;
    }
    return true;
  }

  function barHtml(row) {
    const limit = Math.max(row.limit_pence, 1);
    const spent = Math.min(100, Math.round((row.spent_pence / limit) * 100));
    const committed = Math.max(0, Math.min(100 - spent, Math.round((row.committed_pence / limit) * 100)));
    return `<span class="fin-budget-bar" role="img" aria-label="${row.percent_spent}% spent">
      <span class="fin-budget-fill fin-budget-${row.state}" style="width:${spent}%"></span>
      ${committed ? `<span class="fin-budget-committed fin-budget-${row.state}" style="width:${committed}%"></span>` : ''}
    </span>`;
  }

  function rowHtml(row) {
    if (editing === row.category_id) {
      return `<div class="memory-item fin-edit-card">${formHtml(row)}</div>`;
    }
    const left = row.remaining_pence >= 0 ? `${formatPence(row.remaining_pence)} left` : `${formatPence(-row.remaining_pence)} over`;
    const committed = row.committed_pence
      ? ` · ${formatPence(row.committed_pence)} expected, then ${formatPence(row.left_after_committed_pence)}` : '';
    const label = { over: 'Over budget', warning: 'Close to the limit', ok: '' }[row.state];
    return `
      <div class="memory-item fin-row fin-budget-row" data-budget="${esc(row.category_id)}" role="button" tabindex="0">
        ${categoryDot(row.color)}
        <div class="fin-row-main">
          <div class="fin-budget-head">
            <span class="memory-item-title">${esc(row.name)}</span>
            <span class="fin-amount">${esc(formatPence(row.spent_pence))} <span class="fin-row-sub">of ${esc(formatPence(row.limit_pence))}</span></span>
          </div>
          ${barHtml(row)}
          <span class="fin-row-sub">${esc(left)}${esc(committed)}${label ? ` · <span class="fin-budget-label fin-budget-${row.state}">${esc(label)}</span>` : ''}</span>
        </div>
      </div>`;
  }

  function formHtml(row) {
    const isNew = !row;
    const budgeted = new Set((status?.budgets || []).map((b) => b.category_id));
    const choices = app.state.categories.filter((c) => c.kind === 'expense' && !budgeted.has(c.id))
      .map((c) => ({ value: c.id, label: c.name }));
    return `
      <form class="task-form fin-inline-form" data-budget-form novalidate>
        <div class="fin-form-row">
          ${isNew ? `<div class="fin-form-col"><label class="task-form-label">Category</label>
            <select class="task-form-input" name="category_id">${optionsHtml(choices, '')}</select></div>` : ''}
          <div class="fin-form-col"><label class="task-form-label">Monthly limit (£)${isNew ? '' : ` for ${esc(row.name)}`}</label>
            <input class="task-form-input" name="limit" inputmode="decimal" autocomplete="off" value="${row ? esc(penceToInput(row.limit_pence)) : ''}"></div>
        </div>
        <div class="fin-hint">Applies from ${esc(monthLabel(app.state.month))} onwards. Earlier months keep their limit.</div>
        <div class="fin-form-error" role="alert" hidden></div>
        <div class="task-form-actions">
          ${isNew ? '' : '<button type="button" class="task-btn task-btn-danger" data-action="remove-budget">Remove budget</button><span style="flex:1"></span>'}
          <button type="button" class="task-btn" data-action="cancel-budget">Cancel</button>
          <button type="submit" class="task-btn task-btn-primary">${isNew ? 'Add budget' : 'Save'}</button>
        </div>
      </form>`;
  }

  function draw(container) {
    mounted = container;
    const head = monthNav.html();
    if (!status) {
      container.innerHTML = `<div class="admin-card fin-card">${head}${errorText ? `<div class="fin-form-error" role="alert">${esc(errorText)}</div>` : emptyHtml('Loading…')}</div>`;
      monthNav.wire(container);
      return;
    }
    const rows = status.budgets;
    container.innerHTML = `
      <div class="admin-card fin-card">
        ${head}
        ${errorText ? `<div class="fin-form-error" role="alert">${esc(errorText)}</div>` : ''}
        ${rows.length ? `
        <div class="fin-stats">
          <div class="fin-stat"><span class="fin-stat-label">Budgeted</span><span class="fin-amount">${esc(formatPence(status.total_limit_pence))}</span></div>
          <div class="fin-stat"><span class="fin-stat-label">Spent</span><span class="fin-amount">${esc(formatPence(status.total_spent_pence))}</span></div>
          <div class="fin-stat"><span class="fin-stat-label">${status.is_past ? 'Difference' : 'Left'}</span><span class="fin-amount">${esc(formatPence(status.total_limit_pence - status.total_spent_pence))}</span></div>
        </div>` : ''}
        <div class="fin-card-head"><h2 class="fin-card-title">Budgets</h2><span style="flex:1"></span>
          <button type="button" class="memory-toolbar-btn" data-action="new-budget">+ Add budget</button></div>
        ${editing === 'new' ? `<div class="memory-item fin-edit-card">${formHtml(null)}</div>` : ''}
        <div class="memory-list fin-list">${rows.map(rowHtml).join('') || (editing === 'new' ? '' : emptyHtml('No budgets for this month. Add a monthly limit for groceries, eating out and so on.'))}</div>
        ${status.unbudgeted_spent_pence ? `<div class="fin-hint">${esc(formatPence(status.unbudgeted_spent_pence))} spent in categories without a budget.</div>` : ''}
        <div class="fin-hint">Transfers between your accounts don't count. Each month starts fresh.</div>
      </div>`;
    monthNav.wire(container);
    wire(container);
  }

  function wire(container) {
    container.querySelector('[data-action="new-budget"]')?.addEventListener('click', () => { editing = 'new'; draw(container); });
    container.querySelectorAll('[data-budget]').forEach((row) => {
      const open = () => { editing = row.dataset.budget; draw(container); };
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    });
    const form = container.querySelector('[data-budget-form]');
    if (!form) return;
    const errorBox = form.querySelector('.fin-form-error');
    const fail = (msg) => { errorBox.textContent = msg; errorBox.hidden = !msg; };
    const month = `${app.state.month.year}-${String(app.state.month.month).padStart(2, '0')}`;
    const save = async (categoryId, limit, message) => {
      try {
        await app.setBudget(categoryId, { limit, month });
        ui.showToast(message);
        editing = null;
        await load();
        draw(container);
      } catch (err) { fail(err.message); }
    };
    form.querySelector('[data-action="cancel-budget"]').addEventListener('click', () => { editing = null; draw(container); });
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const limit = form.limit.value.trim();
      const categoryId = editing === 'new' ? form.category_id?.value : editing;
      if (!categoryId) { fail('Choose a category'); return; }
      if (!limit) { fail('Enter a monthly limit'); return; }
      save(categoryId, limit, 'Budget saved');
    });
    form.querySelector('[data-action="remove-budget"]')?.addEventListener('click', async () => {
      const ok = await ui.styledConfirm(`Remove this budget from ${monthLabel(app.state.month)} onwards? Earlier months keep it.`,
        { title: 'Remove budget', confirmText: 'Remove', danger: true });
      if (ok) save(editing, null, 'Budget removed');
    });
  }

  return {
    async render(container) {
      mounted = container;
      draw(container);
      if (await load()) draw(mounted);
    },
    reset: () => { editing = null; },
    isEditing: () => editing !== null,
  };
}
