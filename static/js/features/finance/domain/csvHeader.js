/**
 * Read just enough of a bank CSV in the browser to offer column choices and
 * guess a mapping. The server does the real parsing on preview and import.
 */

const DELIMITERS = [',', ';', '\t', '|'];

/** Split one CSV line, honouring double quotes. */
export function splitCsvLine(line, delimiter = ',') {
  const cells = [];
  let cell = '';
  let quoted = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quoted) {
      if (ch === '"' && line[i + 1] === '"') { cell += '"'; i += 1; }
      else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === delimiter) { cells.push(cell.trim()); cell = ''; }
    else cell += ch;
  }
  cells.push(cell.trim());
  return cells;
}

/** First two non-empty rows of the file, split with the likeliest delimiter. */
export function sniffCsv(text) {
  const lines = String(text || '').replace(/^﻿/, '').split(/\r?\n/).filter((l) => l.trim());
  const first = lines[0] || '';
  const delimiter = DELIMITERS
    .map((d) => [d, splitCsvLine(first, d).length])
    .sort((a, b) => b[1] - a[1])[0][0];
  return {
    delimiter,
    first: splitCsvLine(first, delimiter),
    second: lines[1] ? splitCsvLine(lines[1], delimiter) : [],
    rowCount: lines.length,
  };
}

/** True when the first row looks like column names rather than data. */
export function looksLikeHeader(first) {
  const hasDate = first.some((c) => /^\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}/.test(c));
  const hasAmount = first.some((c) => /^[-+(£]?\s*£?\d[\d,]*(\.\d+)?\)?$/.test(c));
  return !(hasDate || hasAmount);
}

const GUESSES = {
  date_column: [/^(transaction |posting |value )?date$/i, /date/i],
  description_column: [/^(description|details|name|merchant|payee|narrative|reference|memo)$/i,
    /desc|detail|name|merchant|payee|narrative/i],
  amount_column: [/^(amount|value|amount \(gbp\))$/i, /amount|value/i],
  paid_in_column: [/^(paid in|money in|credit|credits|in)$/i, /paid in|money in|credit/i],
  paid_out_column: [/^(paid out|money out|debit|debits|out)$/i, /paid out|money out|debit/i],
};

function pick(header, patterns, taken) {
  for (const pattern of patterns) {
    const found = header.find((h) => pattern.test(h) && !taken.has(h));
    if (found) return found;
  }
  return '';
}

/** Best-guess mapping from header names; empty strings where unsure. */
export function guessMapping(header) {
  const taken = new Set();
  const out = {};
  for (const key of ['date_column', 'description_column', 'paid_in_column', 'paid_out_column', 'amount_column']) {
    out[key] = pick(header, GUESSES[key], taken);
    if (out[key]) taken.add(out[key]);
  }
  const split = out.paid_in_column && out.paid_out_column;
  return {
    has_header: true,
    date_column: out.date_column,
    description_column: out.description_column,
    amount_mode: split ? 'split' : 'signed',
    amount_column: split ? '' : out.amount_column,
    paid_in_column: split ? out.paid_in_column : '',
    paid_out_column: split ? out.paid_out_column : '',
    date_format: 'dd/mm/yyyy',
    invert_amounts: false,
  };
}
