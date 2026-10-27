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
  };
}
