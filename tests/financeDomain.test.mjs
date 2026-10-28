import test from 'node:test';
import assert from 'node:assert/strict';
import {
  addMonths, formatDirected, formatPence, monthBounds, monthLabel, parsePence, penceToInput, sharePence, splitPence,
} from '../frontend/src/features/finance/domain/money.js';
import { guessMapping, looksLikeHeader, sniffCsv, splitCsvLine } from '../frontend/src/features/finance/domain/csvHeader.js';

test('parsePence reads pounds into integer pence like the server', () => {
  const cases = [
    ['12.50', 1250], ['12.5', 1250], ['12', 1200], ['.99', 99], ['0.005', 1], ['£1,234.56', 123456],
    ['-3.07', -307], ['(4.99)', -499], ['4.99-', -499], ['+7.10', 710], [' 2 ', 200], ['0.10', 10],
    ['19.99', 1999], ['0.995', 100],
  ];
  for (const [raw, pence] of cases) assert.equal(parsePence(raw), pence, raw);
  for (const bad of ['', 'abc', '1.2.3', '£', '--5', null, '1e5']) assert.equal(parsePence(bad), null, String(bad));
});

test('formatting pence', () => {
  assert.equal(formatPence(123456), '£1,234.56');
  assert.equal(formatPence(-1250), '-£12.50');
  assert.equal(formatPence(5), '£0.05');
  assert.equal(penceToInput(1250), '12.50');
  assert.equal(penceToInput(-7), '-0.07');
  assert.equal(formatDirected(410, 'out'), '-£4.10');
  assert.equal(formatDirected(2000, 'in'), '+£20.00');
});

test('month helpers', () => {
  assert.deepEqual(addMonths({ year: 2026, month: 12 }, 1), { year: 2027, month: 1 });
  assert.deepEqual(addMonths({ year: 2026, month: 1 }, -1), { year: 2025, month: 12 });
  assert.deepEqual(addMonths({ year: 2026, month: 10 }, -14), { year: 2025, month: 8 });
  assert.equal(monthLabel({ year: 2026, month: 10 }), 'October 2026');
  assert.deepEqual(monthBounds({ year: 2028, month: 2 }), { start: '2028-02-01', end: '2028-02-29' });
});

test('CSV header sniffing and mapping guesses', () => {
  assert.deepEqual(splitCsvLine('a,"b, c","say ""hi"""'), ['a', 'b, c', 'say "hi"']);
  const monzo = sniffCsv('﻿Date,Name,Amount\r\n01/10/2026,Tesco,-2.50\r\n');
  assert.equal(monzo.delimiter, ',');
  assert.deepEqual(monzo.first, ['Date', 'Name', 'Amount']);
  assert.equal(looksLikeHeader(monzo.first), true);
  assert.equal(looksLikeHeader(monzo.second), false);
  const guess = guessMapping(monzo.first);
  assert.equal(guess.amount_mode, 'signed');
  assert.equal(guess.date_column, 'Date');
  assert.equal(guess.description_column, 'Name');
  assert.equal(guess.amount_column, 'Amount');
  const split = guessMapping(sniffCsv('Transaction Date;Description;Paid In;Paid Out\n').first);
  assert.equal(split.amount_mode, 'split');
  assert.equal(split.paid_in_column, 'Paid In');
  assert.equal(split.paid_out_column, 'Paid Out');
  assert.equal(split.date_column, 'Transaction Date');
});

test('chart helpers: compact axis money, clean ticks and month labels', async () => {
  const { compactPence, niceTicks, monthShort } = await import('../frontend/src/features/finance/ui/charts.js');
  assert.equal(compactPence(0), '£0');
  assert.equal(compactPence(85000), '£850');
  assert.equal(compactPence(120000), '£1.2K');
  assert.equal(compactPence(1500000), '£15K');
  assert.equal(compactPence(120000000), '£1.2M');
  assert.equal(compactPence(-200000), '-£2K');
  const ticks = niceTicks(-300000, 1200000);
  assert.ok(ticks.includes(0) && ticks[0] <= -300000 && ticks[ticks.length - 1] >= 1200000);
  const steps = new Set(ticks.slice(1).map((t, i) => t - ticks[i]));
  assert.equal(steps.size, 1);
  assert.deepEqual(niceTicks(0, 0).slice(0, 1), [0]);
  assert.equal(monthShort('2026-10'), 'Oct');
  assert.equal(monthShort('2027-01', true), 'Jan 27');
});

test('splitting pence between owners matches the server', () => {
  assert.deepEqual(splitPence(30001, 2), [15001, 15000]);
  assert.deepEqual(splitPence(1001, 3), [335, 333, 333]);
  assert.deepEqual(splitPence(-1001, 3), [-335, -333, -333]);
  assert.deepEqual(splitPence(2, 3), [2, 0, 0]);
  assert.deepEqual(splitPence(500, 1), [500]);
  for (let amount = -40; amount <= 40; amount += 1) {
    for (let count = 1; count <= 5; count += 1) assert.equal(splitPence(amount, count).reduce((a, b) => a + b, 0), amount);
  }
  assert.equal(sharePence(1001, ['a', 'b', 'c'], 'a'), 335);
  assert.equal(sharePence(1001, ['a', 'b', 'c'], 'c'), 333);
  assert.equal(sharePence(1001, ['a', 'b'], 'z'), 0);
  assert.equal(sharePence(1001, ['a', 'b'], ''), 1001);
});
