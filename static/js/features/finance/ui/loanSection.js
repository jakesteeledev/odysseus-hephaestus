/**
 * Loan part of the liability editor: summary and projection, missed-payment
 * warning, repayment schedule (not for student loans), interest rate for
 * student loans, and payments with their interest/capital split.
 */

import { formatPence, isoDay, penceToInput } from '../domain/money.js';
import { balanceHtml, emptyHtml, esc } from './dom.js';

export function createLoanSection({ app, ui, now }) {
  function statsHtml(loan) {
    const p = loan.projection;
    const stats = [`<div class="fin-stat"><span class="fin-stat-label">Owed now</span>${balanceHtml(loan.balance_pence)}</div>`];
    if (p && loan.balance_pence > 0 && p.payoff_date) {
      stats.push(
        `<div class="fin-stat"><span class="fin-stat-label">Next payment</span><span class="fin-amount">${esc(formatPence(p.monthly_payment_pence))}</span><span class="fin-row-sub">${esc(p.next_payment_date)}</span></div>`,
        `<div class="fin-stat"><span class="fin-stat-label">Paid off</span><span class="fin-amount">${esc(p.payoff_date)}</span><span class="fin-row-sub">${p.remaining_payments} payment${p.remaining_payments === 1 ? '' : 's'} left</span></div>`,
        `<div class="fin-stat"><span class="fin-stat-label">Interest still to pay</span>${balanceHtml(p.interest_remaining_pence)}</div>`);
    }
    if (loan.total_paid_pence) {
      stats.push(`<div class="fin-stat"><span class="fin-stat-label">Paid so far</span>${balanceHtml(loan.total_paid_pence)}<span class="fin-row-sub">${esc(formatPence(loan.interest_paid_pence))} interest</span></div>`);
    }
    return `<div class="fin-stats">${stats.join('')}</div>`;
  }

  function warningsHtml(loan) {
    const out = [];
    if (loan.missed_payments?.length) {
      out.push(`<div class="fin-warning" role="status">No payment found near ${loan.missed_payments.map(esc).join(', ')}. Link the payment from its transaction, or record it below.</div>`);
    }
    if (loan.projection && loan.projection.pays_off === false) {
      out.push('<div class="fin-warning" role="status">At this payment the debt never pays off: the payment is less than the monthly interest.</div>');
    }
    return out.join('');
  }

  function scheduleFormHtml(loan) {
    const s = loan.schedule;
    return `
      <form class="task-form" data-schedule-form>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label">Start date</label>
            <input class="task-form-input" type="date" name="start_date" value="${esc(s?.start_date || isoDay(now()))}" required></div>
          <div class="fin-form-col"><label class="task-form-label">Starting balance (£)</label>
            <input class="task-form-input" name="start_balance" inputmode="decimal" autocomplete="off" value="${s ? esc(penceToInput(s.start_balance_pence)) : ''}"></div>
          <div class="fin-form-col"><label class="task-form-label">Interest rate (APR %)</label>
            <input class="task-form-input" name="apr" inputmode="decimal" autocomplete="off" value="${s ? esc(String(s.apr_percent)) : ''}" placeholder="0 for interest-free"></div>
        </div>
        <div class="fin-form-row">
          <div class="fin-form-col"><label class="task-form-label">Term (months)</label>
            <input class="task-form-input" name="term_months" inputmode="numeric" autocomplete="off" value="${esc(s?.term_months ?? '')}"></div>
          <div class="fin-form-col"><label class="task-form-label">Monthly payment (£, optional)</label>
            <input class="task-form-input" name="monthly_payment" inputmode="decimal" autocomplete="off" value="${s?.monthly_payment_pence ? esc(penceToInput(s.monthly_payment_pence)) : ''}" placeholder="worked out from the term"></div>
          <div class="fin-form-col"><label class="task-form-label">Payment day</label>
            <input class="task-form-input" name="payment_day" inputmode="numeric" autocomplete="off" value="${esc(s?.payment_day ?? '')}" placeholder="1-31"></div>
        </div>
        <label class="task-form-label">Final (balloon) payment, e.g. PCP (£, optional)</label>
        <input class="task-form-input" name="balloon" inputmode="decimal" autocomplete="off" value="${s?.balloon_pence ? esc(penceToInput(s.balloon_pence)) : ''}">
        <div class="fin-hint">Give a term or a monthly payment. A statement balance recorded below corrects the calculated balance from that date.</div>
        <div class="task-form-actions">
          ${s ? '<button type="button" class="task-btn task-btn-danger" data-action="remove-schedule">Remove schedule</button><span style="flex:1"></span>' : ''}
          <button type="submit" class="task-btn task-btn-primary">${s ? 'Save schedule' : 'Add schedule'}</button>
        </div>
      </form>`;
  }

  function rateFormHtml(loan, item) {
    return `
      <form class="fin-form-row fin-snapshot-add" data-rate-form>
        <div class="fin-form-col"><input class="task-form-input" name="rate" inputmode="decimal" autocomplete="off"
          value="${item.interest_rate_percent ?? ''}" placeholder="Interest rate %" aria-label="Interest rate"></div>
        <button type="submit" class="task-btn">Save rate</button>
      </form>
      <div class="fin-hint">Student loans are repaid through payroll, so there is no schedule. Record balances from your SLC statements and repayments here.</div>`;
  }

  function paymentsHtml(loan) {
    return `
      <form class="fin-form-row fin-snapshot-add" data-repayment-form>
        <div class="fin-form-col"><input class="task-form-input" name="amount" inputmode="decimal" autocomplete="off" placeholder="Amount (£)" aria-label="Payment amount"></div>
        <div class="fin-form-col"><input class="task-form-input" type="date" name="date" value="${isoDay(now())}" aria-label="Date"></div>
        <div class="fin-form-col"><input class="task-form-input" name="note" maxlength="200" placeholder="Note" aria-label="Note"></div>
        <button type="submit" class="task-btn">Add payment</button>
      </form>
      <div class="fin-hint">Payments from your accounts are linked from the transaction (Payment towards).</div>
      <div class="memory-list fin-list">${loan.payments.map((p) => `
        <div class="memory-item fin-row fin-static-row">
          <div class="fin-row-main">
            <span class="memory-item-title">${esc(formatPence(p.amount_pence))} <span class="fin-row-sub">on ${esc(p.date)}</span></span>
            <span class="fin-row-sub">${p.balance_after_pence === null ? 'before the schedule started'
              : `interest ${esc(formatPence(p.interest_pence))} · capital ${esc(formatPence(p.capital_pence))} · then owed ${esc(formatPence(p.balance_after_pence))}`}${p.transaction_id ? ' · from a transaction' : ''}${p.note ? ` · ${esc(p.note)}` : ''}</span>
          </div>
          ${p.transaction_id ? '' : `<button type="button" class="memory-item-btn" data-delete-repayment="${esc(p.id)}" title="Delete payment" aria-label="Delete payment">&times;</button>`}
        </div>`).join('') || emptyHtml('No payments recorded yet.')}</div>`;
  }

  async function render(el, item, onChange) {
    el.innerHTML = emptyHtml('Loading…');
    let loan;
    try {
      loan = await app.loanDetails(item.id);
    } catch (err) {
      el.innerHTML = `<div class="fin-form-error" role="alert">${esc(err.message)}</div>`;
      return;
    }
    const student = item.type === 'student_loan';
    el.innerHTML = `
      ${statsHtml(loan)}
      ${warningsHtml(loan)}
      <h3 class="fin-section-title">${student ? 'Interest' : 'Repayment schedule'}</h3>
      ${student ? rateFormHtml(loan, item) : scheduleFormHtml(loan)}
      <h3 class="fin-section-title">Payments</h3>
      ${paymentsHtml(loan)}`;
    wire(el, item, loan, onChange);
  }

  function wire(el, item, loan, onChange) {
    const scheduleForm = el.querySelector('[data-schedule-form]');
    scheduleForm?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = scheduleForm;
      const body = {
        start_date: f.start_date.value, start_balance: f.start_balance.value.trim(), apr: f.apr.value.trim() || '0',
        term_months: f.term_months.value.trim() ? Number(f.term_months.value.trim()) : null,
        monthly_payment: f.monthly_payment.value.trim() || null,
        payment_day: f.payment_day.value.trim() ? Number(f.payment_day.value.trim()) : null,
        balloon: f.balloon.value.trim() || '0',
      };
      if (!body.start_balance) { ui.showError('Enter the starting balance'); return; }
      try { await app.setSchedule(item.id, body); ui.showToast('Schedule saved'); onChange(); } catch (err) { ui.showError(err.message); }
    });
    el.querySelector('[data-action="remove-schedule"]')?.addEventListener('click', async () => {
      const ok = await ui.styledConfirm('Remove the repayment schedule? Payments and balance history are kept.',
        { title: 'Remove schedule', confirmText: 'Remove', danger: true });
      if (!ok) return;
      try { await app.deleteSchedule(item.id); ui.showToast('Schedule removed'); onChange(); } catch (err) { ui.showError(err.message); }
    });
    const rateForm = el.querySelector('[data-rate-form]');
    rateForm?.addEventListener('submit', async (e) => {
      e.preventDefault();
      try { await app.setInterestRate(item.id, rateForm.rate.value.trim()); ui.showToast('Rate saved'); onChange(); } catch (err) { ui.showError(err.message); }
    });
    const repaymentForm = el.querySelector('[data-repayment-form]');
    repaymentForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const amount = repaymentForm.amount.value.trim();
      if (!amount) return;
      try {
        await app.addRepayment(item.id, { amount, date: repaymentForm.date.value, note: repaymentForm.note.value.trim() });
        ui.showToast('Payment recorded');
        onChange();
      } catch (err) { ui.showError(err.message); }
    });
    el.querySelectorAll('[data-delete-repayment]').forEach((b) => b.addEventListener('click', async () => {
      const ok = await ui.styledConfirm('Delete this payment?', { title: 'Delete payment', confirmText: 'Delete', danger: true });
      if (!ok) return;
      try { await app.deleteRepayment(b.dataset.deleteRepayment); ui.showToast('Payment deleted'); onChange(); } catch (err) { ui.showError(err.message); }
    }));
  }

  return { render };
}
