/** Per-browser conveniences for finance. Never holds finance data itself. */

export const LAST_ACCOUNT_KEY = 'odysseus-finance-last-account';
export const LAST_TAB_KEY = 'odysseus-finance-tab';
export const CHART_RANGE_KEY = 'odysseus-finance-chart-range';
export const PERSON_KEY = 'odysseus-finance-person';

export function createFinanceStorage({ storage }) {
  const get = (key) => { try { return storage ? storage.getItem(key) : null; } catch { return null; } };
  const set = (key, value) => { try { if (storage) storage.setItem(key, value); } catch { /* storage unavailable */ } };
  return {
    lastAccountId: () => get(LAST_ACCOUNT_KEY) || '',
    rememberAccount: (id) => { if (id) set(LAST_ACCOUNT_KEY, id); },
    lastTab: () => get(LAST_TAB_KEY) || '',
    rememberTab: (tab) => set(LAST_TAB_KEY, tab),
    chartRange: () => (['6m', '1y', 'all'].includes(get(CHART_RANGE_KEY)) ? get(CHART_RANGE_KEY) : ''),
    rememberChartRange: (value) => set(CHART_RANGE_KEY, value),
    // Whose figures to show: a member id, or '' for the whole household.
    person: () => get(PERSON_KEY) || '',
    rememberPerson: (id) => set(PERSON_KEY, id || ''),
  };
}
