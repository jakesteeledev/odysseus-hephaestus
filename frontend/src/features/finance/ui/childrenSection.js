/**
 * Children section of the overview: each child's age, what they own (at
 * their share), Junior ISA lock dates and this tax year's allowance, and
 * regular transfers such as pocket money.
 */

import { formatPence, longDate } from '../domain/money.js';
import { avatarHtml, balanceHtml, esc } from './dom.js';

/** The children to show: everyone in the household view, or the chosen child. */
function shownChildren(app) {
  const all = app.state.children?.children || [];
  const person = app.state.person;
  return person ? all.filter((c) => c.member.id === person) : all;
}

/** Junior ISA status by asset id, for notes on asset rows. */
export function juniorIsaById(app) {
  const out = new Map();
  for (const child of app.state.children?.children || []) {
    for (const isa of child.junior_isas) out.set(isa.id, isa);
  }
  return out;
}

/** " · locked until 1 Mar 2036" / " · unlocked" for a Junior ISA row. */
export function juniorIsaNote(isa) {
  if (!isa) return '';
  if (!isa.unlock_date) return 'locked until 18 (add a date of birth to see when)';
  return isa.locked ? `locked until ${longDate(isa.unlock_date)}` : `unlocked ${longDate(isa.unlock_date)}`;
}

function allowanceHtml(allowance, taxYear) {
  if (!allowance) return '';
  const limit = Math.max(allowance.limit_pence, 1);
  const width = Math.min(100, Math.round((allowance.contributed_pence / limit) * 100));
  const state = allowance.over_pence ? 'over' : allowance.remaining_pence === 0 ? 'warning' : 'ok';
  const text = allowance.over_pence
    ? `${formatPence(allowance.over_pence)} over the allowance`
    : `${formatPence(allowance.remaining_pence)} left`;
  return `
    <div class="fin-child-allowance">
      <div class="fin-budget-head"><span class="fin-row-sub">Junior ISA allowance ${esc(taxYear)}</span>
        <span class="fin-amount">${esc(formatPence(allowance.contributed_pence))} <span class="fin-row-sub">of ${esc(formatPence(allowance.limit_pence))}</span></span></div>
      <span class="fin-budget-bar" role="img" aria-label="${width}% of the allowance used">
        <span class="fin-budget-fill fin-budget-${state}" style="width:${width}%"></span></span>
      <span class="fin-row-sub">${allowance.over_pence ? `<span class="fin-budget-label fin-budget-over">${esc(text)}</span>` : esc(text)}</span>
    </div>`;
}

function childHtml(child, taxYear) {
  const m = child.member;
  const about = [];
  if (m.age !== null && m.age !== undefined) about.push(`age ${m.age}`);
  if (child.adult_on) about.push(m.age >= 18 ? `turned 18 on ${longDate(child.adult_on)}` : `18 on ${longDate(child.adult_on)}`);
  else about.push('no date of birth');
  const isaNotes = new Map(child.junior_isas.map((i) => [i.id, juniorIsaNote(i)]));
  const sipps = new Set(child.junior_sipps.map((s) => s.id));
  const items = child.items.map((item) => {
    const notes = [];
    if (isaNotes.has(item.id)) notes.push(isaNotes.get(item.id));
    if (sipps.has(item.id)) notes.push('pension: kept until pension age');
    if (item.joint) notes.push(`share of ${formatPence(item.full_value_pence)}`);
    return `<div class="fin-child-item"><span>${esc(item.name)}${notes.length ? ` <span class="fin-row-sub">· ${esc(notes.join(' · '))}</span>` : ''}</span>
      ${balanceHtml(item.side === 'liability' ? -item.value_pence : item.value_pence)}</div>`;
  }).join('');
  const transfers = child.regular_transfers.map((r) => `
    <div class="fin-child-item"><span>${esc(r.name)} <span class="fin-row-sub">· ${esc(r.frequency_label.toLowerCase())} from ${esc(r.from_account)} to ${esc(r.to_account)}</span></span>
      <span class="fin-amount fin-amount-in">${esc(formatPence(r.amount_pence))}</span></div>`).join('');
  return `
    <div class="fin-child">
      <div class="fin-member-worth">
        ${avatarHtml(m, { title: false })}
        <div class="fin-row-main"><span class="memory-item-title">${esc(m.display_name)}</span>
          <span class="fin-row-sub">${esc(about.join(' · '))}</span></div>
        ${balanceHtml(child.net_worth_pence)}
      </div>
      ${items || '<div class="fin-row-sub">Nothing in their name yet. Make them the owner of an account, Junior ISA or Junior SIPP.</div>'}
      ${allowanceHtml(child.junior_isa_allowance, taxYear)}
      ${transfers ? `<div class="fin-child-transfers">${transfers}</div>`
        : '<button type="button" class="memory-toolbar-btn fin-link-btn" data-fin-go="upcoming">Set up pocket money</button>'}
    </div>`;
}

export function childrenSectionHtml(app) {
  const data = app.state.children;
  const shown = shownChildren(app);
  if (!data || !shown.length) return '';
  const total = shown.reduce((sum, c) => sum + c.net_worth_pence, 0);
  return `
    <div class="admin-card fin-card">
      <div class="fin-card-head">
        <h2 class="fin-card-title">Children</h2>
        ${balanceHtml(total)}
      </div>
      ${shown.map((c) => childHtml(c, data.tax_year)).join('')}
      <div class="fin-hint">Junior ISAs unlock at 18. Pocket money is a regular transfer: add one in Upcoming and choose Transfer.</div>
    </div>`;
}
