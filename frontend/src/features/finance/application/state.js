/**
 * Finance application state: the one owner of loaded data and view filters.
 * Views read `state` and call these operations; operations talk to the API
 * port and notify subscribers after each change.
 */

import { addMonths } from '../domain/money.js';

export function createFinanceState({ api, storage, now }) {
  const today = now();
  const state = {
    accounts: [],          // active + archived, with balance_pence
    totalPence: 0,         // total of active accounts
    categories: [],
    month: { year: today.getFullYear(), month: today.getMonth() + 1 },
    filters: { accountId: '', categoryId: '', direction: '', q: '' },
    transactions: [],
    summary: null,
    loading: false,
    loaded: false,
    error: '',
  };
  const listeners = new Set();
  let generation = 0;

  const notify = () => { for (const fn of listeners) { try { fn(state); } catch (err) { console.error('[finance] view update failed', err); } } };

  function subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  const activeAccounts = () => state.accounts.filter((a) => !a.archived);
  const accountById = (id) => state.accounts.find((a) => a.id === id) || null;
  const categoryById = (id) => state.categories.find((c) => c.id === id) || null;

  /** Default account for new entries: last used if still active, else the first. */
  function defaultAccountId() {
    const remembered = storage.lastAccountId();
    const active = activeAccounts();
    if (active.some((a) => a.id === remembered)) return remembered;
    return active[0]?.id || '';
  }

  async function loadBase() {
    const [accounts, categories] = await Promise.all([api.accounts(true), api.categories()]);
    state.accounts = accounts.accounts;
    state.totalPence = accounts.total_pence;
    state.categories = categories.categories;
  }

  async function loadMonth() {
    const { year, month } = state.month;
    const f = state.filters;
    const [list, summary] = await Promise.all([
      api.transactions({ year, month, account_id: f.accountId, category_id: f.categoryId,
        direction: f.direction, q: f.q }),
      api.summary({ year, month, account_id: f.accountId }),
    ]);
    return { list: list.transactions, summary };
  }

  /** Reload everything; late responses from an older refresh are dropped. */
  async function refresh() {
    const mine = ++generation;
    state.loading = true;
    state.error = '';
    notify();
    try {
      await loadBase();
      const { list, summary } = await loadMonth();
      if (mine !== generation) return;
      state.transactions = list;
      state.summary = summary;
      state.loaded = true;
    } catch (err) {
      if (mine !== generation) return;
      state.error = err.message || 'Could not load finances';
    } finally {
      if (mine === generation) {
        state.loading = false;
        notify();
      }
    }
  }

  async function refreshMonth() {
    const mine = ++generation;
    try {
      const { list, summary } = await loadMonth();
      if (mine !== generation) return;
      state.transactions = list;
      state.summary = summary;
      state.error = '';
    } catch (err) {
      if (mine !== generation) return;
      state.error = err.message || 'Could not load transactions';
    }
    notify();
  }

  function shiftMonth(delta) {
    state.month = addMonths(state.month, delta);
    return refreshMonth();
  }

  function goToMonth(year, month) {
    state.month = { year, month };
    return refreshMonth();
  }

  function setFilters(changes) {
    Object.assign(state.filters, changes);
    return refreshMonth();
  }

  /** Run a write, then reload. Re-throws so the caller can show the error. */
  async function mutate(work) {
    const result = await work();
    await refresh();
    return result;
  }

  return {
    state,
    subscribe,
    activeAccounts,
    accountById,
    categoryById,
    defaultAccountId,
    refresh,
    refreshMonth,
    shiftMonth,
    goToMonth,
    setFilters,
    addTransaction: (body) => mutate(async () => {
      const tx = await api.createTransaction(body);
      storage.rememberAccount(body.account_id);
      return tx;
    }),
    addTransfer: (body) => mutate(async () => {
      const pair = await api.createTransfer(body);
      storage.rememberAccount(body.from_account_id);
      return pair;
    }),
    updateTransaction: (id, body) => mutate(() => api.updateTransaction(id, body)),
    deleteTransaction: (id) => mutate(() => api.deleteTransaction(id)),
    createAccount: (body) => mutate(() => api.createAccount(body)),
    updateAccount: (id, body) => mutate(() => api.updateAccount(id, body)),
    deleteAccount: (id) => mutate(() => api.deleteAccount(id)),
    createCategory: (body) => mutate(() => api.createCategory(body)),
    updateCategory: (id, body) => mutate(() => api.updateCategory(id, body)),
    deleteCategory: (id) => mutate(() => api.deleteCategory(id)),
    categoryUsage: (id) => api.categoryUsage(id),
    commitImport: (body) => mutate(() => api.commitImport(body)),
    undoImport: (id) => mutate(() => api.undoImport(id)),
    // Import helpers, rules and saved mappings change no balances, so they
    // do not trigger a full reload.
    previewImport: (body) => api.previewImport(body),
    listImports: () => api.imports().then((r) => r.imports),
    listRules: () => api.rules().then((r) => r.rules),
    createRule: (body) => api.createRule(body),
    updateRule: (id, body) => api.updateRule(id, body),
    deleteRule: (id) => api.deleteRule(id),
    listMappings: () => api.mappings().then((r) => r.mappings),
    saveMapping: (body) => api.saveMapping(body),
    deleteMapping: (id) => api.deleteMapping(id),
    exportUrl: (params) => api.exportUrl(params),
    lastTab: () => storage.lastTab(),
    rememberTab: (tab) => storage.rememberTab(tab),
  };
}
