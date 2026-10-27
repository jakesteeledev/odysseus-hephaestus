/**
 * Add / edit form for money out, money in and transfers. Built for quick
 * entry on a phone: amount first, today's date and the last-used account
 * preselected, details tucked behind "More".
 */

import { formatPence, isoDay, parsePence, penceToInput } from '../domain/money.js';
import { esc, optionsHtml } from './dom.js';

export function createEntryForm({ app, ui, now }) {
  function categoryOptions(kind, selected) {
    const all = app.state.categories;
    const preferred = all.filter((c) => c.kind === kind || c.is_system);
    const others = all.filter((c) => !(c.kind === kind || c.is_system));
    const toItem = (c) => ({ value: c.id, label: c.name });
    const blank = all.find((c) => c.is_system)?.id || '';
    const pick = selected || blank;
    let html = optionsHtml(preferred.map(toItem), pick);
    if (others.length) {
      html += `<optgroup label="${kind === 'income' ? 'Spending' : 'Income'}">${optionsHtml(others.map(toItem), pick)}</optgroup>`;
    }
    return html;
  }

  function accountOptions(selected, { exclude } = {}) {
    const items = app.state.accounts
      .filter((a) => (!a.archived || a.id === selected) && a.id !== exclude)
      .map((a) => ({ value: a.id, label: a.archived ? `${a.name} (archived)` : a.name }));
    return optionsHtml(items, selected);
  }

  /** Investments and pensions that money out can be linked to as a contribution. */
  function contributionAssets(existing) {
    return app.state.assets.filter((a) => ['investment', 'pension'].includes(a.type)
      && (!a.archived || a.id === existing?.contribution_asset_id));
  }

  /**
   * Render into `container`. `existing` is a transaction row to edit (or
   * null for a new entry). `onDone()` runs after a successful save/delete.
   */
  function render(container, { existing = null, onDone = () => {}, onCancel = null } = {}) {
    if (!app.activeAccounts().length && !existing) {
      container.innerHTML = `<div class="fin-empty">Add an account first, then you can record money in and out.</div>
        <div class="task-form-actions" style="justify-content:center"><button type="button" class="task-btn task-btn-primary" data-fin-go="accounts">Add an account</button></div>`;
      return;
    }
    const isTransfer = !!existing?.transfer_id;
    let mode = isTransfer ? 'transfer' : (existing?.direction || 'out');
    let fromId = existing ? (isTransfer && existing.direction === 'in' ? existing.transfer_account_id : existing.account_id) : app.defaultAccountId();
    let toId = isTransfer ? (existing.direction === 'in' ? existing.account_id : existing.transfer_account_id) : '';
    const today = isoDay(now());

    container.innerHTML = `
      <form class="task-form fin-entry-form" novalidate>
        ${isTransfer ? '' : `
        <div class="task-form-toggle fin-mode-toggle" role="radiogroup" aria-label="Type">
          <button type="button" class="task-toggle-btn" data-mode="out" role="radio">Money out</button>
          <button type="button" class="task-toggle-btn" data-mode="in" role="radio">Money in</button>
          ${existing ? '' : '<button type="button" class="task-toggle-btn" data-mode="transfer" role="radio">Transfer</button>'}
        </div>`}
        <label class="task-form-label" for="fin-amount">Amount (£)</label>
        <input id="fin-amount" class="task-form-input fin-amount-input" name="amount" inputmode="decimal" autocomplete="off" placeholder="0.00"
          value="${existing ? esc(penceToInput(existing.amount_pence)) : ''}">
        <div class="fin-form-row">
          <div class="fin-form-col">
            <label class="task-form-label" for="fin-account" data-label-from>Account</label>
            <select id="fin-account" class="task-form-input" name="account">${accountOptions(fromId)}</select>
          </div>
          <div class="fin-form-col" data-show="transfer">
            <label class="task-form-label" for="fin-to-account">To account</label>
            <select id="fin-to-account" class="task-form-input" name="to_account"></select>
          </div>
          <div class="fin-form-col" data-hide="transfer">
            <label class="task-form-label" for="fin-category">Category</label>
            <select id="fin-category" class="task-form-input" name="category"></select>
          </div>
        </div>
        ${contributionAssets(existing).length && !isTransfer ? `
        <div data-show-out>
          <label class="task-form-label" for="fin-contribution">Contribution to (optional)</label>
          <select id="fin-contribution" class="task-form-input" name="contribution">${optionsHtml(
            contributionAssets(existing).map((a) => ({ value: a.id, label: a.name })), existing?.contribution_asset_id || '', { blank: 'Not a contribution' })}</select>
        </div>` : ''}
        <label class="task-form-label" for="fin-description">Description</label>
        <input id="fin-description" class="task-form-input" name="description" maxlength="200" autocomplete="off"
          placeholder="e.g. Tesco" value="${esc(existing?.description || '')}">
        <details class="fin-more"${existing && (existing.notes || existing.date !== today) ? ' open' : ''}>
          <summary class="task-form-label">More: date and notes</summary>
          <label class="task-form-label" for="fin-date">Date</label>
          <input id="fin-date" class="task-form-input" type="date" name="date" value="${esc(existing?.date || today)}" required>
          <label class="task-form-label" for="fin-notes">Notes</label>
          <textarea id="fin-notes" class="task-form-input task-form-textarea" name="notes" rows="2" maxlength="2000">${esc(existing?.notes || '')}</textarea>
        </details>
        <div class="fin-form-error" role="alert" hidden></div>
        <div class="task-form-actions">
          ${existing ? '<button type="button" class="task-btn task-btn-danger" data-action="delete">Delete</button><span style="flex:1"></span>' : ''}
          ${onCancel ? '<button type="button" class="task-btn" data-action="cancel">Cancel</button>' : ''}
          <button type="submit" class="task-btn task-btn-primary">${existing ? 'Save' : 'Add'}</button>
        </div>
      </form>`;

    const form = container.querySelector('form');
    const amountInput = form.querySelector('#fin-amount');
    const accountSel = form.querySelector('#fin-account');
    const toSel = form.querySelector('#fin-to-account');
    const catSel = form.querySelector('#fin-category');
    const errorBox = form.querySelector('.fin-form-error');
    const fromLabel = form.querySelector('[data-label-from]');

    function syncMode() {
      form.querySelectorAll('[data-mode]').forEach((b) => {
        const on = b.dataset.mode === mode;
        b.classList.toggle('active', on);
        b.setAttribute('aria-checked', on ? 'true' : 'false');
      });
      form.querySelectorAll('[data-show="transfer"]').forEach((el) => { el.hidden = mode !== 'transfer'; });
      form.querySelectorAll('[data-hide="transfer"]').forEach((el) => { el.hidden = mode === 'transfer'; });
      fromLabel.textContent = mode === 'transfer' ? 'From account' : 'Account';
      form.querySelectorAll('[data-show-out]').forEach((el) => { el.hidden = mode !== 'out'; });
      if (mode === 'transfer') {
        const options = accountOptions(toId, { exclude: accountSel.value });
        toSel.innerHTML = options;
        if (!toSel.value && toSel.options.length) toSel.selectedIndex = 0;
        toId = toSel.value;
      } else {
        const current = catSel.value || existing?.category_id || '';
        catSel.innerHTML = categoryOptions(mode === 'in' ? 'income' : 'expense', current);
      }
    }

    form.querySelectorAll('[data-mode]').forEach((b) => b.addEventListener('click', () => {
      mode = b.dataset.mode;
      catSel.value = '';
      syncMode();
    }));
    accountSel.addEventListener('change', () => { fromId = accountSel.value; if (mode === 'transfer') syncMode(); });
    toSel.addEventListener('change', () => { toId = toSel.value; });
    syncMode();

    function showError(message) {
      errorBox.textContent = message;
      errorBox.hidden = !message;
    }

    let saving = false;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (saving) return;
      const pence = parsePence(amountInput.value);
      if (pence === null || pence <= 0) { showError('Enter an amount greater than £0.00'); amountInput.focus(); return; }
      const date = form.querySelector('#fin-date').value;
      if (!date) { showError('Choose a date'); return; }
      const amount = penceToInput(pence);
      const description = form.querySelector('#fin-description').value.trim();
      const notes = form.querySelector('#fin-notes').value.trim();
      showError('');
      saving = true;
      const submit = form.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        if (existing) {
          const body = { date, amount, description, notes };
          if (isTransfer) {
            body.from_account_id = accountSel.value;
            body.to_account_id = toSel.value;
          } else {
            Object.assign(body, { account_id: accountSel.value, direction: mode, category_id: catSel.value || null });
          }
          await app.updateTransaction(existing.id, body);
          const contribution = mode === 'out' ? (form.querySelector('#fin-contribution')?.value || '') : '';
          if (!isTransfer && contribution !== (existing.contribution_asset_id || '')) {
            await app.linkContribution(existing.id, contribution || null);
          }
          ui.showToast('Saved');
        } else if (mode === 'transfer') {
          if (!toSel.value || toSel.value === accountSel.value) { throw new Error('Choose two different accounts'); }
          await app.addTransfer({ from_account_id: accountSel.value, to_account_id: toSel.value, date, amount, description, notes });
          ui.showToast(`Transferred ${formatPence(pence)}`);
        } else {
          const contribution = mode === 'out' ? (form.querySelector('#fin-contribution')?.value || '') : '';
          await app.addTransaction({ account_id: accountSel.value, date, amount, direction: mode,
            category_id: catSel.value || null, description, notes }, { contributionAssetId: contribution || null });
          ui.showToast(`Added ${mode === 'in' ? 'money in' : 'money out'}: ${formatPence(pence)}`);
        }
        onDone();
      } catch (err) {
        showError(err.message || 'Could not save');
      } finally {
        saving = false;
        submit.disabled = false;
      }
    });

    form.querySelector('[data-action="cancel"]')?.addEventListener('click', () => onCancel && onCancel());
    form.querySelector('[data-action="delete"]')?.addEventListener('click', async () => {
      const what = isTransfer ? 'this transfer (both accounts)' : 'this transaction';
      const ok = await ui.styledConfirm(`Delete ${what}? This cannot be undone.`, { title: 'Delete', confirmText: 'Delete', danger: true });
      if (!ok) return;
      try {
        await app.deleteTransaction(existing.id);
        ui.showToast('Deleted');
        onDone();
      } catch (err) {
        showError(err.message || 'Could not delete');
      }
    });

    if (!existing) requestAnimationFrame(() => { try { amountInput.focus({ preventScroll: true }); } catch { /* ignore */ } });
  }

  return { render };
}
