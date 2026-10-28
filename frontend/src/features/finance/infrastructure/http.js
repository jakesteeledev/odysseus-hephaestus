/** HTTP adapter for /api/finance. Throws an Error carrying the server's message on failure. */

export function createFinanceApi({ request }) {
  async function call(method, path, body) {
    const init = { method, credentials: 'same-origin', headers: {} };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const response = await request(`/api/finance${path}`, init);
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    if (!response.ok) {
      const detail = data && (typeof data.detail === 'string' ? data.detail : null);
      const error = new Error(detail || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  const qs = (params) => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params || {})) {
      if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
    }
    const text = search.toString();
    return text ? `?${text}` : '';
  };

  return {
    meta: () => call('GET', '/meta'),
    accounts: (includeArchived = false) => call('GET', `/accounts${qs({ include_archived: includeArchived || undefined })}`),
    createAccount: (body) => call('POST', '/accounts', body),
    updateAccount: (id, body) => call('PATCH', `/accounts/${encodeURIComponent(id)}`, body),
    deleteAccount: (id) => call('DELETE', `/accounts/${encodeURIComponent(id)}`),
    categories: () => call('GET', '/categories'),
    createCategory: (body) => call('POST', '/categories', body),
    updateCategory: (id, body) => call('PATCH', `/categories/${encodeURIComponent(id)}`, body),
    categoryUsage: (id) => call('GET', `/categories/${encodeURIComponent(id)}/usage`),
    deleteCategory: (id) => call('DELETE', `/categories/${encodeURIComponent(id)}`),
    transactions: (params) => call('GET', `/transactions${qs(params)}`),
    createTransaction: (body) => call('POST', '/transactions', body),
    updateTransaction: (id, body) => call('PATCH', `/transactions/${encodeURIComponent(id)}`, body),
    deleteTransaction: (id) => call('DELETE', `/transactions/${encodeURIComponent(id)}`),
    createTransfer: (body) => call('POST', '/transfers', body),
    summary: (params) => call('GET', `/summary${qs(params)}`),
    exportUrl: (params) => `/api/finance/export${qs(params)}`,
    previewImport: (body) => call('POST', '/import/preview', body),
    commitImport: (body) => call('POST', '/import/commit', body),
    imports: () => call('GET', '/imports'),
    undoImport: (id) => call('POST', `/imports/${encodeURIComponent(id)}/undo`),
    rules: () => call('GET', '/rules'),
    createRule: (body) => call('POST', '/rules', body),
    updateRule: (id, body) => call('PATCH', `/rules/${encodeURIComponent(id)}`, body),
    deleteRule: (id) => call('DELETE', `/rules/${encodeURIComponent(id)}`),
    mappings: () => call('GET', '/mappings'),
    saveMapping: (body) => call('POST', '/mappings', body),
    deleteMapping: (id) => call('DELETE', `/mappings/${encodeURIComponent(id)}`),
    // v2: net worth, assets and liabilities. `collection` is 'assets' or 'liabilities'.
    networth: (asOf, person) => call('GET', `/networth${qs({ as_of: asOf, person })}`),
    items: (collection, includeArchived = false) => call('GET', `/${collection}${qs({ include_archived: includeArchived || undefined })}`),
    createItem: (collection, body) => call('POST', `/${collection}`, body),
    updateItem: (collection, id, body) => call('PATCH', `/${collection}/${encodeURIComponent(id)}`, body),
    deleteItem: (collection, id) => call('DELETE', `/${collection}/${encodeURIComponent(id)}`),
    valuations: (collection, id) => call('GET', `/${collection}/${encodeURIComponent(id)}/valuations`),
    addValuation: (collection, id, body) => call('POST', `/${collection}/${encodeURIComponent(id)}/valuations`, body),
    deleteValuation: (id) => call('DELETE', `/valuations/${encodeURIComponent(id)}`),
    contributions: (assetId) => call('GET', `/assets/${encodeURIComponent(assetId)}/contributions`),
    addContribution: (assetId, body) => call('POST', `/assets/${encodeURIComponent(assetId)}/contributions`, body),
    deleteContribution: (id) => call('DELETE', `/contributions/${encodeURIComponent(id)}`),
    linkContribution: (transactionId, assetId) => call('PUT', `/transactions/${encodeURIComponent(transactionId)}/contribution`, { asset_id: assetId || null }),
    // v2 stage 2: loans.
    loan: (liabilityId) => call('GET', `/liabilities/${encodeURIComponent(liabilityId)}/loan`),
    setSchedule: (liabilityId, body) => call('PUT', `/liabilities/${encodeURIComponent(liabilityId)}/schedule`, body),
    deleteSchedule: (liabilityId) => call('DELETE', `/liabilities/${encodeURIComponent(liabilityId)}/schedule`),
    setInterestRate: (liabilityId, rate) => call('PUT', `/liabilities/${encodeURIComponent(liabilityId)}/interest-rate`, { interest_rate: rate || null }),
    addRepayment: (liabilityId, body) => call('POST', `/liabilities/${encodeURIComponent(liabilityId)}/repayments`, body),
    deleteRepayment: (id) => call('DELETE', `/repayments/${encodeURIComponent(id)}`),
    linkPayment: (transactionId, liabilityId) => call('PUT', `/transactions/${encodeURIComponent(transactionId)}/payment`, { liability_id: liabilityId || null }),
    // v2 stage 3: recurring items.
    upcoming: (days = 30, person) => call('GET', `/upcoming${qs({ days, person })}`),
    recurring: (includeArchived = false) => call('GET', `/recurring${qs({ include_archived: includeArchived || undefined })}`),
    recurringSuggestions: () => call('GET', '/recurring/suggestions'),
    createRecurring: (body) => call('POST', '/recurring', body),
    updateRecurring: (id, body) => call('PATCH', `/recurring/${encodeURIComponent(id)}`, body),
    deleteRecurring: (id) => call('DELETE', `/recurring/${encodeURIComponent(id)}`),
    occurrence: (id, action, body) => call('POST', `/recurring/${encodeURIComponent(id)}/${action}`, body),
    makeRecurring: (transactionId, body) => call('POST', `/transactions/${encodeURIComponent(transactionId)}/make-recurring`, body),
    // v2 stage 4: budgets.
    budgets: (year, month, person) => call('GET', `/budgets${qs({ year, month, person })}`),
    setBudget: (categoryId, body) => call('PUT', `/budgets/${encodeURIComponent(categoryId)}`, body),
    // v2 stage 5: chart data.
    history: (range, person) => call('GET', `/insights/history${qs({ range, person })}`),
    monthly: (range, categoryId, person) => call('GET', `/insights/monthly${qs({ range, category_id: categoryId, person })}`),
    // v3: household members.
    members: (includeArchived = false) => call('GET', `/members${qs({ include_archived: includeArchived || undefined })}`),
    createMember: (body) => call('POST', '/members', body),
    updateMember: (id, body) => call('PATCH', `/members/${encodeURIComponent(id)}`, body),
    deleteMember: (id) => call('DELETE', `/members/${encodeURIComponent(id)}`),
    // v3 step 4: children and settings.
    children: () => call('GET', '/children'),
    settings: () => call('GET', '/settings'),
    updateSettings: (body) => call('PATCH', '/settings', body),
    // Projections and salary.
    projection: (years, scenarioIds) => call('GET', `/insights/projection${qs({ years, scenario: (scenarioIds || []).join(',') })}`),
    updateAssumptions: (body) => call('PATCH', '/projection/assumptions', body),
    createScenario: (body) => call('POST', '/projection/scenarios', body),
    updateScenario: (id, body) => call('PUT', `/projection/scenarios/${encodeURIComponent(id)}`, body),
    deleteScenario: (id) => call('DELETE', `/projection/scenarios/${encodeURIComponent(id)}`),
    salary: () => call('GET', '/salary'),
    createJob: (body) => call('POST', '/salary/jobs', body),
    updateJob: (id, body) => call('PATCH', `/salary/jobs/${encodeURIComponent(id)}`, body),
    deleteJob: (id) => call('DELETE', `/salary/jobs/${encodeURIComponent(id)}`),
    addJobChange: (jobId, body) => call('POST', `/salary/jobs/${encodeURIComponent(jobId)}/changes`, body),
    deleteJobChange: (id) => call('DELETE', `/salary/changes/${encodeURIComponent(id)}`),
    addPayslip: (jobId, body) => call('POST', `/salary/jobs/${encodeURIComponent(jobId)}/payslips`, body),
    deletePayslip: (id) => call('DELETE', `/salary/payslips/${encodeURIComponent(id)}`),
    replaceManualSalary: (recurringId) => call('POST', `/salary/replace-manual/${encodeURIComponent(recurringId)}`),
    // `collection` is 'accounts', 'assets' or 'liabilities'.
    setOwners: (collection, id, memberIds) => call('PUT', `/${collection}/${encodeURIComponent(id)}/owners`, { member_ids: memberIds }),
  };
}
