/** Finance composition and public API: openFinance / closeFinance / toggleFinance / isFinanceOpen. */
import { createFinanceApi } from './infrastructure/http.js';
import { createFinanceStorage } from './infrastructure/storage.js';
import { createFinanceState } from './application/state.js';
import { createEntryForm } from './ui/entryForm.js';
import { createAccountsView } from './ui/accountsView.js';
import { createItemView } from './ui/itemView.js';
import { createLoanSection } from './ui/loanSection.js';
import { createOverviewView } from './ui/overviewView.js';
import { createMonthNav, createSummaryView, createTransactionsView } from './ui/transactionsView.js';
import { createImportView } from './ui/importView.js';
import { createSettingsView } from './ui/settingsView.js';
import { createPanel } from './ui/panel.js';
import uiModule from '/static/js/ui.js?v=20260916largetoolscroll1';
import * as Modals from '/static/js/modalManager.js';
import { makeWindowDraggable } from '/static/js/windowDrag.js';
import spinnerModule from '/static/js/spinner.js';

const now = () => new Date();
const app = createFinanceState({
  api: createFinanceApi({ request: (...args) => fetch(...args) }),
  storage: createFinanceStorage({ storage: typeof localStorage === 'undefined' ? undefined : localStorage }),
  now,
});
const ui = {
  showToast: (msg) => uiModule.showToast(msg),
  showError: (msg) => uiModule.showError(msg),
  styledConfirm: (msg, opts) => uiModule.styledConfirm(msg, opts),
  styledPrompt: (msg, opts) => uiModule.styledPrompt(msg, opts),
};
const entryForm = createEntryForm({ app, ui, now });
const monthNav = createMonthNav({ app });
const views = {
  overview: createOverviewView({
    app,
    accountsView: createAccountsView({ app, ui }),
    itemView: createItemView({ app, ui, now, loanSection: createLoanSection({ app, ui, now }) }),
  }),
  transactions: createTransactionsView({ app, entryForm, monthNav }),
  summary: createSummaryView({ app, monthNav }),
  add: (container, afterSave) => entryForm.render(container, { onDone: afterSave }),
  import: createImportView({ app, ui }),
  settings: createSettingsView({ app, ui, now }),
};
const panel = createPanel({ app, views, Modals, makeWindowDraggable, spinner: spinnerModule });

export function openFinance(options) { panel.open(options); }
export function closeFinance() { panel.close(); }
export function toggleFinance() { panel.toggle(); }
export function isFinanceOpen() { return panel.isOpen(); }

export default { openFinance, closeFinance, toggleFinance, isFinanceOpen };
