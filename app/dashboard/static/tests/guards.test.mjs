// The guards that keep the XSS fix whole: the lint rules fire on every
// markup sink, src/ has exactly one innerHTML (inside setHTML), and the
// committed bundle is the one the sources build.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { ESLint } from 'eslint';
import { STATIC, REPO } from './harness.mjs';
import { outputs } from '../build.mjs';

const SRC = join(STATIC, 'src');
function walk(dir) {
  return readdirSync(dir).flatMap((n) => { const p = join(dir, n); return statSync(p).isDirectory() ? walk(p) : [p]; });
}

test('every markup sink is a lint error outside core/html.js', async () => {
  const eslint = new ESLint({ cwd: REPO });
  const cases = {
    innerHTML: "export function f(el, x) { el.innerHTML = '<b>' + x + '</b>'; }",
    innerHTMLTemplate: 'export function f(el, x) { el.innerHTML = `<b>${x}</b>`; }',
    compound: "export function f(el, x) { el.innerHTML += x; }",
    outerHTML: 'export function f(el, x) { el.outerHTML = x; }',
    insertAdjacent: "export function f(el, x) { el.insertAdjacentHTML('beforeend', x); }",
    write: 'export function f(x) { document.write(x); }',
    fragment: 'export function f(r, x) { return r.createContextualFragment(x); }',
    parser: "export function f(p, x) { return p.parseFromString(x, 'text/html'); }",
    htmlAsCall: "import { html } from './core/html.js'; export function f(x) { return html(['<b>' + x]); }",
    onAttr: "export function f(el) { el.setAttribute('onclick', 'x()'); }",
    computedAttr: 'export function f(el, n) { el.setAttribute(n, 1); }',
    onProp: 'export function f(el) { el.onclick = () => 1; }',
    srcdoc: 'export function f(el, x) { el.srcdoc = x; }',
  };
  for (const [name, code] of Object.entries(cases)) {
    const [res] = await eslint.lintText(code, { filePath: join(SRC, 'views', 'fixture-' + name + '.js') });
    const hits = res.messages.filter((m) => m.ruleId === 'no-restricted-syntax');
    assert.ok(hits.length > 0, name + ' was not flagged: ' + JSON.stringify(res.messages));
  }
  // And the sanctioned path is clean.
  const [ok] = await eslint.lintText("import { html, setHTML } from '../core/html.js';\nexport function f(el, x) { setHTML(el, html`<b>${x}</b>`); }\n",
    { filePath: join(SRC, 'views', 'fixture-ok.js') });
  assert.deepEqual(ok.messages, []);
});

test('src/ assigns innerHTML in exactly one place: setHTML', () => {
  const hits = [];
  for (const f of walk(SRC)) {
    // Comments may name the sinks (html.js documents them); code may not.
    const text = readFileSync(f, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
    for (const m of text.matchAll(/\.(innerHTML|outerHTML)\s*\+?=|insertAdjacentHTML|document\.write/g)) hits.push(relative(SRC, f) + ': ' + m[0]);
  }
  assert.deepEqual(hits, ['core' + (process.platform === 'win32' ? '\\' : '/') + 'html.js: .innerHTML =']);
});

test('the committed app.js, app.css and fonts.css are what the sources build', async () => {
  const out = await outputs();
  for (const [rel, text] of Object.entries(out)) {
    const committed = readFileSync(join(STATIC, rel), 'utf8').replace(/\r\n/g, '\n');
    assert.ok(committed === text, rel + ' is stale: run `npm run build`');
  }
});
