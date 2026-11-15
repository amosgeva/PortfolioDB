// The static half of the markup guarantee (see src/core/html.js): every lint
// rule fires on its must-fail fixture, an independent AST scan of src/ finds
// exactly one markup sink and no banned shape, and the committed outputs
// (bundle, CSS, Codacy's eslintrc) are what the sources build.
//
// The fixtures include every shape QA showed passing an earlier version of
// these rules (qa-pr-122.md rounds 1 and 2). The rules now ban shapes - any
// computed member write or call, any reference to a sink name, any non-plain
// import of core/html.js - so a new spelling of an old trick is caught by the
// same rule rather than needing a new one.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { ESLint } from 'eslint';
import * as espree from 'espree';
import { STATIC, REPO } from './harness.mjs';
import { outputs } from '../build.mjs';

const SRC = join(STATIC, 'src');
function walk(dir) {
  return readdirSync(dir).flatMap((n) => { const p = join(dir, n); return statSync(p).isDirectory() ? walk(p) : [p]; });
}

const IMPORT = "import { html, setHTML } from '../core/html.js';\n";
export const MUST_FAIL = {
  innerHTML: "export function f(el, x) { el.innerHTML = '<b>' + x + '</b>'; }",
  innerHTMLTemplate: 'export function f(el, x) { el.innerHTML = `<b>${x}</b>`; }',
  compound: 'export function f(el, x) { el.innerHTML += x; }',
  outerHTML: 'export function f(el, x) { el.outerHTML = x; }',
  insertAdjacent: "export function f(el, x) { el.insertAdjacentHTML('beforeend', x); }",
  write: 'export function f(x) { document.write(x); }',
  writeln: 'export function f(x) { document.writeln(x); }',
  fragment: 'export function f(r, x) { return r.createContextualFragment(x); }',
  parser: "export function f(p, x) { return p.parseFromString(x, 'text/html'); }",
  setHTMLUnsafe: 'export function f(el, x) { el.setHTMLUnsafe(x); }',
  onAttr: "export function f(el) { el.setAttribute('onclick', 'x()'); }",
  srcdocAttr: "export function f(el, x) { el.setAttribute('srcdoc', x); }",
  computedAttr: 'export function f(el, n) { el.setAttribute(n, 1); }',
  setAttributeNS: "export function f(el, x) { el.setAttributeNS(null, 'onclick', x); }",
  onProp: 'export function f(el) { el.onclick = () => 1; }',
  srcdoc: 'export function f(el, x) { el.srcdoc = x; }',
  // Round 1 (B1): computed literal spellings.
  bracketInnerHTML: "export function f(el, x) { el['innerHTML'] = x; }",
  bracketOuterHTML: 'export function f(el, x) { el["outerHTML"] = x; }',
  bracketHandler: "export function f(el, x) { el['onclick'] = x; }",
  bracketHandlerCase: "export function f(el, x) { el['OnMouseOver'] = x; }",
  bracketInsert: "export function f(el, x) { el['insertAdjacentHTML']('beforeend', x); }",
  bracketWrite: "export function f(x) { document['write'](x); }",
  bracketSetAttribute: "export function f(el, x) { el['setAttribute']('onclick', x); }",
  dynamicCall: 'export function f(el, m, x) { el[m](x); }',
  builtKey: "export function f(el, x) { el['inner' + 'HTML'] = x; }",
  templateKey: 'export function f(el, x) { el[`innerHTML`] = x; }',
  // Round 2 (B1): identifier keys bound to a sink name, and other routes by name.
  constKey: "export function computedSink(el, text) {\n  const key = 'innerHTML';\n  el[key] = text;\n}",
  paramKey: 'export function f(el, key, text) { el[key] = text; }',
  moduleConstKey: "const K = 'onclick';\nexport function f(el, x) { el[K] = x; }",
  methodRef: "export function f(el, x) { const g = el.insertAdjacentHTML; g.call(el, 'beforeend', x); }",
  destructure: "export function f(el, x) { const { insertAdjacentHTML } = el; insertAdjacentHTML.call(el, 'beforeend', x); }",
  readInnerHTML: 'export function f(el) { return el.innerHTML; }',
  computedObjectKey: "export function f(el, k, x) { const o = { [k]: x }; return [el, o]; }",
  objectAssign: 'export function f(el, x) { Object.assign(el, { innerHTML: x }); }',
  objectAssignHandler: "export function f(el, x) { Object.assign(el, { 'onclick': x }); }",
  objectAssignVar: 'export function f(el, o) { Object.assign(el, o); }',
  defineProperty: "export function f(el, x) { Object.defineProperty(el, 'innerHTML', { value: x }); }",
  descriptor: "export function f(el, x) { Object.getOwnPropertyDescriptor(Element.prototype, 'inner' + 'HTML').set.call(el, x); }",
  prototypeRef: 'export function f() { return Element.prototype; }',
  reflect: "export function f(el, x) { Reflect.set(el, 'innerHTML', x); }",
  reflectAlias: 'export function f() { const R = Reflect; return R; }',
  // Round 3 (B1): a computed read, later invoked; and any computed access at all.
  computedMethodRead: "export function computedMethodRead(el, text) {\n  const key = 'insertAdjacentHTML';\n  const fn = el[key];\n  fn.call(el, 'beforeend', text);\n}",
  computedRead: 'export function f(o, k) { return o[k]; }',
  indexRead: 'export function f(a) { return a[0]; }',
  optionalComputed: 'export function f(o, k) { return o?.[k]; }',
  // Round 1 and 2 (B2): html used other than as a tag, or imported non-plainly.
  htmlAsCall: IMPORT + "export function f(x) { return html(['<b>' + x]); }",
  aliasTemplate: "import { html as h, setHTML } from '../core/html.js';\nexport function f(el, x) { const raw = Object.freeze([x]); const a = Object.freeze(Object.assign([x], { raw })); setHTML(el, h(a)); }",
  aliasSetHTML: "import { setHTML as put } from '../core/html.js';\nexport function f(el, x) { put(el, x); }",
  quotedImports: "import { \"html\" as h, \"setHTML\" as put } from '../core/html.js';\nexport function forgedTemplate(el, text) {\n  const raw = Object.freeze([text]);\n  const strings = Object.freeze(Object.assign([text], { raw }));\n  put(el, h(strings));\n}",
  quotedSameName: "import { \"html\" as html } from '../core/html.js';\nexport function f(x) { return html`<b>${x}</b>`; }",
  quotedExport: "const h = 1;\nexport { h as \"html\" };",
  foreignHtml: "import { html } from './not-core.js';\nexport function f(x) { return html`<b>${x}</b>`; }",
  namespace: "import * as H from '../core/html.js';\nexport function f(el, x) { H.setHTML(el, H.html([x])); }",
  stored: IMPORT + 'const t = html; export function f(x) { return t([x]); }',
  passed: IMPORT + 'export function f(g, x) { return g(html, x); }',
  applied: IMPORT + 'export function f(x) { return html.call(null, [x]); }',
  shadowed: 'export function f(x) { const html = (s) => s; return html`<b>${x}</b>`; }',
  shadowParam: 'export function f(html, x) { return html`<b>${x}</b>`; }',
  reexport: "export { html } from '../core/html.js';",
  reexportAll: "export * from '../core/html.js';",
  dynamicImport: "export async function f() { const m = await import('../core/html.js'); return m.html; }",
  // Inline disable comments are not honoured in src/.
  inlineDisabled: 'export function f(el, x) {\n  // eslint-disable-next-line no-restricted-syntax\n  el.innerHTML = x;\n}',
  blockDisabled: '/* eslint-disable */\nexport function f(el, x) { el.innerHTML = x; }',
};

test('every banned shape is a lint error (QA B1/B2, rounds 1 and 2)', async () => {
  const eslint = new ESLint({ cwd: REPO });
  for (const [name, code] of Object.entries(MUST_FAIL)) {
    const [res] = await eslint.lintText(code, { filePath: join(SRC, 'views', 'fixture-' + name + '.js') });
    const hits = res.messages.filter((m) => m.ruleId === 'no-restricted-syntax');
    assert.ok(hits.length > 0, name + ' was not flagged: ' + JSON.stringify(res.messages));
  }
  // The sanctioned forms stay clean.
  const [ok] = await eslint.lintText("import { html, attrs, safeUrl, setHTML } from '../core/html.js';\nimport { put, item } from '../core/dict.js';\n" +
    'export function f(el, x, u) { setHTML(el, html`<a class="x" ${attrs({ "data-a": x })} href="${safeUrl(u)}">${html`<b>${x}</b>`}</a>`); }\n' +
    'export function g(m, k, v) { put(m, k, v); return item(m, k); }\n',
  { filePath: join(SRC, 'views', 'fixture-ok.js') });
  assert.deepEqual(ok.messages, []);
});

// ---- AST scan of src/ (independent of the lint configuration) -------------
const SINK_NAMES = new Set(['innerHTML', 'outerHTML', 'srcdoc', 'insertAdjacentHTML', 'insertAdjacentElement', 'createContextualFragment',
  'parseFromString', 'write', 'writeln', 'setHTMLUnsafe', 'setAttributeNS', 'setAttributeNode', 'execCommand', '__defineSetter__', '__lookupSetter__', '__proto__']);
const OWN = new Set(['html', 'setHTML', 'attrs', 'safeUrl']);
function propName(m) {
  if (!m.computed && m.property.type === 'Identifier') return m.property.name;
  if (m.property.type === 'Literal' && typeof m.property.value === 'string') return m.property.value;
  if (m.property.type === 'TemplateLiteral' && !m.property.expressions.length) return m.property.quasis[0].value.cooked;
  return null;
}
function visit(node, parent, fn) {
  if (!node || typeof node.type !== 'string') return;
  fn(node, parent);
  for (const k of Object.keys(node)) {
    const v = node[k];
    if (Array.isArray(v)) v.forEach((c) => visit(c, node, fn));
    else if (v && typeof v.type === 'string') visit(v, node, fn);
  }
}
const isHtmlModule = (src) => /core\/html(\.js)?$/.test(src);
export function scanSource(file, code) {
  const ast = espree.parse(code, { ecmaVersion: 2022, sourceType: 'module', loc: true });
  const out = [];
  const at = (n, what) => out.push(file + ':' + n.loc.start.line + ' ' + what);
  visit(ast, null, (n, p) => {
    if (n.type === 'MemberExpression') {
      const name = propName(n);
      const isWrite = p && p.type === 'AssignmentExpression' && p.left === n;
      const isCall = p && p.type === 'CallExpression' && p.callee === n;
      if (name && SINK_NAMES.has(name)) at(n, (isWrite ? 'write ' : isCall ? 'call ' : 'reference ') + name);
      if (name && /^on[a-z]+$/i.test(name) && isWrite) at(n, 'handler ' + name);
      if (n.computed && !file.endsWith('core/dict.js')) at(n, 'computed member access');
      if (name === 'prototype' && !file.endsWith('core/dict.js')) at(n, 'prototype access');
      if (n.object.type === 'Identifier' && n.object.name === 'Object' && /^(assign|defineProperty|defineProperties|getOwnPropertyDescriptors?|setPrototypeOf)$/.test(name || '')) at(n, 'Object.' + name);
    }
    if (n.type === 'Identifier' && n.name === 'Reflect') at(n, 'Reflect');
    if (n.type === 'Property' && (n.computed || SINK_NAMES.has(n.key.name || n.key.value) || /^on/i.test(String(n.key.value || '')))) at(n, 'object key');
    if (n.type === 'Identifier' && n.name === 'html') {
      const tag = p && p.type === 'TaggedTemplateExpression' && p.tag === n;
      const imp = p && p.type === 'ImportSpecifier';
      const decl = p && p.type === 'FunctionDeclaration' && p.id === n && file.endsWith('core/html.js');
      if (!tag && !imp && !decl) at(n, 'html used other than as a tag');
    }
    if (n.type === 'ImportSpecifier') {
      if (n.imported.type !== 'Identifier') at(n, 'string-literal import name');
      else if (OWN.has(n.imported.name) && n.local.name !== n.imported.name) at(n, 'aliased ' + n.imported.name);
      if (OWN.has(n.local.name) && !isHtmlModule(p.source.value)) at(n, n.local.name + ' imported from ' + p.source.value);
    }
    if (n.type === 'ExportSpecifier' && (n.local.type !== 'Identifier' || n.exported.type !== 'Identifier')) at(n, 'string-literal export name');
    if ((n.type === 'ImportNamespaceSpecifier' || n.type === 'ImportDefaultSpecifier') && isHtmlModule(p.source.value)) at(n, 'non-plain import of html.js');
    if (n.type === 'ImportExpression' || n.type === 'ExportAllDeclaration') at(n, n.type);
  });
  return out;
}

test('AST scan: src/ has exactly one markup sink (setHTML) and no banned shape', () => {
  const hits = walk(SRC).filter((f) => f.endsWith('.js')).flatMap((f) => scanSource(relative(SRC, f).split(sep).join('/'), readFileSync(f, 'utf8')));
  assert.equal(hits.length, 1, hits.join('\n'));
  assert.match(hits[0], /^core\/html\.js:\d+ write innerHTML$/);
});

test('AST scan independently catches every must-fail fixture except lint-comment ones', () => {
  for (const [name, code] of Object.entries(MUST_FAIL)) {
    if (/Disabled$|onAttr|srcdocAttr|computedAttr/.test(name)) continue; // setAttribute literals and comments are lint-only
    assert.ok(scanSource('views/x.js', code).length > 0, name);
  }
});

test('the committed app.js, app.css, fonts.css and .eslintrc.json are what the sources build', async () => {
  const out = await outputs();
  for (const [rel, text] of Object.entries(out)) {
    const committed = readFileSync(join(STATIC, rel), 'utf8').replace(/\r\n/g, '\n');
    assert.ok(committed === text, rel + ' is stale: run `npm run build`');
  }
});
