/**
 * Who owns an account, asset or liability: avatars with a "Joint" label for
 * rows, and a toggle-chip picker for forms. Both stay out of the way while
 * the household has only one member.
 */

import { avatarHtml, esc } from './dom.js';

/** True once there is anyone besides the primary member (archived included). */
export function hasHousehold(app) {
  return app.state.members.length > 1;
}

/** Owner avatars for a row, plus "Joint" when there is more than one owner. */
export function ownersHtml(app, ownerIds) {
  if (!hasHousehold(app) || !ownerIds?.length) return '';
  const members = ownerIds.map((id) => app.memberById(id)).filter(Boolean);
  const names = members.map((m) => m.display_name).join(', ');
  return `<span class="fin-owners" title="${esc(names)}" aria-label="Owned by ${esc(names)}">
      <span class="fin-avatar-stack">${members.map((m) => avatarHtml(m, { title: false })).join('')}</span>
      ${members.length > 1 ? '<span class="fin-badge">Joint</span>' : ''}
    </span>`;
}

/** Default owners for a new item: the primary member. */
export function defaultOwnerIds(app) {
  const primary = app.state.members.find((m) => m.is_primary);
  return primary ? [primary.id] : [];
}

/**
 * Toggle chips for choosing owners. Archived members are only offered when
 * they already own the item. Hidden (but still submitted) with one member.
 */
export function ownerPickerHtml(app, selectedIds) {
  const selected = new Set(selectedIds || []);
  const members = app.state.members.filter((m) => !m.archived || selected.has(m.id));
  if (!hasHousehold(app)) {
    return `<div data-owner-picker hidden>${members.map((m) => `
      <button type="button" data-owner-id="${esc(m.id)}" aria-pressed="${selected.has(m.id)}"></button>`).join('')}</div>`;
  }
  return `
    <label class="task-form-label">Owners</label>
    <div class="fin-owner-picker" data-owner-picker role="group" aria-label="Owners">${members.map((m) => `
      <button type="button" class="fin-owner-chip${selected.has(m.id) ? ' active' : ''}" data-owner-id="${esc(m.id)}"
        aria-pressed="${selected.has(m.id)}">${avatarHtml(m, { title: false })}<span>${esc(m.display_name)}</span></button>`).join('')}
    </div>
    <div class="fin-hint" data-owner-hint>Joint items are split equally between their owners.</div>`;
}

/** Wire the picker's chips; returns a function that reads the chosen ids in household order. */
export function wireOwnerPicker(form) {
  const picker = form.querySelector('[data-owner-picker]');
  if (!picker) return () => [];
  picker.querySelectorAll('[data-owner-id]').forEach((chip) => chip.addEventListener('click', () => {
    const on = chip.getAttribute('aria-pressed') !== 'true';
    chip.setAttribute('aria-pressed', String(on));
    chip.classList.toggle('active', on);
  }));
  return () => [...picker.querySelectorAll('[data-owner-id]')]
    .filter((chip) => chip.getAttribute('aria-pressed') === 'true')
    .map((chip) => chip.dataset.ownerId);
}

export function sameOwners(a, b) {
  const left = [...(a || [])].sort();
  const right = [...(b || [])].sort();
  return left.length === right.length && left.every((id, i) => id === right[i]);
}
