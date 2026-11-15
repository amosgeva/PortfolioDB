// The front end's half of the navigation contract (audit R-3): the rail, the
// phone tab bar, the pane titles and the command palette are drawn from
// nav.json - the file the native pages' rail is drawn from too
// (app/tests/test_nav_contract.py checks that half). Run against the real,
// committed bundle, so a stale app.js fails here as well as in build:check.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { mount, makeParent, click, STATIC } from './harness.mjs';
import { demo } from './fixtures.mjs';

const NAV = JSON.parse(readFileSync(join(STATIC, 'nav.json'), 'utf8'));
const NATIVE = new Set(NAV.groups.filter((g) => g.renderer === 'native').map((g) => g.id));
const SHAPE_ATTRS = { path: ['d'], rect: ['x', 'y', 'width', 'height', 'rx'], circle: ['cx', 'cy', 'r'], line: ['x1', 'y1', 'x2', 'y2'] };

const $ = (page, s) => page.document.querySelector(s);
const $$ = (page, s) => Array.from(page.document.querySelectorAll(s));

function withPage(fn, opts = {}) {
  const page = mount(demo(), opts);
  try { fn(page); assert.deepEqual(page.errors, [], 'page errors'); } finally { page.close(); }
}

/** An <svg> element as nav.json writes a glyph. */
function glyphOf(svg) {
  assert.ok(svg, 'entry has no <svg>');
  return {
    stroke: svg.getAttribute('stroke'), width: svg.getAttribute('stroke-width'),
    cap: svg.getAttribute('stroke-linecap'), join: svg.getAttribute('stroke-linejoin'),
    shapes: Array.from(svg.children, (el) => {
      const tag = el.tagName.toLowerCase();
      const out = { el: tag };
      for (const a of SHAPE_ATTRS[tag] || []) out[a] = el.getAttribute(a);
      return out;
    }),
  };
}
const expected = (name) => {
  const i = NAV.icons[name];
  return { stroke: i.stroke, width: i.width, cap: i.cap, join: i.join, shapes: i.shapes };
};
const label = (a) => Array.from(a.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent).join('').trim();

test('the rail draws every nav.json group and view, in order, with its glyph', () => withPage((page) => {
  const sects = $$(page, '[data-rail-nav] > .rail__sect');
  assert.deepEqual(sects.map((s) => s.dataset.railGroup), NAV.groups.map((g) => g.id));
  for (const g of NAV.groups) {
    const sect = $(page, `[data-rail-group="${g.id}"]`);
    assert.equal(sect.querySelector('.rail__label').textContent, g.label);
    const links = Array.from(sect.querySelectorAll('.nav a'));
    const views = NAV.views.filter((v) => v.group === g.id);
    assert.deepEqual(links.map(label), views.map((v) => v.label), g.id + ' labels');
    views.forEach((v, i) => {
      const a = links[i];
      // Native pages are reached through the parent's hidden buttons (data-nav);
      // the front end's own panes switch in place (data-view).
      if (NATIVE.has(g.id)) assert.equal(a.dataset.nav, v.id); else assert.equal(a.dataset.view, v.id);
      assert.deepEqual(glyphOf(a.querySelector('svg')), expected(v.icon), v.id + ' glyph');
      assert.equal(Boolean(a.querySelector('.badge')), Boolean(v.badge), v.id + ' badge');
    });
  }
}));

test('the phone tab bar is nav.json tabbar.frame: short labels and glyphs', () => withPage((page) => {
  const tabs = $$(page, '.tabbar [data-tab]');
  assert.deepEqual(tabs.map((b) => b.dataset.tab), NAV.tabbar.frame);
  for (const b of tabs) {
    const v = NAV.views.find((x) => x.id === b.dataset.tab);
    assert.equal(b.querySelector('span').textContent, v.short);
    assert.deepEqual(glyphOf(b.querySelector('svg')), expected(v.icon), v.id + ' tab glyph');
    assert.ok(!NATIVE.has(v.group), 'the front end tab bar switches panes; a native page there would need data-nav');
  }
}));

test('every pane takes its title and subtitle from nav.json, and has a pane to show', () => withPage((page) => {
  for (const v of NAV.views.filter((x) => !NATIVE.has(x.group))) {
    assert.ok($(page, `.view[data-view-pane="${v.id}"]`), v.id + ' has no pane in shell.html');
    click(page.window, $(page, `.nav a[data-view="${v.id}"]`));
    assert.equal($(page, '[data-title]').textContent, v.label);
    assert.equal($(page, '[data-subtitle]').textContent, v.subtitle);
    assert.equal($(page, '.view.is-active').dataset.viewPane, v.id);
  }
}));

test('a native rail entry clicks the parent hidden nav button for that view', () => {
  const natives = NAV.views.filter((v) => NATIVE.has(v.group)).map((v) => v.id);
  const parent = makeParent({ navButtons: natives });
  withPage((page) => {
    for (const id of natives) click(page.window, $(page, `.nav a[data-nav="${id}"]`));
    assert.deepEqual(parent.clicks.nav, natives);
    assert.deepEqual(parent.clicks.anchors, [], 'no page load while the hidden buttons exist');
  }, { parent });
});

test('the command palette lists every nav.json view', () => withPage((page) => {
  page.document.dispatchEvent(new page.window.KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }));
  const items = $$(page, '[data-cmdk-list] .cmdk__item').map((e) => e.textContent.trim());
  for (const v of NAV.views) assert.ok(items.some((t) => t.startsWith(v.label)), v.label + ' missing from the palette: ' + items.join(' | '));
}));

test('no nav glyph or label is hard-coded outside nav.json', () => {
  // The shell carries containers, not entries; src/ draws from the JSON.
  const shell = readFileSync(join(STATIC, 'shell.html'), 'utf8');
  assert.doesNotMatch(shell, /data-view="|data-nav="|data-tab="/, 'shell.html hard-codes a rail or tab entry');
  const srcFiles = [];
  const walk = (d) => readdirSync(d, { withFileTypes: true }).forEach((e) => (e.isDirectory() ? walk(join(d, e.name)) : e.name.endsWith('.js') && srcFiles.push(join(d, e.name))));
  walk(join(STATIC, 'src'));
  for (const [name, icon] of Object.entries(NAV.icons)) {
    const paths = icon.shapes.filter((s) => s.el === 'path').map((s) => s.d);
    if (!paths.length) continue;
    for (const f of srcFiles) {
      // A literal <svg> with the same stroke width and every path is a copy.
      // (FEED.up reuses the trend outline at a heavier 2.4 stroke: a variant.)
      for (const [svg] of readFileSync(f, 'utf8').matchAll(/<svg[^>]*>.*?<\/svg>/gs)) {
        const copy = svg.includes(`stroke-width="${icon.width}"`) && paths.every((d) => svg.includes(`d="${d}"`));
        assert.ok(!copy, `${f} carries its own copy of the ${name} glyph; use icon('${name}')`);
      }
    }
  }
});
