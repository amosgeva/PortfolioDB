// The one way markup reaches the page.
//
// Every innerHTML in the dashboard goes through setHTML(), and setHTML() only
// accepts a value produced by the `html` tagged template below. The template
// escapes every interpolated value unless that value is itself html`` output,
// so text from the payload (a symbol, a note, an account name, a news title)
// cannot become markup however it is spelled: `<img onerror=…>`, `&#60;…`
// entities and quotes all arrive as text.
//
// This is a structural guarantee, not a convention. The audit that produced it
// found that fixing sinks one at a time leaves the next one: fifteen call sites
// were "individually argued safe" and two of them were not. Here there is no
// second path. eslint.config.mjs fails the build on any innerHTML/outerHTML
// assignment, insertAdjacentHTML or document.write outside this file, and on
// calling html() as a plain function (which would let a concatenated string
// pose as a template literal). tests/xss.test.mjs renders hostile payloads
// through every view and asserts that no element or attribute was injected.

const SAFE = new WeakSet();

function mark(s) {
  const o = Object.freeze({ s });
  SAFE.add(o);
  return o;
}

export function isSafe(v) {
  return v !== null && typeof v === 'object' && SAFE.has(v);
}

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' };

/** Escape a value for use as HTML text or inside a quoted attribute. */
export function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"'`]/g, (c) => ESC[c]);
}

function part(v) {
  if (v === null || v === undefined || v === false) return '';
  if (isSafe(v)) return v.s;
  if (Array.isArray(v)) return v.map(part).join('');
  return esc(v);
}

function isTemplateStrings(strings) {
  // A real template-strings array is frozen and carries a frozen `raw`. A
  // plain array built at run time - the shape a concatenated string would have
  // to take to get in here - is neither.
  return Array.isArray(strings) && Object.isFrozen(strings) &&
    Array.isArray(strings.raw) && Object.isFrozen(strings.raw);
}

/**
 * Tagged template: html`<b>${text}</b>`. Interpolations are escaped; arrays
 * are joined (each element escaped unless it is html`` output); null,
 * undefined and false render as nothing, so `${cond && html`…`}` works.
 */
export function html(strings, ...values) {
  if (!isTemplateStrings(strings)) throw new TypeError('html must be used as a tagged template');
  let out = strings[0];
  for (let i = 0; i < values.length; i++) out += part(values[i]) + strings[i + 1];
  return mark(out);
}

/** Join a list of fragments with a separator (both escaped unless safe). */
export function join(items, sep) {
  const s = sep === undefined ? '' : part(sep);
  return mark(items.map(part).join(s));
}

/** The only innerHTML assignment in the front end. */
export function setHTML(el, content) {
  if (!el) return;
  if (!isSafe(content)) throw new TypeError('setHTML accepts html`` output only');
  // The single audited sink: content.s exists only on html`` output, whose
  // every interpolation was escaped. Static analysers flag the shape, not the
  // flow, hence the two suppressions on exactly this line and nowhere else.
  // eslint-disable-next-line no-restricted-syntax
  el.innerHTML = content.s; // nosemgrep
}

const SAFE_SCHEMES = /^(https?:)\/\//i;
const DATA_IMAGE = /^data:image\/(png|jpe?g|gif|webp|svg\+xml);base64,[a-z0-9+/=\s]+$/i;

/**
 * A URL that may become an href or src. Only absolute http(s) URLs pass, plus
 * base64 raster/SVG data images when `allowDataImage` is set (the self-hosted
 * logos). Anything else - javascript:, vbscript:, data:text/html, a relative
 * path - comes back as null and the caller renders no link.
 */
export function safeUrl(u, { allowDataImage = false } = {}) {
  if (typeof u !== 'string') return null;
  const t = u.trim();
  if (SAFE_SCHEMES.test(t)) return t;
  if (allowDataImage && DATA_IMAGE.test(t)) return t;
  return null;
}
