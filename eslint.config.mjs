// ESLint for the dashboard front end (frontend.yml runs it with
// --max-warnings 0). .eslintrc.json carries the same rules in ESLint 8 form
// for Codacy; tests/guards.test.mjs fails if the two drift apart.
//
// The no-restricted-syntax rules are the static layer of the XSS guarantee
// described in app/dashboard/static/src/core/html.js: markup reaches the page
// only through setHTML(el, html`...`). Every other sink is an error in both
// its dotted and its computed spelling, `html` may appear only as a template
// tag, and inline eslint-disable comments are ignored in src/ so a rule cannot
// be switched off where it matters. tests/guards.test.mjs lints a fixture for
// every rule and proves each one still fires.

import js from '@eslint/js';
import globals from 'globals';

const SINK_PROPS = 'innerHTML|outerHTML|srcdoc|insertAdjacentHTML|insertAdjacentElement|createContextualFragment|parseFromString|write|writeln|setAttributeNS|setAttributeNode|setHTMLUnsafe';

/** Markup and handler sinks, dotted or computed. */
export const SINKS = [
  { selector: "AssignmentExpression > MemberExpression.left[property.name=/^(innerHTML|outerHTML|srcdoc)$/]",
    message: 'Write markup with setHTML(el, html`...`) from core/html.js; it escapes every interpolation.' },
  { selector: "AssignmentExpression > MemberExpression.left[computed=true][property.value=/^(innerHTML|outerHTML|srcdoc|on.*)$/i]",
    message: 'A computed write to a markup or event-handler property is still a sink.' },
  { selector: "AssignmentExpression > MemberExpression.left[computed=true][property.type=/^(TemplateLiteral|BinaryExpression|CallExpression|ConditionalExpression|LogicalExpression)$/]",
    message: 'Computed member writes may not build their key at run time: the key could be innerHTML or on*.' },
  { selector: "AssignmentExpression > MemberExpression.left[property.name=/^on[a-z]+$/i]",
    message: 'No on* property handlers; use addEventListener.' },
  { selector: `CallExpression > MemberExpression.callee[property.name=/^(${SINK_PROPS})$/]`,
    message: 'This call parses or injects markup without the escaping template.' },
  { selector: "CallExpression > MemberExpression.callee[computed=true][property.type!='Literal']",
    message: 'No computed method calls: el[name](...) can reach insertAdjacentHTML or document.write.' },
  { selector: `CallExpression > MemberExpression.callee[computed=true][property.value=/^(${SINK_PROPS}|setAttribute)$/]`,
    message: 'A computed call to a markup sink is still a sink.' },
  { selector: "CallExpression[callee.property.name='setAttribute'][arguments.0.type='Literal'][arguments.0.value=/^on/i]",
    message: 'No inline event-handler attributes; use addEventListener.' },
  { selector: "CallExpression[callee.property.name='setAttribute'][arguments.0.type='Literal'][arguments.0.value=/^(srcdoc|formaction)$/i]",
    message: 'srcdoc and formaction are markup/navigation sinks.' },
  { selector: "CallExpression[callee.property.name='setAttribute'][arguments.0.type!='Literal']",
    message: 'setAttribute with a computed name could set an event handler. Name the attribute literally.' },
  { selector: "Property[key.name=/^(innerHTML|outerHTML|srcdoc)$/], Property[key.value=/^(innerHTML|outerHTML|srcdoc|on.*)$/i]",
    message: 'An object literal can carry a markup property into Object.assign/defineProperty.' },
  { selector: "MemberExpression[object.name='Reflect'][property.name=/^(set|apply|defineProperty)$/]",
    message: 'Reflect.set/apply/defineProperty can reach any sink by name.' },
  { selector: "CallExpression[callee.object.name='Object'][callee.property.name=/^(defineProperty|defineProperties)$/]",
    message: 'Object.defineProperty can install a markup setter or handler.' },
];

/** `html` is a template tag and nothing else. */
export const HTML_USAGE = [
  { selector: "Identifier[name='html']:not(TaggedTemplateExpression > Identifier.tag):not(ImportSpecifier > Identifier)",
    message: 'html may only be used as a tag: html`...`. Calling, storing, passing or shadowing it lets runtime text pose as a template.' },
  { selector: "ImportSpecifier[imported.name=/^(html|setHTML)$/][local.name!=/^(html|setHTML)$/]",
    message: 'Import html and setHTML under their own names, so the tag rule can see every use.' },
  { selector: "ImportSpecifier[imported.name='html'][local.name='setHTML'], ImportSpecifier[imported.name='setHTML'][local.name='html']",
    message: 'Import html and setHTML under their own names.' },
  { selector: "ImportDeclaration[source.value=/core\\/html(\\.js)?$/] > ImportNamespaceSpecifier, ImportDeclaration[source.value=/core\\/html(\\.js)?$/] > ImportDefaultSpecifier",
    message: 'No namespace or default import of core/html.js: it would allow html to be called as a property.' },
  { selector: "ImportExpression, ExportAllDeclaration, ExportSpecifier[local.name=/^(html|setHTML)$/]",
    message: 'html and setHTML are imported statically from core/html.js and never re-exported.' },
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

// core/html.js defines `html` and holds the one sanctioned innerHTML write;
// it is exempt from exactly those two rules and from nothing else.
export const HTML_JS_SINKS = SINKS.filter((s) => !s.selector.startsWith('AssignmentExpression > MemberExpression.left[property.name=/^(innerHTML'));

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
    // An inline eslint-disable could switch off the sink rules where it
    // matters; in src/ such comments are ignored.
    linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: 'off' },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, DATA: 'readonly' },
    },
    rules: SRC_RULES,
  },
  {
    files: ['app/dashboard/static/src/core/html.js'],
    rules: { 'no-restricted-syntax': ['error', ...HTML_JS_SINKS] },
  },
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
