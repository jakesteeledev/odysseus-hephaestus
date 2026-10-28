/**
 * Finance window: a Calendar-style modal (draggable window on desktop,
 * bottom sheet on phones) with tabs for accounts, transactions, summary,
 * quick add, import and settings.
 */

import { ICONS, esc } from './dom.js';
import { personBarHtml, wirePersonBar } from './ownerChips.js';

const TABS = [
  { id: 'overview', label: 'Overview', icon: ICONS.accounts },
  { id: 'transactions', label: 'Transactions', icon: ICONS.list },
  { id: 'upcoming', label: 'Upcoming', icon: ICONS.upcoming },
  { id: 'budgets', label: 'Budgets', icon: ICONS.budgets },
  { id: 'insights', label: 'Insights', icon: ICONS.summary },
  { id: 'import', label: 'Import', icon: ICONS.upload },
  { id: 'settings', label: 'Settings', icon: ICONS.settings },
];
// "add" is opened from the header button rather than a tab.
const VIEWS = new Set([...TABS.map((t) => t.id), 'add']);
// Tabs whose figures follow the Household / person switcher.
const PERSON_TABS = new Set(['overview', 'transactions', 'upcoming', 'budgets', 'insights']);
// Tab names remembered from v1.
const RENAMED = { accounts: 'overview', summary: 'insights' };
const MODAL_ID = 'finance-modal';

export function createPanel({ app, views, Modals, makeWindowDraggable, spinner }) {
  let modal = null;
  let open = false;
  const remembered = RENAMED[app.lastTab()] || app.lastTab();
  let tab = VIEWS.has(remembered) && remembered !== 'add' ? remembered : 'overview';
  let unsubscribe = null;
  let escHandler = null;

  function body() { return modal?.querySelector('.fin-body'); }

  function build() {
    if (modal) return modal;
    modal = document.createElement('div');
    modal.id = MODAL_ID;
    modal.className = 'modal';
    modal.style.display = 'none';
    modal.innerHTML = `
      <div class="modal-content fin-modal-content" role="dialog" aria-label="Finance">
        <div class="modal-header">
          <h4><span class="fin-title-icon">${ICONS.finance(14)}</span>Finance</h4>
          <span style="flex:1"></span>
          <button type="button" class="memory-toolbar-btn fin-add-btn" data-fin-add title="Add money in, money out or a transfer">${ICONS.add()} Add</button>
          <button class="close-btn" id="finance-close" aria-label="Close finance">✖</button>
        </div>
        <div class="memory-tabs fin-tabs" role="tablist">
          ${TABS.map((t) => `<button class="memory-tab fin-tab" data-tab="${t.id}" role="tab" aria-selected="false">
            <span class="fin-tab-icon">${t.icon()}</span><span class="fin-tab-label">${esc(t.label)}</span></button>`).join('')}
        </div>
        <div class="fin-person-slot"></div>
        <div class="modal-body fin-body"></div>
      </div>`;
    document.body.appendChild(modal);
    modal.querySelector('#finance-close').addEventListener('click', close);
    modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
    modal.querySelectorAll('.fin-tab').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
    modal.querySelector('[data-fin-add]').addEventListener('click', () => switchTab('add'));
    // Clicks on "Add an account" inside empty states jump to that tab.
    modal.addEventListener('click', (e) => {
      const go = e.target.closest('[data-fin-go]');
      if (go) switchTab(go.dataset.finGo);
    });
    makeWindowDraggable(modal, { content: modal.querySelector('.modal-content'), header: modal.querySelector('.modal-header') });
    return modal;
  }

  function markTabs() {
    modal.querySelectorAll('.fin-tab').forEach((b) => {
      const on = b.dataset.tab === tab;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }

  function renderPersonBar() {
    const slot = modal?.querySelector('.fin-person-slot');
    if (!slot) return;
    slot.innerHTML = app.state.loaded && PERSON_TABS.has(tab) ? personBarHtml(app) : '';
    wirePersonBar(slot, async (id) => {
      views.overview.reset();
      views.transactions.reset();
      views.upcoming.reset();
      views.budgets.reset();
      await app.setPerson(id);
      // The reload redraws the other tabs; Upcoming loads its own data, so draw it again.
      if (tab === 'upcoming') renderTab();
    });
  }

  function renderTab() {
    const el = body();
    if (!el) return;
    markTabs();
    renderPersonBar();
    const { state } = app;
    if (!state.loaded) {
      if (state.error) {
        el.innerHTML = `<div class="fin-empty">${esc(state.error)}</div>
          <div class="task-form-actions" style="justify-content:center"><button type="button" class="task-btn" data-fin-retry>Try again</button></div>`;
        el.querySelector('[data-fin-retry]').addEventListener('click', () => app.refresh());
      } else {
        el.innerHTML = '';
        el.appendChild(spinner.createLoadingRow('Loading…'));
      }
      return;
    }
    switch (tab) {
      case 'overview': views.overview.render(el); break;
      case 'transactions': views.transactions.render(el); break;
      case 'upcoming': views.upcoming.render(el); break;
      case 'budgets': views.budgets.render(el); break;
      case 'insights': views.insights.render(el); break;
      case 'add': views.add(el, () => renderTab()); break;  // stay ready for the next entry
      case 'import': views.import.render(el); break;
      case 'settings': views.settings.render(el); break;
      default: break;
    }
  }

  // Views that only show loaded data refresh when it changes. Add, import
  // and settings hold half-filled forms, so they are not redrawn under the user.
  function onState() {
    if (!open) return;
    if (!app.state.loaded || ['transactions', 'insights'].includes(tab)) renderTab();
    // The overview holds editors; only redraw it when none is open.
    else if (tab === 'overview' && !views.overview.isEditing()) renderTab();
    // Budgets follow the month navigation.
    else if (tab === 'budgets' && !views.budgets.isEditing()) renderTab();
  }

  function switchTab(next) {
    next = RENAMED[next] || next;
    if (!VIEWS.has(next)) return;
    tab = next;
    if (tab !== 'add') app.rememberTab(tab);
    views.overview.reset();
    views.transactions.reset();
    views.upcoming.reset();
    views.budgets.reset();
    renderTab();
  }

  function show() {
    const m = build();
    m.classList.remove('hidden', 'modal-minimized', 'minimized');
    m.style.display = 'flex';
    const content = m.querySelector('.modal-content');
    content.classList.remove('modal-closing', 'sheet-ready');
    for (const prop of ['transform', 'transition', 'animation', 'opacity']) content.style[prop] = '';
  }

  function openPanel(options = {}) {
    if (Modals.isRegistered(MODAL_ID) && Modals.isMinimized(MODAL_ID)) {
      Modals.restore(MODAL_ID);
      return;
    }
    if (options.tab) tab = options.tab;
    if (open) { show(); renderTab(); return; }
    open = true;
    show();
    Modals.register(MODAL_ID, {
      railBtnId: 'rail-finance',
      sidebarBtnId: 'tool-finance-btn',
      label: 'Finance',
      icon: ICONS.finance(14),
      closeFn: () => teardown(),
      restoreFn: () => { open = true; show(); renderTab(); },
    });
    unsubscribe = app.subscribe(onState);
    escHandler = (e) => {
      if (e.key !== 'Escape' || !open || modal.style.display === 'none') return;
      if (document.getElementById('styled-confirm-overlay')?.style.display === 'flex') return;
      if (document.getElementById('styled-prompt-overlay')?.style.display === 'flex') return;
      if (views.transactions.isEditing()) { views.transactions.reset(); renderTab(); return; }
      close();
    };
    document.addEventListener('keydown', escHandler);
    renderTab();
    app.refresh();
  }

  function teardown() {
    open = false;
    if (modal) { modal.style.display = 'none'; modal.classList.add('hidden'); }
    if (unsubscribe) { unsubscribe(); unsubscribe = null; }
    if (escHandler) { document.removeEventListener('keydown', escHandler); escHandler = null; }
    views.overview.reset();
    views.transactions.reset();
  }

  function close() {
    if (!open && !Modals.isMinimized(MODAL_ID)) return;
    if (Modals.isRegistered(MODAL_ID)) Modals.close(MODAL_ID);
    else teardown();
  }

  function isOpen() {
    if (Modals.isMinimized(MODAL_ID)) return false;
    return open;
  }

  function toggle() {
    if (Modals.toggle(MODAL_ID)) return;
    if (isOpen()) close(); else openPanel();
  }

  return { open: openPanel, close, toggle, isOpen };
}
