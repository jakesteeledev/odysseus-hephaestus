/** Pence <-> text and month helpers for the finance tracker. Pure: no DOM or clock. */

/** Signed integer pence from text like "12.50", "£1,234.56", "-3" or "(4.99)"; null if unreadable. */
export function parsePence(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? Math.round(value * 100) : null;
  let text = String(value ?? '').trim();
  if (!text) return null;
  let negative = false;
  if (text.startsWith('(') && text.endsWith(')')) { negative = true; text = text.slice(1, -1); }
  if (text.endsWith('-')) { negative = true; text = text.slice(0, -1); }
  text = text.replace(/[£,\s ]/g, '').replace(/^GBP/i, '');
  if (text.startsWith('+')) text = text.slice(1);
  if (text.startsWith('-')) { negative = !negative; text = text.slice(1); }
  if (!/^(\d+(\.\d*)?|\.\d+)$/.test(text)) return null;
  const [whole, frac = ''] = text.split('.');
  const pennies = (frac + '00').slice(0, 2);
  const roundUp = (frac[2] || '0') >= '5' ? 1 : 0;
  const pence = Number(whole || '0') * 100 + Number(pennies) + roundUp;
  return negative ? -pence : pence;
}

/** "£1,234.56" / "-£12.50". */
export function formatPence(pence) {
  const value = Math.trunc(Number(pence) || 0);
  const sign = value < 0 ? '-' : '';
  const abs = Math.abs(value);
  const pounds = Math.floor(abs / 100).toLocaleString('en-GB');
  return `${sign}£${pounds}.${String(abs % 100).padStart(2, '0')}`;
}

/**
 * Split pence into equal shares that add up exactly; leftover pence go to
 * the first share. Mirrors the server (src/finance/members.split_pence).
 */
export function splitPence(amount, count) {
  if (count <= 0) return [];
  const value = Math.trunc(Number(amount) || 0);
  const sign = value < 0 ? -1 : 1;
  const abs = Math.abs(value);
  const base = Math.floor(abs / count);
  const shares = new Array(count).fill(base);
  shares[0] += abs - base * count;
  return shares.map((s) => (s === 0 ? 0 : sign * s));
}

/** A member's share of an amount owned by `ownerIds` (in split order); the whole amount with no member. */
export function sharePence(amount, ownerIds, memberId) {
  if (!memberId) return Math.trunc(Number(amount) || 0);
  const index = (ownerIds || []).indexOf(memberId);
  return index < 0 ? 0 : splitPence(amount, ownerIds.length)[index];
}

/** Plain pounds for form fields: 1250 -> "12.50". */
export function penceToInput(pence) {
  const value = Math.trunc(Number(pence) || 0);
  const sign = value < 0 ? '-' : '';
  const abs = Math.abs(value);
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, '0')}`;
}

/** Signed display for a transaction: "+£20.00" for money in, "-£4.10" for money out. */
export function formatDirected(pence, direction) {
  return (direction === 'in' ? '+' : '-') + formatPence(Math.abs(pence));
}

/** "2026-10-27" for a Date, using its local calendar day. */
export function isoDay(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

/** Shift a {year, month} (month 1-12) by `delta` months. */
export function addMonths({ year, month }, delta) {
  const index = year * 12 + (month - 1) + delta;
  return { year: Math.floor(index / 12), month: (index % 12) + 1 };
}

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
  'September', 'October', 'November', 'December'];

export function monthLabel({ year, month }) {
  return `${MONTHS[month - 1]} ${year}`;
}

/** First and last ISO day of a month. */
export function monthBounds({ year, month }) {
  const last = new Date(year, month, 0).getDate();
  const mm = String(month).padStart(2, '0');
  return { start: `${year}-${mm}-01`, end: `${year}-${mm}-${String(last).padStart(2, '0')}` };
}

/** "Mon 27 Oct" style label for an ISO day. */
export function dayLabel(iso) {
  const [y, m, d] = String(iso).split('-').map(Number);
  if (!y || !m || !d) return String(iso || '');
  const date = new Date(y, m - 1, d);
  return date.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
}

export const ACCOUNT_TYPE_LABELS = {
  current: 'Current',
  savings: 'Savings',
  credit_card: 'Credit card',
  cash: 'Cash',
};
