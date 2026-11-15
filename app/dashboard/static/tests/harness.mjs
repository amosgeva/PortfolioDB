// Runs the real, committed front end (shell.html + app.css + app.js) in jsdom,
// inside a fake Streamlit parent page, the way streamlit_app.py serves it.
//
// build_html() in streamlit_app.py is replicated here placeholder for
// placeholder; tests/dashboard.test.mjs reads streamlit_app.py and fails if a
// placeholder is added there that this harness does not fill.

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM, VirtualConsole } from 'jsdom';

export const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..');
export const REPO = join(STATIC, '..', '..', '..');
const read = (p) => readFileSync(p, 'utf8');

export function loadFixture(name) {
  return JSON.parse(read(join(STATIC, 'tests', 'fixtures', name)));
}

/** The `.replace("__X__", …)` placeholders build_html fills. */
export function buildHtmlPlaceholders() {
  const src = read(join(REPO, 'app', 'streamlit_app.py'));
  const fn = src.slice(src.indexOf('def build_html'), src.indexOf('# Render', src.indexOf('def build_html')));
  return Array.from(fn.matchAll(/\.replace\("(__[A-Z_]+__)"/g), (m) => m[1]);
}

export function buildHtml(payload, initialView = 'portfolio') {
  const svg = (name) => `<svg data-icon="${name}" viewBox="0 0 24 24"></svg>`;
  // Same escaping as build_html: "<" so a payload string cannot close the script.
  const dataJson = JSON.stringify({ ...payload, initialView }).replace(/</g, '\\u003c');
  const fills = {
    __CSS__: read(join(STATIC, 'app.css')),
    __USER_NAME__: 'Test User', __APP_VERSION__: 'v0-test', __APP_BUILD__: 'test', __USER_INITIALS__: 'TU',
    __DATA_JSON__: dataJson, __APP_JS__: read(join(STATIC, 'app.js')),
  };
  let html = read(join(STATIC, 'shell.html'));
  for (const ph of buildHtmlPlaceholders()) {
    const val = fills[ph] !== undefined ? fills[ph] : (ph.startsWith('__SVG_') ? svg(ph) : null);
    if (val === null) throw new Error('harness does not fill ' + ph);
    // A function replacement, so "$&" or "$1" inside the bundle stays literal.
    html = html.split(ph).join(val);
  }
  return html;
}

// jsdom reports browser features it lacks (navigation, canvas, scrollTo) as
// "Not implemented"; the page guards each one, so they are not page errors.
const IGNORED = [/^Not implemented: /];

/** A Streamlit-like parent page. Reuse one across mounts to test remounting. */
export function makeParent({ refreshButton = true } = {}) {
  const dom = new JSDOM(`<!doctype html><html><head><title>Streamlit</title></head><body>
    <div data-testid="stElementContainer" style="flex:0 0 1180px"><iframe id="pdb-frame" title="dashboard"></iframe></div>
    ${refreshButton ? '<div class="st-key-bg_refresh"><button type="button">refresh</button></div>' : ''}
  </body></html>`, { url: 'http://localhost:8501/?view=portfolio', pretendToBeVisual: true });
  const w = dom.window;
  // Track live listeners so a test can count what a mount left behind.
  const live = new Map();
  const add = w.addEventListener.bind(w), remove = w.removeEventListener.bind(w);
  w.addEventListener = (type, fn, opt) => { if (!live.has(type)) live.set(type, new Set()); live.get(type).add(fn); add(type, fn, opt); };
  w.removeEventListener = (type, fn, opt) => { if (live.has(type)) live.get(type).delete(fn); remove(type, fn, opt); };
  const clicks = { refresh: 0, anchors: [] };
  const btn = w.document.querySelector('.st-key-bg_refresh button');
  if (btn) btn.addEventListener('click', () => { clicks.refresh += 1; });
  w.document.addEventListener('click', (e) => { if (e.target.tagName === 'A') { clicks.anchors.push(e.target.getAttribute('href')); e.preventDefault(); } }, true);
  return { dom, window: w, live, clicks, listenerCount: (t) => (live.get(t) ? live.get(t).size : 0) };
}

/**
 * Mount the dashboard. Returns { window, document, errors, blobs, parent, close }.
 * `errors` collects uncaught exceptions and console.error calls from the page.
 */
export function mount(payload, { parent = makeParent(), initialView = 'portfolio', storage = {}, matchMedia = {}, sandboxed = false } = {}) {
  const errors = [], blobs = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => { if (!IGNORED.some((re) => re.test(e.message))) errors.push(e.message + (e.detail ? ' ' + e.detail : '')); });
  vc.on('error', (...a) => errors.push(a.map(String).join(' ')));
  const html = buildHtml(payload, initialView);
  // `sandboxed`: a parent the frame may not touch - every property read throws,
  // the way a cross-origin or sandboxed embedder behaves.
  const parentView = sandboxed
    ? new Proxy({}, { get() { throw new Error('SecurityError: Blocked a frame from accessing a cross-origin frame'); } })
    : parent.window;
  const frame = sandboxed ? null : parent.window.document.getElementById('pdb-frame');
  const dom = new JSDOM(html, {
    url: 'http://localhost:8501/?view=portfolio',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(w) {
      Object.defineProperty(w, 'parent', { get: () => parentView, configurable: true });
      Object.defineProperty(w, 'frameElement', { get: () => frame, configurable: true });
      w.matchMedia = (q) => ({ matches: Boolean(matchMedia[q]), media: q, addEventListener() {}, removeEventListener() {} });
      for (const [k, v] of Object.entries(storage)) w.localStorage.setItem(k, v);
      const RealBlob = w.Blob;
      w.Blob = class extends RealBlob { constructor(parts, opts) { super(parts, opts); blobs.push({ parts, type: opts && opts.type }); } };
      w.URL.createObjectURL = () => 'blob:http://localhost:8501/test';
      w.URL.revokeObjectURL = () => {};
      w.addEventListener('error', (e) => errors.push('uncaught: ' + (e.error && e.error.stack ? e.error.stack : e.message)));
    },
  });
  const w = dom.window;
  return {
    window: w, document: w.document, errors, blobs, parent,
    blobText: (i) => blobs[i].parts.map(String).join(''),
    close: () => w.close(),
  };
}

export function click(win, el) {
  el.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
}
export function key(win, el, k, extra = {}) {
  el.dispatchEvent(new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...extra }));
}
export function pointer(win, el, type, props = {}) {
  const Ctor = win.PointerEvent || win.MouseEvent;
  const ev = new Ctor(type, { bubbles: true, cancelable: true, clientX: props.clientX || 0, clientY: props.clientY || 0 });
  if (!('pointerType' in ev) || ev.pointerType !== props.pointerType) {
    Object.defineProperty(ev, 'pointerType', { value: props.pointerType || 'mouse' });
  }
  el.dispatchEvent(ev);
}
