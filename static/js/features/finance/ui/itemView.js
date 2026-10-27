/**
 * Editor for one asset or liability: details, value history (dated
 * snapshots), contributions for investments/pensions, equity for property.
 */

import { formatPence, isoDay } from '../domain/money.js';
import { CONTRIBUTION_TYPES, typesFor } from '../domain/items.js';
import { balanceHtml, emptyHtml, esc, optionsHtml } from './dom.js';

export function createItemView({ app, ui, now, loanSection }) {
  /**
   * Render the editor into `container`. `item` is null for a new one.
   * `onClose()` runs after save/cancel/delete.
   */
  async function render(container, { kind, item = null, onClose, onChange = onClose }) {
    const isNew = !item;
    const types = typesFor(kind);
    const typeValue = item?.type || types[0].value;
    const partyLabel = kind === 'asset' ? 'Provider (optional)' : 'Lender (optional)';
    const partyKey = kind === 'asset' ? 'provider' : 'lender';
    const properties = app.state.assets.filter((a) => a.type === 'property' && (!a.archived || a.id === item?.linked_asset_id));
    container.innerHTML = `
      <form class="task-form fin-inline-form" data-item-form novalidate>
        <label class="task-form-label" for="fin-item-name">Name</label>
        <input id="fin-item-name" class="task-form-input" name="name" maxlength="80" autocomplete="off"
          value="${esc(item?.name || '')}" placeholder="${kind === 'asset' ? 'e.g. Vanguard ISA' : 'e.g. Home mortgage'}">
        <div class="fin-form-row">
          <div class="fin-form-col">
            <label class="task-form-label" for="fin-item-type">Type</label>
            <select id="fin-item-type" class="task-form-input" name="type">${optionsHtml(types.map((t) => ({ value: t.value, label: t.label })), typeValue)}</select>
          </div>
          <div class="fin-form-col">
            <label class="task-form-label" for="fin-item-subtype">Kind (optional)</label>
            <input id="fin-item-subtype" class="task-form-input" name="subtype" maxlength="40" list="fin-item-subtypes" autocomplete="off" value="${esc(item?.subtype || '')}">
            <datalist id="fin-item-subtypes"></datalist>
          </div>
        </div>
        <label class="task-form-label" for="fin-item-party">${partyLabel}</label>
        <input id="fin-item-party" class="task-form-input" name="party" maxlength="80" autocomplete="off" value="${esc(item?.[partyKey] || '')}">
        ${kind === 'liability' ? `
          <div data-mortgage-only>
            <label class="task-form-label" for="fin-item-link">Secured on property (for equity)</label>
            <select id="fin-item-link" class="task-form-input" name="linked_asset_id">${optionsHtml(properties.map((p) => ({ value: p.id, label: p.name })), item?.linked_asset_id || '', { blank: 'None' })}</select>
          </div>` : ''}
        ${isNew ? `
          <div class="fin-form-row">
            <div class="fin-form-col">
              <label class="task-form-label" for="fin-item-value">${kind === 'asset' ? 'Current value (£)' : 'Balance owed (£)'}</label>
              <input id="fin-item-value" class="task-form-input" name="value" inputmode="decimal" autocomplete="off" placeholder="0.00">
            </div>
            <div class="fin-form-col">
              <label class="task-form-label" for="fin-item-value-date">As of</label>
              <input id="fin-item-value-date" class="task-form-input" type="date" name="value_date" value="${isoDay(now())}">
            </div>
          </div>` : ''}
        <label class="task-form-label" for="fin-item-notes">Notes</label>
        <textarea id="fin-item-notes" class="task-form-input task-form-textarea" name="notes" rows="2" maxlength="2000">${esc(item?.notes || '')}</textarea>
        <div class="fin-form-error" role="alert" hidden></div>
        <div class="task-form-actions">
          ${isNew ? '' : `
            <button type="button" class="task-btn task-btn-danger" data-action="delete">Delete</button>
            <button type="button" class="task-btn" data-action="archive">${item.archived ? 'Unarchive' : 'Archive'}</button>
            <span style="flex:1"></span>`}
          <button type="button" class="task-btn" data-action="cancel">${isNew ? 'Cancel' : 'Close'}</button>
          <button type="submit" class="task-btn task-btn-primary">${isNew ? `Add ${kind}` : 'Save'}</button>
        </div>
      </form>
      ${isNew ? '' : '<div data-item-history></div>'}`;

    const form = container.querySelector('[data-item-form]');
    const errorBox = form.querySelector('.fin-form-error');
    const fail = (msg) => { errorBox.textContent = msg; errorBox.hidden = !msg; };
    const typeSel = form.querySelector('#fin-item-type');
    const syncType = () => {
      const type = types.find((t) => t.value === typeSel.value) || types[0];
      form.querySelector('#fin-item-subtypes').innerHTML = type.subtypes.map((s) => `<option value="${esc(s)}"></option>`).join('');
      const mortgageOnly = form.querySelector('[data-mortgage-only]');
      if (mortgageOnly) mortgageOnly.hidden = typeSel.value !== 'mortgage';
    };
    typeSel.addEventListener('change', syncType);
    syncType();
    requestAnimationFrame(() => { if (isNew) form.querySelector('#fin-item-name')?.focus(); });

    form.querySelector('[data-action="cancel"]').addEventListener('click', () => onClose());
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = {
        name: form.name.value.trim(),
        type: typeSel.value,
        subtype: form.subtype.value.trim(),
        notes: form.notes.value.trim(),
        [partyKey]: form.party.value.trim(),
      };
      if (kind === 'liability') body.linked_asset_id = typeSel.value === 'mortgage' ? (form.linked_asset_id.value || null) : null;
      if (!body.name) { fail('Give it a name'); return; }
      try {
        if (isNew) {
          if (form.value.value.trim()) { body.value = form.value.value.trim(); body.value_date = form.value_date.value; }
          await app.createItem(kind, body);
          ui.showToast(kind === 'asset' ? 'Asset added' : 'Liability added');
        } else {
          await app.updateItem(kind, item.id, body);
          ui.showToast('Saved');
        }
        onClose();
      } catch (err) { fail(err.message); }
    });
    form.querySelector('[data-action="archive"]')?.addEventListener('click', async () => {
      try {
        await app.updateItem(kind, item.id, { archived: !item.archived });
        ui.showToast(item.archived ? 'Restored' : 'Archived');
        onClose();
      } catch (err) { fail(err.message); }
    });
    form.querySelector('[data-action="delete"]')?.addEventListener('click', async () => {
      const ok = await ui.styledConfirm(`Delete "${item.name}"? Only items with no value history or links can be deleted; archive it to keep its history.`,
        { title: `Delete ${kind}`, confirmText: 'Delete', danger: true });
      if (!ok) return;
      try {
        await app.deleteItem(kind, item.id);
        ui.showToast('Deleted');
        onClose();
      } catch (err) { fail(err.status === 409 ? 'This has value history or links. Archive it instead to keep its history.' : err.message); }
    });

    if (!isNew) await renderHistory(container.querySelector('[data-item-history]'), kind, item, onChange);
  }

  async function renderHistory(el, kind, item, onChange) {
    el.innerHTML = emptyHtml('Loading…');
    let valuations = [];
    let contributions = [];
    const tracksContributions = kind === 'asset' && CONTRIBUTION_TYPES.has(item.type);
    try {
      [valuations, contributions] = await Promise.all([
        app.listValuations(kind, item.id),
        tracksContributions ? app.listContributions(item.id) : Promise.resolve([]),
      ]);
    } catch (err) {
      el.innerHTML = `<div class="fin-form-error" role="alert">${esc(err.message)}</div>`;
      return;
    }
    const valueWord = kind === 'asset' ? 'Value' : 'Balance owed';
    const summary = [];
    if (tracksContributions) {
      summary.push(`<div class="fin-stat"><span class="fin-stat-label">Value</span>${balanceHtml(item.value_pence)}</div>`,
        `<div class="fin-stat"><span class="fin-stat-label">Contributed</span>${balanceHtml(item.contributions_pence || 0)}</div>`,
        `<div class="fin-stat"><span class="fin-stat-label">Gain / loss</span>${balanceHtml(item.gain_pence || 0)}</div>`);
    }
    if (item.type === 'property' && item.mortgages?.length) {
      summary.push(`<div class="fin-stat"><span class="fin-stat-label">Value</span>${balanceHtml(item.value_pence)}</div>`,
        `<div class="fin-stat"><span class="fin-stat-label">Mortgage</span>${balanceHtml(item.mortgages.reduce((t, m) => t + m.balance_pence, 0))}</div>`,
        `<div class="fin-stat"><span class="fin-stat-label">Equity</span>${balanceHtml(item.equity_pence)}</div>`);
    }
    el.innerHTML = `
      ${summary.length ? `<div class="fin-stats">${summary.join('')}</div>` : ''}
      ${kind === 'liability' ? '<div data-loan-slot></div>' : ''}
      <h3 class="fin-section-title">${kind === 'liability' ? 'Balance snapshots (statements)' : `${valueWord} history`}</h3>
      <form class="fin-form-row fin-snapshot-add" data-valuation-form>
        <div class="fin-form-col"><input class="task-form-input" name="value" inputmode="decimal" autocomplete="off" placeholder="New ${valueWord.toLowerCase()} (£)" aria-label="New ${valueWord.toLowerCase()}"></div>
        <div class="fin-form-col"><input class="task-form-input" type="date" name="date" value="${isoDay(now())}" aria-label="Date"></div>
        <button type="submit" class="task-btn">Record</button>
      </form>
      <div class="memory-list fin-list">${valuations.map((v) => `
        <div class="memory-item fin-row fin-static-row">
          <div class="fin-row-main"><span class="memory-item-title">${esc(formatPence(v.value_pence))}</span>
            <span class="fin-row-sub">${esc(v.date)}${v.note ? ` · ${esc(v.note)}` : ''}</span></div>
          <button type="button" class="memory-item-btn" data-delete-valuation="${esc(v.id)}" title="Delete this entry" aria-label="Delete this entry">&times;</button>
        </div>`).join('') || emptyHtml(`No ${valueWord.toLowerCase()} recorded yet.`)}</div>
      ${tracksContributions ? `
        <h3 class="fin-section-title">Contributions</h3>
        <form class="fin-form-row fin-snapshot-add" data-contribution-form>
          <div class="fin-form-col"><input class="task-form-input" name="amount" inputmode="decimal" autocomplete="off" placeholder="Amount (£)" aria-label="Contribution amount"></div>
          <div class="fin-form-col"><input class="task-form-input" type="date" name="date" value="${isoDay(now())}" aria-label="Date"></div>
          <div class="fin-form-col"><input class="task-form-input" name="note" maxlength="200" placeholder="Note, e.g. employer" aria-label="Note"></div>
          <button type="submit" class="task-btn">Add</button>
        </form>
        <div class="fin-hint">Payments from your accounts can be linked as contributions from the transaction itself.</div>
        <div class="memory-list fin-list">${contributions.map((c) => `
          <div class="memory-item fin-row fin-static-row">
            <div class="fin-row-main"><span class="memory-item-title">${esc(formatPence(c.amount_pence))}</span>
              <span class="fin-row-sub">${esc(c.date)}${c.note ? ` · ${esc(c.note)}` : ''}${c.transaction_id ? ' · from a transaction' : ''}</span></div>
            ${c.transaction_id ? '' : `<button type="button" class="memory-item-btn" data-delete-contribution="${esc(c.id)}" title="Delete contribution" aria-label="Delete contribution">&times;</button>`}
          </div>`).join('') || emptyHtml('No contributions recorded yet.')}</div>` : ''}`;

    const loanSlot = el.querySelector('[data-loan-slot]');
    if (loanSlot && loanSection) loanSection.render(loanSlot, item, onChange);
    const valuationForm = el.querySelector('[data-valuation-form]');
    valuationForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const value = valuationForm.value.value.trim();
      if (!value) return;
      try {
        await app.addValuation(kind, item.id, { value, date: valuationForm.date.value });
        ui.showToast(`${valueWord} recorded`);
        onChange();
      } catch (err) { ui.showError(err.message); }
    });
    el.querySelectorAll('[data-delete-valuation]').forEach((b) => b.addEventListener('click', async () => {
      const ok = await ui.styledConfirm('Delete this entry from the history?', { title: 'Delete entry', confirmText: 'Delete', danger: true });
      if (!ok) return;
      try { await app.deleteValuation(b.dataset.deleteValuation); ui.showToast('Entry deleted'); onChange(); } catch (err) { ui.showError(err.message); }
    }));
    const contributionForm = el.querySelector('[data-contribution-form]');
    contributionForm?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const amount = contributionForm.amount.value.trim();
      if (!amount) return;
      try {
        await app.addContribution(item.id, { amount, date: contributionForm.date.value, note: contributionForm.note.value.trim() });
        ui.showToast('Contribution added');
        onChange();
      } catch (err) { ui.showError(err.message); }
    });
    el.querySelectorAll('[data-delete-contribution]').forEach((b) => b.addEventListener('click', async () => {
      const ok = await ui.styledConfirm('Delete this contribution?', { title: 'Delete contribution', confirmText: 'Delete', danger: true });
      if (!ok) return;
      try { await app.deleteContribution(b.dataset.deleteContribution); ui.showToast('Contribution deleted'); onChange(); } catch (err) { ui.showError(err.message); }
    }));
  }

  return { render };
}
