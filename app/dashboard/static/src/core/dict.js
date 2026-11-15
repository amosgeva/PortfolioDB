// The one place src/ writes to an object under a computed key.
//
// Lint forbids `obj[key] = value` everywhere else, because a key held in a
// variable can be 'innerHTML' or 'onclick' and no static rule can follow every
// binding (QA round 2 showed `const k = 'innerHTML'; el[k] = x`). Rather than
// chase spellings, the shape is banned and this helper is the audited
// replacement: it writes only to plain data objects and arrays, so it cannot
// reach the DOM. The prototype keys are refused (pollution); a markup or
// handler name is not, because the target is plain data - a ticker called ON
// is a real symbol, not an event handler.

const SINK_KEY = /^(__proto__|constructor|prototype)$/;

/** obj[key] = value for a plain object or array; throws for anything else. */
export function put(obj, key, value) {
  const proto = obj !== null && typeof obj === 'object' ? Object.getPrototypeOf(obj) : undefined;
  if (!(proto === Object.prototype || proto === null || Array.isArray(obj))) {
    throw new TypeError('put: only plain objects and arrays');
  }
  if (typeof key === 'string' && SINK_KEY.test(key)) throw new TypeError('put: key "' + key + '" is not allowed');
  obj[key] = value;
  return value;
}
