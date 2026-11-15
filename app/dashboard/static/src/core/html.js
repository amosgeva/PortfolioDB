// The one way markup reaches the page.
//
// setHTML(el, x) is the only innerHTML write in src/, and x must be output of
// the `html` tagged template below. The rules, exactly as enforced:
//
// RUNTIME (core/html.js; applies to every template object html receives)
//  R1 Slots. A `${}` slot may sit in exactly one of these positions; any other
//     position throws when the template is first used:
//       text     - between tags, outside any element listed under R3.
//                  Takes strings/numbers (escaped) or html`` Markup.
//       rcdata   - inside <title>. Takes strings/numbers (escaped) only.
//       attr     - inside a quoted attribute value (whole or part) of a
//                  non-URL attribute. Takes strings/numbers (escaped) only.
//       url      - the ENTIRE quoted value of a URL attribute (href, src, …).
//                  Takes a safeUrl() value only.
//       tag      - an attribute-list position inside a start tag, with
//                  whitespace, '>' or '/' (never a name character) after it.
//                  Takes an attrs() list only.
//     So a slot never sits in a tag name, an attribute name, an unquoted
//     value, beside a name it could extend, or inside a comment.
//  R2 Nothing is promoted by nesting: each branded kind (Markup, SafeUrl,
//     AttrList) is accepted only in the slot made for it; arrays are checked
//     element by element.
//  R3 Literal text is tokenized once per template (cached by identity):
//     every attribute name it completes is checked (no on*, srcdoc,
//     formaction, xlink:href), every element it opens is checked (no script,
//     style, iframe, object, embed, textarea, xmp, plaintext, noscript,
//     noembed, noframes, template, form, base, meta, link, math, frame,
//     svg animate/set/foreignObject …), and comments, <!…> and <?…> are refused.
//     A text pass also refuses javascript:/vbscript:/data:text/html and handler
//     spellings, including after decoding character references.
//  R4 Every template ends where it began - outside any tag, attribute,
//     <title> or dangling '<' - so a fragment cannot change its parent's
//     parsing context.
//  attrs() names are allow-listed (data-*, aria-*, role, tabindex, class,
//  title, id, opacity, type, name, value, alt, lang, dir, hidden, disabled).
//
// STATIC (eslint.config.mjs and the AST guard in tests/guards.test.mjs; src/)
//  S1 `html` is used only as a template tag, imported as exactly
//     `import { html, … } from '…/core/html.js'` (identifier names, no alias,
//     no namespace/default/dynamic import, re-export, storage, call or shadow).
//  S2 No reference - read, write, call or destructure - to a markup sink name
//     (innerHTML, outerHTML, srcdoc, insertAdjacentHTML, document.write, …)
//     outside this file.
//  S3 No computed member access at all (read, write or call) outside
//     core/dict.js, whose item()/put() touch only plain objects, arrays and
//     strings; no computed object keys, Reflect, prototype/constructor access,
//     or Object.assign/defineProperty/descriptor routes; inline
//     eslint-disable comments are ignored.
//
// KNOWN LIMITS (what is NOT claimed)
//  * A template object forged from runtime text (a frozen array with a frozen
//    `raw`) cannot be told from a genuine one at run time. S1 is what excludes
//    it; R3 refuses handlers and script-capable elements in it regardless.
//  * The rules cover src/. Code outside it (tests, tooling) is not linted
//    with them and does not run in the dashboard.
//  * CSS inside a quoted style attribute is escaped text, not parsed: the
//    rules stop markup and handler injection, not every CSS-level effect.
//  * The static rules are syntactic; they do not model `eval`-free dynamic
//    property access through APIs not listed (none is used in src/).

import { item } from './dict.js';

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
  return String(v == null ? '' : v).replace(/[&<>"'`]/g, (c) => item(ESC, c));
}

const empty = (v) => v === null || v === undefined || v === false || v === '';

/** Render one interpolated value for a slot kind, or throw (R1, R2). */
function render(v, kind, attr) {
  if (empty(v)) return '';
  if (Array.isArray(v)) return v.map((x) => render(x, kind, attr)).join('');
  switch (kind) {
    case 'text':
      if (isSafe(v)) return v.s;
      if (isBranded(v)) throw new TypeError('html: only text or html`` markup belongs between tags');
      return esc(v);
    case 'rcdata':
      if (isBranded(v)) throw new TypeError('html: only text may be interpolated into <' + attr + '>');
      return esc(v);
    case 'attr':
      if (isBranded(v)) throw new TypeError('html: only text may be interpolated into the ' + attr + ' attribute');
      return esc(v);
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

// ---- R3: literal text ----------------------------------------------------
const FORBIDDEN_ELEMENT = new Set(['script', 'style', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet', 'base', 'meta', 'link',
  'form', 'template', 'noscript', 'noembed', 'noframes', 'textarea', 'xmp', 'plaintext', 'listing', 'math', 'animate', 'animatemotion',
  'animatetransform', 'set', 'foreignobject', 'handler', 'portal', 'svg:script']);
const RCDATA = new Set(['title']);
const FORBIDDEN_ATTR = /^(on.*|srcdoc|formaction|xlink:href|xmlns:.*|is)$/i;
const DENY_TEXT = [
  /(^|[\s"'/`=])on[a-z]+\s*=/i,
  /javascript:|vbscript:|livescript:|data:text\/html|<!--|<!\[cdata\[/i,
];
const NAMED_REFS = { colon: ':', tab: '\t', newline: '\n', lpar: '(', rpar: ')', sol: '/', equals: '=', lt: '<', gt: '>', quot: '"', apos: '\'', amp: '&' };
function decoded(s) {
  return s.replace(/&#x([0-9a-f]+);?/gi, (m, h) => String.fromCodePoint(Number.parseInt(h, 16) || 32))
    .replace(/&#(\d+);?/g, (m, d) => String.fromCodePoint(Number(d) || 32))
    .replace(/&([a-z]+);/gi, (m, n) => (Object.hasOwn(NAMED_REFS, n.toLowerCase()) ? item(NAMED_REFS, n.toLowerCase()) : m));
}
function checkText(strings) {
  // Slots are joined as a space, so a boundary is a separator, never glue.
  const literal = strings.join(' ');
  const dec = decoded(literal);
  const squeezed = Array.from(dec).filter((ch) => ch.codePointAt(0) > 32).join('');
  for (const text of [literal, dec, squeezed]) {
    for (const re of DENY_TEXT) {
      if (re.test(text)) throw new TypeError('html: template literal text is not allowed (' + re.source.slice(0, 40) + '…)');
    }
  }
}

// ---- R1/R3/R4: the tokenizer --------------------------------------------
const URL_ATTRS = new Set(['href', 'src', 'action', 'formaction', 'srcset', 'poster', 'background', 'cite', 'data', 'ping', 'xlink:href', 'manifest', 'codebase', 'longdesc', 'usemap']);
const isSpace = (c) => c === ' ' || c === '\t' || c === '\n' || c === '\r' || c === '\f';
const fail = (why) => { throw new TypeError('html: ' + why); };

/**
 * Walk the literal parts as an HTML tokenizer would, and return one slot
 * descriptor per hole. Slots do not change the state: escaped text cannot,
 * Markup is a balanced template (R4) accepted only in text slots, and an
 * AttrList is a complete ` name="value"` sequence followed by a separator.
 */
function classify(strings) {
  const holes = [];
  let st = 'text', name = '', attr = '', endTag = false, rc = '', valueLen = 0, quote = '';
  const n = strings.length;
  const completeTagName = () => {
    const tag = name.toLowerCase();
    if (!/^[a-z][a-z0-9:-]*$/.test(tag)) fail('malformed tag name "' + name + '"');
    if (!endTag && FORBIDDEN_ELEMENT.has(tag)) fail('<' + tag + '> is not allowed in a template');
    rc = !endTag && RCDATA.has(tag) ? tag : '';
  };
  const completeAttrName = () => {
    if (FORBIDDEN_ATTR.test(attr)) fail('attribute "' + attr + '" is not allowed in a template');
  };
  const closeTag = () => { st = rc ? 'rcdata' : 'text'; };
  for (let i = 0; i < n; i++) {
    const s = item(strings, i);
    for (let j = 0; j < s.length; j++) {
      const c = item(s, j);
      switch (st) {
        case 'text':
          if (c === '<') {
            if (j === s.length - 1) fail(i < n - 1 ? 'a slot may not name a tag' : 'a template may not end with "<"');
            const d = item(s, j + 1);
            if (d === '!' || d === '?') fail('comments and <!…>/<?…> are not allowed in a template');
            if (d === '/') { endTag = true; j++; } else endTag = false;
            if (j === s.length - 1) fail('a slot may not name a tag');
            name = ''; st = 'tagname';
          }
          break;
        case 'rcdata':
          // HTML leaves RCDATA only on an end tag whose name is exactly the
          // element's, followed by whitespace, '/' or '>': `</titlex>` is text.
          if (c === '<' && s.slice(j + 1, j + 2 + rc.length).toLowerCase() === '/' + rc) {
            const k = j + 2 + rc.length;
            if (k >= s.length) fail(i < n - 1 ? 'a slot may not complete an end tag' : 'a template must end outside <' + rc + '>');
            const d = item(s, k);
            if (isSpace(d) || d === '/' || d === '>') { endTag = true; name = ''; j += 1; st = 'tagname'; }
          }
          break;
        case 'tagname':
          if (isSpace(c)) { completeTagName(); st = endTag ? 'endtag' : 'tag'; }
          else if (c === '>') { completeTagName(); if (endTag) st = 'text'; else closeTag(); }
          else if (c === '/') { completeTagName(); st = 'tag'; }
          else name += c;
          break;
        case 'endtag':
          if (c === '>') st = 'text';
          break;
        case 'tag':
          if (c === '>') closeTag();
          // A self-closing flag does not close a non-void element: `<title/>`
          // still opens RCDATA, so `/>` is treated exactly like `>`.
          else if (c === '/') { if (item(s, j + 1) === '>') { j++; closeTag(); } }
          else if (!isSpace(c)) { st = 'attrname'; attr = c.toLowerCase(); }
          break;
        case 'attrname':
          if (c === '=') { completeAttrName(); st = 'beforevalue'; }
          else if (c === '>') { completeAttrName(); closeTag(); }
          else if (isSpace(c) || c === '/') { completeAttrName(); st = 'afterattr'; }
          else attr += c.toLowerCase();
          break;
        case 'afterattr':
          if (c === '=') st = 'beforevalue';
          else if (c === '>') closeTag();
          else if (!isSpace(c) && c !== '/') { st = 'attrname'; attr = c.toLowerCase(); }
          break;
        case 'beforevalue':
          if (c === '"' || c === '\'') { st = 'value'; quote = c; valueLen = 0; }
          else if (c === '>') closeTag();
          else if (!isSpace(c)) st = 'unquoted';
          break;
        case 'value':
          if (c === quote) st = 'tag';
          else valueLen++;
          break;
        case 'unquoted':
          if (isSpace(c)) st = 'tag';
          else if (c === '>') closeTag();
          break;
        default: break;
      }
    }
    if (i === n - 1) break;
    const next = item(strings, i + 1);
    const first = next.length ? item(next, 0) : '';
    switch (st) {
      case 'text': holes.push({ kind: 'text' }); break;
      case 'rcdata': holes.push({ kind: 'rcdata', attr: rc }); break;
      case 'value':
        if (URL_ATTRS.has(attr)) {
          if (valueLen !== 0 || first !== quote) fail('a ' + attr + ' slot must be the entire quoted value');
          holes.push({ kind: 'url', attr });
        } else holes.push({ kind: 'attr', attr });
        valueLen++;
        break;
      case 'tag': case 'afterattr':
        if (!(first === '' ? false : isSpace(first) || first === '>' || first === '/')) fail('an attribute-list slot must be followed by whitespace, ">" or "/"');
        holes.push({ kind: 'tag' });
        break;
      case 'tagname': fail('a slot may not sit in a tag name'); break;
      case 'attrname': fail('a slot may not sit in or beside an attribute name'); break;
      default: fail('a slot may not sit in an unquoted attribute value');
    }
  }
  if (st !== 'text') fail('a template must end outside any tag or <title> (it ends in ' + st + ')');
  return holes;
}

const PLANS = new WeakMap();
function plan(strings) {
  let p = PLANS.get(strings);
  if (p) return p;
  checkText(strings);
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
 * Arrays are joined, each element rendered for its slot; null, undefined,
 * false and '' render as nothing, so `${cond && html`…`}` works.
 */
export function html(strings, ...values) {
  if (!isTemplateStrings(strings)) throw new TypeError('html must be used as a tagged template');
  const holes = plan(strings);
  let out = item(strings, 0);
  for (let i = 0; i < values.length; i++) {
    const h = item(holes, i);
    out += render(item(values, i), h.kind, h.attr) + item(strings, i + 1);
  }
  return brand(MARKUP, { s: out });
}

/** Join fragments with a separator, for a text slot. */
export function join(items, sep) {
  const s = sep === undefined ? '' : render(sep, 'text');
  return brand(MARKUP, { s: items.map((x) => render(x, 'text')).join(s) });
}

const ATTR_NAME = /^(?:(?:data|aria)-[a-z0-9-]+|role|tabindex|class|title|id|opacity|type|name|value|alt|lang|dir|hidden|disabled)$/;

/**
 * An attribute list for a tag slot: attrs({ 'data-sym': s, role: 'button' })
 * renders ` data-sym="…" role="button"`. Names must be on the allow-list;
 * values are escaped text; null/undefined/false values are omitted.
 */
export function attrs(map) {
  let s = '';
  for (const [key, v] of Object.entries(map)) {
    if (!ATTR_NAME.test(key)) throw new TypeError('attrs: attribute "' + key + '" is not allowed');
    if (v === null || v === undefined || v === false) continue;
    if (isBranded(v)) throw new TypeError('attrs: ' + key + ' takes text, not a branded value');
    s += ' ' + key + '="' + esc(v) + '"';
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
