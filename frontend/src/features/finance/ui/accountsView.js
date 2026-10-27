/** Accounts overview: active accounts with balances, the total, and account editing. */

import { ACCOUNT_TYPE_LABELS, penceToInput } from '../domain/money.js';
import { balanceHtml, emptyHtml, esc, optionsHtml } from './dom.js';

export function createAccountsView({ app, ui }) {
  let showArchived = false;
  let editing = null; // account id, 'new', or null

  const typeOptions = (selected) => optionsHtml(
    Object.entries(ACCOUNT_TYPE_LABELS).map(([value, label]) => ({ value, label })), selected);

  function formHtml(account) {
    const isNew = !account;
    return `
      <form class="task-form fin-inline-form" data-account-form novalidate>
        <label class="task-form-label" for="fin-acc-name">Name</label>
        <input id="fin-acc-name" class="task-form-input" name="name" maxlength="80" value="${esc(account?.name || '')}" placeholder="e.g. Monzo current" autocomplete="off">
        <div class="fin-form-row">
          <div class="fin-form-col">
            <label class="task-form-label" for="fin-acc-type">Type</label>
            <select id="fin-acc-type" class="task-form-input" name="account_type">${typeOptions(account?.account_type || 'current')}</select>
          </div>
          <div class="fin-form-col">
            <label class="task-form-label" for="fin-acc-opening">Opening balance (£)</label>
            <input id="fin-acc-opening" class="task-form-input" name="opening_balance" inputmode="decimal" autocomplete="off"
              value="${esc(account ? penceToInput(account.opening_balance_pence) : '0.00')}">
          </div>
        </div>
        <div class="fin-hint">For a credit card, enter what you owe as a negative number, e.g. -250.00.</div>
        <div class="fin-form-error" role="alert" hidden></div>
        <div class="task-form-actions">
          ${isNew ? '' : `
            <button type="button" class="task-btn task-btn-danger" data-action="delete">Delete</button>
            <button type="button" class="task-btn" data-action="archive">${account.archived ? 'Unarchive' : 'Archive'}</button>
            <span style="flex:1"></span>`}
          <button type="button" class="task-btn" data-action="cancel">Cancel</button>
          <button type="submit" class="task-btn task-btn-primary">${isNew ? 'Add account' : 'Save'}</button>
        </div>
      </form>`;
  }

  function itemHtml(account) {
    return `
      <div class="memory-item fin-row fin-account-row${account.archived ? ' fin-archived' : ''}" data-account-id="${esc(account.id)}" role="button" tabindex="0">
        <div class="fin-row-main">
          <span class="memory-item-title">${esc(account.name)}</span>
          <span class="fin-row-sub">${esc(ACCOUNT_TYPE_LABELS[account.account_type] || account.account_type)}${account.archived ? ' · archived' : ''}</span>
        </div>
        ${balanceHtml(account.balance_pence)}
      </div>`;
  }

  function render(container) {
    const { state } = app;
    const accounts = state.accounts.filter((a) => showArchived || !a.archived);
    const archivedCount = state.accounts.filter((a) => a.archived).length;
    const rows = [];
    for (const account of accounts) {
      rows.push(editing === account.id ? `<div class="memory-item fin-edit-card">${formHtml(account)}</div>` : itemHtml(account));
    }
    container.innerHTML = `
      <div class="admin-card fin-card">
        <div class="fin-card-head">
          <h2 class="fin-card-title">Accounts</h2>
          <span style="flex:1"></span>
          <button type="button" class="memory-toolbar-btn" data-action="new">+ New account</button>
        </div>
        ${editing === 'new' ? `<div class="memory-item fin-edit-card">${formHtml(null)}</div>` : ''}
        <div class="memory-list fin-list">
          ${rows.join('') || (editing === 'new' ? '' : emptyHtml('No accounts yet. Add your current account, savings, credit cards or cash to get started.'))}
        </div>
        ${state.accounts.some((a) => !a.archived) ? `
        <div class="fin-total-row">
          <span>Total across active accounts</span>
          ${balanceHtml(state.totalPence)}
        </div>` : ''}
        ${archivedCount ? `<button type="button" class="memory-toolbar-btn fin-link-btn" data-action="toggle-archived">${showArchived ? 'Hide' : 'Show'} archived (${archivedCount})</button>` : ''}
      </div>`;
    wire(container);
  }

  function wire(container) {
    container.querySelector('[data-action="new"]')?.addEventListener('click', () => { editing = 'new'; render(container); focusName(container); });
    container.querySelector('[data-action="toggle-archived"]')?.addEventListener('click', () => { showArchived = !showArchived; render(container); });
    container.querySelectorAll('[data-account-id]').forEach((row) => {
      const open = () => { editing = row.dataset.accountId; render(container); focusName(container); };
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    });
    const form = container.querySelector('[data-account-form]');
    if (!form) return;
    const account = editing === 'new' ? null : app.accountById(editing);
    const errorBox = form.querySelector('.fin-form-error');
    const fail = (msg) => { errorBox.textContent = msg; errorBox.hidden = !msg; };
    const close = () => { editing = null; render(container); };
    form.querySelector('[data-action="cancel"]').addEventListener('click', close);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = {
        name: form.name.value.trim(),
        account_type: form.account_type.value,
        opening_balance: form.opening_balance.value.trim() || '0',
      };
      if (!body.name) { fail('Give the account a name'); return; }
      try {
        if (account) await app.updateAccount(account.id, body);
        else await app.createAccount(body);
        ui.showToast(account ? 'Account saved' : 'Account added');
        editing = null;
        render(container);
      } catch (err) { fail(err.message); }
    });
    form.querySelector('[data-action="archive"]')?.addEventListener('click', async () => {
      try {
        await app.updateAccount(account.id, { archived: !account.archived });
        ui.showToast(account.archived ? 'Account restored' : 'Account archived');
        editing = null;
        render(container);
      } catch (err) { fail(err.message); }
    });
    form.querySelector('[data-action="delete"]')?.addEventListener('click', async () => {
      const ok = await ui.styledConfirm(`Delete "${account.name}"? Only accounts with no transactions can be deleted; archive it to keep its history.`,
        { title: 'Delete account', confirmText: 'Delete', danger: true });
      if (!ok) return;
      try {
        await app.deleteAccount(account.id);
        ui.showToast('Account deleted');
        editing = null;
        render(container);
      } catch (err) { fail(err.status === 409 ? 'This account has transactions. Archive it instead to keep its history.' : err.message); }
    });
  }

  function focusName(container) {
    requestAnimationFrame(() => container.querySelector('#fin-acc-name')?.focus());
  }

  return { render, reset: () => { editing = null; } };
}
