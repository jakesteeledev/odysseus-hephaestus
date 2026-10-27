/** Finance settings: categories, categorisation rules, saved bank mappings and CSV export. */

import { isoDay, monthBounds } from '../domain/money.js';
import { ICONS, categoryDot, emptyHtml, esc, optionsHtml } from './dom.js';

const COLORS = ['', 'red', 'orange', 'yellow', 'green', 'blue', 'purple'];

export function createSettingsView({ app, ui, now }) {
  let rules = [];
  let mappings = [];
  let editingCategory = null; // id, 'new' or null
  let errorText = '';

  async function load() {
    try {
      [rules, mappings] = await Promise.all([app.listRules(), app.listMappings()]);
      errorText = '';
    } catch (err) {
      errorText = err.message;
    }
  }

  function colorPicker(selected) {
    return `<div class="fin-color-picker" role="radiogroup" aria-label="Colour">${COLORS.map((c) => `
      <button type="button" class="fin-color-swatch${c === selected ? ' active' : ''}" data-color="${c}" role="radio"
        aria-checked="${c === selected}" title="${c || 'No colour'}">${categoryDot(c)}</button>`).join('')}</div>`;
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
    const rows = rules.map((r) => `
      <div class="memory-item fin-row fin-rule-row" data-rule-id="${esc(r.id)}">
        <div class="fin-row-main"><span class="fin-row-sub">Description contains</span>
          <input class="task-form-input" data-rule-pattern value="${esc(r.pattern)}" maxlength="80" aria-label="Text to match"></div>
        <div class="fin-row-main"><span class="fin-row-sub">Category</span>
          <select class="task-form-input" data-rule-category aria-label="Category">${optionsHtml(cats, r.category_id)}</select></div>
        <button type="button" class="memory-item-btn" data-delete-rule title="Delete rule" aria-label="Delete rule">&times;</button>
      </div>`).join('');
    return `
      <h3 class="fin-section-title">Categorisation rules</h3>
      <p class="memory-desc">Applied when importing: a description containing the text (any case) gets the category. Longer matches win. Add rules here or with "Remember" in the import preview.</p>
      <form class="fin-form-row fin-rule-add" data-rule-form>
        <div class="fin-form-col"><input class="task-form-input" name="pattern" maxlength="80" placeholder="e.g. tesco" aria-label="Text to match"></div>
        <div class="fin-form-col"><select class="task-form-input" name="category_id" aria-label="Category">${optionsHtml(cats, '')}</select></div>
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
        ${categoriesHtml()}
        ${rulesHtml()}
        ${mappingsHtml()}
        ${exportHtml()}
      </div>`;
    wire(container);
  }

  function wire(container) {
    const rerender = () => render(container);
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
        await app.createRule({ pattern, category_id: ruleForm.category_id.value });
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
