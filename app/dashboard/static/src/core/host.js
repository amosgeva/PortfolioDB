// Everything the front end knows about the page that embeds it.
//
// The dashboard runs in a same-origin iframe that Streamlit re-creates on every
// rerun. A handful of features need the parent page: the soft refresh clicks a
// hidden native button, the frame sizes itself to the parent viewport, the URL
// is kept in step with the active view, and the tab title/favicon carry the
// day's P&L. Those touch Streamlit's DOM, which is not a public API, so every
// such contact lives here - one file to read when a Streamlit upgrade moves
// something - and every one degrades to a working page when the parent is
// absent or unreachable (a sandboxed frame, a test harness, a direct open).
//
// Listeners on the parent outlive this frame: the parent keeps running while
// Streamlit throws the old iframe away. They are therefore registered through
// onParent(), which records them on the parent window and releases the ones a
// previous mount left behind before adding its own, and again when this frame
// is hidden. A long-lived tab no longer accumulates a resize and a popstate
// handler per refresh (audit U-5).

/** The Streamlit elements this file depends on. */
export const STREAMLIT = {
  // st.button("refresh", key="bg_refresh") in streamlit_app.py renders inside
  // an element carrying this key class.
  refreshButton: '.st-key-bg_refresh button',
  // The flex item that reserves st.iframe's server-side height.
  elementContainer: '[data-testid="stElementContainer"]',
  // st.button(key="pdb_nav_<view>") per native page: a rerun in the same
  // session, so the unlock and form state survive (the fix for audit N-5).
  navButton: (view) => '.st-key-pdb_nav_' + view + ' button',
};

const REGISTRY = '__pdbHostListeners';

/** The embedding window, or null when there is none we may touch. */
export function parentWin() {
  try {
    const p = window.parent;
    if (!p || p === window) return null;
    void p.document.body; // throws when cross-origin / sandboxed
    return p;
  } catch (e) { return null; }
}
export function parentDoc() { const p = parentWin(); return p ? p.document : null; }

// One token per mount, so a frame being torn down releases only what it added
// and never the listeners of the frame that replaced it.
const MOUNT_ID = {};

/** Remove parent listeners: every one (`all`), or only this mount's. */
export function releaseParentListeners(all) {
  const p = parentWin();
  if (!p) return 0;
  const list = p[REGISTRY] || [];
  const keep = [];
  let n = 0;
  list.forEach((l) => {
    if (!all && l.owner !== MOUNT_ID) { keep.push(l); return; }
    try { p.removeEventListener(l.type, l.fn); } catch (e) { /* parent gone */ }
    n++;
  });
  p[REGISTRY] = keep;
  return n;
}

/** addEventListener on the parent window, released on the next mount. */
export function onParent(type, fn) {
  const p = parentWin();
  if (!p) return false;
  if (!p[REGISTRY]) p[REGISTRY] = [];
  p.addEventListener(type, fn);
  p[REGISTRY].push({ type, fn, owner: MOUNT_ID });
  return true;
}

/** Start of a mount: only one dashboard frame exists at a time, so anything
 *  still registered belongs to a frame Streamlit has already discarded. */
export function beginMount() {
  releaseParentListeners(true);
  window.addEventListener('pagehide', () => releaseParentListeners(false));
}

export const session = {
  get(k) { try { const p = parentWin(); return p ? p.sessionStorage.getItem(k) : null; } catch (e) { return null; } },
  set(k, v) { try { const p = parentWin(); if (p) p.sessionStorage.setItem(k, v); } catch (e) { /* storage blocked */ } },
  take(k) { const v = session.get(k); if (v != null) { try { parentWin().sessionStorage.removeItem(k); } catch (e) { /* blocked */ } } return v; },
};

/** Ask Streamlit for a soft rerun. Returns false when the control is absent,
 *  and the caller falls back to a hard navigation. */
export function softRefresh(view) {
  const d = parentDoc();
  const btn = d && d.querySelector(STREAMLIT.refreshButton);
  if (!btn) return false;
  session.set('pdb_refresh_view', view);
  session.set('pdb_refreshed', '1');
  btn.click();
  return true;
}

/** Navigate the top page. The frame is sandboxed without top navigation, but
 *  it may click an anchor it creates in the parent, which navigates itself. */
export function topNavigate(qs) {
  const d = parentDoc();
  if (d) {
    const a = d.createElement('a');
    a.href = qs; a.style.display = 'none';
    d.body.appendChild(a); a.click(); d.body.removeChild(a);
    return;
  }
  try { window.open(qs, '_self'); } catch (e) { window.location.href = qs; }
}

/** Open a native Streamlit page: the parent's hidden nav button when it has
 *  one (no reload), a page load otherwise. */
export function nativeNav(view) {
  const d = parentDoc();
  const btn = d && /^[a-z_]+$/.test(view) ? d.querySelector(STREAMLIT.navButton(view)) : null;
  if (btn) { btn.click(); return true; }
  topNavigate('?view=' + view);
  return false;
}

/** Keep ?view= in the parent URL in step with the active pane. */
export function syncUrl(view, replace) {
  const p = parentWin();
  if (!p) return;
  try {
    const st = { pdbView: view };
    if (replace) p.history.replaceState(st, '', '?view=' + view);
    else p.history.pushState(st, '', '?view=' + view);
  } catch (e) { /* history unavailable */ }
}
export function parentQueryView() {
  try { const p = parentWin(); return p ? new URLSearchParams(p.location.search).get('view') : null; }
  catch (e) { return null; }
}

/** Size this frame to the parent viewport so the 100vh shell owns one screen. */
export function fitFrame() {
  const p = parentWin();
  const fe = p && window.frameElement;
  if (!fe) return;
  const h = p.innerHeight + 'px';
  fe.style.height = h;
  fe.style.width = '100%';
  // Streamlit reserves the server-side height on the element container. It is
  // a flex item with `flex: 0 0 <height>`, so the basis wins over an inline
  // height; both are set because older Streamlit sized by height.
  const box = fe.closest && fe.closest(STREAMLIT.elementContainer);
  if (box) { box.style.flexBasis = h; box.style.height = h; }
  p.document.documentElement.style.overflow = 'hidden';
  p.document.body.style.overflow = 'hidden';
}

/** Theme the page behind the frame so over-scroll does not flash. */
export function themeParent(dark) {
  const d = parentDoc();
  if (!d) return;
  const c = dark ? '#16181d' : '#ffffff';
  d.body.style.background = c;
  let meta = d.querySelector('meta[name="theme-color"]');
  if (!meta) { meta = d.createElement('meta'); meta.name = 'theme-color'; d.head.appendChild(meta); }
  meta.content = c;
}

/** Day P&L in the tab title and a green/red favicon dot, plus the manifest. */
export function brandParent(dayPct) {
  const d = parentDoc();
  if (!d) return;
  const dp = Number(dayPct) || 0;
  const arrow = dp > 0 ? '▲' : dp < 0 ? '▼' : '●';
  d.title = arrow + ' ' + (dp >= 0 ? '+' : '') + dp.toFixed(2) + '% · PortfolioDB';
  try {
    const c = document.createElement('canvas'); c.width = 64; c.height = 64;
    const x = c.getContext('2d');
    if (x) {
      x.beginPath(); x.arc(32, 32, 24, 0, Math.PI * 2);
      // The tab strip is light or dark by OS theme, not ours, so the dot has to
      // clear 3:1 on both: the --up/--down hues re-solved at that lightness.
      x.fillStyle = dp >= 0 ? '#0c994f' : '#ed4a46'; x.fill();
      let link = d.querySelector('link[rel~="icon"]');
      if (!link) { link = d.createElement('link'); link.rel = 'icon'; d.head.appendChild(link); }
      link.href = c.toDataURL('image/png');
    }
  } catch (e) { /* canvas unavailable: keep the default favicon */ }
  if (!d.querySelector('link[rel="manifest"]')) {
    const ml = d.createElement('link'); ml.rel = 'manifest'; ml.href = '/app/static/manifest.json';
    d.head.appendChild(ml);
  }
}
