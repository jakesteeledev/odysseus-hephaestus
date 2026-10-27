/**
 * CSV import: choose a file and account, map columns (or reuse a saved bank
 * mapping), preview every row, then import as an undoable batch.
 */

import { guessMapping, looksLikeHeader, sniffCsv } from '../domain/csvHeader.js';
import { amountHtml, emptyHtml, esc, optionsHtml } from './dom.js';

const MAX_FILE_BYTES = 2 * 1024 * 1024;
const DATE_FORMATS = ['dd/mm/yyyy', 'dd/mm/yy', 'dd-mm-yyyy', 'dd.mm.yyyy', 'yyyy-mm-dd', 'mm/dd/yyyy', 'dd mmm yyyy'];

export function createImportView({ app, ui }) {
  // One import at a time; kept while the window stays open.
  let file = null;          // { name, text, columns: [{value,label}], sample }
  let accountId = '';
  let mapping = null;
  let mappingName = '';
  let savedMappings = [];
  let preview = null;       // server preview response
  let overrides = {};       // row_number -> category id
  let excluded = new Set(); // row numbers the user unticked
  let recent = [];
  let busy = false;
  let errorText = '';

  async function loadLists() {
    try {
      [savedMappings, recent] = await Promise.all([app.listMappings(), app.listImports()]);
    } catch (err) {
      errorText = err.message;
    }
  }

  function columnsFor(text, hasHeader) {
    const sniff = sniffCsv(text);
    const cols = sniff.first.map((name, i) => ({
      value: hasHeader ? name : String(i),
      label: hasHeader ? (name || `Column ${i + 1}`) : `Column ${i + 1}: ${name}`,
    }));
    return { cols, sniff };
  }

  function categorySelect(row) {
    const chosen = overrides[row.row_number] || row.category_id || '';
    const items = app.state.categories.map((c) => ({ value: c.id, label: c.name }));
    return `<select class="task-form-input fin-row-cat" data-row-cat="${row.row_number}" aria-label="Category for row ${row.row_number}">${optionsHtml(items, chosen)}</select>`;
  }

  function previewHtml() {
    if (!preview) return '';
    const c = preview.counts;
    const importable = preview.rows.filter((r) => !r.error && !r.duplicate && !excluded.has(r.row_number)).length;
    const rows = preview.rows.map((r) => {
      let status = '';
      if (r.error) status = `<span class="fin-badge fin-badge-error" title="${esc(r.error)}">Error</span>`;
      else if (r.duplicate) status = '<span class="fin-badge">Duplicate</span>';
      const usable = !r.error && !r.duplicate;
      return `
        <div class="memory-item fin-row fin-preview-row${usable ? '' : ' fin-row-skipped'}">
          ${usable ? `<input type="checkbox" class="fin-check" data-row-include="${r.row_number}" ${excluded.has(r.row_number) ? '' : 'checked'} aria-label="Import row ${r.row_number}">` : '<span class="fin-check"></span>'}
          <div class="fin-row-main">
            <span class="memory-item-title">${esc(r.description || r.raw.join(', '))}</span>
            <span class="fin-row-sub">Row ${r.row_number}${r.date ? ` · ${esc(r.date)}` : ''}${r.error ? ` · ${esc(r.error)}` : ''}</span>
            ${usable ? `<div class="fin-preview-cat">${categorySelect(r)}
              <button type="button" class="memory-toolbar-btn" data-remember="${r.row_number}" title="Always use this category for descriptions containing this text">Remember</button></div>` : ''}
          </div>
          ${r.amount_pence ? amountHtml(r.amount_pence, r.direction) : ''}
          ${status}
        </div>`;
    }).join('');
    return `
      <h3 class="fin-section-title">Preview</h3>
      <div class="fin-hint">${c.total} rows: ${c.importable} new, ${c.duplicates} already recorded (skipped), ${c.errors} could not be read.</div>
      <div class="memory-list fin-list fin-preview-list">${rows}</div>
      <div class="task-form-actions">
        <button type="button" class="task-btn" data-action="save-mapping">Save mapping as…</button>
        <span style="flex:1"></span>
        <button type="button" class="task-btn task-btn-primary" data-action="commit" ${importable ? '' : 'disabled'}>Import ${importable} row${importable === 1 ? '' : 's'}</button>
      </div>`;
  }

  function mappingHtml() {
    if (!file || !mapping) return '';
    const sel = (key, blank) => `<select class="task-form-input" data-map="${key}">${optionsHtml(file.columns, String(mapping[key] ?? ''), { blank })}</select>`;
    return `
      <div class="fin-form-row">
        <div class="fin-form-col"><label class="task-form-label">Date column</label>${sel('date_column', 'Choose…')}</div>
        <div class="fin-form-col"><label class="task-form-label">Description column</label>${sel('description_column', 'Choose…')}</div>
      </div>
      <label class="task-form-label">Amounts</label>
      <div class="task-form-toggle" role="radiogroup" aria-label="Amount columns">
        <button type="button" class="task-toggle-btn${mapping.amount_mode === 'signed' ? ' active' : ''}" data-amount-mode="signed">One amount column (+/-)</button>
        <button type="button" class="task-toggle-btn${mapping.amount_mode === 'split' ? ' active' : ''}" data-amount-mode="split">Paid in / paid out columns</button>
      </div>
      ${mapping.amount_mode === 'signed' ? `
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label">Amount column</label>${sel('amount_column', 'Choose…')}</div>
          <div class="fin-form-col fin-check-col"><label class="fin-check-label"><input type="checkbox" data-map-flag="invert_amounts" ${mapping.invert_amounts ? 'checked' : ''}> Money out is positive</label></div>
        </div>` : `
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label">Paid in column</label>${sel('paid_in_column', 'None')}</div>
          <div class="fin-form-col"><label class="task-form-label">Paid out column</label>${sel('paid_out_column', 'None')}</div>
        </div>`}
      <div class="fin-form-row">
        <div class="fin-form-col"><label class="task-form-label">Date format</label>
          <select class="task-form-input" data-map="date_format">${optionsHtml(DATE_FORMATS.map((d) => ({ value: d, label: d })), mapping.date_format)}</select></div>
        <div class="fin-form-col fin-check-col"><label class="fin-check-label"><input type="checkbox" data-map-flag="has_header" ${mapping.has_header ? 'checked' : ''}> First row is column names</label></div>
      </div>
      <div class="task-form-actions"><button type="button" class="task-btn task-btn-primary" data-action="preview" ${busy ? 'disabled' : ''}>Preview</button></div>`;
  }

  function recentHtml() {
    if (!recent.length) return '';
    return `
      <h3 class="fin-section-title">Recent imports</h3>
      <div class="memory-list fin-list">${recent.map((b) => `
        <div class="memory-item fin-row${b.undone_at ? ' fin-row-skipped' : ''}">
          <div class="fin-row-main">
            <span class="memory-item-title">${esc(b.filename || 'Import')}${b.mapping_name ? ` · ${esc(b.mapping_name)}` : ''}</span>
            <span class="fin-row-sub">${esc(b.account_name)} · ${b.imported_count} imported, ${b.skipped_count} skipped · ${esc((b.created_at || '').slice(0, 10))}${b.undone_at ? ' · undone' : ''}</span>
          </div>
          ${b.undone_at || !b.imported_count ? '' : `<button type="button" class="task-btn task-btn-danger" data-undo="${esc(b.id)}">Undo</button>`}
        </div>`).join('')}</div>`;
  }

  function render(container) {
    const accounts = app.activeAccounts();
    if (!accountId || !accounts.some((a) => a.id === accountId)) accountId = app.defaultAccountId();
    if (!accounts.length) {
      container.innerHTML = `<div class="admin-card fin-card">${emptyHtml('Add an account first, then import its statement.')}</div>`;
      return;
    }
    container.innerHTML = `
      <div class="admin-card fin-card">
        <h2 class="fin-card-title">Import a bank statement (CSV)</h2>
        <p class="memory-desc">Download a CSV statement from your bank, choose it here, and check the preview before anything is saved.</p>
        <div class="fin-form-row">
          <div class="fin-form-col">
            <label class="task-form-label" for="fin-import-account">Import into</label>
            <select id="fin-import-account" class="task-form-input" data-import-account>${optionsHtml(accounts.map((a) => ({ value: a.id, label: a.name })), accountId)}</select>
          </div>
          <div class="fin-form-col">
            <label class="task-form-label" for="fin-import-mapping">Saved bank mapping</label>
            <select id="fin-import-mapping" class="task-form-input" data-saved-mapping>${optionsHtml(savedMappings.map((m) => ({ value: m.id, label: m.name })), '', { blank: savedMappings.length ? 'Choose a bank…' : 'None saved yet' })}</select>
          </div>
        </div>
        <label class="task-form-label" for="fin-import-file">CSV file</label>
        <input id="fin-import-file" type="file" accept=".csv,text/csv" class="task-form-input" data-import-file>
        ${file ? `<div class="fin-hint">${esc(file.name)} · ${file.rowCount} lines${mappingName ? ` · using "${esc(mappingName)}"` : ''}</div>` : ''}
        ${mappingHtml()}
        <div class="fin-form-error" role="alert" ${errorText ? '' : 'hidden'}>${esc(errorText)}</div>
        ${previewHtml()}
        ${recentHtml()}
      </div>`;
    wire(container);
  }

  function resetPreview() { preview = null; overrides = {}; excluded = new Set(); }

  function wire(container) {
    const rerender = () => render(container);
    container.querySelector('[data-import-account]').addEventListener('change', (e) => { accountId = e.target.value; resetPreview(); rerender(); });
    container.querySelector('[data-saved-mapping]').addEventListener('change', (e) => {
      const saved = savedMappings.find((m) => m.id === e.target.value);
      if (!saved) return;
      mapping = { ...saved.config };
      mappingName = saved.name;
      if (file) file.columns = columnsFor(file.text, mapping.has_header).cols;
      resetPreview();
      errorText = '';
      if (file) runPreview(container); else rerender();
    });
    container.querySelector('[data-import-file]').addEventListener('change', (e) => {
      const chosen = e.target.files && e.target.files[0];
      if (!chosen) return;
      if (chosen.size > MAX_FILE_BYTES) { errorText = 'That file is larger than 2 MB.'; rerender(); return; }
      const reader = new FileReader();
      reader.onload = () => {
        const text = String(reader.result || '');
        const { sniff } = columnsFor(text, true);
        const hasHeader = looksLikeHeader(sniff.first);
        if (!mapping) {
          mapping = hasHeader ? guessMapping(sniff.first) : {
            has_header: false, date_column: '0', description_column: '1', amount_mode: 'signed',
            amount_column: '2', paid_in_column: '', paid_out_column: '', date_format: 'dd/mm/yyyy', invert_amounts: false,
          };
        }
        file = { name: chosen.name, text, rowCount: sniff.rowCount, columns: columnsFor(text, mapping.has_header).cols };
        resetPreview();
        errorText = '';
        rerender();
      };
      reader.onerror = () => { errorText = 'Could not read that file.'; rerender(); };
      reader.readAsText(chosen);
    });
    container.querySelectorAll('[data-map]').forEach((sel) => sel.addEventListener('change', () => {
      mapping[sel.dataset.map] = sel.value;
      mappingName = '';
      resetPreview();
    }));
    container.querySelectorAll('[data-map-flag]').forEach((box) => box.addEventListener('change', () => {
      mapping[box.dataset.mapFlag] = box.checked;
      if (box.dataset.mapFlag === 'has_header') {
        file.columns = columnsFor(file.text, box.checked).cols;
        const fresh = box.checked ? guessMapping(sniffCsv(file.text).first) : null;
        for (const key of ['date_column', 'description_column', 'amount_column', 'paid_in_column', 'paid_out_column']) {
          mapping[key] = fresh ? fresh[key] : '';
        }
      }
      mappingName = '';
      resetPreview();
      rerender();
    }));
    container.querySelectorAll('[data-amount-mode]').forEach((b) => b.addEventListener('click', () => {
      mapping.amount_mode = b.dataset.amountMode;
      mappingName = '';
      resetPreview();
      rerender();
    }));
    container.querySelector('[data-action="preview"]')?.addEventListener('click', () => runPreview(container));
    container.querySelectorAll('[data-row-cat]').forEach((sel) => sel.addEventListener('change', () => {
      overrides[sel.dataset.rowCat] = sel.value;
    }));
    container.querySelectorAll('[data-row-include]').forEach((box) => box.addEventListener('change', () => {
      const n = Number(box.dataset.rowInclude);
      if (box.checked) excluded.delete(n); else excluded.add(n);
      const importable = preview.rows.filter((r) => !r.error && !r.duplicate && !excluded.has(r.row_number)).length;
      const btn = container.querySelector('[data-action="commit"]');
      btn.textContent = `Import ${importable} row${importable === 1 ? '' : 's'}`;
      btn.disabled = !importable;
    }));
    container.querySelectorAll('[data-remember]').forEach((b) => b.addEventListener('click', () => remember(container, Number(b.dataset.remember))));
    container.querySelector('[data-action="save-mapping"]')?.addEventListener('click', () => saveMapping(container));
    container.querySelector('[data-action="commit"]')?.addEventListener('click', () => commit(container));
    container.querySelectorAll('[data-undo]').forEach((b) => b.addEventListener('click', () => undo(container, b.dataset.undo)));
  }

  function requestBody() {
    return { csv_text: file.text, mapping, account_id: accountId, filename: file.name, mapping_name: mappingName };
  }

  async function runPreview(container) {
    if (!file) return;
    busy = true;
    errorText = '';
    try {
      preview = await app.previewImport(requestBody());
    } catch (err) {
      preview = null;
      errorText = err.message;
    }
    busy = false;
    render(container);
  }

  async function remember(container, rowNumber) {
    const row = preview.rows.find((r) => r.row_number === rowNumber);
    const categoryId = overrides[rowNumber] || row.category_id;
    const category = app.categoryById(categoryId);
    const pattern = await ui.styledPrompt(
      `Always file descriptions containing this text under "${category ? category.name : 'this category'}":`,
      { title: 'Remember category', defaultValue: row.description, confirmText: 'Remember' });
    if (!pattern || !String(pattern).trim()) return;
    try {
      await app.createRule({ pattern: String(pattern).trim(), category_id: categoryId });
      ui.showToast('Rule saved');
      const keep = { ...overrides };
      await runPreview(container);
      // Choices made by hand still win over the new rule.
      overrides = keep;
      render(container);
    } catch (err) {
      ui.showError(err.message);
    }
  }

  async function saveMapping(container) {
    const name = await ui.styledPrompt('Name this mapping so you can reuse it next time (e.g. the bank name):',
      { title: 'Save mapping', defaultValue: mappingName, confirmText: 'Save', maxLength: 60 });
    if (!name || !String(name).trim()) return;
    try {
      const saved = await app.saveMapping({ name: String(name).trim(), config: mapping });
      mappingName = saved.name;
      savedMappings = await app.listMappings();
      ui.showToast(`Saved mapping "${saved.name}"`);
      render(container);
    } catch (err) {
      ui.showError(err.message);
    }
  }

  async function commit(container) {
    const btn = container.querySelector('[data-action="commit"]');
    if (btn) btn.disabled = true;
    try {
      const result = await app.commitImport({ ...requestBody(), category_overrides: overrides, exclude_rows: [...excluded] });
      const dupes = result.skipped.filter((s) => s.reason === 'duplicate').length;
      ui.showToast(`Imported ${result.batch.imported_count} transaction${result.batch.imported_count === 1 ? '' : 's'}${dupes ? `, skipped ${dupes} duplicate${dupes === 1 ? '' : 's'}` : ''}`);
      file = null;
      mappingName = '';
      mapping = null;
      resetPreview();
      await loadLists();
      render(container);
    } catch (err) {
      errorText = err.message;
      render(container);
    }
  }

  async function undo(container, batchId) {
    const ok = await ui.styledConfirm('Remove every transaction added by this import?', { title: 'Undo import', confirmText: 'Undo import', danger: true });
    if (!ok) return;
    try {
      const result = await app.undoImport(batchId);
      ui.showToast(`Removed ${result.removed} transaction${result.removed === 1 ? '' : 's'}`);
      await loadLists();
      render(container);
    } catch (err) {
      ui.showError(err.message);
    }
  }

  return {
    async render(container) {
      await loadLists();
      render(container);
    },
    rerender: render,
  };
}
