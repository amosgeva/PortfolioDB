// Loaded with `node --import` by `npm test`. src/ imports nav.json the way
// esbuild bundles it - a plain `import NAV from '../../nav.json'`, a default
// export of the parsed file - because the import-attribute spelling Node
// needs (`with { type: 'json' }`) is ES2025 and Codacy's ESLint 8 cannot
// parse it. This hook gives the tests esbuild's reading of a .json import, so
// modules under src/ can be imported straight from Node as before.
import { register } from 'node:module';

register('data:text/javascript,' + encodeURIComponent(`
  import { readFileSync } from 'node:fs';
  import { fileURLToPath } from 'node:url';
  export async function load(url, context, next) {
    if (url.startsWith('file:') && url.endsWith('.json') && !(context.importAttributes && context.importAttributes.type)) {
      const text = readFileSync(fileURLToPath(url), 'utf8');
      JSON.parse(text);
      return { format: 'module', source: 'export default ' + text + ';', shortCircuit: true };
    }
    return next(url, context);
  }
`));
