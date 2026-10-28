/**
 * Salary: UK jobs with PAYE tax, NI, student loans and workplace pensions.
 * Shows a payslip-style breakdown for the next pay date, the tax year's
 * summary, dated changes (pay rises, bonuses, code changes) and a payslip
 * check. All figures come from the server's payroll calculation.
 */

import { formatPence, longDate, penceToInput } from '../domain/money.js';
import { emptyHtml, esc, optionsHtml } from './dom.js';

const PLANS = [['plan_1', 'Plan 1'], ['plan_2', 'Plan 2'], ['plan_4', 'Plan 4'], ['plan_5', 'Plan 5']];
const CHANGE_KINDS = [
  ['raise', 'Pay rise (%)'], ['salary', 'New salary'], ['bonus', 'Bonus'], ['allowance', 'Allowance or overtime (a month)'],
  ['sacrifice', 'Other salary sacrifice (a month)'], ['tax_code', 'Tax code change'], ['pension_percent', 'Your pension %'],
  ['employer_percent', 'Employer pension %'], ['pension_method', 'Pension method'], ['hours', 'Reduced hours (% of pay)'],
  ['stop', 'Job ends'],
];
// Which inputs each change kind needs.
const CHANGE_FIELDS = {
  raise: ['percent', 'repeat'], salary: ['amount'], bonus: ['amount', 'repeat'], allowance: ['amount', 'end', 'label'],
  sacrifice: ['amount', 'end', 'label'], tax_code: ['code'], pension_percent: ['percent'], employer_percent: ['percent'],
  pension_method: ['method'], hours: ['percent'], stop: [],
};
const SLIP_FIELDS = [['gross', 'Gross pay'], ['tax', 'Income tax'], ['ni', 'National Insurance'], ['pension', 'Pension'],
  ['student_loan', 'Student loan'], ['net', 'Net pay']];

const pct = (value) => `${Number(value || 0).toLocaleString('en-GB', { maximumFractionDigits: 2 })}%`;

export function createSalaryView({ app, ui }) {
  let data = null;
  let errorText = '';
  let editing = null; // job id or 'new'
  let addingChange = null; // job id
  let checking = null; // job id
  let mounted = null;
  let generation = 0;

  async function load() {
    const mine = ++generation;
    try {
      const next = await app.salary();
      if (mine !== generation) return false;
      data = next;
      errorText = '';
    } catch (err) {
      if (mine !== generation) return false;
      errorText = err.message;
    }
    return true;
  }

  const accounts = () => app.state.accounts.filter((a) => !a.archived && a.account_type !== 'credit_card')
    .map((a) => ({ value: a.id, label: a.name }));
  const pensions = () => app.state.assets.filter((a) => !a.archived && a.type === 'pension').map((a) => ({ value: a.id, label: a.name }));
  const studentLoans = () => app.state.liabilities.filter((l) => !l.archived).map((l) => ({ value: l.id, label: l.name }));
  const kv = (obj) => Object.entries(obj || {}).map(([value, label]) => ({ value, label }));

  function warningsHtml() {
    const list = data?.warnings || [];
    if (!list.length) return '';
    return `<div class="fin-warnings">${list.map((w) => `
      <div class="fin-warning">${esc(w.text)}${w.action === 'replace'
        ? ` <button type="button" class="fin-link-btn fin-inline-link" data-replace="${esc(w.item_id)}">Replace it</button>` : ''}</div>`).join('')}</div>`;
  }

  function line(label, value, ytd, { strong = false, minus = false } = {}) {
    if (!value && !ytd && !strong) return '';
    const fmt = (v) => (v === null || v === undefined ? '' : `${minus && v ? '−' : ''}${formatPence(Math.abs(v))}`);
    return `<tr${strong ? ' class="fin-payslip-total"' : ''}><th scope="row">${esc(label)}</th><td>${esc(fmt(value))}</td><td>${esc(fmt(ytd))}</td></tr>`;
  }

  function payslipHtml(job) {
    const p = job.next_period;
    if (!p) return emptyHtml('No upcoming pay dates (the job may have ended).');
    const y = p.ytd || {};
    const loans = (p.student_loan_pence || 0) + (p.postgraduate_pence || 0);
    const ytdLoans = (y.student_loan_pence || 0) + (y.postgraduate_pence || 0);
    return `
      <div class="fin-section-title">Next pay · ${esc(longDate(p.date))}${p.rules_assumed ? ' · rates assumed' : ''}</div>
      <table class="fin-payslip">
        <thead><tr><th></th><th scope="col">This pay</th><th scope="col">Tax year to date</th></tr></thead>
        <tbody>
          ${line('Basic pay', p.basic_pence, null)}
          ${line('Allowances and overtime', p.extras_pence, null)}
          ${line('Bonus', p.bonus_pence, null)}
          ${line('Gross pay', p.gross_pence, y.gross_pence, { strong: true })}
          ${line('Pension (salary sacrifice)', p.pension_sacrifice_pence, y.pension_sacrifice_pence, { minus: true })}
          ${line('Other salary sacrifice', p.other_sacrifice_pence, y.other_sacrifice_pence, { minus: true })}
          ${line('Income tax', p.tax_pence, y.tax_pence, { minus: true })}
          ${line('National Insurance', p.ni_pence, y.ni_pence, { minus: true })}
          ${line('Student loan', loans, ytdLoans, { minus: true })}
          ${p.pension_method === 'net_pay' || p.pension_method === 'relief_at_source'
            ? line(p.pension_method === 'net_pay' ? 'Pension (net pay)' : 'Pension (you pay 80%)', p.pension_deduction_pence, y.pension_deduction_pence, { minus: true }) : ''}
          ${line('Net pay', p.net_pence, y.net_pence, { strong: true })}
        </tbody>
      </table>
      <div class="fin-hint">Tax code ${esc(p.tax_code)}${p.cumulative ? '' : ' (non-cumulative)'} · taxable pay ${esc(formatPence(p.taxable_pence))} · NI pay ${esc(formatPence(p.ni_pay_pence))}${p.pension_employer_pence ? ` · employer pension ${esc(formatPence(p.pension_employer_pence))}` : ''}${p.pension_relief_pence ? ` · tax relief added ${esc(formatPence(p.pension_relief_pence))}` : ''}${p.ni_sacrifice_added_back_pence ? ` · ${esc(formatPence(p.ni_sacrifice_added_back_pence))} of sacrifice over the £2,000 cap is subject to NI` : ''}</div>`;
  }

  function summaryHtml(job) {
    const s = job.summary;
    if (!s || !s.periods) return '';
    const stats = [
      ['Take-home this tax year', formatPence(s.net_pence)], ['A month on average', formatPence(s.take_home_monthly_pence)],
      ['Effective rate', pct(s.effective_rate_percent)], ['Rate on your next £1', pct(s.marginal_rate_percent)],
    ];
    const notes = [];
    const sacrifice = job.pension_method === 'salary_sacrifice' || job.pension_method === 'net_pay';
    notes.push(`Of each extra £1 of salary, ${pct(s.marginal_tax_percent)} goes in income tax and ${pct(s.marginal_rate_percent)} in total with NI and any student loan${sacrifice ? ' (part of each extra £1 goes into your pension first, which lowers these)' : ''}.`);
    if (s.taper_band) notes.push('You are in the personal allowance taper (£100,000 to £125,140), where tax is effectively 60%.');
    if (Math.abs(s.paye_difference_pence) > 100) {
      notes.push(s.paye_difference_pence > 0
        ? `PAYE on this code will collect about ${formatPence(s.paye_difference_pence)} less than you owe; HMRC usually adjusts your code or asks for it through self assessment.`
        : `PAYE on this code will collect about ${formatPence(-s.paye_difference_pence)} more than you owe; you may get a refund.`);
    }
    if (s.into_pension_pence) {
      notes.push(`${formatPence(s.into_pension_pence)} goes into your pension this year, costing you ${formatPence(s.pension_cost_pence)} of take-home after tax and NI relief.`);
    }
    if (s.pension_relief_claimable_pence) notes.push(`You can claim another ${formatPence(s.pension_relief_claimable_pence)} of higher-rate relief through self assessment (not added automatically).`);
    return `
      <div class="fin-section-title">${esc(s.tax_year)} summary${s.rules_assumed ? ' (rates assumed)' : ''}</div>
      <div class="fin-stats">${stats.map(([label, value]) => `
        <div class="fin-stat"><span class="fin-stat-label">${esc(label)}</span><span class="fin-amount">${esc(value)}</span></div>`).join('')}</div>
      ${notes.map((n) => `<div class="fin-hint">${esc(n)}</div>`).join('')}`;
  }

  function changesHtml(job) {
    const rows = job.changes.map((c) => `
      <div class="memory-item fin-row fin-static-row">
        <div class="fin-row-main"><span class="memory-item-title">${esc(c.description)}</span></div>
        <button type="button" class="fin-link-btn" data-delete-change="${esc(c.id)}" aria-label="Remove this change">Remove</button>
      </div>`).join('');
    return `
      <div class="fin-card-head"><div class="fin-section-title">Pay rises and changes</div><span style="flex:1"></span>
        <button type="button" class="memory-toolbar-btn" data-add-change="${esc(job.id)}">+ Add</button></div>
      ${addingChange === job.id ? `<div class="memory-item fin-edit-card">${changeForm()}</div>` : ''}
      <div class="memory-list fin-list">${rows || (addingChange === job.id ? '' : emptyHtml('No pay rises, bonuses or code changes yet.'))}</div>`;
  }

  function changeForm() {
    const today = new Date().toISOString().slice(0, 10);
    return `
      <form class="task-form fin-inline-form" data-change-form novalidate>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label" for="fin-change-kind">Change</label>
            <select id="fin-change-kind" class="task-form-input" name="kind">${optionsHtml(CHANGE_KINDS.map(([value, label]) => ({ value, label })), 'raise')}</select></div>
          <div class="fin-form-col"><label class="task-form-label" for="fin-change-start">From</label>
            <input id="fin-change-start" class="task-form-input" type="date" name="start_date" value="${today}"></div>
        </div>
        <div class="fin-form-row">
          <div class="fin-form-col" data-field="amount"><label class="task-form-label" for="fin-change-amount">Amount (£)</label>
            <input id="fin-change-amount" class="task-form-input" name="amount" inputmode="decimal" autocomplete="off"></div>
          <div class="fin-form-col" data-field="percent"><label class="task-form-label" for="fin-change-percent">Percentage</label>
            <input id="fin-change-percent" class="task-form-input" name="percent" inputmode="decimal" autocomplete="off"></div>
          <div class="fin-form-col" data-field="code"><label class="task-form-label" for="fin-change-code">New tax code</label>
            <input id="fin-change-code" class="task-form-input" name="code" autocomplete="off" placeholder="1257L"></div>
          <div class="fin-form-col" data-field="method"><label class="task-form-label" for="fin-change-method">Method</label>
            <select id="fin-change-method" class="task-form-input" name="new_method">${optionsHtml(kv(data.pension_methods), 'salary_sacrifice')}</select></div>
          <div class="fin-form-col" data-field="end"><label class="task-form-label" for="fin-change-end">Until <span class="fin-row-sub">(optional)</span></label>
            <input id="fin-change-end" class="task-form-input" type="date" name="end_date"></div>
          <div class="fin-form-col" data-field="label"><label class="task-form-label" for="fin-change-label">Name <span class="fin-row-sub">(optional)</span></label>
            <input id="fin-change-label" class="task-form-input" name="label" maxlength="80" autocomplete="off" placeholder="e.g. Cycle to work"></div>
        </div>
        <label class="fin-check-label" data-field="repeat"><input type="checkbox" class="fin-check" name="repeat"> Every year</label>
        <div class="fin-form-error" role="alert" hidden></div>
        <div class="task-form-actions"><span style="flex:1"></span>
          <button type="button" class="task-btn" data-action="cancel-change">Cancel</button>
          <button type="submit" class="task-btn task-btn-primary">Add change</button></div>
      </form>`;
  }

  function checkHtml(job) {
    const c = job.latest_check;
    const result = c ? `
      <div class="fin-section-title">Payslip ${esc(longDate(c.date))} ${c.matches ? '· matches your setup' : '· differences found'}</div>
      <table class="fin-payslip">
        <thead><tr><th></th><th scope="col">Payslip</th><th scope="col">Calculated</th><th scope="col">Difference</th></tr></thead>
        <tbody>${c.rows.map((r) => `
          <tr class="${r.flag ? 'fin-payslip-flag' : ''}"><th scope="row">${esc(SLIP_FIELDS.find(([k]) => k === r.field)?.[1] || r.field)}</th>
            <td>${esc(formatPence(r.actual_pence))}</td><td>${esc(formatPence(r.expected_pence))}</td>
            <td>${r.flag ? '<span class="fin-badge fin-badge-error">' : ''}${esc(formatPence(r.difference_pence))}${r.flag ? '</span>' : ''}</td></tr>`).join('')}
        </tbody>
      </table>
      ${c.causes.map((x) => `<div class="fin-hint">${esc(x)}</div>`).join('')}` : '';
    const form = checking === job.id ? `
      <div class="memory-item fin-edit-card">
        <form class="task-form fin-inline-form" data-payslip-form novalidate>
          <div class="fin-hint">Copy the figures from a real payslip. Leave out any you don't have.</div>
          <div class="fin-form-row">
            <div class="fin-form-col"><label class="task-form-label" for="fin-slip-date">Pay date</label>
              <input id="fin-slip-date" class="task-form-input" type="date" name="pay_date" value="${esc(job.next_period?.date || '')}"></div>
            <div class="fin-form-col"><label class="task-form-label" for="fin-slip-code">Tax code on the payslip</label>
              <input id="fin-slip-code" class="task-form-input" name="tax_code" autocomplete="off" value="${esc(job.tax_code)}"></div>
          </div>
          <div class="fin-form-row fin-form-grid">${SLIP_FIELDS.map(([key, label]) => `
            <div class="fin-form-col"><label class="task-form-label" for="fin-slip-${key}">${esc(label)} (£)</label>
              <input id="fin-slip-${key}" class="task-form-input" name="${key}" inputmode="decimal" autocomplete="off"></div>`).join('')}
          </div>
          <div class="fin-form-error" role="alert" hidden></div>
          <div class="task-form-actions"><span style="flex:1"></span>
            <button type="button" class="task-btn" data-action="cancel-payslip">Cancel</button>
            <button type="submit" class="task-btn task-btn-primary">Check payslip</button></div>
        </form>
      </div>` : '';
    const history = (job.payslips || []).map((p) => `
      <div class="memory-item fin-row fin-static-row"><div class="fin-row-main">
        <span class="memory-item-title">${esc(longDate(p.pay_date))}</span>
        <span class="fin-row-sub">${p.net_pence !== null ? `net ${esc(formatPence(p.net_pence))}` : ''}${p.tax_code ? ` · ${esc(p.tax_code)}` : ''}</span></div>
        <button type="button" class="fin-link-btn" data-delete-payslip="${esc(p.id)}">Remove</button></div>`).join('');
    return `
      <div class="fin-card-head"><div class="fin-section-title">Payslip check</div><span style="flex:1"></span>
        <button type="button" class="memory-toolbar-btn" data-check="${esc(job.id)}">+ Enter a payslip</button></div>
      ${form}
      ${result || (checking === job.id ? '' : emptyHtml('Enter a real payslip to check your setup matches what your employer pays.'))}
      ${history ? `<details class="fin-chart-table"><summary>Entered payslips</summary><div class="memory-list fin-list">${history}</div></details>` : ''}`;
  }

  function jobForm(job) {
    const j = job || { frequency: 'monthly', pension_method: 'none', pension_basis: 'qualifying', student_plans: [], ni_category: 'A' };
    const methodOptions = optionsHtml(kv(data.pension_methods), j.pension_method);
    const fixed = (v) => (v === null || v === undefined ? '' : penceToInput(v));
    return `
      <form class="task-form fin-inline-form" data-job-form novalidate>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label" for="fin-job-employer">Employer</label>
            <input id="fin-job-employer" class="task-form-input" name="employer" maxlength="80" autocomplete="off" value="${esc(j.employer || '')}"></div>
          <div class="fin-form-col"><label class="task-form-label" for="fin-job-salary">Gross salary a year (£)</label>
            <input id="fin-job-salary" class="task-form-input" name="salary" inputmode="decimal" autocomplete="off" value="${j.salary_pence ? esc(penceToInput(j.salary_pence)) : ''}"></div>
        </div>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label" for="fin-job-frequency">Paid</label>
            <select id="fin-job-frequency" class="task-form-input" name="frequency">${optionsHtml(kv(data.frequencies), j.frequency)}</select></div>
          <div class="fin-form-col"><label class="task-form-label" for="fin-job-anchor">A pay date</label>
            <input id="fin-job-anchor" class="task-form-input" type="date" name="pay_anchor" value="${esc(j.pay_anchor || '')}"></div>
          <div class="fin-form-col"><label class="task-form-label" for="fin-job-account">Paid into</label>
            <select id="fin-job-account" class="task-form-input" name="account_id">${optionsHtml(accounts(), j.account_id || '', { blank: 'Choose an account' })}</select></div>
        </div>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label" for="fin-job-code">Tax code</label>
            <input id="fin-job-code" class="task-form-input" name="tax_code" autocomplete="off" placeholder="1257L" value="${esc(j.tax_code || '')}"></div>
          <div class="fin-form-col"><label class="task-form-label" for="fin-job-region">Income tax region</label>
            <select id="fin-job-region" class="task-form-input" name="region">${optionsHtml(kv(data.regions), j.region || '', { blank: 'From the tax code' })}</select></div>
          <div class="fin-form-col"><label class="task-form-label" for="fin-job-ni">NI category</label>
            <select id="fin-job-ni" class="task-form-input" name="ni_category">${optionsHtml(data.ni_categories.map((c) => ({ value: c, label: c })), j.ni_category)}</select></div>
        </div>
        <fieldset class="fin-fieldset"><legend class="task-form-label">Student loans</legend>
          <div class="fin-check-row">${PLANS.map(([value, label]) => `
            <label class="fin-check-label"><input type="checkbox" class="fin-check" name="plan" value="${value}"${j.student_plans.includes(value) ? ' checked' : ''}> ${label}</label>`).join('')}
            <label class="fin-check-label"><input type="checkbox" class="fin-check" name="postgraduate"${j.postgraduate ? ' checked' : ''}> Postgraduate</label></div>
          <div class="fin-form-row">
            <div class="fin-form-col"><label class="task-form-label" for="fin-job-sl">Student loan balance</label>
              <select id="fin-job-sl" class="task-form-input" name="sl_liability_id">${optionsHtml(studentLoans(), j.sl_liability_id || '', { blank: 'Not linked' })}</select></div>
            <div class="fin-form-col"><label class="task-form-label" for="fin-job-pg">Postgraduate loan balance</label>
              <select id="fin-job-pg" class="task-form-input" name="pg_liability_id">${optionsHtml(studentLoans(), j.pg_liability_id || '', { blank: 'Not linked' })}</select></div>
          </div>
        </fieldset>
        <fieldset class="fin-fieldset"><legend class="task-form-label">Workplace pension</legend>
          <div class="fin-form-row">
            <div class="fin-form-col"><label class="task-form-label" for="fin-job-method">Method</label>
              <select id="fin-job-method" class="task-form-input" name="pension_method">${methodOptions}</select></div>
            <div class="fin-form-col"><label class="task-form-label" for="fin-job-pension">Pension</label>
              <select id="fin-job-pension" class="task-form-input" name="pension_asset_id">${optionsHtml(pensions(), j.pension_asset_id || '', { blank: 'Not linked' })}</select></div>
          </div>
          <div class="fin-form-row" data-pension>
            <div class="fin-form-col"><label class="task-form-label" for="fin-job-ee">You pay (%)</label>
              <input id="fin-job-ee" class="task-form-input" name="pension_employee_percent" inputmode="decimal" value="${esc(j.pension_employee_percent ?? '')}"></div>
            <div class="fin-form-col"><label class="task-form-label" for="fin-job-er">Employer pays (%)</label>
              <input id="fin-job-er" class="task-form-input" name="pension_employer_percent" inputmode="decimal" value="${esc(j.pension_employer_percent ?? '')}"></div>
            <div class="fin-form-col"><label class="task-form-label">Worked out on</label>
              <div class="task-form-toggle" role="radiogroup" aria-label="Pension basis">
                <button type="button" class="task-toggle-btn${j.pension_basis === 'qualifying' ? ' active' : ''}" data-basis="qualifying">Qualifying earnings</button>
                <button type="button" class="task-toggle-btn${j.pension_basis === 'full' ? ' active' : ''}" data-basis="full">Full salary</button></div></div>
          </div>
          <details class="fin-chart-table" data-pension><summary>Fixed amounts and NI savings</summary>
            <div class="fin-form-row">
              <div class="fin-form-col"><label class="task-form-label" for="fin-job-eef">You pay a fixed £ a month</label>
                <input id="fin-job-eef" class="task-form-input" name="pension_employee_fixed" inputmode="decimal" value="${esc(fixed(j.pension_employee_fixed_pence))}"></div>
              <div class="fin-form-col"><label class="task-form-label" for="fin-job-erf">Employer pays a fixed £ a month</label>
                <input id="fin-job-erf" class="task-form-input" name="pension_employer_fixed" inputmode="decimal" value="${esc(fixed(j.pension_employer_fixed_pence))}"></div>
              <div class="fin-form-col"><label class="task-form-label" for="fin-job-pb">Employer's NI saving passed on (%)</label>
                <input id="fin-job-pb" class="task-form-input" name="ni_passback_percent" inputmode="decimal" value="${esc(j.ni_passback_percent || '')}"></div>
            </div>
          </details>
        </fieldset>
        <div class="fin-form-error" role="alert" hidden></div>
        <div class="task-form-actions">
          ${job ? '<button type="button" class="task-btn task-btn-danger" data-action="delete-job">Delete job</button>' : ''}
          <span style="flex:1"></span>
          <button type="button" class="task-btn" data-action="cancel-job">Cancel</button>
          <button type="submit" class="task-btn task-btn-primary">${job ? 'Save' : 'Add job'}</button>
        </div>
      </form>`;
  }

  function jobHtml(job) {
    if (editing === job.id) return `<div class="admin-card fin-card"><div class="fin-card-title">Edit ${esc(job.employer)}</div>${jobForm(job)}</div>`;
    const info = job.tax_code_info || {};
    return `
      <div class="admin-card fin-card" data-job="${esc(job.id)}">
        <div class="fin-card-head">
          <h2 class="fin-card-title">${esc(job.employer)}</h2>
          <span class="fin-row-sub">${esc(formatPence(job.salary_pence))} a year · ${esc(job.frequency_label)}${job.archived ? ' · archived' : ''}</span>
          <span style="flex:1"></span>
          <button type="button" class="memory-toolbar-btn" data-edit-job="${esc(job.id)}">Edit</button>
        </div>
        ${info.error ? `<div class="fin-warning">${esc(info.error)}</div>` : `<div class="fin-hint">${esc(info.explanation || '')}</div>`}
        ${(info.notes || []).map((n) => `<div class="fin-hint">${esc(n)}</div>`).join('')}
        ${payslipHtml(job)}
        ${summaryHtml(job)}
        ${changesHtml(job)}
        ${checkHtml(job)}
      </div>`;
  }

  function draw(container) {
    mounted = container;
    if (!data) {
      container.innerHTML = `<div class="admin-card fin-card">${errorText ? `<div class="fin-form-error" role="alert">${esc(errorText)}</div>` : emptyHtml('Loading…')}</div>`;
      return;
    }
    container.innerHTML = `
      ${errorText ? `<div class="fin-form-error" role="alert">${esc(errorText)}</div>` : ''}
      ${warningsHtml()}
      <div class="admin-card fin-card">
        <div class="fin-card-head"><h2 class="fin-card-title">Salary</h2><span class="fin-row-sub">${esc(data.tax_year)} UK rates${data.rules_assumed ? ' (assumed)' : ''}</span>
          <span style="flex:1"></span>
          <button type="button" class="memory-toolbar-btn" data-action="new-job">+ Add job</button></div>
        <div class="fin-hint">Tax, NI, student loan and pension are worked out from your setup. Your net pay appears in Upcoming and projections and can't be edited by hand there.</div>
        ${editing === 'new' ? jobForm(null) : ''}
        ${!data.jobs.length && editing !== 'new' ? emptyHtml('Add your job to see your take-home pay, check your payslip and project your salary.') : ''}
      </div>
      ${data.jobs.map(jobHtml).join('')}`;
    wire(container);
  }

  async function act(work, message) {
    try {
      await work();
      if (message) ui.showToast(message);
      await load();
      if (mounted) draw(mounted);
      return true;
    } catch (err) {
      return err;
    }
  }

  function showFieldsFor(form) {
    const wanted = new Set(CHANGE_FIELDS[form.kind.value] || []);
    form.querySelectorAll('[data-field]').forEach((el) => { el.hidden = !wanted.has(el.dataset.field); });
  }

  function wire(container) {
    container.querySelector('[data-action="new-job"]')?.addEventListener('click', () => { editing = 'new'; draw(container); });
    container.querySelectorAll('[data-edit-job]').forEach((b) => b.addEventListener('click', () => { editing = b.dataset.editJob; draw(container); }));
    container.querySelectorAll('[data-add-change]').forEach((b) => b.addEventListener('click', () => { addingChange = b.dataset.addChange; draw(container); }));
    container.querySelectorAll('[data-check]').forEach((b) => b.addEventListener('click', () => { checking = b.dataset.check; draw(container); }));
    container.querySelectorAll('[data-replace]').forEach((b) => b.addEventListener('click', async () => {
      const ok = await ui.styledConfirm('Archive this hand-entered salary item? Your calculated pay replaces it.', { title: 'Replace salary item', confirmText: 'Replace' });
      if (ok) await act(() => app.replaceManualSalary(b.dataset.replace), 'Replaced with calculated pay');
    }));
    container.querySelectorAll('[data-delete-change]').forEach((b) => b.addEventListener('click', () => act(() => app.deleteJobChange(b.dataset.deleteChange), 'Change removed')));
    container.querySelectorAll('[data-delete-payslip]').forEach((b) => b.addEventListener('click', () => act(() => app.deletePayslip(b.dataset.deletePayslip), 'Payslip removed')));

    const jobFormEl = container.querySelector('[data-job-form]');
    if (jobFormEl) {
      const errorBox = jobFormEl.querySelector('.fin-form-error');
      const fail = (msg) => { errorBox.textContent = msg; errorBox.hidden = !msg; };
      let basis = jobFormEl.querySelector('[data-basis].active')?.dataset.basis || 'qualifying';
      jobFormEl.querySelectorAll('[data-basis]').forEach((b) => b.addEventListener('click', () => {
        basis = b.dataset.basis;
        jobFormEl.querySelectorAll('[data-basis]').forEach((x) => x.classList.toggle('active', x === b));
      }));
      const togglePension = () => jobFormEl.querySelectorAll('[data-pension]').forEach((el) => { el.hidden = jobFormEl.pension_method.value === 'none'; });
      jobFormEl.pension_method.addEventListener('change', togglePension);
      togglePension();
      jobFormEl.querySelector('[data-action="cancel-job"]').addEventListener('click', () => { editing = null; draw(container); });
      jobFormEl.querySelector('[data-action="delete-job"]')?.addEventListener('click', async () => {
        const ok = await ui.styledConfirm('Delete this job, its changes and payslips? Its pay item stops; confirmed pay stays in your transactions.', { title: 'Delete job', confirmText: 'Delete', danger: true });
        if (!ok) return;
        const id = editing;
        editing = null;
        await act(() => app.deleteJob(id), 'Job deleted');
      });
      jobFormEl.addEventListener('submit', async (e) => {
        e.preventDefault();
        const f = jobFormEl;
        if (!f.employer.value.trim()) { fail('Enter the employer'); return; }
        if (!f.salary.value.trim()) { fail('Enter your gross salary'); return; }
        if (!f.pay_anchor.value) { fail('Choose a pay date'); return; }
        const body = {
          employer: f.employer.value.trim(), salary: f.salary.value.trim(), frequency: f.frequency.value,
          pay_anchor: f.pay_anchor.value, account_id: f.account_id.value || null, tax_code: f.tax_code.value.trim(),
          region: f.region.value || null, ni_category: f.ni_category.value,
          student_plans: [...f.querySelectorAll('[name="plan"]:checked')].map((x) => x.value),
          postgraduate: f.postgraduate.checked, sl_liability_id: f.sl_liability_id.value || null,
          pg_liability_id: f.pg_liability_id.value || null, pension_method: f.pension_method.value,
          pension_asset_id: f.pension_asset_id.value || null, pension_basis: basis,
          pension_employee_percent: f.pension_employee_percent.value.trim(), pension_employer_percent: f.pension_employer_percent.value.trim(),
          pension_employee_fixed: f.pension_employee_fixed.value.trim() || null, pension_employer_fixed: f.pension_employer_fixed.value.trim() || null,
          ni_passback_percent: f.ni_passback_percent.value.trim(),
        };
        const id = editing;
        const result = await act(async () => {
          if (id === 'new') await app.createJob(body); else await app.updateJob(id, body);
          editing = null;
        }, id === 'new' ? 'Job added' : 'Salary saved');
        if (result !== true) fail(result.message);
      });
    }

    const changeFormEl = container.querySelector('[data-change-form]');
    if (changeFormEl) {
      const errorBox = changeFormEl.querySelector('.fin-form-error');
      const fail = (msg) => { errorBox.textContent = msg; errorBox.hidden = !msg; };
      showFieldsFor(changeFormEl);
      changeFormEl.kind.addEventListener('change', () => showFieldsFor(changeFormEl));
      changeFormEl.querySelector('[data-action="cancel-change"]').addEventListener('click', () => { addingChange = null; draw(container); });
      changeFormEl.addEventListener('submit', async (e) => {
        e.preventDefault();
        const f = changeFormEl;
        const kind = f.kind.value;
        const fields = CHANGE_FIELDS[kind];
        const body = { kind, start_date: f.start_date.value };
        if (fields.includes('amount')) body.amount = f.amount.value.trim();
        if (fields.includes('percent')) body.percent = f.percent.value.trim();
        if (fields.includes('code')) body.label = f.code.value.trim();
        if (fields.includes('method')) body.label = f.new_method.value;
        if (fields.includes('end') && f.end_date.value) body.end_date = f.end_date.value;
        if (fields.includes('label') && f.label.value.trim()) body.label = f.label.value.trim();
        if (fields.includes('repeat')) body.repeat = f.repeat.checked ? 'yearly' : 'once';
        if (!body.start_date) { fail('Choose a date'); return; }
        const jobId = addingChange;
        const result = await act(async () => { await app.addJobChange(jobId, body); addingChange = null; }, 'Change added');
        if (result !== true) fail(result.message);
      });
    }

    const slipForm = container.querySelector('[data-payslip-form]');
    if (slipForm) {
      const errorBox = slipForm.querySelector('.fin-form-error');
      const fail = (msg) => { errorBox.textContent = msg; errorBox.hidden = !msg; };
      slipForm.querySelector('[data-action="cancel-payslip"]').addEventListener('click', () => { checking = null; draw(container); });
      slipForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const body = { pay_date: slipForm.pay_date.value, tax_code: slipForm.tax_code.value.trim() };
        for (const [key] of SLIP_FIELDS) if (slipForm[key].value.trim()) body[key] = slipForm[key].value.trim();
        if (!body.pay_date) { fail('Choose the pay date'); return; }
        const jobId = checking;
        const result = await act(async () => { await app.addPayslip(jobId, body); checking = null; }, 'Payslip checked');
        if (result !== true) fail(result.message);
      });
    }
  }

  return {
    async render(container) {
      mounted = container;
      draw(container);
      if (await load()) draw(mounted);
    },
    reset: () => { editing = null; addingChange = null; checking = null; },
    isEditing: () => editing !== null || addingChange !== null || checking !== null,
  };
}
