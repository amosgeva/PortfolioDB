// Point inspection for the line charts, by mouse, touch and keyboard.
//
// The tooltips used to listen for mousemove only, so the value under a point
// was readable with a mouse and with nothing else (audit U-2). The plot is now
// a focusable group: arrow keys step the cursor one snapshot at a time
// (Home/End jump to the ends, PageUp/PageDown move a tenth of the series,
// Escape hides it) and each step is announced through a polite live region,
// so a screen reader hears "Sep 2, 14:00 - $12,345.67" rather than nothing.
// Pointer events replace mouse events, which covers touch and pen: a finger
// dragged across the plot scrubs it, and the reading stays up after lifting
// until the reader taps elsewhere. `touch-action: pan-y` (app.css) keeps a
// vertical swipe scrolling the page.

import { setHTML } from '../core/html.js';

const HINT = 'Use the left and right arrow keys to read each point.';

export function attachInspector(o) {
  const { plot, svg, W, H, points, label, tipHTML, cross, cursor, tip, live } = o;
  if (!plot || !svg || !points.length) return null;
  let idx = null;
  let touchHold = false;

  plot.tabIndex = 0;
  plot.setAttribute('role', 'group');
  plot.setAttribute('aria-roledescription', 'chart');
  plot.setAttribute('aria-label', o.ariaLabel + '. ' + HINT);
  // The summary now lives on the focusable group; the svg would repeat it.
  svg.setAttribute('aria-hidden', 'true');

  function nearest(svgX) {
    if (o.nearest) return o.nearest(svgX);
    let best = 0, bestD = Infinity;
    for (let i = 0; i < points.length; i++) {
      const d = Math.abs(points[i].x - svgX);
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  }

  function show(i, announce) {
    idx = Math.max(0, Math.min(points.length - 1, i));
    const p = points[idx];
    cross.setAttribute('x1', p.x); cross.setAttribute('x2', p.x); cross.setAttribute('opacity', '1');
    cursor.setAttribute('cx', p.x); cursor.setAttribute('cy', p.y); cursor.setAttribute('opacity', '1');
    const rect = svg.getBoundingClientRect();
    const rw = rect.width || W, rh = rect.height || H;
    tip.style.left = (p.x / W) * rw + 'px';
    tip.style.top = (p.y / H) * rh + 'px';
    tip.style.opacity = '1';
    setHTML(tip, tipHTML(idx));
    if (announce && live) live.textContent = label(idx);
  }
  function hide() {
    idx = null; touchHold = false;
    cross.setAttribute('opacity', '0'); cursor.setAttribute('opacity', '0'); tip.style.opacity = '0';
  }
  function fromPointer(ev) {
    const rect = svg.getBoundingClientRect();
    if (!(rect.width > 0)) return null;
    return nearest(((ev.clientX - rect.left) / rect.width) * W);
  }

  plot.addEventListener('pointermove', (ev) => {
    const i = fromPointer(ev);
    if (i != null) show(i, false);
  });
  plot.addEventListener('pointerdown', (ev) => {
    if (ev.pointerType !== 'touch') return;
    touchHold = true;
    const i = fromPointer(ev);
    if (i != null) show(i, true);
  });
  plot.addEventListener('pointerleave', (ev) => {
    // A lifted finger fires pointerleave too; the reading should outlast it.
    if (ev.pointerType === 'touch' && touchHold) return;
    hide();
  });
  plot.addEventListener('pointercancel', hide);
  document.addEventListener('pointerdown', (ev) => {
    if (touchHold && !plot.contains(ev.target)) hide();
  });

  plot.addEventListener('keydown', (e) => {
    const last = points.length - 1;
    const page = Math.max(1, Math.round(points.length / 10));
    let next;
    switch (e.key) {
      case 'ArrowLeft': next = idx == null ? last : idx - 1; break;
      case 'ArrowRight': next = idx == null ? last : idx + 1; break;
      case 'Home': next = 0; break;
      case 'End': next = last; break;
      case 'PageUp': next = (idx == null ? last : idx) - page; break;
      case 'PageDown': next = (idx == null ? last : idx) + page; break;
      case 'Escape': if (idx != null) { e.stopPropagation(); hide(); } return;
      default: return;
    }
    e.preventDefault();
    show(next, true);
  });
  plot.addEventListener('blur', hide);

  return { show, hide, get index() { return idx; } };
}
