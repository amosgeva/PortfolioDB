// The one way markup reaches the page.
//
// setHTML(el, x) is the only innerHTML write in src/, and it accepts only a
// value produced by the `html` tagged template below. What holds, and why:
//
// GUARANTEED AT RUN TIME, for every template object the engine built from
// source text (a genuine html`...` call site):
//   * Every interpolated runtime value is either escaped or one of three
//     branded kinds, and each kind is accepted only in the one slot it was made
//     for - nothing is promoted by being nested:
//       - text slot (between tags):   strings (escaped), Markup (html``)
//       - quoted attribute value:     strings (escaped) - Markup is refused
//       - URL attribute (href, src):  SafeUrl (safeUrl()) only
//       - attribute-list slot <a ${}>: AttrList (attrs()) only, whose names
//         are checked against an allow-list (no on*, no URL attributes)
//     An unquoted attribute value, a tag or attribute name, a comment or raw
//     text slot is refused outright.
//   * Every template must end where it began, outside any tag, so a fragment
//     cannot leave its parent inside an open tag for the next slot to fill.
//   * Literal text is checked once per template (cached by identity): no
//     event-handler attributes, script-capable elements, comments,
//     javascript:/srcdoc/formaction/xlink:href - also after decoding character
//     references.
//
// GUARANTEED BY LINT AND THE AST GUARD (eslint.config.mjs, tests/guards.test.mjs):
//   * `html` is used only as a template tag, imported as exactly
//     `import { html, … } from '…/core/html.js'` - no call, alias, quoted
//     import name, namespace, re-export, storage or shadowing. This is what
//     makes "genuine template object" true: at run time a frozen array with a
//     frozen `raw` cannot be told apart from one, so forging is stopped here.
//   * No other markup sink, read or written, dotted or computed; no computed
//     member write or call at all outside core/dict.js; no reflection,
//     prototype access or Object.assign/defineProperty route to a sink.
//   * Inline eslint-disable comments are ignored in src/.

const MARKUP = new WeakSet();
const URLS = new WeakSet();
const ATTRS = new WeakSet();

function brand(set, o) { set.add(Object.freeze(o)); return o; }

export function isSafe(v) { return v !== null && typeof v === 'object' && MARKUP.has(v); }
export function isSafeUrl(v) { return v !== null && typeof v === 'object' && URLS.has(v); }
export function isAttrList(v) { return v !== null && typeof v === 'object' && ATTRS.has(v); }
const isBranded = (v) => isSafe(v) || isSafeUrl(v) || isAttrList(v);

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' };

/** Escape a value for use as HTML text or inside a quoted attribute. */
export function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"'`]/g, (c) => ESC[c]);
}

const empty = (v) => v === null || v === undefined || v === false || v === '';

/** Render one interpolated value for a slot kind, or throw. */
function render(v, kind, attr) {
  if (empty(v)) return '';
  if (Array.isArray(v)) return v.map((x) => render(x, kind, attr)).join('');
  switch (kind) {
    case 'text':
      if (isSafe(v)) return v.s;
      if (isAttrList(v)) throw new TypeError('html: an attrs() list belongs in a tag, not in text');
      return esc(isSafeUrl(v) ? v.u : v);
    case 'attr':
      if (isSafe(v) || isAttrList(v)) throw new TypeError('html: markup may not be interpolated into the ' + attr + ' attribute');
      return esc(isSafeUrl(v) ? v.u : v);
    case 'url':
      if (!isSafeUrl(v)) throw new TypeError('html: ' + attr + ' needs a safeUrl() value');
      return esc(v.u);
    case 'tag':
      if (!isAttrList(v)) throw new TypeError('html: only an attrs() list may be interpolated into a tag');
      return v.s;
    default:
      throw new TypeError('html: unsupported slot');
  }
}

// ---- literal-text policy -------------------------------------------------
const DENY_LITERAL = [
  /(^|[\s"'/`=])on[a-z]+\s*=/i,
  /<\s*\/?\s*(script|iframe|frame|frameset|object|embed|applet|base|meta|link|form|style|template|noscript|math|animate|set|foreignobject|handler|xmp|plaintext)\b/i,
  /javascript:|vbscript:|livescript:|data:text\/html|srcdoc|formaction|xlink:href|<!--|<!\[cdata\[|<\?/i,
];
const NAMED_REFS = { colon: ':', tab: '\t', newline: '\n', lpar: '(', rpar: ')', sol: '/', equals: '=', lt: '<', gt: '>', quot: '"', apos: '\'', amp: '&' };
/** Literal text with character references decoded and control/space runs
 *  removed, so `java&#115;cript:` or `on&#99;lick=` are seen for what they are. */
function decoded(s) {
  return s.replace(/&#x([0-9a-f]+);?/gi, (m, h) => String.fromCodePoint(Number.parseInt(h, 16) || 32))
    .replace(/&#(\d+);?/g, (m, d) => String.fromCodePoint(Number(d) || 32))
    .replace(/&([a-z]+);/gi, (m, n) => (Object.hasOwn(NAMED_REFS, n.toLowerCase()) ? NAMED_REFS[n.toLowerCase()] : m));
}
function checkLiteral(literal) {
  const dec = decoded(literal);
  const squeezed = Array.from(dec).filter((ch) => ch.codePointAt(0) > 32).join('');
  const variants = [literal, dec, squeezed];
  for (const text of variants) {
    for (const re of DENY_LITERAL) {
      if (re.test(text)) throw new TypeError('html: template literal text is not allowed (' + re.source.slice(0, 40) + '…)');
    }
  }
}

// ---- slot classification -------------------------------------------------
const URL_ATTRS = new Set(['href', 'src', 'action', 'formaction', 'srcset', 'poster', 'background', 'cite', 'data', 'ping', 'xlink:href', 'manifest', 'codebase', 'longdesc', 'usemap']);

/**
 * Classify every hole by the context the literal text puts it in, with a
 * small HTML tokenizer over the literal parts. Holes do not change the state:
 * escaped text cannot, Markup is only accepted in text slots and is itself a
 * balanced template, and an AttrList is a complete `name="value"` sequence.
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
          if (c === '<' && (j === s.length - 1 ? i < strings.length - 1 : /[a-zA-Z/!?]/.test(s[j + 1]))) st = 'tagname';
          break;
        case 'tagname':
          if (c === '>') st = 'text';
          else if (/\s/.test(c)) st = 'tag';
          break;
        case 'tag':
          if (c === '>') st = 'text';
          else if (c !== '/' && !/\s/.test(c)) { st = 'attrname'; attr = c.toLowerCase(); }
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
  if (st !== 'text') throw new TypeError('html: a template must end outside any tag (it ends inside ' + st + ')');
  return holes;
}

const PLANS = new WeakMap();
function plan(strings) {
  let p = PLANS.get(strings);
  if (p) return p;
  checkLiteral(strings.join('\u0000'));
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
 * Tagged template: html`<b>${text}</b>` returns Markup for a text slot.
 * Arrays are joined, each element rendered for the slot; null, undefined,
 * false and '' render as nothing, so `${cond && html`…`}` works.
 */
export function html(strings, ...values) {
  if (!isTemplateStrings(strings)) throw new TypeError('html must be used as a tagged template');
  const holes = plan(strings);
  let out = strings[0];
  for (let i = 0; i < values.length; i++) {
    const h = holes[i];
    if (h.kind === 'bad') throw new TypeError('html: interpolation into ' + h.why + ' is not supported');
    out += render(values[i], h.kind, h.attr) + strings[i + 1];
  }
  return brand(MARKUP, { s: out });
}

/** Join fragments with a separator, for a text slot. */
export function join(items, sep) {
  const s = sep === undefined ? '' : render(sep, 'text');
  return brand(MARKUP, { s: items.map((x) => render(x, 'text')).join(s) });
}

// Attribute names attrs() may set. No event handlers (on*), no URL-bearing
// attributes (href/src/... go in the template literally, with a safeUrl()
// value), no srcdoc/formaction/style-with-url tricks.
const ATTR_NAME = /^(?:(?:data|aria)-[a-z0-9-]+|role|tabindex|class|title|id|opacity|type|name|value|alt|lang|dir|hidden|disabled)$/;

/**
 * An attribute list for a tag slot: attrs({ 'data-sym': s, role: 'button' })
 * renders ` data-sym="…" role="button"`. Names must be on the allow-list;
 * values are escaped; null/undefined/false values are omitted.
 */
export function attrs(map) {
  let s = '';
  for (const name of Object.keys(map)) {
    if (!ATTR_NAME.test(name)) throw new TypeError('attrs: attribute "' + name + '" is not allowed');
    const v = map[name];
    if (v === null || v === undefined || v === false) continue;
    if (isBranded(v) && !isSafeUrl(v)) throw new TypeError('attrs: ' + name + ' takes text, not markup');
    s += ' ' + name + '="' + esc(isSafeUrl(v) ? v.u : v) + '"';
  }
  return brand(ATTRS, { s });
}

/** The only innerHTML assignment in the front end. */
export function setHTML(el, content) {
  if (!el) return;
  if (!isSafe(content)) throw new TypeError('setHTML accepts html`` output only');
  // The single audited sink: content.s exists only on html`` output.
  // This file alone is exempt from the sink rule (eslint.config.mjs).
  el.innerHTML = content.s; // nosemgrep
}

const SAFE_SCHEMES = /^(https?:)\/\//i;
const DATA_IMAGE = /^data:image\/(png|jpe?g|gif|webp|svg\+xml);base64,[a-z0-9+/=\s]+$/i;

/**
 * A URL that may become an href or src: absolute http(s), plus base64 data
 * images when `allowDataImage` is set (the self-hosted logos). Anything else
 * - javascript:, vbscript:, data:text/html, a relative path - is null and the
 * caller renders no link. The result is branded: a URL slot accepts nothing else.
 */
export function safeUrl(u, { allowDataImage = false } = {}) {
  if (typeof u !== 'string') return null;
  const t = u.trim();
  if (!SAFE_SCHEMES.test(t) && !(allowDataImage && DATA_IMAGE.test(t))) return null;
  return brand(URLS, { u: t, toString() { return t; } });
}
