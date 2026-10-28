/** Transactions by month with previous/next, filters, search, and tap-to-edit. */

import { dayLabel, formatPence, monthLabel } from '../domain/money.js';
import { ICONS, amountHtml, categoryDot, emptyHtml, esc, optionsHtml } from './dom.js';
import { possessive, shareNote } from './ownerChips.js';

export function createMonthNav({ app }) {
  function html() {
    return `
      <div class="fin-month-nav">
        <button type="button" class="memory-item-btn fin-nav-btn" data-month="-1" title="Previous month" aria-label="Previous month">${ICONS.prev()}</button>
        <span class="fin-month-label">${esc(monthLabel(app.state.month))}</span>
        <button type="button" class="memory-item-btn fin-nav-btn" data-month="1" title="Next month" aria-label="Next month">${ICONS.next()}</button>
      </div>`;
  }
  function wire(container) {
    container.querySelectorAll('[data-month]').forEach((b) => b.addEventListener('click', () => {
      app.shiftMonth(Number(b.dataset.month));
    }));
  }
  return { html, wire };
}

export function createTransactionsView({ app, entryForm, monthNav }) {
  let editingId = null;
  let searchTimer = null;

  function rowHtml(tx) {
    const account = app.accountById(tx.account_id);
    let label;
    let dot;
    if (tx.transfer_id) {
      const other = app.accountById(tx.transfer_account_id);
      label = `${tx.direction === 'out' ? 'Transfer to' : 'Transfer from'} ${other ? other.name : 'account'}`;
      if (tx.between_members) label += ' · between members';
      dot = `<span class="fin-dot-icon" aria-hidden="true">${ICONS.transfer()}</span>`;
    } else {
      const category = app.categoryById(tx.category_id);
      label = category ? category.name : 'Uncategorised';
      dot = categoryDot(category?.color);
    }
    const share = tx.share_pence ?? tx.amount_pence;
    const sub = [label, app.state.filters.accountId ? '' : account?.name].filter(Boolean).join(' · ')
      + shareNote(app, tx.amount_pence, share, formatPence);
    return `
      <div class="memory-item fin-row fin-tx-row" data-tx-id="${esc(tx.id)}" role="button" tabindex="0">
        ${dot}
        <div class="fin-row-main">
          <span class="memory-item-title">${esc(tx.description || (tx.transfer_id ? 'Transfer' : '(no description)'))}</span>
          <span class="fin-row-sub">${esc(sub)}${tx.import_batch_id ? ' · imported' : ''}</span>
        </div>
        ${amountHtml(share, tx.direction)}
      </div>`;
  }

  function listHtml() {
    const { transactions } = app.state;
    if (!transactions.length) {
      const f = app.state.filters;
      const filtered = f.accountId || f.categoryId || f.direction || f.q;
      const person = app.personMember();
      if (filtered) return emptyHtml('No transactions match these filters.');
      return emptyHtml(person ? `${possessive(person)} accounts have no transactions this month.` : 'No transactions this month.');
    }
    const parts = [];
    let day = null;
    for (const tx of transactions) {
      if (tx.date !== day) {
        day = tx.date;
        parts.push(`<div class="fin-day-head">${esc(dayLabel(day))}</div>`);
      }
      if (tx.id === editingId) {
        parts.push(`<div class="memory-item fin-edit-card" data-edit-slot></div>`);
      } else {
        parts.push(rowHtml(tx));
      }
    }
    return parts.join('');
  }

  function render(container) {
    const { state } = app;
    const f = state.filters;
    const accountItems = state.accounts.filter((a) => (!a.archived || a.id === f.accountId) && app.ownsIt(a.owner_ids))
      .map((a) => ({ value: a.id, label: a.name }));
    const categoryItems = state.categories.map((c) => ({ value: c.id, label: c.name }));
    container.innerHTML = `
      <div class="admin-card fin-card">
        ${monthNav.html()}
        <div class="fin-filters">
          <select class="task-form-input" data-filter="accountId" aria-label="Account">${optionsHtml(accountItems, f.accountId, { blank: 'All accounts' })}</select>
          <select class="task-form-input" data-filter="categoryId" aria-label="Category">${optionsHtml(categoryItems, f.categoryId, { blank: 'All categories' })}</select>
          <div class="task-form-toggle fin-dir-toggle" role="radiogroup" aria-label="Direction">
            <button type="button" class="task-toggle-btn${f.direction === '' ? ' active' : ''}" data-direction="">All</button>
            <button type="button" class="task-toggle-btn${f.direction === 'in' ? ' active' : ''}" data-direction="in">In</button>
            <button type="button" class="task-toggle-btn${f.direction === 'out' ? ' active' : ''}" data-direction="out">Out</button>
          </div>
          <input type="search" class="memory-search-input fin-search" placeholder="Search descriptions" value="${esc(f.q)}" aria-label="Search descriptions">
        </div>
        <div class="memory-list fin-list" data-tx-list>${listHtml()}</div>
      </div>`;
    wire(container);
  }

  function wire(container) {
    monthNav.wire(container);
    container.querySelectorAll('select[data-filter]').forEach((sel) => sel.addEventListener('change', () => {
      editingId = null;
      app.setFilters({ [sel.dataset.filter]: sel.value });
    }));
    container.querySelectorAll('[data-direction]').forEach((b) => b.addEventListener('click', () => {
      editingId = null;
      app.setFilters({ direction: b.dataset.direction });
    }));
    const search = container.querySelector('.fin-search');
    search.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        editingId = null;
        app.setFilters({ q: search.value.trim() }).then(() => {
          // Keep typing focus after the list re-renders.
          const again = container.querySelector('.fin-search');
          if (again && document.activeElement !== again) {
            again.focus();
            again.setSelectionRange(again.value.length, again.value.length);
          }
        });
      }, 250);
    });
    container.querySelectorAll('[data-tx-id]').forEach((row) => {
      const open = () => { editingId = row.dataset.txId; render(container); };
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    });
    const slot = container.querySelector('[data-edit-slot]');
    if (slot) {
      const tx = app.state.transactions.find((t) => t.id === editingId);
      const close = () => { editingId = null; render(container); };
      entryForm.render(slot, { existing: tx, onDone: close, onCancel: close });
      slot.scrollIntoView({ block: 'nearest' });
    }
  }

  return { render, reset: () => { editingId = null; }, isEditing: () => editingId !== null };
}
