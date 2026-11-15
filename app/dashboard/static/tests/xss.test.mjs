// Audit A-3, front-end half: payload text never becomes markup.
//
// A payload whose symbols, names, sectors, notes, accounts, news, fundamentals,
// alerts and config carry <img onerror>, attribute-breaking quotes, numeric
// entities, </script> and javascript: URLs is rendered through every view and
// every interaction that renders, and after each step the whole document is
// checked: no event-handler attribute, no injected element, no smuggled
// attribute, no script beyond the page's own three, no non-http link, no image
// from anywhere but a data URI - and window.__pwned() never ran.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mount, click, key } from './harness.mjs';
import { hostile, H } from './fixtures.mjs';

function audit(page, step) {
  const { document: doc, window: w } = page;
  const where = ' (after ' + step + ')';
  assert.equal(w.__pwnedCount || 0, 0, 'hostile script ran' + where);
  const all = Array.from(doc.querySelectorAll('*'));
  const handlerAttrs = all.flatMap((el) => Array.from(el.attributes).filter((a) => /^on/i.test(a.name)).map((a) => el.tagName + '[' + a.name + ']'));
  assert.deepEqual(handlerAttrs, [], 'event-handler attribute injected' + where);
  const smuggled = all.filter((el) => el.hasAttribute('data-x') || el.hasAttribute('srcdoc') || el.hasAttribute('formaction'));
  assert.equal(smuggled.length, 0, 'attribute smuggled through a quote' + where);
  assert.equal(doc.querySelectorAll('script').length, 3, 'only the page’s own scripts' + where);
  assert.equal(doc.querySelectorAll('iframe,object,embed,frame,base,meta[http-equiv],form').length, 0, 'injected element' + where);
  doc.querySelectorAll('img').forEach((img) => {
    assert.ok(img.classList.contains('sym-logo'), 'unexpected <img>' + where);
    assert.match(img.getAttribute('src'), /^data:image\/(png|jpe?g|gif|webp|svg\+xml);base64,/, 'logo from an unexpected source' + where);
  });
  doc.querySelectorAll('[href]').forEach((a) => {
    assert.match(a.getAttribute('href'), /^https?:\/\//, 'non-http href ' + a.getAttribute('href') + where);
  });
  doc.querySelectorAll('[src]').forEach((el) => assert.equal(el.tagName, 'IMG', 'src on ' + el.tagName + where));
}

test('hostile payload text renders as text in every view and interaction', () => {
  const p = hostile();
  // A real data-URI logo must still render (and the hostile ones must not).
  p.logos.AAPL = 'data:image/png;base64,iVBORw0KGgo=';
  const page = mount(p);
  const { window: w, document: doc } = page;
  w.__pwnedCount = 0;
  w.__pwned = () => { w.__pwnedCount += 1; };
  try {
    assert.deepEqual(page.errors, [], 'page errors on mount');
    audit(page, 'mount');
    // The hostile strings are on the page, as text.
    const text = doc.body.textContent;
    for (const s of [H.tag, H.entity, H.script]) assert.ok(text.includes(s), 'missing literal text ' + s);
    assert.equal(doc.querySelectorAll('img.sym-logo').length > 0, true, 'the valid data-URI logo still renders');

    for (const view of ['portfolio', 'movers', 'stats', 'alerts', 'fundamentals', 'history']) {
      click(w, doc.querySelector('.nav a[data-view="' + view + '"]'));
      audit(page, 'view ' + view);
    }
    // Every fundamentals symbol (hostile option values included).
    const fd = doc.querySelector('#fd-symbol');
    for (const opt of Array.from(fd.options)) {
      fd.value = opt.value; fd.dispatchEvent(new w.Event('change'));
      audit(page, 'fundamentals ' + opt.value);
    }
    // Every price-history symbol, with the benchmark overlay on.
    const bench = doc.querySelector('#ph-spy');
    bench.checked = true; bench.dispatchEvent(new w.Event('change'));
    const ph = doc.querySelector('#ph-symbol');
    for (const opt of Array.from(ph.options)) {
      ph.value = opt.value; ph.dispatchEvent(new w.Event('change'));
      audit(page, 'price chart ' + opt.value);
    }
    // Every chip on the portfolio view.
    for (const sel of ['#alloc-chips [data-dim]', '#alloc-mode [data-mode]', '#attr-chips [data-attr]', '#pv-chips [data-range]', '#ph-chips [data-phr]', '#sector-chips [data-sector]', '[data-filter]']) {
      doc.querySelectorAll(sel).forEach((c) => { if (!c.disabled) click(w, c); });
      audit(page, sel);
    }
    // Every symbol's drawer, opened through its trigger.
    const syms = new Set(Array.from(doc.querySelectorAll('[data-sym]')).map((el) => el.getAttribute('data-sym')));
    assert.ok(syms.has(H.tag) && syms.has(H.quote) && syms.has(H.entity), 'hostile symbols reach the triggers intact');
    for (const s of syms) {
      const el = Array.from(doc.querySelectorAll('[data-sym]')).find((x) => x.getAttribute('data-sym') === s);
      click(w, el.querySelector('.sym-open') || el);
      assert.ok(doc.querySelector('[data-drawer]').classList.contains('open'), 'drawer opens for ' + s);
      audit(page, 'drawer ' + s);
      key(w, doc, 'Escape');
    }
    // Every KPI's evidence panel.
    doc.querySelectorAll('[data-kpi]').forEach((k) => { click(w, k); audit(page, 'evidence ' + k.getAttribute('data-kpi')); key(w, doc, 'Escape'); });
    // The command palette, unfiltered and filtered by a hostile query.
    key(w, doc, 'k', { ctrlKey: true });
    audit(page, 'palette');
    const input = doc.querySelector('[data-cmdk-input]');
    input.value = '<img'; input.dispatchEvent(new w.Event('input'));
    audit(page, 'palette search');
    key(w, input, 'Escape');
    // Theme toggle re-renders the heatmap and the correlation grid.
    click(w, doc.querySelector('[data-theme-toggle]'));
    audit(page, 'theme toggle');
    assert.deepEqual(page.errors, [], 'page errors during the walk');
  } finally { page.close(); }
});

test('a hostile reporting zone, currency and label fall back instead of breaking the page', () => {
  const page = mount(hostile());
  try {
    assert.deepEqual(page.errors, []);
    // reporting_tz was markup: the clock uses UTC rather than throwing.
    assert.match(page.document.querySelector('[data-clock]').textContent, /UTC/);
    // currency was markup: money falls back to USD.
    assert.match(page.document.querySelector('#kpi-mktval').textContent, /^\$/);
  } finally { page.close(); }
});
