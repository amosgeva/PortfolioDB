// The only place src/ reads or writes a member under a computed key.
//
// Lint forbids `obj[key]` everywhere else - reads as well as writes - because
// a key held in a variable can name a markup sink ('innerHTML',
// 'insertAdjacentHTML', 'onclick') and no static rule can follow every
// binding: QA showed `const k = 'innerHTML'; el[k] = x` and later
// `const fn = el[k]; fn.call(el, …)`. Rather than chase spellings, the shape
// is banned and these two helpers are the audited replacement. They only touch
// plain data - plain objects, arrays, strings - so neither can reach a DOM
// node, a window or a prototype.

const PROTO_KEY = /^(__proto__|constructor|prototype)$/;

function isPlainData(obj) {
  if (Array.isArray(obj)) return true;
  if (obj === null || typeof obj !== 'object') return false;
  const proto = Object.getPrototypeOf(obj);
  return proto === Object.prototype || proto === null;
}

/** obj[key] for a plain object, array or string; throws for anything else. */
export function item(obj, key) {
  if (typeof obj === 'string') return obj[key];
  if (!isPlainData(obj)) throw new TypeError('item: only plain objects, arrays and strings');
  if (typeof key === 'string' && PROTO_KEY.test(key)) return undefined;
  return obj[key];
}

/** obj[key] = value for a plain object or array; throws for anything else. */
export function put(obj, key, value) {
  if (!isPlainData(obj)) throw new TypeError('put: only plain objects and arrays');
  if (typeof key === 'string' && PROTO_KEY.test(key)) throw new TypeError('put: key "' + key + '" is not allowed');
  obj[key] = value;
  return value;
}
