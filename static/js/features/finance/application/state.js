/**
 * Finance application state: the one owner of loaded data and view filters.
 * Views read `state` and call these operations; operations talk to the API
 * port and notify subscribers after each change.
 */

import { addMonths, sharePence } from '../domain/money.js';

export function createFinanceState({ api, storage, now }) {
  const today = now();
  const state = {
    accounts: [],          // active + archived, with balance_pence
    totalPence: 0,         // total of active accounts
    categories: [],
    networth: null,        // assets - liabilities, grouped by type
    assets: [],            // including archived, with current values
    liabilities: [],
    members: [],           // household members, primary first, including archived
    children: null,        // the Children section: each child, Junior ISAs, allowance, pocket money
    person: storage.person ? storage.person() : '', // whose figures: a member id, or '' for the household
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
  const itemById = (kind, id) => (kind === 'asset' ? state.assets : state.liabilities).find((i) => i.id === id) || null;
  const collectionOf = (kind) => (kind === 'asset' ? 'assets' : 'liabilities');
  const categoryById = (id) => state.categories.find((c) => c.id === id) || null;
  const memberById = (id) => state.members.find((m) => m.id === id) || null;
  /** The member whose figures are shown, or null for the whole household. */
  const personMember = () => (state.person ? memberById(state.person) : null);
  /** The person filter as an API parameter (undefined for the household). */
  const personParam = () => state.person || undefined;
  /** True when something with these owners belongs in the current view. */
  const ownsIt = (ownerIds) => !state.person || (ownerIds || []).includes(state.person);
  /** The shown person's share of an amount (the whole amount for the household). */
  const shareOf = (amount, ownerIds) => sharePence(amount, ownerIds, state.person);

  /** Default account for new entries: last used if still active, else the first. */
  function defaultAccountId() {
    const remembered = storage.lastAccountId();
    const active = activeAccounts();
    if (active.some((a) => a.id === remembered)) return remembered;
    return active[0]?.id || '';
  }

  async function loadBase() {
    const members = await api.members(true);
    state.members = members.members;
    // A remembered person who has since been deleted falls back to the household.
    // So does a household of one, where the person switcher is hidden.
    if (state.person && (!memberById(state.person) || state.members.length < 2)) state.person = '';
    const hasChildren = state.members.some((m) => m.relationship === 'child');
    const [accounts, categories, networth, assets, liabilities, children] = await Promise.all([
      api.accounts(true), api.categories(), api.networth(undefined, personParam()), api.items('assets', true),
      api.items('liabilities', true), hasChildren ? api.children() : Promise.resolve(null),
    ]);
    state.children = children;
    state.accounts = accounts.accounts;
    state.totalPence = accounts.total_pence;
    state.categories = categories.categories;
    state.networth = networth;
    state.assets = assets.assets;
    state.liabilities = liabilities.liabilities;
  }

  async function loadMonth() {
    const { year, month } = state.month;
    const f = state.filters;
    const [list, summary] = await Promise.all([
      api.transactions({ year, month, account_id: f.accountId, category_id: f.categoryId,
        direction: f.direction, q: f.q, person: personParam() }),
      api.summary({ year, month, account_id: f.accountId, person: personParam() }),
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

  /** Show one member's figures ('' for the household); remembered on this device. */
  function setPerson(id) {
    state.person = id && memberById(id) ? id : '';
    storage.rememberPerson?.(state.person);
    // The account filter may point at an account the new person doesn't own.
    if (state.filters.accountId && !ownsIt(accountById(state.filters.accountId)?.owner_ids)) state.filters.accountId = '';
    return refresh();
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
    memberById,
    personMember,
    ownsIt,
    shareOf,
    setPerson,
    defaultAccountId,
    refresh,
    refreshMonth,
    shiftMonth,
    goToMonth,
    setFilters,
    addTransaction: (body, { contributionAssetId, paymentLiabilityId } = {}) => mutate(async () => {
      const tx = await api.createTransaction(body);
      storage.rememberAccount(body.account_id);
      if (contributionAssetId) await api.linkContribution(tx.id, contributionAssetId);
      if (paymentLiabilityId) await api.linkPayment(tx.id, paymentLiabilityId);
      return tx;
    }),
    linkPayment: (transactionId, liabilityId) => mutate(() => api.linkPayment(transactionId, liabilityId)),
    loanDetails: (liabilityId) => api.loan(liabilityId),
    setSchedule: (liabilityId, body) => mutate(() => api.setSchedule(liabilityId, body)),
    deleteSchedule: (liabilityId) => mutate(() => api.deleteSchedule(liabilityId)),
    setInterestRate: (liabilityId, rate) => mutate(() => api.setInterestRate(liabilityId, rate)),
    addRepayment: (liabilityId, body) => mutate(() => api.addRepayment(liabilityId, body)),
    deleteRepayment: (id) => mutate(() => api.deleteRepayment(id)),
    // Recurring items: confirm/match/undo change transactions, so they reload everything.
    upcoming: (days) => api.upcoming(days, personParam()),
    listRecurring: () => api.recurring(true).then((r) => r.recurring),
    recurringSuggestions: () => api.recurringSuggestions().then((r) => r.suggestions),
    createRecurring: (body) => mutate(() => api.createRecurring(body)),
    updateRecurring: (id, body) => mutate(() => api.updateRecurring(id, body)),
    deleteRecurring: (id) => mutate(() => api.deleteRecurring(id)),
    occurrence: (id, action, body) => mutate(() => api.occurrence(id, action, body)),
    makeRecurring: (transactionId, body) => mutate(() => api.makeRecurring(transactionId, body)),
    budgetStatus: () => api.budgets(state.month.year, state.month.month, personParam()),
    setBudget: (categoryId, body) => api.setBudget(categoryId, body),
    netWorthHistory: (range) => api.history(range, personParam()).then((r) => r.points),
    monthlyTotals: (range, categoryId) => api.monthly(range, categoryId, personParam()).then((r) => r.points),
    linkContribution: (transactionId, assetId) => mutate(() => api.linkContribution(transactionId, assetId)),
    itemById,
    createItem: (kind, body) => mutate(() => api.createItem(collectionOf(kind), body)),
    updateItem: (kind, id, body) => mutate(() => api.updateItem(collectionOf(kind), id, body)),
    saveItem: (kind, id, body, ownerIds) => mutate(async () => {
      const item = await api.updateItem(collectionOf(kind), id, body);
      if (ownerIds) await api.setOwners(collectionOf(kind), id, ownerIds);
      return item;
    }),
    deleteItem: (kind, id) => mutate(() => api.deleteItem(collectionOf(kind), id)),
    listValuations: (kind, id) => api.valuations(collectionOf(kind), id).then((r) => r.valuations),
    addValuation: (kind, id, body) => mutate(() => api.addValuation(collectionOf(kind), id, body)),
    deleteValuation: (id) => mutate(() => api.deleteValuation(id)),
    listContributions: (assetId) => api.contributions(assetId).then((r) => r.contributions),
    addContribution: (assetId, body) => mutate(() => api.addContribution(assetId, body)),
    deleteContribution: (id) => mutate(() => api.deleteContribution(id)),
    addTransfer: (body) => mutate(async () => {
      const pair = await api.createTransfer(body);
      storage.rememberAccount(body.from_account_id);
      return pair;
    }),
    updateTransaction: (id, body) => mutate(() => api.updateTransaction(id, body)),
    deleteTransaction: (id) => mutate(() => api.deleteTransaction(id)),
    createAccount: (body) => mutate(() => api.createAccount(body)),
    updateAccount: (id, body) => mutate(() => api.updateAccount(id, body)),
    /** Save an account's details and, when given, its owners, with one reload. */
    saveAccount: (id, body, ownerIds) => mutate(async () => {
      const account = await api.updateAccount(id, body);
      if (ownerIds) await api.setOwners('accounts', id, ownerIds);
      return account;
    }),
    deleteAccount: (id) => mutate(() => api.deleteAccount(id)),
    createCategory: (body) => mutate(() => api.createCategory(body)),
    updateCategory: (id, body) => mutate(() => api.updateCategory(id, body)),
    deleteCategory: (id) => mutate(() => api.deleteCategory(id)),
    categoryUsage: (id) => api.categoryUsage(id),
    createMember: (body) => mutate(() => api.createMember(body)),
    updateMember: (id, body) => mutate(() => api.updateMember(id, body)),
    deleteMember: (id) => mutate(() => api.deleteMember(id)),
    settings: () => api.settings(),
    updateSettings: (body) => mutate(() => api.updateSettings(body)),
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
    chartRange: () => storage.chartRange(),
    rememberChartRange: (value) => storage.rememberChartRange(value),
  };
}
