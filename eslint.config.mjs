// ESLint for the dashboard front end (frontend.yml runs it with
// --max-warnings 0). .eslintrc.json is rendered from this file by build.mjs
// for Codacy (ESLint 8), so the two cannot drift.
//
// The no-restricted-syntax rules are the static half of the guarantee stated
// at the top of app/dashboard/static/src/core/html.js. They ban SHAPES, not
// spellings, because spellings can always be rebuilt at run time:
//   * no computed member access at all - read, write or call - outside
//     core/dict.js (`item`/`put`, plain data only), so `el[k] = x`,
//     `el[m](x)` and `const fn = el[k]; fn.call(...)` are all unwritable;
//   * no reference at all - read, write, call or destructure - to a markup
//     sink name outside core/html.js, so `const f = el.insertAdjacentHTML`
//     is caught too;
//   * no reflection, prototype access or Object.assign/defineProperty route;
//   * `html` only as a template tag, imported from core/html.js under its own
//     name with an identifier (not a quoted string) import name.
// Inline eslint-disable comments are ignored in src/. tests/guards.test.mjs
// lints a must-fail fixture per rule and runs an independent AST scan.

import js from '@eslint/js';
import globals from 'globals';

const SINK_NAMES = 'innerHTML|outerHTML|srcdoc|insertAdjacentHTML|insertAdjacentElement|createContextualFragment|parseFromString|' +
  'write|writeln|setAttributeNS|setAttributeNode|setHTMLUnsafe|execCommand|__defineSetter__|__lookupSetter__|__proto__';
const HTML_JS = '/core\\/html(\\.js)?$/';
const OWN_EXPORTS = ['html', 'setHTML', 'attrs', 'safeUrl', 'esc', 'join', 'isSafe', 'isSafeUrl', 'isAttrList'];

/** Markup and handler sinks, and every shape that could reach one by name. */
export const SINKS = [
  { selector: `MemberExpression[property.name=/^(${SINK_NAMES})$/]`,
    message: 'Markup sinks are reached only through setHTML(el, html`...`) from core/html.js.' },
  { selector: `MemberExpression[computed=true][property.value=/^(${SINK_NAMES}|on.*)$/i]`,
    message: 'A computed reference to a markup or event-handler property is still a sink.' },
  { selector: 'MemberExpression[computed=true]',
    message: 'No computed member access (read, write or call): a key held in a variable can name a sink. Use item()/put() from core/dict.js, Map, or Array#at.' },
  { selector: 'AssignmentExpression > MemberExpression.left[property.name=/^on[a-z]+$/i]',
    message: 'No on* property handlers; use addEventListener.' },
  { selector: "CallExpression[callee.property.name='setAttribute'][arguments.0.type='Literal'][arguments.0.value=/^(on.*|srcdoc|formaction)$/i]",
    message: 'No event-handler, srcdoc or formaction attributes.' },
  { selector: "CallExpression[callee.property.name='setAttribute'][arguments.0.type!='Literal']",
    message: 'setAttribute with a computed name could set an event handler. Name the attribute literally.' },
  { selector: 'Property[computed=true]',
    message: 'No computed object keys: { [k]: v } can carry innerHTML into Object.assign or a spread.' },
  { selector: `Property[key.name=/^(${SINK_NAMES})$/], Property[key.value=/^(${SINK_NAMES}|on.*)$/i]`,
    message: 'An object key naming a markup sink or handler can be copied onto an element.' },
  { selector: "Identifier[name='Reflect']",
    message: 'Reflect can reach any property by name.' },
  { selector: "MemberExpression[object.name='Object'][property.name=/^(assign|defineProperty|defineProperties|setPrototypeOf|getOwnPropertyDescriptor|getOwnPropertyDescriptors)$/]",
    message: 'Object.assign/defineProperty/descriptors can copy a markup property or setter onto an element.' },
  { selector: "MemberExpression[property.name=/^(prototype|constructor)$/]",
    message: 'No prototype or constructor access: Element.prototype descriptors are a route to innerHTML.' },
];

/** `html` is a template tag, imported plainly from core/html.js. */
export const HTML_USAGE = [
  { selector: "Identifier[name='html']:not(TaggedTemplateExpression > Identifier.tag):not(ImportSpecifier > Identifier)",
    message: 'html may only be used as a tag: html`...`. Calling, storing, passing or shadowing it lets runtime text pose as a template.' },
  { selector: "ImportSpecifier[imported.type='Literal'], ExportSpecifier[local.type='Literal'], ExportSpecifier[exported.type='Literal']",
    message: 'No string-literal import or export names: they hide which binding is which from every check.' },
  ...OWN_EXPORTS.map((name) => ({
    selector: `ImportSpecifier[imported.name='${name}'][local.name!='${name}']`,
    message: 'Import ' + name + ' under its own name, so the checks can see every use.' })),
  { selector: `ImportDeclaration[source.value!=${HTML_JS}] > ImportSpecifier[local.name=/^(html|setHTML|attrs|safeUrl)$/]`,
    message: 'html, setHTML, attrs and safeUrl come from core/html.js only.' },
  { selector: `ImportDeclaration[source.value=${HTML_JS}] > ImportNamespaceSpecifier, ImportDeclaration[source.value=${HTML_JS}] > ImportDefaultSpecifier`,
    message: 'No namespace or default import of core/html.js: it would allow html to be called as a property.' },
  { selector: 'ImportExpression, ExportAllDeclaration, ExportSpecifier[local.name=/^(html|setHTML|attrs|safeUrl)$/]',
    message: 'html and friends are imported statically from core/html.js and never re-exported.' },
];

export const SRC_RULES = {
  'no-restricted-syntax': ['error', ...SINKS, ...HTML_USAGE],
  'no-eval': 'error',
  'no-implied-eval': 'error',
  'no-new-func': 'error',
  'no-script-url': 'error',
  'no-empty': ['error', { allowEmptyCatch: true }],
  'no-unused-vars': ['error', { args: 'none', caughtErrors: 'none' }],
  'no-var': 'error',
  'prefer-const': 'error',
  eqeqeq: ['error', 'always', { null: 'ignore' }],
};

// core/html.js holds the one innerHTML write and defines `html`; core/dict.js
// holds the one computed write and checks prototypes. Each is exempt from
// exactly the rules it exists to implement, and from nothing else.
const without = (list, ...starts) => list.filter((r) => !starts.some((s) => r.selector.startsWith(s)));
export const HTML_JS_RULES = [...without(SINKS, 'MemberExpression[property.name=/^(innerHTML'), ...without(HTML_USAGE, "Identifier[name='html']")];
export const DICT_JS_RULES = [...without(SINKS, 'MemberExpression[computed=true]', 'MemberExpression[property.name=/^(prototype'), ...HTML_USAGE];

export default [
  {
    ignores: [
      'node_modules/**',
      'archive/**',
      // Generated by build.mjs from the linted sources under src/.
      'app/dashboard/static/app.js',
    ],
  },
  js.configs.recommended,
  {
    files: ['app/dashboard/static/src/**/*.js'],
    linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: 'off' },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, DATA: 'readonly' },
    },
    rules: SRC_RULES,
  },
  { files: ['app/dashboard/static/src/core/html.js'], rules: { 'no-restricted-syntax': ['error', ...HTML_JS_RULES] } },
  { files: ['app/dashboard/static/src/core/dict.js'], rules: { 'no-restricted-syntax': ['error', ...DICT_JS_RULES] } },
  {
    files: ['app/dashboard/static/build.mjs', 'app/dashboard/static/tools/**/*.mjs', 'app/dashboard/static/tests/**/*.mjs', 'eslint.config.mjs'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.node },
    },
    rules: {
      'no-empty': ['error', { allowEmptyCatch: true }],
      'no-unused-vars': ['error', { args: 'none', caughtErrors: 'none' }],
    },
  },
];
