// The one way markup reaches the page.
//
// Every innerHTML in the dashboard goes through setHTML(), and setHTML() only
// accepts a value produced by the `html` tagged template below. Three layers
// keep that true, and each is tested to fail without it:
//
// 1. Static: eslint.config.mjs lets `html` appear ONLY as the tag of a tagged
//    template (html`...`) imported under its own name - never called, aliased,
//    namespaced, re-exported, stored or shadowed - so the strings array it
//    receives is always a template object the engine built from source text.
//    It also rejects every other markup sink, in dotted and computed form.
//    Inline eslint-disable comments are not honoured in src/.
// 2. Runtime, literal text: each template's literal parts are checked once
//    (cached by identity) against a policy our own templates satisfy - no
//    event-handler attributes, no script-capable elements, no javascript:,
//    srcdoc or formaction. A strings array forged from runtime text (frozen,
//    with a frozen `raw`) that carries an <img onerror> is refused here even
//    if it got past layer 1.
// 3. Runtime, interpolation context: each `${}` hole is classified by where
//    it sits. Text and quoted attribute values are escaped; a hole in a URL
//    attribute (href, src, ...) needs a safeUrl() value; a hole in tag
//    position (an attribute list) needs html`` output; a hole in an unquoted
//    attribute value, an event-handler attribute, a comment or raw text is an
//    error. Escaping is only a guarantee in the contexts it was designed for,
//    so the others are refused rather than left to the caller's memory.

const SAFE = new WeakSet();
const SAFE_URL = new WeakSet();

function mark(s) {
  const o = Object.freeze({ s });
  SAFE.add(o);
  return o;
}

export function isSafe(v) {
  return v !== null && typeof v === 'object' && SAFE.has(v);
}
export function isSafeUrl(v) {
  return v !== null && typeof v === 'object' && SAFE_URL.has(v);
}

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' };

/** Escape a value for use as HTML text or inside a quoted attribute. */
export function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"'`]/g, (c) => ESC[c]);
}

const empty = (v) => v === null || v === undefined || v === false || v === '';

function part(v) {
  if (empty(v)) return '';
  if (isSafe(v)) return v.s;
  if (isSafeUrl(v)) return esc(v.u);
  if (Array.isArray(v)) return v.map(part).join('');
  return esc(v);
}

// ---- layer 2: literal-text policy ----------------------------------------
const DENY_LITERAL = [
  // An event-handler attribute, wherever it is spelled (an attribute-list
  // fragment like symTrigger's is plain text until it is spliced into a tag).
  /(^|[\s"'/`])on[a-z]+\s*=/i,
  // Elements that run script, load documents or rewrite the page.
  /<\s*\/?\s*(script|iframe|frame|frameset|object|embed|applet|base|meta|link|form|style|template|noscript|math|animate|set|foreignobject|handler)\b/i,
  /javascript:|vbscript:|livescript:|data:text\/html|srcdoc|formaction|xlink:href|<!--/i,
];

// ---- layer 3: interpolation contexts -------------------------------------
const URL_ATTRS = new Set(['href', 'src', 'action', 'formaction', 'srcset', 'poster', 'background', 'cite', 'data', 'ping', 'xlink:href']);

/**
 * Classify every hole of a template by the context the literal text puts it
 * in. A small HTML tokenizer over the literal parts only - the holes are
 * assumed not to change the state, which holds for every allowed value: text
 * is escaped, and an attribute-list fragment is itself a complete html``.
 */
function classify(strings) {
  const holes = [];
  let st = 'text', attr = '';
  for (let i = 0; i < strings.length; i++) {
    const s = strings[i];
    for (let j = 0; j < s.length; j++) {
      const c = s[j];
      switch (st) {
        case 'text':
          // A '<' right before a hole would let the hole name the tag.
          if (c === '<' && (j === s.length - 1 ? i < strings.length - 1 : /[a-zA-Z/!]/.test(s[j + 1]))) st = 'tagname';
          break;
        case 'tagname':
          if (c === '>') st = 'text';
          else if (/\s/.test(c)) st = 'tag';
          break;
        case 'tag':
          if (c === '>') st = 'text';
          else if (c === '/') { /* self-closing */ }
          else if (!/\s/.test(c)) { st = 'attrname'; attr = c.toLowerCase(); }
          break;
        case 'attrname':
          if (c === '=') st = 'beforevalue';
          else if (c === '>') st = 'text';
          else if (/\s/.test(c)) st = 'afterattr';
          else attr += c.toLowerCase();
          break;
        case 'afterattr':
          if (c === '=') st = 'beforevalue';
          else if (c === '>') st = 'text';
          else if (!/\s/.test(c)) { st = 'attrname'; attr = c.toLowerCase(); }
          break;
        case 'beforevalue':
          if (c === '"') st = 'dq';
          else if (c === '\'') st = 'sq';
          else if (c === '>') st = 'text';
          else if (!/\s/.test(c)) st = 'unquoted';
          break;
        case 'dq': if (c === '"') st = 'tag'; break;
        case 'sq': if (c === '\'') st = 'tag'; break;
        case 'unquoted':
          if (/\s/.test(c)) st = 'tag';
          else if (c === '>') st = 'text';
          break;
        default: break;
      }
    }
    if (i < strings.length - 1) {
      if (st === 'text') holes.push({ kind: 'text' });
      else if (st === 'dq' || st === 'sq') holes.push({ kind: URL_ATTRS.has(attr) ? 'url' : 'attr', attr });
      else if (st === 'tag' || st === 'afterattr') holes.push({ kind: 'tag' });
      else holes.push({ kind: 'bad', why: st === 'tagname' ? 'a tag name' : st === 'attrname' ? 'an attribute name' : 'an unquoted attribute value' });
    }
  }
  return holes;
}

const PLANS = new WeakMap();
function plan(strings) {
  let p = PLANS.get(strings);
  if (p) return p;
  const literal = strings.join('\u0000');
  for (const re of DENY_LITERAL) {
    if (re.test(literal)) throw new TypeError('html: template literal text is not allowed (' + re.source.slice(0, 40) + '…)');
  }
  p = classify(strings);
  PLANS.set(strings, p);
  return p;
}

function isTemplateStrings(strings) {
  return Array.isArray(strings) && Object.isFrozen(strings) &&
    Array.isArray(strings.raw) && Object.isFrozen(strings.raw) && strings.raw.length === strings.length &&
    strings.every((s) => typeof s === 'string');
}

/**
 * Tagged template: html`<b>${text}</b>`. See the header for the contexts a
 * hole may sit in. Arrays are joined (each element escaped unless it is
 * html`` output); null, undefined, false and '' render as nothing, so
 * `${cond && html`…`}` works.
 */
export function html(strings, ...values) {
  if (!isTemplateStrings(strings)) throw new TypeError('html must be used as a tagged template');
  const holes = plan(strings);
  let out = strings[0];
  for (let i = 0; i < values.length; i++) {
    const v = values[i], h = holes[i];
    if (h.kind === 'bad') throw new TypeError('html: interpolation into ' + h.why + ' is not supported');
    if (h.kind === 'tag' && !empty(v) && !isSafe(v)) throw new TypeError('html: only html`` output may be interpolated into a tag');
    if (h.kind === 'url' && !empty(v) && !isSafeUrl(v)) throw new TypeError('html: ' + h.attr + ' needs a safeUrl() value');
    out += part(v) + strings[i + 1];
  }
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
  // flow; this file alone is exempt from the sink rule (eslint.config.mjs).
  el.innerHTML = content.s; // nosemgrep
}

const SAFE_SCHEMES = /^(https?:)\/\//i;
const DATA_IMAGE = /^data:image\/(png|jpe?g|gif|webp|svg\+xml);base64,[a-z0-9+/=\s]+$/i;

/**
 * A URL that may become an href or src. Only absolute http(s) URLs pass, plus
 * base64 raster/SVG data images when `allowDataImage` is set (the self-hosted
 * logos). Anything else - javascript:, vbscript:, data:text/html, a relative
 * path - comes back as null and the caller renders no link. The result is a
 * branded value: html`` accepts nothing else in a URL attribute.
 */
export function safeUrl(u, { allowDataImage = false } = {}) {
  if (typeof u !== 'string') return null;
  const t = u.trim();
  if (!SAFE_SCHEMES.test(t) && !(allowDataImage && DATA_IMAGE.test(t))) return null;
  const o = Object.freeze({ u: t, toString() { return t; } });
  SAFE_URL.add(o);
  return o;
}
