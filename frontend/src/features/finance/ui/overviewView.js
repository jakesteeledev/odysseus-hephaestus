/**
 * Overview: the net worth headline (assets - liabilities), accounts, and
 * assets and liabilities grouped by type, each opening its editor.
 */

import { ACCOUNT_TYPE_LABELS, formatPence } from '../domain/money.js';
import { CONTRIBUTION_TYPES, typeLabel } from '../domain/items.js';
import { avatarHtml, balanceHtml, emptyHtml, esc } from './dom.js';
import { hasHousehold, ownersHtml, possessive, shareNote } from './ownerChips.js';
import { childrenSectionHtml, juniorIsaById, juniorIsaNote } from './childrenSection.js';

export function createOverviewView({ app, accountsView, itemView }) {
  let editing = null; // { kind, id } or { kind, id: 'new' }

  function itemSub(item, kind) {
    const parts = [];
    if (item.kind === 'account') {
      parts.push(ACCOUNT_TYPE_LABELS[item.type] || item.type);
    } else {
      parts.push(item.subtype || typeLabel(kind, item.type));
      if (item.value_date) parts.push(`as of ${item.value_date}`);
      else parts.push('no value yet');
      if (CONTRIBUTION_TYPES.has(item.type) && item.contributions_pence) {
        parts.push(`gain ${item.gain_pence >= 0 ? '+' : ''}${formatPence(item.gain_pence)}`);
      }
      if (item.type === 'property' && item.mortgages?.length) {
        parts.push(`equity ${formatPence(item.equity_pence)}`);
      }
      if (item.owner_warning) parts.push('owners differ from the property');
      const isa = juniorIsaById(app).get(item.id);
      if (isa) parts.push(juniorIsaNote(isa));
    }
    if (item.archived) parts.push('archived');
    return parts.join(' · ') + shareNote(app, item.full_value_pence ?? item.value_pence, item.value_pence, formatPence);
  }

  function groupHtml(group, kind) {
    return `
      <div class="fin-group">
        <div class="fin-group-head"><span>${esc(group.label)}</span>${balanceHtml(group.total_pence)}</div>
        ${group.items.map((item) => {
          if (item.kind !== 'account' && editing && editing.kind === kind && editing.id === item.id) {
            return '<div class="memory-item fin-edit-card" data-item-edit-slot></div>';
          }
          const attrs = item.kind === 'account'
            ? `data-go-accounts="${esc(item.id)}"`
            : `data-item-kind="${kind}" data-item-id="${esc(item.id)}"`;
          return `
            <div class="memory-item fin-row${item.archived ? ' fin-archived' : ''}" ${attrs} role="button" tabindex="0">
              <div class="fin-row-main">
                <span class="memory-item-title">${esc(item.name)}</span>
                <span class="fin-row-sub">${esc(itemSub(item, kind))}</span>
              </div>
              ${ownersHtml(app, item.owner_ids)}
              ${balanceHtml(item.value_pence)}
            </div>`;
        }).join('')}
      </div>`;
  }

  function sideHtml(kind) {
    const worth = app.state.networth;
    const groups = kind === 'asset' ? worth.assets : worth.liabilities;
    const total = kind === 'asset' ? worth.assets_total_pence : worth.liabilities_total_pence;
    // Items with no value yet are not in the net worth groups; list them so they can be filled in.
    const shown = new Set(groups.flatMap((g) => g.items.filter((i) => i.kind !== 'account').map((i) => i.id)));
    const pending = (kind === 'asset' ? app.state.assets : app.state.liabilities)
      .filter((i) => !i.archived && !shown.has(i.id) && app.ownsIt(i.owner_ids));
    const pendingGroup = pending.length ? groupHtml({ label: 'Not counted yet (no value)', total_pence: 0, items: pending }, kind) : '';
    return `
      <div class="admin-card fin-card">
        <div class="fin-card-head">
          <h2 class="fin-card-title">${kind === 'asset' ? 'Assets' : 'Liabilities'}</h2>
          ${balanceHtml(total)}
          <span style="flex:1"></span>
          <button type="button" class="memory-toolbar-btn" data-new-item="${kind}">+ New ${kind}</button>
        </div>
        ${editing && editing.kind === kind && editing.id === 'new' ? '<div class="memory-item fin-edit-card" data-item-edit-slot></div>' : ''}
        ${groups.map((g) => groupHtml(g, kind)).join('')}${pendingGroup}
        ${groups.length || pending.length ? '' : emptyHtml(kind === 'asset'
          ? 'Add property, investments, pensions or vehicles to include them in your net worth.'
          : 'Add mortgages, loans, student loans or BNPL so your net worth is complete.')}
      </div>`;
  }

  /** Each member's net worth (their share of everything they own), with the household total. */
  function breakdownHtml(worth) {
    const person = app.personMember();
    if (person) return `<div class="fin-hint">${esc(possessive(person))} share: joint items are split equally between their owners. Choose Household to see everything.</div>`;
    if (!hasHousehold(app) || !worth.by_member?.length) return '';
    return `
      <div class="fin-members-breakdown" aria-label="Net worth by person">
        <span class="fin-stat-label">By person</span>
        ${worth.by_member.map((row) => `
          <div class="fin-member-worth">
            ${avatarHtml(row.member, { title: false })}
            <div class="fin-row-main"><span class="memory-item-title">${esc(row.member.display_name)}</span>
              <span class="fin-row-sub">${esc(`assets ${formatPence(row.assets_pence)} · owes ${formatPence(row.liabilities_pence)}`)}</span></div>
            ${balanceHtml(row.net_worth_pence)}
          </div>`).join('')}
        <div class="fin-total-row"><span>Household</span>${balanceHtml(worth.net_worth_pence)}</div>
      </div>
      <div class="fin-hint">Joint items count equally towards each owner.</div>`;
  }

  function headline() {
    const person = app.personMember();
    if (person) return `${possessive(person)} net worth`;
    return hasHousehold(app) ? 'Household net worth' : 'Net worth';
  }

  function render(container) {
    const worth = app.state.networth;
    if (!worth) { container.innerHTML = emptyHtml('Loading…'); return; }
    container.innerHTML = `
      <div class="admin-card fin-card fin-headline-card">
        <span class="fin-stat-label">${esc(headline())}</span>
        <span class="fin-headline">${balanceHtml(worth.net_worth_pence)}</span>
        <div class="fin-stats">
          <div class="fin-stat"><span class="fin-stat-label">Assets</span>${balanceHtml(worth.assets_total_pence)}</div>
          <div class="fin-stat"><span class="fin-stat-label">Liabilities</span>${balanceHtml(worth.liabilities_total_pence)}</div>
        </div>
        <div class="fin-hint">Account balances count automatically; credit cards count as money owed.</div>
        ${breakdownHtml(worth)}
      </div>
      ${childrenSectionHtml(app)}
      ${sideHtml('asset')}
      ${sideHtml('liability')}
      <div data-accounts-slot></div>`;
    accountsView.render(container.querySelector('[data-accounts-slot]'), { afterSave: () => render(container) });
    wire(container);
  }

  function wire(container) {
    const rerender = () => render(container);
    container.querySelectorAll('[data-new-item]').forEach((b) => b.addEventListener('click', () => {
      editing = { kind: b.dataset.newItem, id: 'new' };
      rerender();
    }));
    container.querySelectorAll('[data-item-id]').forEach((row) => {
      const open = () => { editing = { kind: row.dataset.itemKind, id: row.dataset.itemId }; rerender(); };
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    });
    container.querySelectorAll('[data-go-accounts]').forEach((row) => {
      const go = () => container.querySelector('[data-accounts-slot]')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      row.addEventListener('click', go);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    });
    const slot = container.querySelector('[data-item-edit-slot]');
    if (slot && editing) {
      const item = editing.id === 'new' ? null : app.itemById(editing.kind, editing.id);
      const close = () => { editing = null; rerender(); };
      // After a history change the data has reloaded; reopen with fresh values.
      const refreshed = () => rerender();
      itemView.render(slot, { kind: editing.kind, item, onClose: close, onChange: refreshed });
      slot.scrollIntoView({ block: 'nearest' });
    }
  }

  return { render, reset: () => { editing = null; accountsView.reset(); }, isEditing: () => editing !== null || accountsView.isEditing() };
}
