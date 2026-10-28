/** Finance settings: household members, categories, categorisation rules, saved bank mappings and CSV export. */

import { isoDay, monthBounds, penceToInput } from '../domain/money.js';
import { ICONS, avatarHtml, categoryDot, emptyHtml, esc, optionsHtml } from './dom.js';

const COLORS = ['', 'red', 'orange', 'yellow', 'green', 'blue', 'purple'];
const MEMBER_COLORS = COLORS.filter(Boolean);
// New members get the next colour in this order (matches the server's default).
const DEFAULT_MEMBER_COLORS = ['blue', 'green', 'purple', 'orange', 'red', 'yellow'];
const RELATIONSHIPS = [['partner', 'Partner'], ['child', 'Child'], ['other', 'Other']];

export function createSettingsView({ app, ui, now }) {
  let rules = [];
  let mappings = [];
  let settings = null;
  let editingCategory = null; // id, 'new' or null
  let editingMember = null; // id, 'new' or null
  let errorText = '';

  async function load() {
    try {
      [rules, mappings, settings] = await Promise.all([app.listRules(), app.listMappings(), app.settings()]);
      errorText = '';
    } catch (err) {
      errorText = err.message;
    }
  }

  function colorPicker(selected, colors = COLORS) {
    return `<div class="fin-color-picker" role="radiogroup" aria-label="Colour">${colors.map((c) => `
      <button type="button" class="fin-color-swatch${c === selected ? ' active' : ''}" data-color="${c}" role="radio"
        aria-checked="${c === selected}" title="${c || 'No colour'}">${categoryDot(c)}</button>`).join('')}</div>`;
  }

  function memberForm(m) {
    const relationship = m?.relationship || 'partner';
    const relationshipField = m?.is_primary
      ? '<div class="fin-row-sub">Me (the primary member, named after your account)</div>'
      : `<div class="task-form-toggle" role="radiogroup" aria-label="Relationship">${RELATIONSHIPS.map(([value, label]) => `
          <button type="button" class="task-toggle-btn${relationship === value ? ' active' : ''}" data-relationship="${value}">${label}</button>`).join('')}</div>`;
    return `
      <form class="task-form fin-inline-form" data-member-form novalidate>
        <div class="fin-form-row">
          <div class="fin-form-col">
            <label class="task-form-label" for="fin-member-name">Name</label>
            <input id="fin-member-name" class="task-form-input" name="name" maxlength="80" value="${esc(m?.name || '')}" autocomplete="off">
          </div>
          <div class="fin-form-col">
            <label class="task-form-label">Relationship</label>
            ${relationshipField}
          </div>
        </div>
        <div class="fin-form-row">
          <div class="fin-form-col">
            <label class="task-form-label" for="fin-member-dob">Date of birth <span class="fin-row-sub">(optional, needed for children's accounts)</span></label>
            <input id="fin-member-dob" class="task-form-input" type="date" name="date_of_birth" value="${esc(m?.date_of_birth || '')}">
          </div>
          <div class="fin-form-col">
            <label class="task-form-label" for="fin-member-initials">Initials <span class="fin-row-sub">(optional)</span></label>
            <input id="fin-member-initials" class="task-form-input" name="initials" maxlength="3" value="${esc(m?.custom_initials || '')}" placeholder="${esc(m?.initials || '')}" autocomplete="off">
          </div>
        </div>
        <label class="task-form-label">Colour</label>
        ${colorPicker(m?.color || DEFAULT_MEMBER_COLORS[app.state.members.length % DEFAULT_MEMBER_COLORS.length], MEMBER_COLORS)}
        <div class="fin-form-error" role="alert" hidden></div>
        <div class="task-form-actions">
          ${m && !m.is_primary && !m.owns_count ? '<button type="button" class="task-btn task-btn-danger" data-action="delete-member">Delete</button>' : ''}
          ${m && !m.is_primary ? `<button type="button" class="task-btn" data-action="archive-member">${m.archived ? 'Restore' : 'Archive'}</button>` : ''}
          <span style="flex:1"></span>
          <button type="button" class="task-btn" data-action="cancel-member">Cancel</button>
          <button type="submit" class="task-btn task-btn-primary">${m ? 'Save' : 'Add member'}</button>
        </div>
      </form>`;
  }

  function membersHtml() {
    const rows = app.state.members.map((m) => {
      if (editingMember === m.id) return `<div class="memory-item fin-edit-card">${memberForm(m)}</div>`;
      const details = [m.relationship_label];
      if (m.age !== null && m.age !== undefined) details.push(`age ${m.age}`);
      if (m.owns_count) details.push(`owns ${m.owns_count} item${m.owns_count === 1 ? '' : 's'}`);
      return `<div class="memory-item fin-row${m.archived ? ' fin-row-archived' : ''}" data-member-id="${esc(m.id)}" role="button" tabindex="0">
          ${avatarHtml(m, { title: false })}
          <div class="fin-row-main"><span class="memory-item-title">${esc(m.display_name)}</span>
          <span class="fin-row-sub">${esc(details.join(' · '))}</span></div>
          ${m.archived ? '<span class="fin-badge">Archived</span>' : ''}
        </div>`;
    }).join('');
    return `
      <div class="fin-card-head"><h3 class="fin-section-title">Household</h3><span style="flex:1"></span>
        <button type="button" class="memory-toolbar-btn" data-action="new-member">+ Add member</button></div>
      <p class="memory-desc">People in your household. Only you log in; members are used to show who owns each account, asset and debt.</p>
      ${editingMember === 'new' ? `<div class="memory-item fin-edit-card">${memberForm(null)}</div>` : ''}
      <div class="memory-list fin-list">${rows}</div>`;
  }

  function categoryForm(cat) {
    return `
      <form class="task-form fin-inline-form" data-category-form novalidate>
        <div class="fin-form-row">
          <div class="fin-form-col">
            <label class="task-form-label" for="fin-cat-name">Name</label>
            <input id="fin-cat-name" class="task-form-input" name="name" maxlength="80" value="${esc(cat?.name || '')}" autocomplete="off" ${cat?.is_system ? 'disabled' : ''}>
          </div>
          <div class="fin-form-col">
            <label class="task-form-label">Type</label>
            <div class="task-form-toggle" role="radiogroup" aria-label="Type">
              <button type="button" class="task-toggle-btn${(cat?.kind || 'expense') === 'expense' ? ' active' : ''}" data-kind="expense">Spending</button>
              <button type="button" class="task-toggle-btn${cat?.kind === 'income' ? ' active' : ''}" data-kind="income">Income</button>
            </div>
          </div>
        </div>
        <label class="task-form-label">Colour</label>
        ${colorPicker(cat?.color || '')}
        <div class="fin-form-error" role="alert" hidden></div>
        <div class="task-form-actions">
          ${cat && !cat.is_system ? '<button type="button" class="task-btn task-btn-danger" data-action="delete-category">Delete</button><span style="flex:1"></span>' : ''}
          <button type="button" class="task-btn" data-action="cancel-category">Cancel</button>
          <button type="submit" class="task-btn task-btn-primary">${cat ? 'Save' : 'Add category'}</button>
        </div>
      </form>`;
  }

  function categoriesHtml() {
    const rows = app.state.categories.map((c) => (editingCategory === c.id
      ? `<div class="memory-item fin-edit-card">${categoryForm(c)}</div>`
      : `<div class="memory-item fin-row" data-category-id="${esc(c.id)}" role="button" tabindex="0">
          ${categoryDot(c.color)}
          <div class="fin-row-main"><span class="memory-item-title">${esc(c.name)}</span>
          <span class="fin-row-sub">${c.kind === 'income' ? 'Income' : 'Spending'}${c.is_system ? ' · used when nothing else fits' : ''}</span></div>
        </div>`)).join('');
    return `
      <div class="fin-card-head"><h3 class="fin-section-title">Categories</h3><span style="flex:1"></span>
        <button type="button" class="memory-toolbar-btn" data-action="new-category">+ New category</button></div>
      ${editingCategory === 'new' ? `<div class="memory-item fin-edit-card">${categoryForm(null)}</div>` : ''}
      <div class="memory-list fin-list">${rows}</div>`;
  }

  function rulesHtml() {
    const cats = app.state.categories.map((c) => ({ value: c.id, label: c.name }));
    const debts = app.state.liabilities.filter((d) => !d.archived).map((d) => ({ value: d.id, label: d.name }));
    const debtSelect = (selected, attrs) => (debts.length
      ? `<select class="task-form-input" ${attrs} aria-label="Debt this pays">${optionsHtml(debts, selected || '', { blank: 'Not a debt payment' })}</select>` : '');
    const rows = rules.map((r) => `
      <div class="memory-item fin-row fin-rule-row" data-rule-id="${esc(r.id)}">
        <div class="fin-row-main"><span class="fin-row-sub">Description contains</span>
          <input class="task-form-input" data-rule-pattern value="${esc(r.pattern)}" maxlength="80" aria-label="Text to match"></div>
        <div class="fin-row-main"><span class="fin-row-sub">Category</span>
          <select class="task-form-input" data-rule-category aria-label="Category">${optionsHtml(cats, r.category_id)}</select>
          ${debtSelect(r.liability_id, 'data-rule-debt')}</div>
        <button type="button" class="memory-item-btn" data-delete-rule title="Delete rule" aria-label="Delete rule">&times;</button>
      </div>`).join('');
    return `
      <h3 class="fin-section-title">Categorisation rules</h3>
      <p class="memory-desc">Applied when importing: a description containing the text (any case) gets the category, and can count as a payment towards a debt. Longer matches win. Add rules here or with "Remember" in the import preview.</p>
      <form class="fin-form-row fin-rule-add" data-rule-form>
        <div class="fin-form-col"><input class="task-form-input" name="pattern" maxlength="80" placeholder="e.g. tesco" aria-label="Text to match"></div>
        <div class="fin-form-col"><select class="task-form-input" name="category_id" aria-label="Category">${optionsHtml(cats, '')}</select></div>
        ${debts.length ? `<div class="fin-form-col">${debtSelect('', 'name="liability_id"')}</div>` : ''}
        <button type="submit" class="task-btn">Add rule</button>
      </form>
      <div class="memory-list fin-list">${rows || emptyHtml('No rules yet.')}</div>`;
  }

  function mappingsHtml() {
    return `
      <h3 class="fin-section-title">Saved bank mappings</h3>
      <div class="memory-list fin-list">${mappings.map((m) => `
        <div class="memory-item fin-row">
          <div class="fin-row-main"><span class="memory-item-title">${esc(m.name)}</span>
            <span class="fin-row-sub">${m.config.amount_mode === 'split' ? 'Paid in / out columns' : 'One amount column'} · ${esc(m.config.date_format)}</span></div>
          <button type="button" class="memory-item-btn" data-delete-mapping="${esc(m.id)}" title="Delete mapping" aria-label="Delete mapping">&times;</button>
        </div>`).join('') || emptyHtml('Save a mapping from the import preview to reuse it next time.')}</div>`;
  }

  function childrenHtml() {
    if (!settings || !app.state.members.some((m) => m.relationship === 'child')) return '';
    return `
      <h3 class="fin-section-title">Children</h3>
      <form class="task-form" data-children-settings novalidate>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label" for="fin-jisa-limit">Junior ISA allowance per tax year (£)</label>
            <input id="fin-jisa-limit" class="task-form-input" name="junior_isa_limit" inputmode="decimal" autocomplete="off"
              value="${esc(penceToInput(settings.junior_isa_limit_pence))}"></div>
        </div>
        <div class="fin-hint">Per child, from 6 April to 5 April. £9,000 in 2026/27; change it if the government does.</div>
        <div class="fin-form-error" role="alert" hidden></div>
        <div class="task-form-actions"><button type="submit" class="task-btn task-btn-primary">Save</button></div>
      </form>`;
  }

  function exportHtml() {
    const today = now();
    const bounds = monthBounds({ year: today.getFullYear(), month: today.getMonth() + 1 });
    const accounts = app.state.accounts.map((a) => ({ value: a.id, label: a.archived ? `${a.name} (archived)` : a.name }));
    return `
      <h3 class="fin-section-title">Export CSV</h3>
      <form class="task-form" data-export-form>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label">Account</label>
            <select class="task-form-input" name="account_id">${optionsHtml(accounts, '', { blank: 'All accounts' })}</select></div>
          <div class="fin-form-col"><label class="task-form-label">From</label>
            <input class="task-form-input" type="date" name="start" value="${bounds.start}"></div>
          <div class="fin-form-col"><label class="task-form-label">To</label>
            <input class="task-form-input" type="date" name="end" value="${isoDay(today)}"></div>
        </div>
        <div class="task-form-actions"><button type="submit" class="task-btn task-btn-primary">${ICONS.download()} Download CSV</button></div>
      </form>`;
  }

  function render(container) {
    container.innerHTML = `
      <div class="admin-card fin-card">
        ${errorText ? `<div class="fin-form-error" role="alert">${esc(errorText)}</div>` : ''}
        ${membersHtml()}
        ${childrenHtml()}
        ${categoriesHtml()}
        ${rulesHtml()}
        ${mappingsHtml()}
        ${exportHtml()}
      </div>`;
    wire(container);
  }

  function wire(container) {
    const rerender = () => render(container);
    container.querySelector('[data-action="new-member"]').addEventListener('click', () => { editingMember = 'new'; rerender(); });
    container.querySelectorAll('[data-member-id]').forEach((row) => {
      const open = () => { editingMember = row.dataset.memberId; rerender(); };
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    });
    const memberFormEl = container.querySelector('[data-member-form]');
    if (memberFormEl) wireMemberForm(container, memberFormEl);
    const childForm = container.querySelector('[data-children-settings]');
    childForm?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const errorBox = childForm.querySelector('.fin-form-error');
      try {
        settings = await app.updateSettings({ junior_isa_limit: childForm.junior_isa_limit.value.trim() });
        errorBox.hidden = true;
        ui.showToast('Allowance saved');
        rerender();
      } catch (err) { errorBox.textContent = err.message; errorBox.hidden = false; }
    });
    container.querySelector('[data-action="new-category"]').addEventListener('click', () => { editingCategory = 'new'; rerender(); });
    container.querySelectorAll('[data-category-id]').forEach((row) => {
      const open = () => { editingCategory = row.dataset.categoryId; rerender(); };
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    });
    const catForm = container.querySelector('[data-category-form]');
    if (catForm) wireCategoryForm(container, catForm);

    const ruleForm = container.querySelector('[data-rule-form]');
    ruleForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const pattern = ruleForm.pattern.value.trim();
      if (!pattern) return;
      try {
        await app.createRule({ pattern, category_id: ruleForm.category_id.value,
          liability_id: ruleForm.liability_id ? (ruleForm.liability_id.value || null) : null });
        rules = await app.listRules();
        ui.showToast('Rule added');
        rerender();
      } catch (err) { ui.showError(err.message); }
    });
    container.querySelectorAll('[data-rule-id]').forEach((row) => {
      const id = row.dataset.ruleId;
      const save = async (body) => {
        try { await app.updateRule(id, body); rules = await app.listRules(); ui.showToast('Rule saved'); } catch (err) { ui.showError(err.message); rerender(); }
      };
      row.querySelector('[data-rule-pattern]').addEventListener('change', (e) => { if (e.target.value.trim()) save({ pattern: e.target.value.trim() }); });
      row.querySelector('[data-rule-category]').addEventListener('change', (e) => save({ category_id: e.target.value }));
      row.querySelector('[data-rule-debt]')?.addEventListener('change', (e) => save({ liability_id: e.target.value || null }));
      row.querySelector('[data-delete-rule]').addEventListener('click', async () => {
        try { await app.deleteRule(id); rules = await app.listRules(); ui.showToast('Rule deleted'); rerender(); } catch (err) { ui.showError(err.message); }
      });
    });
    container.querySelectorAll('[data-delete-mapping]').forEach((b) => b.addEventListener('click', async () => {
      const ok = await ui.styledConfirm('Delete this saved mapping?', { title: 'Delete mapping', confirmText: 'Delete', danger: true });
      if (!ok) return;
      try { await app.deleteMapping(b.dataset.deleteMapping); mappings = await app.listMappings(); ui.showToast('Mapping deleted'); rerender(); } catch (err) { ui.showError(err.message); }
    }));
    const exportForm = container.querySelector('[data-export-form]');
    exportForm.addEventListener('submit', (e) => {
      e.preventDefault();
      if (exportForm.start.value && exportForm.end.value && exportForm.start.value > exportForm.end.value) {
        ui.showError('The "From" date is after the "To" date');
        return;
      }
      const link = document.createElement('a');
      link.href = app.exportUrl({ account_id: exportForm.account_id.value, start: exportForm.start.value, end: exportForm.end.value });
      link.download = '';
      document.body.appendChild(link);
      link.click();
      link.remove();
    });
  }

  function wireMemberForm(container, form) {
    const member = editingMember === 'new' ? null : app.memberById(editingMember);
    let relationship = member?.relationship || 'partner';
    let color = form.querySelector('[data-color].active')?.dataset.color || '';
    const errorBox = form.querySelector('.fin-form-error');
    const fail = (msg) => { errorBox.textContent = msg; errorBox.hidden = !msg; };
    const close = () => { editingMember = null; render(container); };
    form.querySelectorAll('[data-relationship]').forEach((b) => b.addEventListener('click', () => {
      relationship = b.dataset.relationship;
      form.querySelectorAll('[data-relationship]').forEach((x) => x.classList.toggle('active', x === b));
    }));
    form.querySelectorAll('[data-color]').forEach((b) => b.addEventListener('click', () => {
      color = b.dataset.color;
      form.querySelectorAll('[data-color]').forEach((x) => {
        x.classList.toggle('active', x === b);
        x.setAttribute('aria-checked', String(x === b));
      });
    }));
    form.querySelector('[data-action="cancel-member"]').addEventListener('click', close);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = {
        name: form.name.value.trim(),
        date_of_birth: form.date_of_birth.value || null,
        initials: form.initials.value.trim(),
        color,
      };
      if (!body.name) { fail('Give the member a name'); return; }
      if (!member?.is_primary) body.relationship = relationship;
      try {
        if (member) await app.updateMember(member.id, body);
        else await app.createMember(body);
        ui.showToast(member ? 'Member saved' : 'Member added');
        close();
      } catch (err) { fail(err.message); }
    });
    form.querySelector('[data-action="archive-member"]')?.addEventListener('click', async () => {
      if (!member.archived) {
        const ok = await ui.styledConfirm(
          `Archive ${member.name}? Anything they own stays theirs and still counts in your totals. They just won't be offered for new items.`,
          { title: 'Archive member', confirmText: 'Archive' });
        if (!ok) return;
      }
      try {
        await app.updateMember(member.id, { archived: !member.archived });
        ui.showToast(member.archived ? 'Member restored' : 'Member archived');
        close();
      } catch (err) { fail(err.message); }
    });
    form.querySelector('[data-action="delete-member"]')?.addEventListener('click', async () => {
      const ok = await ui.styledConfirm(`Delete ${member.name}? This can't be undone.`,
        { title: 'Delete member', confirmText: 'Delete', danger: true });
      if (!ok) return;
      try {
        await app.deleteMember(member.id);
        ui.showToast('Member deleted');
        close();
      } catch (err) { fail(err.message); }
    });
  }

  function wireCategoryForm(container, form) {
    const cat = editingCategory === 'new' ? null : app.categoryById(editingCategory);
    let kind = cat?.kind || 'expense';
    let color = cat?.color || '';
    const errorBox = form.querySelector('.fin-form-error');
    const fail = (msg) => { errorBox.textContent = msg; errorBox.hidden = !msg; };
    const close = () => { editingCategory = null; render(container); };
    form.querySelectorAll('[data-kind]').forEach((b) => b.addEventListener('click', () => {
      kind = b.dataset.kind;
      form.querySelectorAll('[data-kind]').forEach((x) => x.classList.toggle('active', x === b));
    }));
    form.querySelectorAll('[data-color]').forEach((b) => b.addEventListener('click', () => {
      color = b.dataset.color;
      form.querySelectorAll('[data-color]').forEach((x) => {
        x.classList.toggle('active', x === b);
        x.setAttribute('aria-checked', String(x === b));
      });
    }));
    form.querySelector('[data-action="cancel-category"]').addEventListener('click', close);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = { kind, color };
      if (!cat?.is_system) {
        body.name = form.name.value.trim();
        if (!body.name) { fail('Give the category a name'); return; }
      }
      try {
        if (cat) await app.updateCategory(cat.id, body);
        else await app.createCategory(body);
        ui.showToast(cat ? 'Category saved' : 'Category added');
        close();
      } catch (err) { fail(err.message); }
    });
    form.querySelector('[data-action="delete-category"]')?.addEventListener('click', async () => {
      let count = 0;
      try { count = (await app.categoryUsage(cat.id)).transactions; } catch { /* show generic text */ }
      const message = count
        ? `Delete "${cat.name}"? Its ${count} transaction${count === 1 ? '' : 's'} will move to Uncategorised, and its rules will be removed.`
        : `Delete "${cat.name}"?`;
      const ok = await ui.styledConfirm(message, { title: 'Delete category', confirmText: 'Delete', danger: true });
      if (!ok) return;
      try {
        await app.deleteCategory(cat.id);
        rules = await app.listRules();
        ui.showToast('Category deleted');
        close();
      } catch (err) { fail(err.message); }
    });
  }

  return {
    async render(container) {
      await load();
      render(container);
    },
    rerender: render,
  };
}
