// Entry point. Bundled by build.mjs into app.js (an IIFE), which shell.html
// runs after `const DATA = <payload>` - one mount per frame, because Streamlit
// builds a fresh iframe on every rerun.

import { $ } from './core/dom.js';
import { initState, app } from './core/state.js';
import { configureFormat } from './core/format.js';
import { beginMount, onParent, fitFrame, brandParent, session } from './core/host.js';
import { initToast, toast } from './ui/toast.js';
import { initTheme, onThemeChange, toggleTheme } from './ui/theme.js';
import { initShell, initRefresh } from './ui/shell.js';
import { initNav, switchView, goTo, onFirstShow, currentView, restoreView, VIEWS } from './ui/nav.js';
import { initDrawer, openDrawer, setDrawerHooks } from './ui/drawer.js';
import { initPalette, isPaletteOpen } from './ui/cmdk.js';
import { initFolds } from './ui/folds.js';
import { renderKPIs } from './views/kpis.js';
import { renderMarkets, renderReturns, renderAlloc, renderTable, renderAttribution, renderWaterfall,
  initPortfolioControls, flashChangedPrices } from './views/portfolio.js';
import { initPVRange, renderPV } from './views/pv-chart.js';
import { renderRisk } from './views/risk.js';
import { initPriceChart, renderLatestPrices } from './views/price-chart.js';
import { renderStats } from './views/stats.js';
import { initMovers, renderHeat } from './views/movers.js';
import { initAlerts, markAllRead } from './views/alerts.js';
import { renderFundamentals } from './views/fundamentals.js';
import { renderHistory, exportPositions } from './views/history.js';

function openFundamentalsFor(sym) {
  switchView('fundamentals');
  const sel = $('#fd-symbol');
  if (sel && sel.options.length) { sel.value = sym; renderFundamentals(); }
}

function staticCommands(refreshBtn) {
  return [
    // Every rail entry, both halves, in rail order (nav.json).
    ...VIEWS.map((v) => ({ sect: 'Views', label: v.label, run: () => goTo(v.id) })),
    { sect: 'Actions', label: 'Refresh data', run: () => { if (refreshBtn) refreshBtn.click(); } },
    { sect: 'Actions', label: 'Toggle dark mode', run: toggleTheme },
    { sect: 'Actions', label: 'Mark all alerts read', run: markAllRead },
    { sect: 'Actions', label: 'Export positions CSV', run: exportPositions },
  ];
}

export function mount(payload) {
  beginMount();
  initState(payload);
  configureFormat(app.cfg);
  initToast();
  initTheme();
  initShell();
  initNav();
  const refreshBtn = initRefresh(currentView);
  setDrawerHooks({ switchView, openFundamentalsFor, isPaletteOpen });
  initDrawer();
  initPalette(() => staticCommands(refreshBtn), openDrawer);
  // A logo that fails to load removes itself and the letter badge shows. One
  // capture-phase listener instead of an inline onerror on every <img>.
  document.addEventListener('error', (e) => {
    const t = e.target;
    if (t && t.classList && t.classList.contains('sym-logo')) t.remove();
  }, true);

  const pos = $('#pos-csv');
  if (pos) pos.addEventListener('click', exportPositions);
  initFolds();
  fitFrame();
  onParent('resize', fitFrame);
  brandParent((app.D.kpi || {}).dayChangePct);

  renderKPIs(); renderMarkets(); renderReturns(); initPVRange(); renderPV(); renderAlloc();
  initPortfolioControls(); renderTable(); renderAttribution(); renderWaterfall(); renderRisk();
  initPriceChart(); renderLatestPrices();
  initMovers();
  renderStats();
  initAlerts();
  onThemeChange(() => { renderHeat(); renderRisk(); });
  onFirstShow('fundamentals', renderFundamentals);
  onFirstShow('history', renderHistory);

  restoreView();
  if (session.take('pdb_refreshed')) toast('Updated just now');
  flashChangedPrices();
}

mount(typeof DATA !== 'undefined' ? DATA : {});
