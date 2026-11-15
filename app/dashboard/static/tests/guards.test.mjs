// The guards that keep the XSS fix whole: every lint rule fires on its sink
// (dotted, computed, aliased, namespaced and inline-disabled forms), an AST
// scan of src/ finds exactly one markup sink and no non-tag use of `html`, and
// the committed outputs (bundle, CSS, Codacy's eslintrc) are what the sources
// build.

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
// Each must produce a no-restricted-syntax error. B1 and B2 are the shapes
// QA showed passing the previous rules (qa-pr-122.md).
const MUST_FAIL = {
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
  // B1: computed spellings.
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
  objectAssign: 'export function f(el, x) { Object.assign(el, { innerHTML: x }); }',
  objectAssignHandler: "export function f(el, x) { Object.assign(el, { 'onclick': x }); }",
  defineProperty: "export function f(el, x) { Object.defineProperty(el, 'innerHTML', { value: x }); }",
  reflect: "export function f(el, x) { Reflect.set(el, 'innerHTML', x); }",
  // B2: html used other than as a tag.
  htmlAsCall: IMPORT + "export function f(x) { return html(['<b>' + x]); }",
  aliasTemplate: "import { html as h, setHTML } from '../core/html.js';\nexport function f(el, x) { const raw = Object.freeze([x]); const a = Object.freeze(Object.assign([x], { raw })); setHTML(el, h(a)); }",
  aliasSetHTML: "import { setHTML as put } from '../core/html.js';\nexport function f(el, x) { put(el, x); }",
  namespace: "import * as H from '../core/html.js';\nexport function f(el, x) { H.setHTML(el, H.html([x])); }",
  stored: IMPORT + 'const t = html; export function f(x) { return t([x]); }',
  passed: IMPORT + 'export function f(g, x) { return g(html, x); }',
  applied: IMPORT + 'export function f(x) { return html.call(null, [x]); }',
  shadowed: "export function f(x) { const html = (s) => s; return html`<b>${x}</b>`; }",
  shadowParam: 'export function f(html, x) { return html`<b>${x}</b>`; }',
  reexport: "export { html } from '../core/html.js';",
  reexportAll: "export * from '../core/html.js';",
  dynamicImport: "export async function f() { const m = await import('../core/html.js'); return m.html; }",
  // Inline disable comments are not honoured in src/.
  inlineDisabled: 'export function f(el, x) {\n  // eslint-disable-next-line no-restricted-syntax\n  el.innerHTML = x;\n}',
  blockDisabled: '/* eslint-disable */\nexport function f(el, x) { el.innerHTML = x; }',
};

test('every markup sink and every non-tag use of html is a lint error (B1, B2)', async () => {
  const eslint = new ESLint({ cwd: REPO });
  for (const [name, code] of Object.entries(MUST_FAIL)) {
    const [res] = await eslint.lintText(code, { filePath: join(SRC, 'views', 'fixture-' + name + '.js') });
    const hits = res.messages.filter((m) => m.ruleId === 'no-restricted-syntax');
    assert.ok(hits.length > 0, name + ' was not flagged: ' + JSON.stringify(res.messages));
  }
  // The sanctioned path stays clean, including the html`` attribute-list form.
  const [ok] = await eslint.lintText(IMPORT +
    'export function f(el, x, u) { setHTML(el, html`<a class="x" ${html`data-a="1"`} href="${u}">${x}</a>`); }\n' +
    'export function g(m, k, v) { m[k] = v; return m[0]; }\n',
  { filePath: join(SRC, 'views', 'fixture-ok.js') });
  assert.deepEqual(ok.messages, []);
});

// ---- AST scan of src/ (independent of the lint configuration) -------------
const SINK_NAMES = new Set(['innerHTML', 'outerHTML', 'srcdoc', 'insertAdjacentHTML', 'insertAdjacentElement', 'createContextualFragment',
  'parseFromString', 'write', 'writeln', 'setHTMLUnsafe', 'setAttributeNS', 'setAttributeNode']);
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
    if (k === 'parent') continue;
    const v = node[k];
    if (Array.isArray(v)) v.forEach((c) => visit(c, node, fn));
    else if (v && typeof v.type === 'string') visit(v, node, fn);
  }
}
export function scanSource(file, code) {
  const ast = espree.parse(code, { ecmaVersion: 2022, sourceType: 'module', loc: true });
  const out = [];
  const at = (n, what) => out.push(file + ':' + n.loc.start.line + ' ' + what);
  visit(ast, null, (n, p) => {
    if (n.type === 'MemberExpression') {
      const name = propName(n);
      const isWrite = p && p.type === 'AssignmentExpression' && p.left === n;
      const isCall = p && p.type === 'CallExpression' && p.callee === n;
      if (name && SINK_NAMES.has(name) && (isWrite || isCall)) at(n, (isWrite ? 'write ' : 'call ') + name);
      if (name && /^on[a-z]+$/i.test(name) && isWrite) at(n, 'handler ' + name);
      if (n.computed && isCall && !(n.property.type === 'Literal' && typeof n.property.value === 'number')) at(n, 'computed call');
    }
    if (n.type === 'Identifier' && n.name === 'html') {
      const tag = p && p.type === 'TaggedTemplateExpression' && p.tag === n;
      const imp = p && p.type === 'ImportSpecifier';
      const decl = p && p.type === 'FunctionDeclaration' && p.id === n && file.endsWith('html.js');
      if (!tag && !imp && !decl) at(n, 'html used other than as a tag');
    }
    if (n.type === 'ImportSpecifier' && /^(html|setHTML)$/.test(n.imported.name) && n.local.name !== n.imported.name) at(n, 'aliased ' + n.imported.name);
    if (n.type === 'ImportNamespaceSpecifier' && /core\/html(\.js)?$/.test(p.source.value)) at(n, 'namespace import of html.js');
  });
  return out;
}

test('AST scan: src/ has exactly one markup sink (setHTML) and uses html only as a tag', () => {
  const hits = walk(SRC).filter((f) => f.endsWith('.js')).flatMap((f) => scanSource(relative(SRC, f).split(sep).join('/'), readFileSync(f, 'utf8')));
  assert.equal(hits.length, 1, hits.join('\n'));
  assert.match(hits[0], /^core\/html\.js:\d+ write innerHTML$/);
});

test('AST scan catches the shapes the old regex scan missed', () => {
  for (const name of ['bracketInnerHTML', 'bracketHandler', 'bracketInsert', 'bracketWrite', 'dynamicCall', 'aliasTemplate', 'namespace', 'stored', 'shadowed', 'templateKey']) {
    assert.ok(scanSource('views/x.js', MUST_FAIL[name]).length > 0, name);
  }
});

test('the committed app.js, app.css, fonts.css and .eslintrc.json are what the sources build', async () => {
  const out = await outputs();
  for (const [rel, text] of Object.entries(out)) {
    const committed = readFileSync(join(STATIC, rel), 'utf8').replace(/\r\n/g, '\n');
    assert.ok(committed === text, rel + ' is stale: run `npm run build`');
  }
});
