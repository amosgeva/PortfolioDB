// Renders .eslintrc.json (ESLint 8 form, read only by Codacy) from
// eslint.config.mjs, so the two can never disagree. `npm run build` writes it;
// tests/guards.test.mjs fails if the committed file is stale.

import { SRC_RULES, HTML_JS_SINKS } from '../../../../eslint.config.mjs';

export function eslintrc() {
  const cfg = {
    root: true,
    noInlineConfig: true,
    env: { browser: true, es2022: true },
    parserOptions: { ecmaVersion: 2022, sourceType: 'module' },
    extends: 'eslint:recommended',
    ignorePatterns: ['archive/', 'node_modules/', 'app/dashboard/static/app.js'],
    globals: { DATA: 'readonly' },
    rules: SRC_RULES,
    overrides: [
      { files: ['app/dashboard/static/src/core/html.js'], rules: { 'no-restricted-syntax': ['error', ...HTML_JS_SINKS] } },
      { files: ['app/dashboard/static/build.mjs', 'app/dashboard/static/tools/**/*.mjs', 'app/dashboard/static/tests/**/*.mjs', 'eslint.config.mjs'],
        env: { browser: false, node: true }, rules: { 'no-restricted-syntax': 'off' } },
    ],
  };
  return JSON.stringify(cfg, null, 2) + '\n';
}
