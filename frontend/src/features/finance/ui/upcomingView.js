/**
 * Upcoming: recurring items that are overdue or due soon (confirm, match,
 * skip, undo), projected month-end balances, the recurring items themselves
 * and suggestions from repeated transactions. Nothing posts automatically.
 */

import { dayLabel, formatPence, isoDay, penceToInput } from '../domain/money.js';
import { amountHtml, balanceHtml, emptyHtml, esc, optionsHtml } from './dom.js';
import { shareNote } from './ownerChips.js';

const FREQUENCIES = [
  { value: 'weekly', label: 'Weekly' },
  { value: 'four_weekly', label: 'Every 4 weeks' },
  { value: 'monthly', label: 'Monthly' },
  { value: 'quarterly', label: 'Quarterly' },
  { value: 'yearly', label: 'Yearly' },
];

export function createUpcomingView({ app, ui, now }) {
  let data = null;        // /upcoming response
  let items = [];         // recurring items (incl. archived)
  let suggestions = [];
  let editing = null;     // recurring id, 'new', or a suggestion index like 's3'
  let errorText = '';
  let mounted = null;

  async function load() {
    try {
      [data, items, suggestions] = await Promise.all([app.upcoming(30), app.listRecurring(), app.recurringSuggestions()]);
      errorText = '';
    } catch (err) {
      errorText = err.message;
    }
  }

  const accountName = (id) => app.accountById(id)?.name || 'account';

  function occurrenceHtml(row) {
    const resolved = row.status === 'confirmed' || row.status === 'skipped';
    const badge = row.status === 'overdue' ? '<span class="fin-badge fin-badge-error">Overdue</span>'
      : resolved ? `<span class="fin-badge">${row.status === 'confirmed' ? 'Confirmed' : 'Skipped'}</span>` : '';
    const matches = (row.matches || []).map((m) => `
      <button type="button" class="memory-toolbar-btn" data-occ-action="match" data-id="${esc(row.recurring_id)}" data-due="${esc(row.due_date)}" data-tx="${esc(m.id)}"
        title="Use the transaction already recorded">Match ${esc(m.description || 'transaction')} ${esc(formatPence(m.amount_pence))} on ${esc(dayLabel(m.date))}</button>`).join('');
    return `
      <div class="memory-item fin-row fin-static-row fin-occurrence${resolved ? ' fin-row-skipped' : ''}">
        <div class="fin-row-main">
          <span class="memory-item-title">${esc(row.name)}</span>
          <span class="fin-row-sub">${esc(dayLabel(row.due_date))} · ${esc(accountName(row.account_id))} · ${esc(row.frequency_label.toLowerCase())}${esc(shareNote(app, row.amount_pence, row.share_pence ?? row.amount_pence, formatPence))}</span>
          <div class="fin-occurrence-actions">
            ${resolved
              ? `<button type="button" class="memory-toolbar-btn" data-occ-action="undo" data-id="${esc(row.recurring_id)}" data-due="${esc(row.due_date)}">Undo</button>`
              : `${matches}
                 <button type="button" class="memory-toolbar-btn" data-occ-action="confirm" data-id="${esc(row.recurring_id)}" data-due="${esc(row.due_date)}"
                   title="Record it as a new transaction">${row.matches?.length ? 'Record new' : 'Confirm'}</button>
                 <button type="button" class="memory-toolbar-btn" data-occ-action="skip" data-id="${esc(row.recurring_id)}" data-due="${esc(row.due_date)}">Skip</button>`}
          </div>
        </div>
        ${amountHtml(row.share_pence ?? row.amount_pence, row.direction)}
        ${badge}
      </div>`;
  }

  function formHtml(item) {
    const accounts = app.state.accounts.filter((a) => !a.archived || a.id === item?.account_id).map((a) => ({ value: a.id, label: a.name }));
    const cats = app.state.categories.map((c) => ({ value: c.id, label: c.name }));
    const debts = app.state.liabilities.filter((d) => !d.archived || d.id === item?.liability_id).map((d) => ({ value: d.id, label: d.name }));
    const invest = app.state.assets.filter((a) => ['investment', 'pension'].includes(a.type) && (!a.archived || a.id === item?.asset_id))
      .map((a) => ({ value: a.id, label: a.name }));
    const isNew = !item?.id;
    return `
      <form class="task-form fin-inline-form" data-recurring-form novalidate>
        <div class="task-form-toggle" role="radiogroup" aria-label="Type">
          <button type="button" class="task-toggle-btn${(item?.direction || 'out') === 'out' ? ' active' : ''}" data-dir="out">Payment</button>
          <button type="button" class="task-toggle-btn${item?.direction === 'in' ? ' active' : ''}" data-dir="in">Income</button>
        </div>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label">Name</label>
            <input class="task-form-input" name="name" maxlength="80" autocomplete="off" value="${esc(item?.name || '')}" placeholder="e.g. Netflix"></div>
          <div class="fin-form-col"><label class="task-form-label">Amount (£)</label>
            <input class="task-form-input" name="amount" inputmode="decimal" autocomplete="off" value="${item?.amount_pence ? esc(penceToInput(item.amount_pence)) : ''}"></div>
        </div>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label">Account</label>
            <select class="task-form-input" name="account_id">${optionsHtml(accounts, item?.account_id || app.defaultAccountId())}</select></div>
          <div class="fin-form-col"><label class="task-form-label">How often</label>
            <select class="task-form-input" name="frequency">${optionsHtml(FREQUENCIES, item?.frequency || 'monthly')}</select></div>
        </div>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label">${isNew ? 'Next due' : 'Next due (changes the schedule)'}</label>
            <input class="task-form-input" type="date" name="next_due" value="${esc(isNew ? (item?.next_due || isoDay(now())) : '')}"></div>
          <div class="fin-form-col"><label class="task-form-label">Ends (optional)</label>
            <input class="task-form-input" type="date" name="end_date" value="${esc(item?.end_date || '')}"></div>
        </div>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label">Category</label>
            <select class="task-form-input" name="category_id">${optionsHtml(cats, item?.category_id || '', { blank: 'Uncategorised' })}</select></div>
          <div class="fin-form-col"><label class="task-form-label">Bank description contains</label>
            <input class="task-form-input" name="match_text" maxlength="200" autocomplete="off" value="${esc(item?.match_text || '')}" placeholder="used to match imported rows"></div>
        </div>
        <div class="fin-form-row" data-out-only>
          ${debts.length ? `<div class="fin-form-col"><label class="task-form-label">Pays towards (optional)</label>
            <select class="task-form-input" name="liability_id">${optionsHtml(debts, item?.liability_id || '', { blank: 'None' })}</select></div>` : ''}
          ${invest.length ? `<div class="fin-form-col"><label class="task-form-label">Contribution to (optional)</label>
            <select class="task-form-input" name="asset_id">${optionsHtml(invest, item?.asset_id || '', { blank: 'None' })}</select></div>` : ''}
        </div>
        <div class="fin-form-error" role="alert" hidden></div>
        <div class="task-form-actions">
          ${isNew ? '' : `<button type="button" class="task-btn task-btn-danger" data-action="delete-recurring">Delete</button>
            <button type="button" class="task-btn" data-action="archive-recurring">${item.archived ? 'Restore' : 'Stop (archive)'}</button><span style="flex:1"></span>`}
          <button type="button" class="task-btn" data-action="cancel-recurring">Cancel</button>
          <button type="submit" class="task-btn task-btn-primary">${isNew ? 'Add' : 'Save'}</button>
        </div>
      </form>`;
  }

  function itemsHtml() {
    // With a person chosen, only items paid from or into their accounts.
    const shown = items.filter((r) => app.ownsIt(app.accountById(r.account_id)?.owner_ids));
    const rows = shown.map((r) => (editing === r.id
      ? `<div class="memory-item fin-edit-card">${formHtml(r)}</div>`
      : `<div class="memory-item fin-row${r.archived ? ' fin-archived' : ''}" data-recurring-id="${esc(r.id)}" role="button" tabindex="0">
          <div class="fin-row-main"><span class="memory-item-title">${esc(r.name)}</span>
            <span class="fin-row-sub">${esc(r.frequency_label)} · ${esc(accountName(r.account_id))}${r.next_due ? ` · next ${esc(dayLabel(r.next_due))}` : ''}${r.archived ? ' · stopped' : ''}</span></div>
          ${amountHtml(r.amount_pence, r.direction)}
        </div>`)).join('');
    return `
      <div class="admin-card fin-card">
        <div class="fin-card-head"><h2 class="fin-card-title">Recurring items</h2><span style="flex:1"></span>
          <button type="button" class="memory-toolbar-btn" data-action="new-recurring">+ New</button></div>
        ${editing === 'new' ? `<div class="memory-item fin-edit-card">${formHtml(null)}</div>` : ''}
        <div class="memory-list fin-list">${rows || (editing === 'new' ? '' : emptyHtml('Add rent, bills, subscriptions and salary to see what is coming up.'))}</div>
      </div>`;
  }

  function suggestionsHtml() {
    if (!suggestions.length) return '';
    return `
      <div class="admin-card fin-card">
        <h2 class="fin-card-title">Looks recurring</h2>
        <p class="memory-desc">Repeated transactions from your history. Add one to track it as expected.</p>
        <div class="memory-list fin-list">${suggestions.map((sug, i) => (editing === `s${i}`
          ? `<div class="memory-item fin-edit-card">${formHtml({ ...sug, match_text: sug.name })}</div>`
          : `<div class="memory-item fin-row fin-static-row">
              <div class="fin-row-main"><span class="memory-item-title">${esc(sug.name)}</span>
                <span class="fin-row-sub">${esc(sug.frequency_label)} · ${esc(accountName(sug.account_id))} · seen ${sug.count} times · next ${esc(dayLabel(sug.next_due))}</span></div>
              ${amountHtml(sug.amount_pence, sug.direction)}
              <button type="button" class="memory-toolbar-btn" data-suggestion="${i}">Add</button>
            </div>`)).join('')}</div>
      </div>`;
  }

  function render(container) {
    mounted = container;
    if (!data) {
      container.innerHTML = errorText ? `<div class="fin-form-error" role="alert">${esc(errorText)}</div>` : emptyHtml('Loading…');
      return;
    }
    const due = data.items.filter((r) => r.status === 'overdue' || r.due_date <= data.today);
    const later = data.items.filter((r) => !due.includes(r));
    container.innerHTML = `
      ${errorText ? `<div class="fin-form-error" role="alert">${esc(errorText)}</div>` : ''}
      <div class="admin-card fin-card">
        <h2 class="fin-card-title">Due now</h2>
        <div class="memory-list fin-list">${due.map(occurrenceHtml).join('') || emptyHtml('Nothing waiting for you.')}</div>
        <h3 class="fin-section-title">Next 30 days</h3>
        <div class="memory-list fin-list">${later.map(occurrenceHtml).join('') || emptyHtml('Nothing expected in the next 30 days.')}</div>
        <div class="fin-hint">Nothing is recorded until you confirm or match it.</div>
      </div>
      ${data.projected_balances.length ? `
      <div class="admin-card fin-card">
        <h2 class="fin-card-title">Projected at ${esc(dayLabel(data.month_end))}</h2>
        <div class="memory-list fin-list">${data.projected_balances.map((p) => `
          <div class="memory-item fin-row fin-static-row">
            <div class="fin-row-main"><span class="memory-item-title">${esc(p.name)}</span>
              <span class="fin-row-sub">now ${esc(formatPence(p.balance_pence))} · expected ${p.expected_pence >= 0 ? '+' : ''}${esc(formatPence(p.expected_pence))}${esc(shareNote(app, p.projected_pence, p.projected_share_pence ?? p.projected_pence, formatPence))}</span></div>
            ${balanceHtml(p.projected_share_pence ?? p.projected_pence)}
          </div>`).join('')}</div>
      </div>` : ''}
      ${itemsHtml()}
      ${suggestionsHtml()}`;
    wire(container);
  }

  async function reload() {
    await load();
    if (mounted) render(mounted);
  }

  function wire(container) {
    container.querySelectorAll('[data-occ-action]').forEach((b) => b.addEventListener('click', async () => {
      const { occAction: action, id, due, tx } = b.dataset;
      b.disabled = true;
      try {
        const body = { due_date: due };
        if (action === 'match') body.transaction_id = tx;
        await app.occurrence(id, action, body);
        ui.showToast({ confirm: 'Recorded', match: 'Matched', skip: 'Skipped', undo: 'Undone' }[action]);
        await reload();
      } catch (err) { ui.showError(err.message); b.disabled = false; }
    }));
    container.querySelector('[data-action="new-recurring"]')?.addEventListener('click', () => { editing = 'new'; render(container); });
    container.querySelectorAll('[data-recurring-id]').forEach((row) => {
      const open = () => { editing = row.dataset.recurringId; render(container); };
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    });
    container.querySelectorAll('[data-suggestion]').forEach((b) => b.addEventListener('click', () => {
      editing = `s${b.dataset.suggestion}`;
      render(container);
    }));
    const form = container.querySelector('[data-recurring-form]');
    if (form) wireForm(container, form);
  }

  function wireForm(container, form) {
    const existing = items.find((r) => r.id === editing) || null;
    let direction = existing?.direction || (editing?.startsWith?.('s') ? suggestions[Number(editing.slice(1))].direction : 'out');
    const errorBox = form.querySelector('.fin-form-error');
    const fail = (msg) => { errorBox.textContent = msg; errorBox.hidden = !msg; };
    const close = () => { editing = null; render(container); };
    const syncDir = () => {
      form.querySelectorAll('[data-dir]').forEach((b) => b.classList.toggle('active', b.dataset.dir === direction));
      const outOnly = form.querySelector('[data-out-only]');
      if (outOnly) outOnly.hidden = direction !== 'out';
    };
    form.querySelectorAll('[data-dir]').forEach((b) => b.addEventListener('click', () => { direction = b.dataset.dir; syncDir(); }));
    syncDir();
    form.querySelector('[data-action="cancel-recurring"]').addEventListener('click', close);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = {
        name: form.name.value.trim(), amount: form.amount.value.trim(), account_id: form.account_id.value,
        frequency: form.frequency.value, direction, end_date: form.end_date.value || null,
        category_id: form.category_id.value || null, match_text: form.match_text.value.trim(),
        liability_id: direction === 'out' ? (form.liability_id?.value || null) : null,
        asset_id: direction === 'out' ? (form.asset_id?.value || null) : null,
      };
      if (form.next_due.value) body.next_due = form.next_due.value;
      if (!body.name || !body.amount) { fail('Give it a name and an amount'); return; }
      try {
        if (existing) await app.updateRecurring(existing.id, body);
        else await app.createRecurring(body);
        ui.showToast(existing ? 'Saved' : 'Recurring item added');
        editing = null;
        await reload();
      } catch (err) { fail(err.message); }
    });
    form.querySelector('[data-action="archive-recurring"]')?.addEventListener('click', async () => {
      try { await app.updateRecurring(existing.id, { archived: !existing.archived }); editing = null; await reload(); } catch (err) { fail(err.message); }
    });
    form.querySelector('[data-action="delete-recurring"]')?.addEventListener('click', async () => {
      const ok = await ui.styledConfirm(`Delete "${existing.name}"? Transactions it recorded or matched are kept.`,
        { title: 'Delete recurring item', confirmText: 'Delete', danger: true });
      if (!ok) return;
      try { await app.deleteRecurring(existing.id); ui.showToast('Deleted'); editing = null; await reload(); } catch (err) { fail(err.message); }
    });
  }

  return {
    async render(container) {
      mounted = container;
      render(container);
      await load();
      render(container);
    },
    reset: () => { editing = null; },
    isEditing: () => editing !== null,
  };
}
