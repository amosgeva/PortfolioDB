
(function () {
  'use strict';
  var stocks = DATA.stocks || {};
  var CASH = DATA.cash || 0;
  function $(s, r) { return (r || document).querySelector(s); }
  function $all(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function get(sym) { return stocks[sym]; }
  function list() { return Object.keys(stocks).map(function (k) { return stocks[k]; }); }

  // ---- formatters ----
  function money(n, dp) { dp = dp == null ? 2 : dp; return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp }); }
  function compact(n) { var a = Math.abs(n);
    if (a >= 1e12) return '$' + (n/1e12).toFixed(2) + 'T'; if (a >= 1e9) return '$' + (n/1e9).toFixed(2) + 'B';
    if (a >= 1e6) return '$' + (n/1e6).toFixed(2) + 'M'; if (a >= 1e3) return '$' + (n/1e3).toFixed(1) + 'K'; return '$' + n.toFixed(0); }
  // null day-change (no prior-session price yet) renders as "n/a", never a fake +0.00%
  function pct(n) { if (n == null) return 'n/a'; return (n >= 0 ? '+' : '') + Number(n).toFixed(2) + '%'; }
  function chgCls(n) { return n == null ? '' : (n >= 0 ? 'up' : 'down'); }
  function esc(v) { return String(v).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  // Attributes that turn a [data-sym] element into a real control. One delegated
  // handler opens the drawer, so each trigger has to carry its own role, tab stop
  // and name — a bare div with a click listener is reachable by mouse only.
  // Table rows are the exception: a <tr> must keep its row semantics, so those
  // get a .sym-open button in the first cell instead (see rowHTML).
  function symTrigger(sym, label) {
    return 'data-sym="' + sym + '" role="button" tabindex="0" aria-label="' +
      esc(label || 'Open ' + sym + ' details') + '"';
  }
  function symOpenBtn(sym, inner) {
    return '<button type="button" class="sym-open" aria-label="Open ' + esc(sym) + ' details">' + inner + '</button>';
  }
  var F = { money: money, compact: compact, pct: pct };
  // Lifted out of renderKPIs: the evidence panels print the same signed money,
  // and a local there is a ReferenceError from anywhere else in the file.
  function signed(v) { return (v >= 0 ? '+' : '−') + F.money(Math.abs(v || 0)); }

  // Written out as 1e6 or 1e-9 these are reported as literals whose runtime
  // value differs from the text, so every magnitude below is composed from
  // small integers instead - the same reason DAY_MS is spelled 24 * 60 * 60 * 1000.
  var THOUSAND = 1000;
  var TEN_THOUSAND = THOUSAND * 10;
  var HUNDRED_THOUSAND = THOUSAND * 100;
  var MILLION = THOUSAND * THOUSAND;
  var TEN_MILLION = MILLION * 10;
  // Ticks are stepped by repeated addition, so the last one can land a float
  // hair above the maximum; this is the tolerance that keeps it.
  var TICK_TOL = 1 / (THOUSAND * MILLION);
  // ---- axes -------------------------------------------------------------
  // Gridlines at fixed fractions of the data range land on values like
  // 8,912.47, which is why they were never labelled - there was nothing worth
  // printing. Ticks are chosen on the 1/2/5 ladder instead, so every line
  // falls on a number a person would say out loud and can therefore carry one.
  function niceTicks(lo, hi, target) {
    var span = hi - lo;
    if (!(span > 0) || !isFinite(span)) return [];
    target = target || 4;
    var mag = Math.pow(10, Math.floor(Math.log(span / target) / Math.LN10));
    // Rounding the ideal step UP to the next rung is the textbook version and it
    // halves the ladder whenever the ideal lands just above one: a $44 range wants
    // an $11 step, takes $20, and draws two gridlines where it should draw four.
    // Score the rungs instead and keep whichever lands nearest the target count.
    var best = null;
    [1, 2, 2.5, 5, 10].forEach(function (m) {
      var step = m * mag;
      if (!(step > 0)) return;
      var first = Math.ceil(lo / step) * step, n = 0;
      // Counting is capped as well as bounded: a step that underflows would
      // otherwise spin forever on a flat series.
      for (var t = first; t <= hi + step * TICK_TOL && n < 24; t += step) n++;
      if (n < 2) return;
      var score = Math.abs(n - target) + (n < 3 ? 2 : 0);
      if (!best || score < best.score) best = { step: step, first: first, n: n, score: score };
    });
    if (!best) return [];
    var out = [];
    for (var i = 0, v = best.first; i < best.n; i++, v += best.step) out.push(v);
    return out;
  }
  // Axis money is not tooltip money: it is read in a column, at 11px, against
  // its neighbours. Precision follows magnitude so the ticks stay distinct
  // without ever printing cents nobody is reading off a gridline.
  function axisMoney(v, step) {
    var a = Math.abs(v), sign = v < 0 ? "-$" : "$";
    if (a >= MILLION) return sign + (a / MILLION).toFixed(a >= TEN_MILLION ? 0 : 1) + "M";
    if (a >= TEN_THOUSAND) return sign + (a / THOUSAND).toFixed(a >= HUNDRED_THOUSAND ? 0 : 1) + "K";
    // A ladder of $0.20 steps rounded to whole dollars prints "$100" three
    // times: the step decides the precision, not the magnitude.
    var dp = !step || step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
    return sign + a.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  }
  // A quarter bar is stamped with the end of the period it covers. Naming it
  // "Q3" would assert a fiscal quarter this row does not carry - NVDA's fiscal
  // Q1 ends in April - and would disagree with the earnings table above, which
  // prints the real fiscal label. The period end date is what is actually known.
  function periodLabel(period) {
    var d = new Date(String(period) + "T00:00:00Z");
    if (isNaN(d.getTime())) return String(period);
    return new Intl.DateTimeFormat("en-US", { month: "short", year: "2-digit", timeZone: "UTC" })
      .format(d).replace(" ", " '");
  }

  // One categorical ramp for every chart, solved in OKLCH rather than picked.
  // Each hue sits at L 56-64% so it clears 3:1 on BOTH --surface values: the old
  // hand-picked hexes ranged L 45-85% and six of nine fell below 3:1 on white,
  // with the Cash slice at 1.48:1 - a slice you could not see. Each also carries
  // a 4.5:1 label so treemap tiles stay readable. Chroma is held to 0.110-0.115
  // across the set so it reads as one family rather than ten highlighters.
  // Hues stay >=18 degrees clear of --up (152), --down (26) and --accent (264):
  // in a ledger a category must never borrow the colour that means profit, loss
  // or "clickable". Index 9 is reserved for the Other bucket so a symbol can
  // never be handed the same colour as the aggregate it might be sitting in.
  // Interleaved, not sorted by hue. Slices take consecutive indices, so a ramp
  // in hue order hands adjacent slices adjacent hues - the sector donut came
  // out a continuous cyan-blue-violet-pink sweep, a sequential ramp pretending
  // to be a categorical one. This order keeps consecutive steps >=77 degrees
  // apart. Hue order is 193,340,242,52,312,218,5,288,88, then 120 for Other.
  var CAT = ['#00a19f','#a45b8d','#277bb2','#ac6231','#8f63a9','#009cba','#af596f','#756cb8','#927102','#6b7d23'];
  var CAT_OTHER = CAT[9];
  // Cash is not a holding, so it is the one slice carrying no hue at all.
  var CAT_CASH = '#72767d';
  // symLots carries every lot per symbol (payload builds it from the same rows
  // the P&L engine uses), so a truncated list can state what it is truncating.
  function lotTotal(sym) { return ((DATA.symLots || {})[sym] || []).length; }
  function lotTotalAll() {
    var m = DATA.symLots || {}, n = 0;
    for (var k in m) if (Object.prototype.hasOwnProperty.call(m, k)) n += m[k].length;
    return n;
  }
  // A ticker whose name is just the ticker - VOO, GLD, XLE - stacked the same
  // four letters twice and read as a rendering fault rather than as data. Emit
  // the subtitle only when it says something the symbol does not.
  function subName(sym, name, tag, attrs) {
    var n = (name == null ? '' : String(name)).trim();
    if (!n || n.toUpperCase() === String(sym).toUpperCase()) return '';
    return '<' + tag + (attrs || '') + '>' + esc(n) + '</' + tag + '>';
  }
  // Hashes over CAT[0..8] only: index 9 belongs to the Other bucket, and a
  // symbol tinted the same as the aggregate beside it reads as a duplicate.
  function symColor(sym) { var h = 0; for (var i = 0; i < sym.length; i++) h = (h*31 + sym.charCodeAt(i)) >>> 0; return CAT[h % 9]; }
  // The label colour for an arbitrary fill, measured rather than listed. The
  // treemap used to name two hex values as exceptions and give everything else
  // white, which silently broke the moment the palette changed. This returns
  // whichever of ink or white actually clears more contrast on that fill.
  var INK_ON_FILL = '#151b24';
  function relLum(hex) {
    var h = String(hex).replace('#', '');
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];   // #fff -> #ffffff
    var c = [0, 2, 4].map(function (i) { return parseInt(h.substr(i, 2), 16) / 255; })
      .map(function (v) { return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }
  var _inkLum = null;
  function onFill(hex) {
    if (_inkLum === null) _inkLum = relLum(INK_ON_FILL);
    var L = relLum(hex);
    return (1.05 / (L + 0.05)) >= ((L + 0.05) / (_inkLum + 0.05)) ? '#fff' : INK_ON_FILL;
  }
  // Ticker logo over the letter badge. Prefer the self-hosted data URI from
  // the payload (DATA.logos, populated by fetch_ticker_logos.py — no
  // third-party request at runtime); fall back to the parqet CDN for symbols
  // not cached yet. On any failure the img removes itself and the colored
  // letters underneath remain.
  function symBadge(sym) {
    var logo = (DATA.logos || {})[sym] ||
      'https://assets.parqet.com/logos/symbol/' + encodeURIComponent(sym) + '?format=png&size=64';
    return '<span class="sym-badge" style="background:' + symColor(sym) + '">' + sym.replace('.B','').slice(0,4) +
      '<img class="sym-logo" loading="lazy" referrerpolicy="no-referrer" alt="" ' +
      'src="' + logo + '" onerror="this.remove()"/></span>';
  }

  // A snapshot the arithmetic cannot support: an isolated point that both of its
  // neighbours tower over. A portfolio does not halve and recover inside one
  // session — that shape is a partial collection, where some symbols returned no
  // price. Drawn faithfully it costs the chart its entire vertical resolution and
  // tells the operator their money briefly vanished. Measured against the
  // neighbours rather than an average, so a real sustained fall never trips it.
  var SUSPECT_RATIO = 1.6;
  function suspectIdx(vals) {
    var bad = {}, n = 0;
    for (var i = 1; i < vals.length - 1; i++) {
      var v = vals[i];
      if (v > 0 && vals[i-1] > v * SUSPECT_RATIO && vals[i+1] > v * SUSPECT_RATIO) { bad[i] = 1; n++; }
    }
    bad.count = n;
    return bad;
  }
  // Extent over the points we believe. Excluding a suspect point from the domain
  // is what restores the rest of the series to a readable scale.
  function cleanExtent(vals, bad) {
    var lo = Infinity, hi = -Infinity;
    vals.forEach(function (v, i) { if (bad[i]) return; if (v < lo) lo = v; if (v > hi) hi = v; });
    if (lo === Infinity) { lo = Math.min.apply(null, vals); hi = Math.max.apply(null, vals); }
    return { min: lo, max: hi, range: (hi - lo) || 1 };
  }
  // The stroke is the data, so it breaks at a suspect point — the gap is the
  // honest rendering, and Data Health explains it. The area wash underneath is
  // decoration, so it carries the last believed value across and stays whole.
  function sparkPath(hist, w, h, pad, bad, ext, hold) {
    w = w||84; h = h||28; pad = pad||2; bad = bad || {};
    var e = ext || cleanExtent(hist, bad);
    var out = '', pen = 'M', last = null;
    hist.forEach(function (v, i) {
      if (bad[i]) { if (!hold) { pen = 'M'; return; } v = last == null ? v : last; }
      else { last = v; }
      var x = pad + (i/(hist.length-1))*(w-pad*2);
      var y = pad + (1-(v-e.min)/e.range)*(h-pad*2);
      out += (out ? ' ' : '') + pen + x.toFixed(1) + ',' + y.toFixed(1);
      pen = 'L';
    });
    return out;
  }
  // Monotonic gradient ids — Math.random() could collide across the many
  // sparklines in one DOM, making one sparkline adopt another's fill.
  var _sparkSeq = 0;
  function sparkSVG(hist, up, w, h) { w = w||84; h = h||28;
    if (!hist || hist.length < 2) return '';
    var bad = suspectIdx(hist), ext = cleanExtent(hist, bad);
    var color = up ? 'var(--up)' : 'var(--down)'; var d = sparkPath(hist, w, h, 2, bad, ext);
    var fillD = sparkPath(hist, w, h, 2, bad, ext, true) + ' L' + (w-2) + ',' + (h-2) + ' L2,' + (h-2) + ' Z';
    var id = 'g' + (_sparkSeq++);
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="' + id + '" x1="0" x2="0" y1="0" y2="1">' +
      '<stop offset="0" stop-color="' + color + '" stop-opacity=".22"/><stop offset="1" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
      '<path d="' + fillD + '" fill="url(#' + id + ')"/><path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>'; }

  var ARROW_UP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M7 14l5-5 5 5"/></svg>';
  // Spelled out as arithmetic rather than in exponential form: the static
  // analyser flags a literal whose written form and runtime value differ, and
  // one named constant beats three spellings of a day scattered through a file.
  var DAY_MS = 24 * 60 * 60 * 1000;
  // Matches the topbar snapshot pill's glyph — the same mark means the same
  // thing wherever collection freshness is in question.
  var CLOCK_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round" style="width:12px;height:12px;vertical-align:-1px;opacity:.8" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
  var ARROW_DN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M7 10l5 5 5-5"/></svg>';

  // ---- toast ----
  var toastEl;
  function toast(msg) { if (!toastEl) { toastEl = document.createElement('div'); toastEl.className = 'toast';
      toastEl.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg><span></span>'; document.body.appendChild(toastEl); }
    toastEl.querySelector('span').textContent = msg; toastEl.classList.add('show');
    clearTimeout(toast._t); toast._t = setTimeout(function () { toastEl.classList.remove('show'); }, 2400); }

  // ---- motion: respect the OS reduced-motion preference everywhere ----
  var REDUCED = false;
  try { REDUCED = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) {}
  function countUp(el, target, fmt) {
    if (!el) return;
    if (REDUCED || !window.requestAnimationFrame) { el.textContent = fmt(target); return; }
    var from = target * 0.985, dur = 700, t0 = null;
    function step(t) { if (t0 == null) t0 = t;
      var f = Math.min(1, (t - t0) / dur); f = 1 - Math.pow(1 - f, 3);
      el.textContent = fmt(from + (target - from) * f);
      if (f < 1) requestAnimationFrame(step); }
    requestAnimationFrame(step);
  }

  // ---- theme: light/dark tokens, persisted, defaulting to the OS scheme ----
  var SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  var MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
  function isDark() { return document.documentElement.getAttribute('data-theme') === 'dark'; }
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    var b = $('[data-theme-toggle]'); if (b) b.innerHTML = t === 'dark' ? SUN : MOON;
    try { localStorage.setItem('pdb_theme', t); } catch (e) {}
    // keep the Streamlit page behind the iframe in step, so over-scroll
    // and the native spinner area don't flash the opposite scheme
    try { window.parent.document.body.style.background = t === 'dark' ? '#16181d' : '#ffffff'; } catch (e) {}
    try {
      var pd0 = window.parent.document;
      var meta = pd0.querySelector('meta[name="theme-color"]');
      if (!meta) { meta = pd0.createElement('meta'); meta.name = 'theme-color'; pd0.head.appendChild(meta); }
      meta.content = t === 'dark' ? '#16181d' : '#ffffff';
    } catch (e) {}
    if (typeof renderHeat === 'function') { try { renderHeat(); } catch (e) {} }
    if (typeof renderRisk === 'function') { try { renderRisk(); } catch (e) {} }
  }
  // glanceable from another tab: day P&L in the title + a green/red dot
  // favicon, plus the PWA manifest so the dashboard is installable
  function brandParentPage() {
    try {
      var pd = window.parent.document;
      var k = DATA.kpi || {}, dp = k.dayChangePct || 0;
      var arrow = dp > 0 ? '▲' : dp < 0 ? '▼' : '●';
      pd.title = arrow + ' ' + (dp >= 0 ? '+' : '') + dp.toFixed(2) + '% · PortfolioDB';
      var c = document.createElement('canvas'); c.width = 64; c.height = 64;
      var x = c.getContext('2d');
      x.beginPath(); x.arc(32, 32, 24, 0, Math.PI * 2);
      // A tab strip is light or dark by OS theme, not by ours, so this dot has to
      // clear 3:1 on both. The old pair failed one each way (green 2.94 on a light
      // strip, red 2.52 on a dark one); these are the --up/--down hues re-solved at
      // the lightness that clears both (3.29/3.31 and 3.30/3.30).
      x.fillStyle = dp >= 0 ? '#0c994f' : '#ed4a46'; x.fill();
      var link = pd.querySelector('link[rel~="icon"]');
      if (!link) { link = pd.createElement('link'); link.rel = 'icon'; pd.head.appendChild(link); }
      link.href = c.toDataURL('image/png');
      if (!pd.querySelector('link[rel="manifest"]')) {
        var ml = pd.createElement('link'); ml.rel = 'manifest'; ml.href = '/app/static/manifest.json';
        pd.head.appendChild(ml);
      }
    } catch (e) {}
  }
  applyTheme(isDark() ? 'dark' : 'light');
  var themeBtn = $('[data-theme-toggle]');
  if (themeBtn) themeBtn.addEventListener('click', function () { applyTheme(isDark() ? 'light' : 'dark'); });

  // ---- shell: clock, refresh, mobile rail ----
  // ---- market clock + session status (US equities, computed in ET) ----
  var clockEl = $('[data-clock]'), dotEl = $('.mkt-pill .dot'), lblEl = $('.mkt-pill .lbl');
  // NYSE full-day closures 2026–2030 (observed dates; half-days not modeled).
  var US_HOLIDAYS = {
    '2026-01-01':1,'2026-01-19':1,'2026-02-16':1,'2026-04-03':1,'2026-05-25':1,'2026-06-19':1,'2026-07-03':1,'2026-09-07':1,'2026-11-26':1,'2026-12-25':1,
    '2027-01-01':1,'2027-01-18':1,'2027-02-15':1,'2027-03-26':1,'2027-05-31':1,'2027-06-18':1,'2027-07-05':1,'2027-09-06':1,'2027-11-25':1,'2027-12-24':1,
    '2028-01-17':1,'2028-02-21':1,'2028-04-14':1,'2028-05-29':1,'2028-06-19':1,'2028-07-04':1,'2028-09-04':1,'2028-11-23':1,'2028-12-25':1,
    '2029-01-01':1,'2029-01-15':1,'2029-02-19':1,'2029-03-30':1,'2029-05-28':1,'2029-06-19':1,'2029-07-04':1,'2029-09-03':1,'2029-11-22':1,'2029-12-25':1,
    '2030-01-01':1,'2030-01-21':1,'2030-02-18':1,'2030-04-19':1,'2030-05-27':1,'2030-06-19':1,'2030-07-04':1,'2030-09-02':1,'2030-11-28':1,'2030-12-25':1
  };
  var MKT_COLORS = { open: 'var(--up)', pre: 'var(--warn)', after: 'var(--warn)', closed: 'var(--faint)' };
  function tzHM(tz) { return new Intl.DateTimeFormat('en-US', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date()); }
  function tzLabel(tz) {
    // Short zone label for the clock (e.g. "IDT", "GMT+3"); falls back to the IANA name.
    try {
      var parts = new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'short' }).formatToParts(new Date());
      var name = parts.find(function (p) { return p.type === 'timeZoneName'; });
      return name ? name.value : tz;
    } catch (e) { return tz; }
  }
  var LOCAL_TZ = DATA.reportingTz || 'Asia/Jerusalem';
  function etParts() {
    var p = {};
    new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short',
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
      .formatToParts(new Date()).forEach(function (x) { p[x.type] = x.value; });
    return p;
  }
  function marketState() {
    var p = etParts();
    var dow = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }[p.weekday];
    var dateStr = p.year + '-' + p.month + '-' + p.day;
    var mins = (Number.parseInt(p.hour, 10) % 24) * 60 + Number.parseInt(p.minute, 10);
    if (dow === 0 || dow === 6) return { level: 'closed', label: 'Closed' };
    if (US_HOLIDAYS[dateStr]) return { level: 'closed', label: 'Holiday' };
    if (mins >= 570 && mins < 960) return { level: 'open', label: 'Market open' };   // 09:30–16:00
    if (mins >= 240 && mins < 570) return { level: 'pre', label: 'Pre-market' };      // 04:00–09:30
    if (mins >= 960 && mins < 1200) return { level: 'after', label: 'After-hours' };  // 16:00–20:00
    return { level: 'closed', label: 'Closed' };
  }
  function updClock() {
    var clockTxt = tzHM('America/New_York') + ' ET · ' + tzHM(LOCAL_TZ) + ' ' + tzLabel(LOCAL_TZ);
    if (clockEl) clockEl.textContent = clockTxt;
    var s = marketState();
    // Below 860px the two clocks are hidden — they wrapped the pill onto four
    // lines — so the pill itself has to carry them or the information is gone.
    var pill = $('[data-mkt-pill]');
    if (pill) pill.title = s.label + ' · ' + clockTxt;
    if (lblEl) lblEl.textContent = s.label;
    if (dotEl) { dotEl.style.setProperty('--dot-c', MKT_COLORS[s.level]);
      dotEl.style.backgroundColor = MKT_COLORS[s.level];  // direct value so the .4s crossfade animates
      dotEl.classList.toggle('closed', s.level === 'closed'); }
  }
  updClock(); setInterval(updClock, 1000);
  $all('[data-asof]').forEach(function (e) { e.textContent = DATA.asOf || '—'; });
  var snapEl = $('[data-snap]');
  if (snapEl) {
    var snap = DATA.snapshot || {};
    var snapTxt = snapEl.querySelector('[data-snap-text]');
    if (snapTxt) snapTxt.textContent = snap.text || '—';
    // This pill is the first thing to give up width when the topbar is tight,
    // so the full sentence has to survive somewhere. The static title said only
    // "Last price snapshot", which is not what the truncation hides.
    snapEl.title = snap.text || 'Last price snapshot';
    snapEl.classList.remove('warn', 'error', 'none');
    if (snap.level === 'warn' || snap.level === 'error' || snap.level === 'none') snapEl.classList.add(snap.level);
  }

  var rail = $('.rail'), scrim = $('.scrim'), menuBtn = $('.menu-btn');
  function openRail(v) { if (!rail) return; rail.classList.toggle('open', v); if (scrim) scrim.classList.toggle('show', v); }
  if (menuBtn) menuBtn.addEventListener('click', function () { openRail(!rail.classList.contains('open')); });
  if (scrim) scrim.addEventListener('click', function () { openRail(false); });
  // Tapping the scrim already closed the rail, but a scrim is a convention, not
  // an affordance — on a phone there was nothing visible that said "close".
  var railClose = $('[data-rail-close]');
  if (railClose) railClose.addEventListener('click', function () { openRail(false); if (menuBtn) menuBtn.focus(); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && rail && rail.classList.contains('open')) { openRail(false); if (menuBtn) menuBtn.focus(); }
  });

  // The component iframe is sandboxed without allow-top-navigation, so the
  // iframe itself cannot navigate the parent. But with allow-same-origin we can
  // create an anchor IN the parent document and click it — the parent then
  // navigates itself, which the sandbox permits. This is how the rail reaches
  // the native Manage/Advisor pages and how the refresh button works.
  function topNavigate(qs) {
    try {
      var d = window.parent.document;
      var a = d.createElement('a');
      a.href = qs; a.style.display = 'none';
      d.body.appendChild(a); a.click(); d.body.removeChild(a);
    } catch (e) {
      try { window.open(qs, '_self'); } catch (e2) { location.href = qs; }
    }
  }
  function currentView() {
    var p = $('.view.is-active'); return p ? p.dataset.viewPane : 'portfolio';
  }

  var refreshBtn = $('[data-refresh]');
  if (refreshBtn) refreshBtn.addEventListener('click', function () {
    toast('Refreshing…');
    // Prefer a soft rerun: click the hidden native Streamlit button in the
    // parent (clears the data cache + reruns over the websocket — no F5). Stash
    // the current pane so the freshly-mounted iframe can restore it on init.
    try {
      var btn = window.parent.document.querySelector('.st-key-bg_refresh button');
      if (btn) {
        try {
          window.parent.sessionStorage.setItem('pdb_refresh_view', currentView());
          window.parent.sessionStorage.setItem('pdb_refreshed', '1');
        } catch (e) {}
        btn.click();
        return;
      }
    } catch (e) {}
    // Fallback (sandbox blocked parent access): hard reload with cache-bust.
    topNavigate('?view=' + currentView() + '&r=' + new Date().getTime());
  });

  // ---- ticker tape ----
  var tapeTrack = $('[data-tape]'); var TAPE = (DATA.tapeSyms || []).filter(function (s) { return get(s); });
  function tapeItem(s) {
    return '<span class="tape__item"><b>' + s.sym + '</b><span class="num">' + F.money(s.price) + '</span>' +
      '<span class="num ' + chgCls(s.dayPct) + '">' + F.pct(s.dayPct) + '</span></span>'; }
  if (tapeTrack && TAPE.length) { var items = TAPE.map(function (sym) { return tapeItem(get(sym)); }).join('');
    tapeTrack.innerHTML = '<span style="display:inline-flex">' + items + '</span><span style="display:inline-flex" aria-hidden="true">' + items + '</span>'; }
  var tapeEl = $('.tape');
  if (tapeEl) { tapeEl.title = 'Click to pause / resume';
    tapeEl.addEventListener('click', function () { tapeEl.classList.toggle('paused'); }); }

  // ---- rail watchlist ----
  var wlEl = $('[data-watchlist]'); var WL = (DATA.watchSyms || []).filter(function (s) { return get(s); });
  if (wlEl) wlEl.innerHTML = WL.length ? WL.map(function (sym) { var s = get(sym);
      return '<div class="wl__row" ' + symTrigger(s.sym) + '><span class="wl__sym">' + s.sym + '</span><span class="wl__px">' + F.money(s.price) +
        '</span><span class="wl__chg ' + chgCls(s.dayPct) + '">' + F.pct(s.dayPct) + '</span></div>'; }).join('')
    : '<div style="padding:8px 10px;font-size:var(--fs-meta);color:var(--rail-muted)">No watchlist symbols</div>';

  // ---- client-side nav ----
  var TITLES = { portfolio: ['Portfolio', 'Real-time overview of your holdings'],
    movers: ['Market Movers', 'Biggest moves across your tracked symbols'],
    stats: ['Statistics', 'Best and worst periods, consistency and streaks'],
    alerts: ['Alerts & News', 'Price triggers and market headlines'],
    fundamentals: ['Fundamentals', 'Company profile, financials, filings and ownership'],
    history: ['History', 'Every recorded lot and snapshot run'] };
  var rendered = {};
  // Keep the parent URL in sync with the active pane so browser back/forward
  // and copy-paste deep links work. pushState is client-side only — Streamlit
  // doesn't rerun — and the '?view=' param is what the server already reads
  // on a hard load.
  function syncHistory(name, replace) {
    try {
      var h = window.parent.history;
      var st = { pdbView: name };
      if (replace) h.replaceState(st, '', '?view=' + name);
      else h.pushState(st, '', '?view=' + name);
    } catch (e) {}
  }
  function switchView(name, skipHistory) {
    $all('.nav a[data-view]').forEach(function (a) {
      var on = a.dataset.view === name;
      a.classList.toggle('is-active', on);
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    $all('.tabbar [data-tab]').forEach(function (b) { b.classList.toggle('is-active', b.dataset.tab === name); });
    $all('.view[data-view-pane]').forEach(function (v) { v.classList.toggle('is-active', v.dataset.viewPane === name); });
    var t = TITLES[name] || TITLES.portfolio; $('[data-title]').textContent = t[0]; $('[data-subtitle]').textContent = t[1];
    if (name === 'fundamentals' && !rendered.fundamentals) { renderFundamentals(); rendered.fundamentals = true; }
    if (name === 'history' && !rendered.history) { renderHistory(); rendered.history = true; }
    openRail(false); window.scrollTo(0, 0);
    if (!skipHistory) syncHistory(name);
  }
  function goNative(view) { topNavigate('?view=' + view); }
  function clickable(el, fn) {
    el.addEventListener('click', function (e) { e.preventDefault(); fn(); });
    el.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(); } });
  }
  $all('.nav a[data-view]').forEach(function (a) { clickable(a, function () { switchView(a.dataset.view); }); });
  $all('.nav a[data-nav]').forEach(function (a) { clickable(a, function () { goNative(a.dataset.nav); }); });
  $all('.tabbar [data-tab]').forEach(function (b) { b.addEventListener('click', function () { switchView(b.dataset.tab); }); });

  // Name every card region from the heading it already has, so a screen reader
  // can jump between them instead of walking one undifferentiated document.
  // Done here rather than in the markup so a new card cannot forget to do it.
  var sectSeq = 0;
  $all('section.card').forEach(function (sec) {
    if (sec.getAttribute('aria-labelledby') || sec.getAttribute('aria-label')) return;
    var h = sec.querySelector('.card__hd h2');
    if (!h) return;
    if (!h.id) h.id = 'sect-h-' + (++sectSeq);
    sec.setAttribute('aria-labelledby', h.id);
  });

  // ---- holdings rows ----
  function holdingRows() {
    return (DATA.holdings || []).map(function (h) { var s = get(h.sym); if (!s) return null;
      var mktVal = s.price * h.qty, cost = h.avgCost * h.qty, gl = mktVal - cost;
      return { sym: s.sym, name: s.name, sector: s.sector, hist: s.hist, qty: h.qty, avgCost: h.avgCost,
        price: s.price, day: s.day, dayPct: s.dayPct, mktVal: mktVal, cost: cost, gl: gl, glPct: cost ? (gl/cost)*100 : 0 };
    }).filter(Boolean);
  }
  function totals(rs) { var t = { mktVal:0, day:0, gl:0, cost:0 };
    rs.forEach(function (r) { t.mktVal += r.mktVal; t.day += r.day*r.qty; t.gl += r.gl; t.cost += r.cost; }); return t; }

  // ---- KPIs ----
  function deltaHTML(val, p, isMoney) { var up = val >= 0;
    var txt = (isMoney ? (up?'+':'−') + F.money(Math.abs(val)) : '') + (p != null ? (isMoney?'  ':'') + F.pct(p) : '');
    return '<span class="delta ' + (up?'up':'down') + '">' + (up?ARROW_UP:ARROW_DN) + txt + '</span>'; }
  function setKpi(id, txt, dir) {
    var e = $('#' + id); if (!e) return;
    e.textContent = txt; e.className = 'kpi__val' + (dir > 0 ? ' up' : dir < 0 ? ' down' : '');
  }
  // one-line story for the hero card: day move, top contributor, MTD vs SPY
  function narrative() {
    var k = DATA.kpi || {}, dc = k.dayChange || 0, parts = [];
    if (Math.abs(dc) < 0.005) parts.push('Flat today');
    else parts.push((dc > 0 ? 'Up ' : 'Down ') + F.money(Math.abs(dc)) + ' (' + F.pct(k.dayChangePct || 0) + ') today');
    var rs = holdingRows();
    if (rs.length && Math.abs(dc) >= 0.005) {
      var top = rs.slice().sort(function (a, b) { return Math.abs(b.day * b.qty) - Math.abs(a.day * a.qty); })[0];
      if (top && Math.abs(top.day * top.qty) > 0.005)
        parts.push((top.day * top.qty >= 0 ? 'led by ' : 'dragged by ') + top.sym + ' ' + F.pct(top.dayPct));
    }
    var mtd = null;
    ((DATA.returns || {}).periods || []).forEach(function (p) { if (p.period === 'MTD') mtd = p; });
    var bench = '';
    if (mtd && mtd.portfolio != null && mtd.benchmark != null) {
      var d = mtd.portfolio - mtd.benchmark;
      bench = 'MTD ' + F.pct(mtd.portfolio) + ', ' + (d >= 0 ? 'beating' : 'trailing') + ' SPY by ' + Math.abs(d).toFixed(2) + ' pts';
    }
    return parts.join(', ') + (bench ? ' — ' + bench : '') + '.';
  }
  // Built with DOM nodes rather than an innerHTML string: the percentage is our
  // own arithmetic, but concatenating any value into markup is the habit that
  // eventually ships an injection, so the label never becomes text-to-parse.
  function sparkWindowLabel(series, up) {
    var pct = series[0] ? ((series[series.length - 1] - series[0]) / series[0]) * 100 : 0;
    var el = document.createElement('div');
    el.className = 'kpi__spark-label';
    el.appendChild(document.createTextNode('past month '));
    var val = document.createElement('span');
    val.className = up ? 'up' : 'down';
    val.textContent = F.pct(pct);
    el.appendChild(val);
    return el;
  }
  function renderKPIs() {
    var k = DATA.kpi || {};
    countUp($('#kpi-total'), k.totalValue || 0, F.money);
    $('#kpi-total-sub').innerHTML = deltaHTML(k.dayChange || 0, k.dayChangePct, true) + '<span style="color:var(--muted)">today</span>';
    var story = $('#kpi-story'); if (story) story.textContent = narrative();
    var spark = $('#kpi-hero-spark');
    if (spark) {
      // The number above this is *today*; the line is the past month, and it can
      // legitimately fall while today rises. Label it, or the card reads as a
      // contradiction — green delta over a red line with nothing explaining why.
      var pvm = ((DATA.pv || {})['1M'] || []).map(function (p) { return p[1]; });
      spark.innerHTML = pvm.length > 1 ? sparkSVG(pvm, pvm[pvm.length - 1] >= pvm[0], 600, 54) : '';
      if (pvm.length > 1) {
        spark.insertBefore(sparkWindowLabel(pvm, pvm[pvm.length - 1] >= pvm[0]), spark.firstChild);
      }
    }
    $('#kpi-mktval').textContent = F.money(k.marketValue || 0);
    setKpi('kpi-gl', signed(k.unrealized), k.unrealized);
    $('#kpi-gl-sub').innerHTML = deltaHTML(k.unrealized || 0, k.unrealizedPct, false) + '<span style="color:var(--muted)">open positions</span>';
    $('#kpi-cash').textContent = F.money(k.cash || 0);
    setKpi('kpi-realized', signed(k.realized), k.realized);
    setKpi('kpi-return', F.pct(k.totalReturnPct || 0), k.totalReturnPct);
    $('#kpi-cost').textContent = F.money(k.costBasis || 0);
    $('#kpi-cost-sub').innerHTML = '<span style="color:var(--muted)">' + (k.activeCount || 0) + ' active · ' + (k.watchlistCount || 0) + ' watching · ' + F.money(k.totalFees || 0) + ' fees</span>';
    setKpi('kpi-delta', signed(k.deltaLast), k.deltaLast);
    $('#kpi-delta-sub').innerHTML = '<span style="color:var(--muted)">since previous snapshot</span>';
    $('#kpi-div').textContent = F.money(k.dividends || 0);
    $('#kpi-div-sub').innerHTML = '<span style="color:var(--muted)">TTM ' + F.money(k.dividendsTtm || 0) +
      ' · YoC ' + F.pct(k.yieldOnCostPct || 0) + ' · w/ div ' + F.pct(k.totalReturnWithIncomePct || 0) + '</span>';
  }

  // ---- returns strip (multi-period, vs SPY) ----
  // ---- market overview strip ----
  // Built from DOM nodes rather than an innerHTML string: the labels come from a
  // free-text setting, which makes them the one genuinely user-controlled string
  // on this page. textContent cannot be markup, so the question does not arise.
  //
  // Index levels are formatted as plain numbers, not money — VIX is not 14.51 of
  // any currency, and putting a $ on a futures level would be inventing a unit.
  function renderMarkets() {
    var card = $('#markets-card'), host = $('#markets-strip');
    if (!card || !host) return;
    var rows = DATA.markets || [];
    if (!rows.length) { card.hidden = true; return; }   // nothing configured
    card.hidden = false;
    host.textContent = '';

    var level = function (v) {
      return v == null ? '—' : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };
    var freshest = null;
    var wrap = document.createElement('div');
    wrap.className = 'mkt-grid';

    rows.forEach(function (r) {
      if (r.ts && (!freshest || r.ts > freshest)) freshest = r.ts;
      var up = r.pct != null && r.pct >= 0;
      var cell = document.createElement('div');
      cell.className = 'mkt';

      var top = document.createElement('div'); top.className = 'mkt__top';
      var name = document.createElement('b'); name.textContent = r.label; top.appendChild(name);
      var pct = document.createElement('span');
      pct.className = 'mkt__pct num ' + (r.pct == null ? 'muted' : (up ? 'up' : 'down'));
      // No snapshot yet is said, not shown as 0.00% — a fresh install has no
      // history until the collector runs, and a zero would read as a flat market.
      pct.textContent = r.pct == null ? 'no data yet' : (up ? '↗ ' : '↘ ') + F.pct(r.pct);
      top.appendChild(pct);
      cell.appendChild(top);

      var bottom = document.createElement('div'); bottom.className = 'mkt__bot';
      var px = document.createElement('span'); px.className = 'num'; px.textContent = level(r.price);
      bottom.appendChild(px);
      var chg = document.createElement('span');
      chg.className = 'num ' + (r.change == null ? 'muted' : (up ? 'up' : 'down'));
      chg.textContent = r.change == null ? '' : (r.change >= 0 ? '+' : '−') + level(Math.abs(r.change));
      bottom.appendChild(chg);
      cell.appendChild(bottom);

      var spark = document.createElement('div'); spark.className = 'mkt__spark';
      if ((r.hist || []).length > 1) spark.innerHTML = sparkSVG(r.hist, up, 240, 46);
      cell.appendChild(spark);
      wrap.appendChild(cell);
    });

    host.appendChild(wrap);
    var sub = $('#markets-sub');
    if (sub) {
      // Says what these are, because a strip of numbers next to a portfolio
      // invites the reader to assume they own them.
      sub.textContent = 'context, not holdings' +
        (freshest ? ' · as of ' + new Date(freshest).toLocaleString() : ' · awaiting first collection');
    }
  }

  function renderReturns() {
    var R = DATA.returns || {}; var host = $('#returns-strip'); if (!host) return;
    var periods = R.periods || [];
    var sub = $('#returns-sub'); if (sub && R.basis) sub.textContent = R.basis + ' · benchmark ' + (R.benchmark || 'SPY');
    var any = periods.some(function (p) { return p.portfolio != null; });
    if (!any) { host.innerHTML = '<div class="empty">Not enough snapshot history for period returns yet.</div>'; return; }
    host.innerHTML = '<div style="display:flex;gap:10px;flex-wrap:wrap">' + periods.map(function (p) {
      var hasP = p.portfolio != null; var dir = hasP ? p.portfolio : 0;
      var col = dir > 0 ? 'var(--up)' : dir < 0 ? 'var(--down)' : 'var(--muted)';
      var pv = hasP ? F.pct(p.portfolio) : '—';
      var bv = p.benchmark != null ? F.pct(p.benchmark) : '—';
      return '<div class="stat-mini stat-mini--well" style="min-width:96px">' +
        '<div class="l">' + esc(p.period) + '</div>' +
        '<div class="v" style="color:' + col + '">' + pv + '</div>' +
        '<div class="l" style="margin-top:4px">SPY ' + bv + '</div></div>';
    }).join('') + '</div>';
  }

  // ---- portfolio value chart ----
  var pvRange = '1D';
  function pvTipDate(ms) {
    var d = new Date(ms);
    var opts = (pvRange === '1D')
      ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }
      : { year: 'numeric', month: 'short', day: 'numeric' };
    return new Intl.DateTimeFormat('en-US', opts).format(d);
  }
  // The tooltip names one point and can afford "Sep 2, 2026"; the axis prints
  // four of these side by side and only has to keep them apart.
  function pvAxisDate(ms) {
    var d = new Date(ms);
    if (pvRange === "1D") return new Intl.DateTimeFormat("en-US", { hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
    // "Sep 25" is September 25th on every other range of this same axis, so a
    // year printed the same way is a different date to anyone reading quickly.
    // The apostrophe is what separates a year from a day here.
    if (pvRange === "1Y") return new Intl.DateTimeFormat("en-US", { month: "short", year: "2-digit" }).format(d).replace(" ", " '");
    return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(d);
  }
  function renderPV() {
    var pairs = (DATA.pv && DATA.pv[pvRange]) || [];
    var host = $('#pv-chart');
    if (!pairs || pairs.length < 2) { host.innerHTML = '<div class="empty">No portfolio-value history for this range yet.</div>'; return; }
    var data = pairs.map(function (p) { return p[1]; });
    var W = 920, H = 240, pad = 6;
    // Points the arithmetic cannot support are excluded from the domain, from the
    // stroke and from the drawdown scan alike. Left in, one partial collection
    // both flattens the whole curve and invents a drawdown that never happened.
    var bad = suspectIdx(data);
    var ext = cleanExtent(data, bad);
    var min = ext.min, range = ext.range;
    var up = data[data.length-1] >= data[0]; var color = up ? 'var(--up)' : 'var(--down)';
    var pts = data.map(function (v, i) { return [pad + (i/(data.length-1))*(W-pad*2), 14 + (1-(v-min)/range)*(H-28)]; });
    var line = '', pen = 'M';
    pts.forEach(function (p, i) {
      if (bad[i]) { pen = 'M'; return; }
      line += (line ? ' ' : '') + pen + p[0].toFixed(1) + ',' + p[1].toFixed(1); pen = 'L';
    });
    // The wash underneath is decoration, so it holds the last believed value
    // across the gap and stays whole.
    var areaLine = '', holdY = null;
    pts.forEach(function (p, i) {
      var y = bad[i] ? (holdY == null ? p[1] : holdY) : (holdY = p[1]);
      areaLine += (areaLine ? ' L' : 'M') + p[0].toFixed(1) + ',' + y.toFixed(1);
    });
    var area = areaLine + ' L' + pts[pts.length-1][0].toFixed(1) + ',' + (H-2) + ' L' + pts[0][0].toFixed(1) + ',' + (H-2) + ' Z';
    var last = pts[pts.length-1];
    var chg = (data[data.length-1] - data[0]) / data[0] * 100;
    // max drawdown over the visible window (peak → trough), shaded if material
    var peakI = 0, ddS = 0, ddE = 0, maxDD = 0;
    for (var di = 1; di < data.length; di++) {
      if (bad[di]) continue;
      if (bad[peakI] || data[di] > data[peakI]) peakI = di;
      var dd = (data[peakI] - data[di]) / data[peakI];
      if (dd > maxDD) { maxDD = dd; ddS = peakI; ddE = di; }
    }
    var ddRect = (maxDD >= 0.005 && ddE > ddS)
      ? '<rect x="' + pts[ddS][0].toFixed(1) + '" y="14" width="' + (pts[ddE][0]-pts[ddS][0]).toFixed(1) + '" height="' + (H-28) +
        '" fill="var(--down)" opacity=".055"><title>Max drawdown −' + (maxDD*100).toFixed(1) + '%</title></rect>'
      : '';
    var tickY = function (v) { return 14 + (1 - (v - min) / range) * (H - 28); };
    var ticks = niceTicks(min, min + range, 4).filter(function (t) {
      var y = tickY(t); return y >= 12 && y <= H - 12;
    });
    var tStep = ticks.length > 1 ? ticks[1] - ticks[0] : 0;
    var yLabels = ticks.map(function (t) {
      return '<span style="top:' + ((tickY(t) / H) * 100).toFixed(2) + '%">' + axisMoney(t, tStep) + '</span>';
    }).join('');
    // Four stops, ends pinned flush so the first and last dates cannot hang off
    // the plot; the middles are centred on their own points.
    var nX = Math.min(4, pairs.length);
    var xLabels = '';
    for (var xi = 0; xi < nX; xi++) {
      var idx = nX === 1 ? 0 : Math.round(xi * (pairs.length - 1) / (nX - 1));
      var place = xi === 0 ? 'left:0'
        : xi === nX - 1 ? 'right:0'
        : 'left:' + ((pts[idx][0] / W) * 100).toFixed(2) + '%;transform:translateX(-50%)';
      xLabels += '<span style="' + place + '">' + pvAxisDate(pairs[idx][0]) + '</span>';
    }
    // Audited 2026-09-02 for the XSS sink warning on this line. Everything
    // interpolated below is a number, a path built from numbers, or a constant
    // defined in this file: yLabels and xLabels come from axisMoney/pvAxisDate,
    // area and line are path data, pvRange is one of the four chip values, and
    // CLOCK_SVG is a literal. No text from the database or the URL reaches it.
    // nosemgrep
    host.innerHTML = '<div class="pv-wrap">' +
      '<div class="pv-y">' + yLabels + '</div>' +
      '<div class="pv-plot">' +
      '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="width:100%;height:240px;display:block" role="img" ' +
      'aria-label="Portfolio value, ' + pvRange + ' range, ' + F.pct(chg) +
        (bad.count ? ', ' + bad.count + ' incomplete snapshot' + (bad.count > 1 ? 's' : '') + ' omitted' : '') + '">' +
      '<defs><linearGradient id="pvg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="' + color + '" stop-opacity=".20"/>' +
      '<stop offset="1" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
      ticks.map(function (t) { var y = tickY(t).toFixed(1); return '<line x1="0" x2="' + W + '" y1="' + y + '" y2="' + y + '" stroke="var(--border)" stroke-width="1" stroke-dasharray="2 4"/>'; }).join('') +
      ddRect +
      '<path d="' + area + '" fill="url(#pvg)"/><path class="pv-line" d="' + line + '" fill="none" stroke="' + color + '" stroke-width="2.2" stroke-linejoin="round"/>' +
      '<line id="pv-cross" x1="0" x2="0" y1="0" y2="' + H + '" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" opacity="0"/>' +
      '<circle id="pv-cursor" cx="0" cy="0" r="4" fill="' + color + '" stroke="var(--surface)" stroke-width="1.5" opacity="0"/>' +
      '<circle cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1) + '" r="4" fill="' + color + '"/>' +
      '<circle cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1) + '" r="8" fill="' + color + '" opacity=".18"/></svg>' +
      '<div id="pv-tip" style="position:absolute;pointer-events:none;opacity:0;transform:translate(-50%,-115%);' +
      'background:var(--surface);border:1px solid var(--border);border-radius:var(--r-sm);padding:6px 9px;' +
      'box-shadow:var(--shadow-tip);font-size:var(--fs-meta);white-space:nowrap;z-index:5"></div>' +
      '<div class="num" style="position:absolute;top:6px;left:10px;font-size:var(--fs-meta);font-weight:600;pointer-events:none">' +
      '<span style="color:' + color + '">' + pvRange + ' ' + F.pct(chg) + '</span>' +
      (maxDD >= 0.005 ? '<span style="color:var(--muted);font-weight:500">  ·  max DD −' + (maxDD*100).toFixed(1) + '%</span>' : '') + '</div>' +
      // Say it on the chart rather than only in the markup: a gap the operator
      // can see but not explain is its own kind of untrustworthy number.
      (bad.count ? '<div style="position:absolute;bottom:26px;right:10px;font-size:var(--fs-meta);color:var(--muted);pointer-events:none">' +
        CLOCK_SVG + ' ' + bad.count + ' incomplete snapshot' + (bad.count > 1 ? 's' : '') + ' omitted · see Data Health</div>' : '') +
      '<div class="pv-x">' + xLabels + '</div>' +
      '</div></div>';
    // draw-in: reveal the line along its own length on (re)render
    var lineEl = host.querySelector('.pv-line');
    if (lineEl && !REDUCED && lineEl.getTotalLength) {
      try {
        var L = lineEl.getTotalLength();
        lineEl.style.strokeDasharray = L + ' ' + L;
        lineEl.style.strokeDashoffset = L;
        lineEl.getBoundingClientRect();
        lineEl.style.transition = 'stroke-dashoffset .7s cubic-bezier(.25,1,.5,1)';
        lineEl.style.strokeDashoffset = '0';
        setTimeout(function () { lineEl.style.strokeDasharray = 'none'; }, 750);
      } catch (e) {}
    }
    // The wrapper now includes a y-axis gutter and an x-axis strip, so it is no
    // longer the plot. Measure the svg itself or every reading is offset.
    var plot = host.querySelector('.pv-plot');
    var svgEl = plot.querySelector('svg');
    var cross = host.querySelector('#pv-cross'), cursor = host.querySelector('#pv-cursor'), tip = host.querySelector('#pv-tip');
    function move(ev) {
      var rect = svgEl.getBoundingClientRect();
      var fx = (ev.clientX - rect.left) / rect.width;
      var i = Math.max(0, Math.min(data.length - 1, Math.round(fx * (data.length - 1))));
      var sx = pts[i][0], sy = pts[i][1];
      cross.setAttribute('x1', sx); cross.setAttribute('x2', sx); cross.setAttribute('opacity', '1');
      cursor.setAttribute('cx', sx); cursor.setAttribute('cy', sy); cursor.setAttribute('opacity', '1');
      var pxX = (sx / W) * rect.width, pxY = (sy / H) * rect.height;
      tip.style.left = pxX + 'px'; tip.style.top = pxY + 'px'; tip.style.opacity = '1';
      tip.innerHTML = '<div style="font-weight:600">' + F.money(pairs[i][1]) + '</div>' +
        '<div style="color:var(--muted);font-size:var(--fs-micro)">' + pvTipDate(pairs[i][0]) + '</div>';
    }
    function leave() { cross.setAttribute('opacity', '0'); cursor.setAttribute('opacity', '0'); tip.style.opacity = '0'; }
    plot.addEventListener('mousemove', move);
    plot.addEventListener('mouseleave', leave);
  }
  $all('#pv-chips [data-range]').forEach(function (b) { b.addEventListener('click', function () {
    pvRange = b.dataset.range; $all('#pv-chips .chip').forEach(function (c) { c.classList.toggle('is-active', c === b); }); renderPV(); }); });

  // Ranges with no snapshots are disabled rather than silently drawn from a wider
  // window, and the narrowest one that has data starts active — so the chip and
  // the "+x%" beside it always describe the same series. Before the first
  // snapshot of the day, and all weekend, 1D genuinely has nothing to show.
  function initPVRange() {
    var chips = $all('#pv-chips [data-range]');
    var firstWithData = null;
    chips.forEach(function (c) {
      var has = (((DATA.pv || {})[c.dataset.range]) || []).length > 1;
      c.disabled = !has;
      c.title = has ? '' : 'No snapshots in this range yet';
      if (has && !firstWithData) firstWithData = c;
    });
    if (firstWithData) {
      pvRange = firstWithData.dataset.range;
      chips.forEach(function (c) { c.classList.toggle('is-active', c === firstWithData); });
    }
  }

  // ---- allocation donut / treemap ----
  var allocDim = 'position', allocMode = 'donut';
  // The same ramp as every other chart, minus the reserved Other slot. The label
  // contrast the old comment protected is now a constraint the ramp is solved
  // against, so it holds for all ten steps instead of two.
  var ALLOC_PALETTE = CAT.slice(0, 9);
  function renderAlloc() {
    var slices, total;
    if (allocDim === 'position') {
      var rs = holdingRows().slice().sort(function (a, b) { return b.mktVal - a.mktVal; });
      var t = totals(rs); total = t.mktVal + CASH;
      if (total <= 0) { $('#alloc').innerHTML = '<div class="empty">No positions to allocate.</div>'; return; }
      var top = rs.slice(0, 6); var otherVal = rs.slice(6).reduce(function (a, r) { return a + r.mktVal; }, 0);
      // symColor hashes, so it keeps a symbol's colour stable across views - but
      // six symbols landing in nine buckets collide about nine times in ten, and
      // two identically coloured arcs make the legend ambiguous. Keep the hash
      // where it lands free and walk to the next free step where it does not:
      // stable identity in the common case, always distinct inside one chart.
      var taken = {};
      slices = top.map(function (r) {
        var c = symColor(r.sym);
        for (var g = 0; taken[c] && g < 9; g++) c = CAT[(CAT.indexOf(c) + 1) % 9];
        taken[c] = 1;
        return { label: r.sym, val: r.mktVal, color: c };
      });
      if (otherVal > 0) slices.push({ label: 'Other', val: otherVal, color: CAT_OTHER });
      if (CASH > 0) slices.push({ label: 'Cash', val: CASH, color: CAT_CASH });
    } else {
      var rows = ((DATA.alloc || {})[allocDim]) || [];
      var topR = rows.slice(0, 7);
      var otherR = rows.slice(7).reduce(function (a, r) { return a + r.value; }, 0);
      slices = topR.map(function (r, i) { return { label: r.key, val: r.value, color: ALLOC_PALETTE[i % ALLOC_PALETTE.length] }; });
      if (otherR > 0) slices.push({ label: 'Other', val: otherR, color: CAT_OTHER });
      total = slices.reduce(function (a, s) { return a + s.val; }, 0);
      if (total <= 0) { $('#alloc').innerHTML = '<div class="empty">No allocation data for this view.</div>'; return; }
    }
    if (allocMode === 'map') { $('#alloc').innerHTML = treemapHTML(slices, total); return; }
    var C = 2*Math.PI*54, off = 0;
    // Every step of the ramp sits at one lightness, so two adjacent arcs can
    // measure ~1:1 against each other however well each reads on the card. Hue
    // alone cannot carry a boundary, so the boundary is a gap - which the treemap
    // already had between its tiles and the donut did not.
    var GAP = 3;
    var ring = slices.map(function (s) { var frac = s.val/total;
      var dash = Math.max(1.5, frac*C - GAP);
      var seg = '<circle cx="80" cy="80" r="54" fill="none" stroke="' + s.color + '" stroke-width="20" stroke-dasharray="' +
        dash.toFixed(2) + ' ' + (C - dash).toFixed(2) + '" stroke-dashoffset="' + (-off*C).toFixed(2) + '" transform="rotate(-90 80 80)"/>';
      off += frac; return seg; }).join('');
    var legend = slices.map(function (s) { return '<div style="display:flex;align-items:center;gap:8px;font-size:var(--fs-sm);padding:3px 0">' +
      '<span style="width:9px;height:9px;border-radius:2px;background:' + s.color + '"></span><span style="flex:1">' + esc(s.label) +
      '</span><span class="num" style="color:var(--muted)">' + (s.val/total*100).toFixed(1) + '%</span></div>'; }).join('');
    $('#alloc').innerHTML = '<div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap">' +
      '<div style="position:relative;flex:0 0 auto"><svg width="160" height="160" viewBox="0 0 160 160" role="img" aria-label="Allocation donut, total ' + F.compact(total) + '">' + ring + '</svg>' +
      '<div style="position:absolute;inset:0;display:grid;place-items:center;text-align:center"><div>' +
      '<div class="num" style="font-size:var(--fs-xl);font-weight:600">' + F.compact(total) + '</div>' +
      '<div style="font-size:var(--fs-micro);color:var(--muted);letter-spacing:.04em">TOTAL</div></div></div></div>' +
      '<div style="flex:1;min-width:150px">' + legend + '</div></div>';
  }
  // weight-proportional treemap (two greedy strips) — tile area = weight, so
  // concentration is visible at a glance, unlike the equal-tile sector heatmap
  function treemapHTML(slices, total) {
    var rows = [[], []], sums = [0, 0];
    slices.forEach(function (s) { var i = sums[0] <= sums[1] ? 0 : 1; rows[i].push(s); sums[i] += s.val; });
    if (!rows[1].length) { rows.pop(); sums.pop(); }
    var html = '<div style="display:flex;flex-direction:column;gap:4px;width:100%" role="img" aria-label="Allocation treemap, tile size is portfolio weight">';
    rows.forEach(function (row, ri) {
      var h = Math.max(64, Math.round(176 * sums[ri] / total));
      html += '<div style="display:flex;gap:4px;height:' + h + 'px">';
      row.forEach(function (s) {
        var p = s.val / total * 100;
        var txt = onFill(s.color);
        html += '<div title="' + esc(s.label) + ' · ' + F.money(s.val) + ' · ' + p.toFixed(1) + '%" ' +
          'style="flex:' + s.val.toFixed(2) + ' 1 0;min-width:0;background:' + s.color + ';border-radius:var(--r-sm);color:' + txt + ';' +
          'padding:8px 9px;display:flex;flex-direction:column;justify-content:space-between;overflow:hidden">' +
          '<b style="font-size:var(--fs-meta);white-space:nowrap;text-overflow:ellipsis;overflow:hidden">' + esc(s.label) + '</b>' +
          '<span class="num" style="font-size:var(--fs-micro)">' + p.toFixed(1) + '%</span></div>';
      });
      html += '</div>';
    });
    return html + '</div>';
  }
  $all('#alloc-chips [data-dim]').forEach(function (b) { b.addEventListener('click', function () {
    allocDim = b.dataset.dim; $all('#alloc-chips [data-dim]').forEach(function (c) { c.classList.toggle('is-active', c === b); }); renderAlloc(); }); });
  $all('#alloc-mode [data-mode]').forEach(function (b) { b.addEventListener('click', function () {
    allocMode = b.dataset.mode; $all('#alloc-mode [data-mode]').forEach(function (c) { c.classList.toggle('is-active', c === b); }); renderAlloc(); }); });

  // ---- holdings table ----
  var sortState = { key: 'mktVal', dir: -1 };
  function rowHTML(r) {
    // null dayPct → neutral tag; spark color falls back to the hist trend
    var dUp = r.dayPct != null ? r.dayPct >= 0 : r.hist[r.hist.length-1] >= r.hist[0];
    var dTag = r.dayPct == null ? '' : (r.dayPct >= 0 ? 'tag--up' : 'tag--down');
    var gUp = r.gl >= 0;
    return '<tr data-sym="' + r.sym + '"><td>' +
      symOpenBtn(r.sym, '<span class="sym-cell">' + symBadge(r.sym) +
        '<span class="nm"><b>' + r.sym + '</b>' + subName(r.sym, r.name, 'span') + '</span></span>') + '</td>' +
      '<td class="price">' + F.money(r.price) + '</td>' +
      '<td><span class="tag ' + dTag + '">' + F.pct(r.dayPct) + '</span></td>' +
      '<td class="num">' + (Math.round(r.qty*1e4)/1e4) + '</td>' +
      '<td class="num">' + F.money(r.mktVal) + '</td>' +
      '<td class="num" style="color:var(--muted)">' + F.money(r.avgCost) + '</td>' +
      '<td class="num ' + (gUp?'up':'down') + '">' + (gUp?'+':'−') + F.money(Math.abs(r.gl)) +
        ' <span style="font-size:var(--fs-micro)">' + F.pct(r.glPct) + '</span></td>' +
      '<td class="spark-cell">' + sparkSVG(r.hist, dUp) + '</td></tr>'; }
  function hcardHTML(r) {
    var dTag = r.dayPct == null ? '' : (r.dayPct >= 0 ? 'tag--up' : 'tag--down');
    var gUp = r.gl >= 0;
    return '<div class="hcard" ' + symTrigger(r.sym) + '><div class="hcard__top">' +
      symBadge(r.sym) +
      '<span class="nm" style="display:flex;flex-direction:column;line-height:1.25"><b>' + r.sym + '</b>' +
      subName(r.sym, r.name, 'span', ' style="font-size:var(--fs-micro);color:var(--muted)"') + '</span>' +
      '<span class="tag ' + dTag + '">' + F.pct(r.dayPct) + '</span></div>' +
      '<div class="hcard__row"><span class="mv">' + F.money(r.mktVal) + '</span>' +
      '<span class="num ' + (gUp?'up':'down') + '">' + (gUp?'+':'−') + F.money(Math.abs(r.gl)) + ' (' + F.pct(r.glPct) + ')</span></div>' +
      '<div class="hcard__meta">' + (Math.round(r.qty*1e4)/1e4) + ' sh · avg ' + F.money(r.avgCost) + ' · now ' + F.money(r.price) + '</div></div>';
  }
  function renderTable() {
    var rs = holdingRows(); var k = sortState.key, dir = sortState.dir;
    rs.sort(function (a, b) { var x = a[k], y = b[k]; if (typeof x === 'string') return x.localeCompare(y)*dir; return (x-y)*dir; });
    var tbody = $('#holdings');
    tbody.innerHTML = rs.length ? rs.map(rowHTML).join('') : '<tr><td colspan="8"><div class="empty">No open positions.</div></td></tr>';
    var cards = $('#holdings-cards');
    if (cards) cards.innerHTML = rs.length ? rs.map(hcardHTML).join('') : '<div class="empty">No open positions.</div>';
    // The engine is named on the table it produced: Data Health offers a
    // FIFO/Average selector, and two screens showing a different avg cost with
    // neither saying which is the confusion this removes.
    $('#holdings-sub').textContent = rs.length + ' position' + (rs.length === 1 ? '' : 's') +
      ' · ' + (DATA.engine || 'FIFO') + ' cost basis · click a column to sort';
    $all('#holdings-tbl thead th').forEach(function (th) { var sorted = th.dataset.key === k; th.classList.toggle('sorted', sorted);
      th.setAttribute('aria-sort', sorted ? (dir < 0 ? 'descending' : 'ascending') : 'none');
      var ar = th.querySelector('.arrow'); if (ar) ar.textContent = sorted ? (dir<0?'▼':'▲') : ''; });
  }
  $all('#holdings-tbl thead th[data-key]').forEach(function (th) { clickable(th, function () {
    var k = th.dataset.key; if (sortState.key === k) sortState.dir *= -1; else { sortState.key = k; sortState.dir = (k==='sym'||k==='name')?1:-1; } renderTable(); }); });
  // Below 640px the table is replaced by cards, which took the column headers —
  // and with them every sort control — out of the document. Same sortState, so
  // the two controls stay in step whichever width the operator is at.
  var hcSort = $('#hc-sort'), hcDir = $('[data-hc-dir]');
  function syncHcSort() {
    if (hcSort) hcSort.value = sortState.key;
    if (hcDir) { hcDir.textContent = sortState.dir === 1 ? '↑' : '↓';
      hcDir.title = sortState.dir === 1 ? 'Ascending — tap for descending' : 'Descending — tap for ascending'; }
  }
  if (hcSort) hcSort.addEventListener('change', function () {
    sortState.key = hcSort.value;
    sortState.dir = (sortState.key === 'sym' || sortState.key === 'name') ? 1 : -1;
    renderTable(); syncHcSort();
  });
  if (hcDir) hcDir.addEventListener('click', function () { sortState.dir *= -1; renderTable(); syncHcSort(); });
  syncHcSort();

  // ---- terminal-style price flash: green/red pulse on cells whose price
  // moved since the last visit (previous prices kept in localStorage) ----
  function flashChangedPrices() {
    try {
      var prev = JSON.parse(localStorage.getItem('pdb_prices') || '{}');
      var cur = {};
      list().forEach(function (s) { cur[s.sym] = s.price; });
      if (!REDUCED) Object.keys(cur).forEach(function (sym) {
        if (prev[sym] == null || prev[sym] === cur[sym]) return;
        var cell = document.querySelector('#holdings tr[data-sym="' + sym + '"] td.price');
        if (cell) cell.classList.add(cur[sym] > prev[sym] ? 'flash-up' : 'flash-down');
      });
      localStorage.setItem('pdb_prices', JSON.stringify(cur));
    } catch (e) {}
  }

  // ---- P&L attribution: diverging contribution bars + net-P&L waterfall ----
  var attrMode = 'day';
  function renderAttribution() {
    var host = $('#attr-chart'); if (!host) return;
    var rs = holdingRows().map(function (r) {
      return { sym: r.sym, val: attrMode === 'day' ? (r.dayPct == null ? null : r.day * r.qty) : r.gl };
    }).filter(function (r) { return r.val != null; });
    if (!rs.length) { host.innerHTML = '<div class="empty">No P&amp;L data yet.</div>'; return; }
    rs.sort(function (a, b) { return b.val - a.val; });
    var max = Math.max.apply(null, rs.map(function (r) { return Math.abs(r.val); })) || 1;
    host.innerHTML = rs.map(function (r) {
      var w = Math.max(1.5, Math.abs(r.val) / max * 50);  // % of the track (half each side)
      var up = r.val >= 0;
      return '<div class="attr-row" ' + symTrigger(r.sym) + ' title="Open ' + r.sym + '">' +
        '<span class="attr-sym">' + r.sym + '</span>' +
        '<span class="attr-track"><span class="attr-bar ' + (up ? 'up' : 'down') + '" style="' +
          (up ? 'left:50%;' : 'right:50%;') + 'width:' + w.toFixed(1) + '%"></span></span>' +
        '<span class="attr-val num ' + (up ? 'up' : 'down') + '">' + (up ? '+' : '−') + F.money(Math.abs(r.val)) + '</span></div>';
    }).join('');
  }
  $all('#attr-chips [data-attr]').forEach(function (b) { b.addEventListener('click', function () {
    attrMode = b.dataset.attr; $all('#attr-chips .chip').forEach(function (c) { c.classList.toggle('is-active', c === b); }); renderAttribution(); }); });
  function renderWaterfall() {
    var host = $('#waterfall'); if (!host) return;
    var k = DATA.kpi || {};
    var steps = [
      { label: 'Unrealized', val: k.unrealized || 0 },
      { label: 'Realized', val: k.realized || 0 },
      { label: 'Dividends', val: k.dividends || 0 },
      { label: 'Fees', val: -(k.totalFees || 0) }
    ];
    var cum = 0;
    var nodes = steps.map(function (s) { var y0 = cum; cum += s.val; return { label: s.label, val: s.val, y0: y0, y1: cum, net: false }; });
    nodes.push({ label: 'Net P&L', val: cum, y0: 0, y1: cum, net: true });
    var lo = 0, hi = 0;
    nodes.forEach(function (nd) { lo = Math.min(lo, nd.y0, nd.y1); hi = Math.max(hi, nd.y0, nd.y1); });
    if (hi - lo < 0.01) { host.innerHTML = '<div class="empty">No P&amp;L recorded yet.</div>'; return; }
    var W = 440, H = 220, padT = 20, padB = 34, span = hi - lo;
    function Y(v) { return padT + (hi - v) / span * (H - padT - padB); }
    var slot = W / nodes.length, bw = slot * 0.54;
    // Name the values, not just the chart: a label reading only "Net P&L
    // waterfall" describes the picture and states none of what it plots.
    var wfLabel = 'Net P&L waterfall. ' + nodes.map(function (nd) {
      return nd.label + ' ' + (nd.val >= 0 ? 'plus ' : 'minus ') + F.money(Math.abs(nd.val));
    }).join(', ') + '.';
    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:220px;display:block" role="img" aria-label="' + esc(wfLabel) + '">' +
      '<line x1="0" x2="' + W + '" y1="' + Y(0).toFixed(1) + '" y2="' + Y(0).toFixed(1) + '" stroke="var(--border-2)" stroke-width="1"/>';
    nodes.forEach(function (nd, i) {
      var x = i * slot + (slot - bw) / 2;
      var yTop = Y(Math.max(nd.y0, nd.y1)), yBot = Y(Math.min(nd.y0, nd.y1));
      var hgt = Math.max(1.5, yBot - yTop);
      // The total was --accent, which made the one bar everybody reads first the
      // only bar whose colour did not say whether it was a gain or a loss, and
      // spent the interaction colour on data. It is sign-coloured like the rest;
      // what marks it as the total is that it alone is anchored to zero and drawn
      // at full strength, behind a rule that separates it from the steps.
      var color = nd.val >= 0 ? 'var(--up)' : 'var(--down)';
      svg += '<rect x="' + x.toFixed(1) + '" y="' + yTop.toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + hgt.toFixed(1) +
        '" rx="3" fill="' + color + '"' + (nd.net ? '' : ' opacity=".85"') + '><title>' + nd.label + ' ' + F.money(nd.val) + '</title></rect>';
      if (nd.net) {
        var dx = (x - (slot - bw) / 2 + 1).toFixed(1);
        svg += '<line x1="' + dx + '" x2="' + dx + '" y1="' + padT + '" y2="' + (H - padB + 6) +
          '" stroke="var(--border-2)" stroke-width="1"/>';
      }
      if (!nd.net && i < nodes.length - 1) {
        var cy = Y(nd.y1).toFixed(1);
        svg += '<line x1="' + (x + bw).toFixed(1) + '" x2="' + ((i + 1) * slot + (slot - bw) / 2).toFixed(1) + '" y1="' + cy + '" y2="' + cy +
          '" stroke="var(--faint)" stroke-width="1" stroke-dasharray="3 3"/>';
      }
      var vy = yTop - 5; if (vy < 12) vy = yBot + 12;
      svg += '<text x="' + (x + bw / 2).toFixed(1) + '" y="' + vy.toFixed(1) + '" text-anchor="middle" font-size="10" class="num" fill="var(--fg-soft)">' +
        (nd.val >= 0 ? '+' : '−') + F.compact(Math.abs(nd.val)) + '</text>' +
        '<text x="' + (x + bw / 2).toFixed(1) + '" y="' + (H - 12) + '" text-anchor="middle" font-size="10.5" fill="var(--muted)">' + nd.label + '</text>';
    });
    host.innerHTML = svg + '</svg>';
  }

  // ---- risk & analytics (payload-computed; rendered client-side) ----
  function renderRisk() {
    var body = $('#risk-body'); if (!body) return;
    var R = DATA.risk;
    if (!R || !R.ok) {
      body.innerHTML = '<div class="empty">Not enough snapshot history for risk stats yet (needs ~20 trading days).</div>';
      return;
    }
    var sub = $('#risk-sub'); if (sub) sub.textContent = R.days + ' trading days · SPY benchmark · current positions over historical closes';
    var p = R.portfolio || {}, conc = R.concentration || {};
    function tile(l, v, cls) {
      return '<div class="stat-mini stat-mini--well"><div class="v num' + (cls ? ' ' + cls : '') + '">' + v + '</div><div class="l">' + l + '</div></div>';
    }
    $('#risk-stats').innerHTML =
      tile('Beta vs SPY', p.beta != null ? p.beta.toFixed(2) : '—') +
      tile('Volatility (ann.)', p.vol != null ? p.vol.toFixed(1) + '%' : '—') +
      tile('Sharpe (1Y)', p.sharpe != null ? p.sharpe.toFixed(2) : '—', p.sharpe != null ? (p.sharpe >= 1 ? 'up' : p.sharpe < 0 ? 'down' : '') : '') +
      tile('Max drawdown', p.maxDD != null ? '−' + p.maxDD.toFixed(1) + '%' : '—', 'down') +
      tile('Top position', conc.top1 ? esc(conc.top1.sym) + ' ' + conc.top1.pct.toFixed(1) + '%' : '—') +
      tile('Top-3 weight', conc.top3Pct != null ? conc.top3Pct.toFixed(1) + '%' : '—');
    $('#risk-tbl').innerHTML = (R.perSymbol || []).map(function (r) {
      return '<tr data-sym="' + r.sym + '"><td>' + symOpenBtn(r.sym, '<b>' + r.sym + '</b>') + '</td>' +
        '<td class="num">' + (r.weight != null ? r.weight.toFixed(1) + '%' : '—') + '</td>' +
        '<td class="num">' + (r.beta != null ? r.beta.toFixed(2) : '—') + '</td>' +
        '<td class="num">' + (r.vol != null ? r.vol.toFixed(1) + '%' : '—') + '</td>' +
        '<td class="num">' + (r.sharpe != null ? r.sharpe.toFixed(2) : '—') + '</td></tr>';
    }).join('');
    var C = R.corr || {}, syms = C.syms || [], m = C.m || [];
    var corrHost = $('#risk-corr');
    if (syms.length < 2) { corrHost.innerHTML = '<div class="empty">Need at least two holdings for correlations.</div>'; return; }
    function corrColor(v) {
      var a = Math.min(Math.abs(v), 1);
      if (a < 0.05) return isDark() ? 'oklch(38% 0.012 260)' : 'oklch(88% 0.008 260)';
      return 'oklch(' + (isDark() ? (36 + a * 20) : (90 - a * 28)).toFixed(1) + '% ' + (0.03 + a * 0.1).toFixed(3) + ' ' + (v >= 0 ? 152 : 26) + ')';
    }
    var html = '<div class="corr" style="grid-template-columns:48px repeat(' + syms.length + ',1fr)" role="img" aria-label="Holdings correlation matrix">';
    html += '<span></span>' + syms.map(function (s) { return '<span class="corr__lbl">' + s + '</span>'; }).join('');
    for (var i = 0; i < syms.length; i++) {
      html += '<span class="corr__lbl" style="text-align:right;padding-right:6px">' + syms[i] + '</span>';
      for (var j = 0; j < syms.length; j++) {
        var v = m[i] ? m[i][j] : null;
        if (v == null) { html += '<span class="corr__cell" style="background:var(--surface-2);color:var(--faint)">—</span>'; continue; }
        var strong = Math.abs(v) > 0.65 && !isDark();
        html += '<span class="corr__cell" title="' + syms[i] + ' × ' + syms[j] + ' = ' + v.toFixed(2) + '" style="background:' + corrColor(v) +
          (strong ? ';color:#fff' : '') + '">' + v.toFixed(2) + '</span>';
      }
    }
    corrHost.innerHTML = html + '</div>' +
      '<div style="font-size:var(--fs-micro);color:var(--muted);margin-top:8px">Pairwise correlation of daily returns — green moves together, red moves opposite.</div>';
  }

  // ---- per-symbol price history chart ----
  var phRange = '3M', phSpy = false;
  function phSpanMs(r) { var d = DAY_MS; return r === '1M' ? 30 * d : r === '3M' ? 90 * d : r === '1Y' ? 365 * d : null; }
  function phTipDate(ms) {
    return new Intl.DateTimeFormat('en-US', { year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(ms));
  }
  function phAxisDate(ms) {
    var d = new Date(ms);
    if (phRange === "1M" || phRange === "3M") {
      return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(d);
    }
    // Same trap as the value chart: a bare "Sep 25" reads as a day on the ranges
    // either side of this one.
    return new Intl.DateTimeFormat("en-US", { month: "short", year: "2-digit" }).format(d).replace(" ", " '");
  }
  function renderPriceChart() {
    var sel = $('#ph-symbol'); var host = $('#ph-chart'); if (!sel || !host) return;
    var sym = sel.value;
    var series = (DATA.priceHist && DATA.priceHist[sym]) || [];
    if (series.length < 2) { host.innerHTML = '<div class="empty">No price history for ' + esc(sym) + ' yet.</div>'; return; }
    var now = series[series.length - 1][0], span = phSpanMs(phRange);
    var data = span ? series.filter(function (p) { return p[0] >= now - span; }) : series.slice();
    if (data.length < 2) data = series.slice();
    var W = 920, H = 280, padL = 8, padR = 8, padT = 14, padB = 18;
    var xs = data.map(function (p) { return p[0]; }), ys = data.map(function (p) { return p[1]; });
    var xmin = xs[0], xmax = xs[xs.length - 1], xr = (xmax - xmin) || 1;
    var ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys), yr = (ymax - ymin) || 1;
    function X(t) { return padL + (t - xmin) / xr * (W - padL - padR); }
    function Y(v) { return padT + (1 - (v - ymin) / yr) * (H - padT - padB); }
    var up = ys[ys.length - 1] >= ys[0], color = up ? 'var(--up)' : 'var(--down)';
    var line = data.map(function (p, i) { return (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1); }).join(' ');
    var area = line + ' L' + X(xmax).toFixed(1) + ',' + (H - padB) + ' L' + X(xmin).toFixed(1) + ',' + (H - padB) + ' Z';
    // Three unlabelled gridlines: a shape with no price against it and no date
    // under it. On the History view this is the full-size price chart, so it
    // gets the same tick ladder as the value chart rather than a summary.
    var phTicks = niceTicks(ymin, ymax, 4).filter(function (t) {
      var y = Y(t); return y >= padT - 1 && y <= H - padB + 1;
    });
    var phStep = phTicks.length > 1 ? phTicks[1] - phTicks[0] : 0;
    var phYLabels = phTicks.map(function (t) {
      return '<span style="top:' + ((Y(t) / H) * 100).toFixed(2) + '%">' + axisMoney(t, phStep) + '</span>';
    }).join('');
    var phNX = Math.min(4, data.length), phXLabels = '';
    for (var pxi = 0; pxi < phNX; pxi++) {
      var pidx = phNX === 1 ? 0 : Math.round(pxi * (data.length - 1) / (phNX - 1));
      var pplace = pxi === 0 ? 'left:0'
        : pxi === phNX - 1 ? 'right:0'
        : 'left:' + ((X(data[pidx][0]) / W) * 100).toFixed(2) + '%;transform:translateX(-50%)';
      phXLabels += '<span style="' + pplace + '">' + phAxisDate(data[pidx][0]) + '</span>';
    }
    var phChg = ys[0] ? ((ys[ys.length - 1] - ys[0]) / ys[0]) * 100 : 0;
    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="width:100%;height:300px" role="img" ' +
      'aria-label="' + esc(sym) + ' price, ' + esc(phRange) + ' range, ' + F.pct(phChg) +
      ', from ' + F.money(ys[0]) + ' to ' + F.money(ys[ys.length - 1]) + '">' +
      '<defs><linearGradient id="phg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="' + color + '" stop-opacity=".18"/><stop offset="1" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
      phTicks.map(function (t) { var y = Y(t).toFixed(1); return '<line x1="0" x2="' + W + '" y1="' + y + '" y2="' + y + '" stroke="var(--border)" stroke-width="1" stroke-dasharray="2 4"/>'; }).join('') +
      '<path d="' + area + '" fill="url(#phg)"/><path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="2.2" stroke-linejoin="round"/>';
    if (phSpy && DATA.priceHist && DATA.priceHist.SPY) {
      var sp = DATA.priceHist.SPY.filter(function (p) { return p[0] >= xmin && p[0] <= xmax; });
      if (sp.length > 1) {
        // rebase SPY to the symbol's starting price: same % move = same slope,
        // honest apples-to-apples on the symbol's own $ scale
        var spyBase = sp[0][1], symBase = data[0][1];
        var sline = sp.map(function (p, i) {
          return (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ',' + Y(symBase * p[1] / spyBase).toFixed(1); }).join(' ');
        svg += '<path d="' + sline + '" fill="none" stroke="var(--warn)" stroke-width="1.6" stroke-dasharray="3 3" opacity=".85"/>';
      }
    }
    var lots = (DATA.symLots && DATA.symLots[sym]) || [];
    lots.forEach(function (l) {
      if (l.ts == null || l.ts < xmin || l.ts > xmax) return;
      var mx = X(l.ts), my = Y(l.price), c = l.side === 'BUY' ? 'var(--up)' : 'var(--down)';
      var tri = l.side === 'BUY'
        ? mx + ',' + (my - 7) + ' ' + (mx - 6) + ',' + (my + 5) + ' ' + (mx + 6) + ',' + (my + 5)
        : mx + ',' + (my + 7) + ' ' + (mx - 6) + ',' + (my - 5) + ' ' + (mx + 6) + ',' + (my - 5);
      svg += '<polygon points="' + tri + '" fill="' + c + '" stroke="var(--surface)" stroke-width="1"><title>' + esc(l.side + ' ' + l.qty + ' @ $' + l.price) + '</title></polygon>';
    });
    svg += '<line id="ph-cross" x1="0" x2="0" y1="' + padT + '" y2="' + (H - padB) + '" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" opacity="0"/>' +
      '<circle id="ph-cursor" cx="0" cy="0" r="4" fill="' + color + '" stroke="var(--surface)" stroke-width="1.5" opacity="0"/>';
    svg += '</svg>';
    var legend = '<div style="font-size:var(--fs-micro);color:var(--muted);margin-top:6px">' +
      '<span class="up">▲ BUY</span> &nbsp; <span class="down">▼ SELL</span>' +
      (phSpy ? ' &nbsp; · &nbsp; <span style="color:var(--warn)">┄ SPY, rebased to ' + esc(sym) + '’s start (same % scale)</span>' : '') + '</div>';
    // Audited 2026-09-02 for the XSS sink warning on this line. The only text
    // here that does not originate as a number is the symbol, and both places
    // it appears - the svg aria-label and the legend - pass it through esc().
    // The lot-marker titles are escaped at the point they are built, above.
    // nosemgrep
    host.innerHTML = '<div class="ph-wrap">' +
      '<div class="ph-y">' + phYLabels + '</div>' +
      '<div class="ph-plot">' + svg +
      '<div id="ph-tip" style="position:absolute;pointer-events:none;opacity:0;transform:translate(-50%,-115%);' +
      'background:var(--surface);border:1px solid var(--border);border-radius:var(--r-sm);padding:6px 9px;' +
      'box-shadow:var(--shadow-tip);font-size:var(--fs-meta);white-space:nowrap;z-index:5"></div>' +
      '<div class="ph-x">' + phXLabels + '</div>' +
      '</div></div>' + legend;
    var plot = host.querySelector('.ph-plot');
    var svgEl = plot.querySelector('svg');
    var cross = host.querySelector('#ph-cross'), cursor = host.querySelector('#ph-cursor'), tip = host.querySelector('#ph-tip');
    function move(ev) {
      var rect = svgEl.getBoundingClientRect();
      var svgX = ((ev.clientX - rect.left) / rect.width) * W;
      var best = 0, bestD = Infinity;
      for (var i = 0; i < data.length; i++) { var d = Math.abs(X(data[i][0]) - svgX); if (d < bestD) { bestD = d; best = i; } }
      var sx = X(data[best][0]), sy = Y(data[best][1]);
      cross.setAttribute('x1', sx); cross.setAttribute('x2', sx); cross.setAttribute('opacity', '1');
      cursor.setAttribute('cx', sx); cursor.setAttribute('cy', sy); cursor.setAttribute('opacity', '1');
      tip.style.left = (sx / W) * rect.width + 'px'; tip.style.top = (sy / H) * rect.height + 'px'; tip.style.opacity = '1';
      tip.innerHTML = '<div style="font-weight:600">' + esc(sym) + '  ' + F.money(data[best][1]) + '</div>' +
        '<div style="color:var(--muted);font-size:var(--fs-micro)">' + phTipDate(data[best][0]) + '</div>';
    }
    function leave() { cross.setAttribute('opacity', '0'); cursor.setAttribute('opacity', '0'); tip.style.opacity = '0'; }
    plot.addEventListener('mousemove', move);
    plot.addEventListener('mouseleave', leave);
  }
  function initPriceChart() {
    var sel = $('#ph-symbol'); if (!sel) return;
    var syms = (DATA.chartSyms || []).filter(function (s) { return DATA.priceHist && DATA.priceHist[s]; });
    if (!syms.length) syms = Object.keys(DATA.priceHist || {});
    sel.innerHTML = syms.map(function (s) { return '<option value="' + s + '">' + s + '</option>'; }).join('');
    sel.addEventListener('change', renderPriceChart);
    $('#ph-spy').addEventListener('change', function () { phSpy = this.checked; renderPriceChart(); });
    $all('#ph-chips [data-phr]').forEach(function (b) { b.addEventListener('click', function () {
      phRange = b.dataset.phr; $all('#ph-chips .chip').forEach(function (c) { c.classList.toggle('is-active', c === b); }); renderPriceChart(); }); });
    renderPriceChart();
  }

  // ---- latest prices table ----
  function renderLatestPrices() {
    var rows = DATA.latestPrices || [], tb = $('#latest-prices'); if (!tb) return;
    // 0.0 is what yfinance hands back for bid/ask outside regular hours, and the
    // collector stores it. It is an absent quote, not a price of nothing, and
    // rendering it as $0.00 in a money column is the one thing this table must
    // never do — a spread computed from it would be pure fiction.
    function px(v) {
      if (v == null || v === 0) return '<span style="color:var(--faint)" title="Not quoted at this snapshot">—</span>';
      return F.money(v);
    }
    // Freshness is judged against the newest row — the collector's own last run —
    // never against a wall clock: the weekend gap alone is 64.2h, so any absolute
    // threshold either fires every Monday or misses a real mid-week outage.
    var stamps = rows.map(function (r) { return Date.parse(String(r.ts || '').replace(' ', 'T')); })
                     .filter(function (t) { return !isNaN(t); });
    var newest = stamps.length ? Math.max.apply(null, stamps) : null;
    var STALE_MS = 4 * DAY_MS;   // clears a long weekend plus a public holiday
    var staleCount = 0;
    tb.innerHTML = rows.map(function (r) {
      var t = Date.parse(String(r.ts || '').replace(' ', 'T'));
      var age = (newest != null && !isNaN(t)) ? newest - t : 0;
      var stale = age > STALE_MS;
      if (stale) staleCount++;
      var days = Math.round(age / DAY_MS);
      return '<tr' + (stale ? ' class="lp-stale" title="Last collected ' + days + ' days before the most recent snapshot"' : '') + '>' +
        '<td><b>' + esc(r.symbol) + '</b></td><td class="price">' + px(r.last) + '</td>' +
        '<td class="num" style="color:var(--muted)">' + px(r.bid) + '</td>' +
        '<td class="num" style="color:var(--muted)">' + px(r.ask) + '</td>' +
        '<td class="num" style="color:var(--muted)">' + esc(r.source) + '</td>' +
        '<td class="num" style="color:var(--muted)">' + (stale ? CLOCK_SVG + ' ' : '') + esc(r.ts) + '</td></tr>';
    }).join('');
    var sub = $('#lp-sub');
    if (sub) sub.textContent = rows.length + ' symbols' +
      (staleCount ? ' · ' + staleCount + ' not refreshed in the last 4 days' : '');
  }


  // ---- statistics: records, monthly grid, consistency, streaks ----
  // Everything here reads DATA.stats, which the server computes from the same
  // TWR growth curve as the returns strip — so "best month" and MTD cannot
  // disagree. Partial periods are excluded from records by the server and are
  // hatched, not hidden, in the grid.
  function pctTxt(v, dp) { return v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(dp == null ? 2 : dp) + '%'; }

  function recCard(label, rec, meta) {
    if (!rec) return '<div class="rec"><div class="rec__lbl">' + label +
      '</div><div class="rec__val">—</div><div class="rec__meta">' + (meta || 'not enough history') + '</div></div>';
    var dir = rec.return_pct >= 0 ? 'up' : 'down';
    var when = rec.start === rec.end ? rec.start : rec.start + ' → ' + rec.end;
    return '<div class="rec rec--' + dir + '">' +
      '<div class="rec__lbl">' + label + '</div>' +
      '<div class="rec__val">' + pctTxt(rec.return_pct) + '</div>' +
      '<div class="rec__when">' + esc(rec.label) + '</div>' +
      '<div class="rec__meta">' + when + '</div></div>';
  }

  function streakRow(lbl, r) {
    if (!r) return '<div class="stk"><div class="stk__lbl">' + lbl +
      '</div><div class="stk__days">—</div></div>';
    return '<div class="stk stk--' + (r.direction === 'up' ? 'up' : 'down') + '">' +
      '<div class="stk__lbl">' + lbl + '</div>' +
      '<div class="stk__days">' + r.days + 'd</div>' +
      '<div class="stk__meta">' + pctTxt(r.return_pct) + ' · ' + r.start + ' → ' + r.end + '</div>' +
    '</div>';
  }

  // Shade a month cell by size relative to the largest absolute move on the
  // grid, so the scale adapts to the portfolio instead of assuming a range.
  function monthTint(v, peak) {
    if (v == null || !peak) return '';
    var a = Math.min(Math.abs(v) / peak, 1) * 0.42 + 0.06;
    // background-color, not the `background` shorthand: the shorthand resets
    // background-image, which silently defeated .stbl td.part's hatch — so every
    // partial period rendered identically to a finished one, and the legend
    // promised a marking that never appeared.
    return 'background-color:' + (v >= 0 ? 'oklch(62% .17 152/' + a.toFixed(3) + ')'
                                         : 'oklch(58% .19 26/' + a.toFixed(3) + ')') + ';';
  }

  function renderStats() {
    var st = DATA.stats;
    var recs = $('#st-recs'), months = $('#st-months'), hit = $('#st-hit'), stk = $('#st-streaks');
    if (!recs) return;

    if (!st || !st.ok) {
      var why = st && st.null_reason === 'insufficient_history'
        ? 'Not enough price history yet — statistics need at least two snapshot days.'
        : 'No statistics available.';
      recs.innerHTML = '<div class="empty">' + why + '</div>';
      if (months) months.innerHTML = '';
      if (hit) hit.innerHTML = '';
      if (stk) stk.innerHTML = '';
      return;
    }

    var cov = $('#st-coverage');
    if (cov) cov.innerHTML = st.coverage.days + ' trading days &middot; ' +
      st.coverage.start + ' → ' + st.coverage.end;

    // ---- records ----
    recs.innerHTML = [
      recCard('Best day', st.day.best),
      recCard('Worst day', st.day.worst),
      recCard('Best week', st.week.best, st.week.null_reason ? 'no complete weeks yet' : null),
      recCard('Worst week', st.week.worst, st.week.null_reason ? 'no complete weeks yet' : null),
      recCard('Best month', st.month.best, st.month.null_reason ? 'no complete months yet' : null),
      recCard('Worst month', st.month.worst, st.month.null_reason ? 'no complete months yet' : null)
    ].join('');

    // ---- monthly grid ----
    if (months) {
      var tbl = st.monthly_table, peak = 0;
      tbl.rows.forEach(function (r) {
        r.months.forEach(function (m) { if (m != null) peak = Math.max(peak, Math.abs(m)); });
      });
      var head = '<tr><th class="stbl__yr">Year</th>' +
        tbl.labels.map(function (l) { return '<th>' + l + '</th>'; }).join('') +
        '<th>Year</th></tr>';
      var body = tbl.rows.map(function (r) {
        var cells = r.months.map(function (m, i) {
          var partial = r.partial_months.indexOf(tbl.labels[i]) >= 0;
          var cls = 'stbl__m' + (m == null ? '' : ' on') + (partial ? ' part' : '');
          var title = m == null ? 'no observations'
            : tbl.labels[i] + ' ' + r.year + ': ' + pctTxt(m) + (partial ? ' (partial period)' : '');
          return '<td class="' + cls + '" style="' + monthTint(m, peak) + '" title="' + title + '">' +
                 (m == null ? '·' : pctTxt(m, 1)) + '</td>';
        }).join('');
        return '<tr><td class="stbl__yr">' + r.year + '</td>' + cells +
          '<td class="tot' + (r.year_pct == null ? '' : ' on') + '" title="' +
          r.months_observed + ' month(s) observed">' + pctTxt(r.year_pct, 1) + '</td></tr>';
      }).join('');
      months.innerHTML = head + body;
    }

    // ---- consistency ----
    if (hit) {
      hit.innerHTML = ['day', 'week', 'month'].map(function (k) {
        var b = st[k], decided = b.positive + b.negative;
        var upPct = decided ? (b.positive / decided) * 100 : 0;
        var note = b.partial_excluded
          ? b.complete + ' complete (' + b.partial_excluded + ' partial excluded)'
          : b.complete + ' ' + k + 's';
        return '<div class="hitrow">' +
          '<div class="hitrow__lbl">' + k + 's</div>' +
          '<div class="hitrow__track">' +
            '<div class="hitrow__up" style="width:' + upPct.toFixed(1) + '%"></div>' +
            '<div class="hitrow__down" style="width:' + (100 - upPct).toFixed(1) + '%"></div>' +
          '</div>' +
          '<div class="hitrow__val" title="' + note + '">' +
            (b.hit_rate_pct == null ? '—' : b.hit_rate_pct.toFixed(0) + '% up') +
            ' <span style="color:var(--faint)">· ' + b.complete + '</span></div>' +
        '</div>';
      }).join('') +
      '<div style="margin-top:12px;font-size:var(--fs-meta);color:var(--muted)">' +
        'Average up month ' + pctTxt(st.month.best_average_pct) +
        ' · average down month ' + pctTxt(st.month.worst_average_pct) +
      '</div>';
    }

    // ---- streaks ----
    if (stk) {
      var s = st.streaks;
      stk.innerHTML = streakRow('Longest up', s.longest_up) + streakRow('Longest down', s.longest_down) +
        streakRow('Current', s.current && s.current.direction !== 'flat' ? s.current : null);
    }
  }

  // ---- movers + heatmap + breadth ----
  // Sign-and-Size, composed per theme. Hue still carries the sign and lightness
  // and chroma still carry the magnitude — but the two themes need opposite
  // ranges, because a tile's label has to be readable on it. One range forced
  // onto both themes cannot work: the old L 64->44 swept through a band where
  // neither white nor dark ink reaches 4.5:1 (worst case 4.35 around a 1% move),
  // so every tile in the middle of the scale was illegible whatever the label
  // colour. Light theme goes pale and takes ink; dark goes deep and takes white.
  function heatColor(p) { var m = Math.min(Math.abs(p)/3.2, 1);
    if (Math.abs(p) < 0.08) return isDark() ? 'oklch(38% 0.012 260)' : 'oklch(72% 0.015 260)';
    var hue = p >= 0 ? 152 : 26;
    return isDark()
      ? 'oklch(' + (52-m*16).toFixed(1) + '% ' + (0.06+m*0.13).toFixed(3) + ' ' + hue + ')'
      : 'oklch(' + (80-m*16).toFixed(1) + '% ' + (0.04+m*0.16).toFixed(3) + ' ' + hue + ')'; }
  function moverRow(s, rank) {
    return '<div class="mv" ' + symTrigger(s.sym) + '><span class="mv__rank">' + rank + '</span><span class="mv__sym"><b>' + s.sym + '</b>' + subName(s.sym, s.name, 'span') +
      '</span></span><span class="mv__px">' + F.money(s.price) + '</span><span class="mv__chg ' + chgCls(s.dayPct) + '">' + F.pct(s.dayPct) + '</span></div>'; }
  function renderMovers() {
    // exclude unknown day change (null) — can't rank what we can't measure
    var all = list().filter(function (s) { return s.dayPct != null && (s.dayPct !== 0 || s.hist.length > 1); });
    // Filter by sign before slicing. Without it a short list is padded from the
    // other direction, so "Top gainers" printed losers directly beneath a breadth
    // bar saying how many symbols actually advanced.
    var gainers = all.filter(function (s) { return s.dayPct > 0; }).sort(function (a, b) { return b.dayPct - a.dayPct; }).slice(0, 6);
    var losers = all.filter(function (s) { return s.dayPct < 0; }).sort(function (a, b) { return a.dayPct - b.dayPct; }).slice(0, 6);
    function moverList(rows, word) {
      if (!rows.length) return '<div class="empty">No symbol ' + word + ' today.</div>';
      var out = rows.map(function (s, i) { return moverRow(s, i + 1); }).join('');
      // Say when a list is short because the day was, not because it was cut.
      if (rows.length < 6) out += '<div class="empty" style="padding:12px 6px">Only ' + rows.length +
        ' symbol' + (rows.length > 1 ? 's' : '') + ' ' + word + ' today.</div>';
      return out;
    }
    $('#gainers').innerHTML = moverList(gainers, 'advanced');
    $('#losers').innerHTML = moverList(losers, 'declined');
  }
  var heatState = { sector: 'All', query: '' };
  var SECTORS = (function () { var set = {}; list().forEach(function (s) { set[s.sector] = 1; }); return Object.keys(set).sort(function (a, b) { return a.localeCompare(b); }); })();
  function tileHTML(s) { return '<div class="heat__tile" ' + symTrigger(s.sym, s.sym + ' ' + F.pct(s.dayPct) + ' — open details') + ' style="background:' + heatColor(s.dayPct) + '" title="' + esc(s.name) + ' · ' + F.money(s.price) +
    '"><div><b>' + s.sym + '</b>' + subName(s.sym, s.name, 'div', ' class="nm"') + '</div><div class="pc">' + F.pct(s.dayPct) + '</div></div>'; }
  function heatVisible(s) { if (heatState.sector !== 'All' && s.sector !== heatState.sector) return false;
    if (heatState.query) { var q = heatState.query.toLowerCase(); if (s.sym.toLowerCase().indexOf(q) < 0 && (s.name||'').toLowerCase().indexOf(q) < 0) return false; } return true; }
  function renderHeat() { var html = '', any = false;
    SECTORS.forEach(function (sec) { var items = list().filter(function (s) { return s.sector === sec && heatVisible(s); }); if (!items.length) return; any = true;
      var known = items.filter(function (s) { return s.dayPct != null; });
      var avg = known.length ? known.reduce(function (a, s) { return a + s.dayPct; }, 0) / known.length : 0;
      html += '<div class="heat__sector-title">' + esc(sec) + '<span class="num ' + (avg>=0?'up':'down') + '" style="font-weight:600">' + F.pct(avg) +
        '</span><span style="color:var(--faint);font-weight:400;font-size:var(--fs-micro)">' + items.length + ' names</span></div>';
      items.sort(function (a, b) { return b.dayPct - a.dayPct; }); html += items.map(tileHTML).join(''); });
    $('#heat').innerHTML = any ? html : '<div class="empty">No symbols match your filter.</div>'; }
  function renderChips() { var chips = ['All'].concat(SECTORS);
    $('#sector-chips').innerHTML = chips.map(function (c) { return '<button class="chip ' + (c===heatState.sector?'is-active':'') + '" data-sector="' + esc(c) + '">' + esc(c) + '</button>'; }).join('');
    $all('#sector-chips [data-sector]').forEach(function (b) { b.addEventListener('click', function () { heatState.sector = b.dataset.sector; renderChips(); renderHeat(); }); }); }
  var mvSearch = $('#mv-search'); if (mvSearch) mvSearch.addEventListener('input', function () { heatState.query = mvSearch.value.trim(); renderHeat(); });
  function renderBreadth() { var all = list(); var adv = all.filter(function (s) { return s.dayPct > 0; }).length;
    var dec = all.filter(function (s) { return s.dayPct < 0; }).length; var unch = all.length - adv - dec;
    var advPct = all.length ? (adv/all.length*100) : 0;
    $('#breadth-sub').textContent = 'advancers vs decliners · ' + all.length + ' names tracked';
    $('#breadth-bar').innerHTML = '<div style="display:flex;height:10px;border-radius:99px;overflow:hidden;background:var(--surface-3)">' +
      '<div style="width:' + advPct + '%;background:var(--up)"></div><div style="flex:1;background:var(--down)"></div></div>';
    $('#breadth-legend').innerHTML = '<span class="up">▲ ' + adv + ' advancing</span><span style="color:var(--muted)">● ' + unch +
      ' flat</span><span class="down">▼ ' + dec + ' declining</span>'; }

  // ---- alerts & news feed ----
  var ICONS = {
    up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg>',
    down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7l6 6 4-4 8 8"/><path d="M17 17h4v-4"/></svg>',
    bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>',
    news: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h13v14H6a2 2 0 0 1-2-2zM17 8h3v9a2 2 0 0 1-2 2"/><path d="M8 9h6M8 13h6"/></svg>'
  };
  var seq = 1, feedItems = [];
  (DATA.news || []).forEach(function (n) { feedItems.push({ id: seq++, cat: 'news', sym: n.sym, title: n.title, body: n.body || '',
    src: n.src, time: n.time, url: n.url, unread: false }); });
  var feedState = { filter: 'all' };
  function passes(it) { if (feedState.filter === 'all') return true;
    if (feedState.filter === 'triggered') return it.cat==='alert' && it.status==='triggered';
    if (feedState.filter === 'armed') return it.cat==='alert' && it.status==='armed';
    if (feedState.filter === 'news') return it.cat==='news'; return true; }
  function iconFor(it) { if (it.cat === 'news') return { cls:'ic--news', svg:ICONS.news };
    if (it.status === 'armed') return { cls:'ic--info', svg:ICONS.bell };
    return it.dir === 'down' ? { cls:'ic--down', svg:ICONS.down } : { cls:'ic--up', svg:ICONS.up }; }
  function itemHTML(it) { var ic = iconFor(it); var badge;
    if (it.cat==='alert' && it.status==='armed') badge = '<span class="tag tag--neu">ARMED</span>';
    else if (it.cat==='alert' && it.status==='triggered') badge = '<span class="tag ' + (it.dir==='down'?'tag--down':'tag--up') + '">TRIGGERED</span>';
    else badge = '<span class="tag tag--neu">' + esc(it.sym || '—') + '</span>';
    var titleHTML = it.url ? '<a href="' + esc(it.url) + '" target="_blank" rel="noopener">' + esc(it.title) + '</a>' : esc(it.title);
    return '<div class="fitem ' + (it.unread?'unread':'') + '" data-id="' + it.id + '"><span class="fitem__ic ' + ic.cls + '">' + ic.svg + '</span>' +
      '<div class="fitem__bd"><h3>' + titleHTML + '</h3>' + (it.body ? '<p>' + esc(it.body) + '</p>' : '') +
      '<div class="fitem__meta"><span class="src">' + esc(it.src) + '</span><span>•</span>' + badge +
      (it.cat==='alert' && it.status==='armed' && get(it.sym) ? '<span>•</span><span>now ' + F.money(get(it.sym).price) + '</span>' : '') + '</div></div>' +
      '<div class="fitem__side"><span class="fitem__time">' + esc(it.time) + '</span>' +
      (it.unread ? '<button class="markread" data-read="' + it.id + '">Mark read</button>' : '') + '</div></div>'; }
  function byId(id) { for (var i = 0; i < feedItems.length; i++) if (feedItems[i].id === id) return feedItems[i]; return null; }
  function updateCounts() { var unread = feedItems.filter(function (it) { return it.unread; }).length;
    $all('.nav .badge').forEach(function (b) { b.textContent = unread; b.style.display = unread ? '' : 'none'; });
    var hc = $('#unread-count'); if (hc) hc.textContent = unread;
    // Offering "Mark all read" against nothing promises work that will not
    // happen; the control now says so before it is pressed.
    var ma = $('#mark-all');
    if (ma) { ma.disabled = unread === 0; ma.title = unread === 0 ? 'Nothing unread' : 'Mark ' + unread + ' as read'; }
    var arm = feedItems.filter(function (it) { return it.cat==='alert' && it.status==='armed'; }).length;
    var ac = $('#armed-count'); if (ac) ac.textContent = arm; }
  function renderFeed() { var list2 = feedItems.filter(passes); var host = $('#feed');
    host.innerHTML = list2.length ? list2.map(itemHTML).join('') : '<div class="empty">Nothing here yet.</div>';
    $all('[data-read]', host).forEach(function (b) { b.addEventListener('click', function () { var it = byId(+b.dataset.read); if (it) { it.unread = false; renderFeed(); } }); });
    updateCounts(); }
  $all('[data-filter]').forEach(function (chip) { chip.addEventListener('click', function () { feedState.filter = chip.dataset.filter;
    $all('[data-filter]').forEach(function (c) { c.classList.toggle('is-active', c === chip); }); renderFeed(); }); });
  var markAll = $('#mark-all'); if (markAll) markAll.addEventListener('click', function () { feedItems.forEach(function (it) { it.unread = false; }); renderFeed(); toast('All notifications marked read'); });

  // create-alert form (ephemeral; evaluated against latest snapshot price)
  var form = $('#alert-form'), symIn = $('#a-sym'), condIn = $('#a-cond'), priceIn = $('#a-price'),
      symErr = $('#a-sym-err'), priceErr = $('#a-price-err'), hint = $('#a-hint');
  function setInvalid(input, errEl, msg) {
    input.classList.toggle('invalid', !!msg);
    if (msg) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
    errEl.textContent = msg || '';
  }
  if (symIn) symIn.addEventListener('input', function () { setInvalid(symIn, symErr, '');
    var s = get(symIn.value.trim().toUpperCase()); hint.textContent = s ? (s.name + ' · now ' + F.money(s.price)) : ''; });
  if (priceIn) priceIn.addEventListener('input', function () { setInvalid(priceIn, priceErr, ''); });
  if (form) form.addEventListener('submit', function (e) { e.preventDefault();
    var v = (symIn.value||'').trim().toUpperCase(); var s = get(v); var n = Number.parseFloat(priceIn.value); var ok = true;
    if (!v) { setInvalid(symIn, symErr, 'Enter a symbol.'); ok = false; }
    else if (!s) { setInvalid(symIn, symErr, 'Unknown symbol in this portfolio.'); ok = false; }
    if (Number.isNaN(n) || n <= 0) { setInvalid(priceIn, priceErr, 'Enter a price above $0.'); ok = false; }
    if (!ok) return;
    var cond = condIn.value, target = +n.toFixed(2);
    var met = cond === 'above' ? s.price >= target : s.price <= target;
    var it = { id: seq++, cat: 'alert', sym: v, cond: cond, target: target, src: 'Price alert', time: 'just now', unread: met };
    if (met) { it.status = 'triggered'; it.dir = cond === 'above' ? 'up' : 'down';
      it.title = v + (cond==='above'?' crossed above ':' dropped below ') + F.money(target);
      it.body = s.name + ' is at ' + F.money(s.price) + ', already ' + (cond==='above'?'above':'below') + ' your ' + F.money(target) + ' target.';
      toast('⚡ Alert triggered: ' + it.title); }
    else { it.status = 'armed'; it.title = v + ' ' + cond + ' ' + F.money(target);
      it.body = 'Notify when ' + s.name + ' trades ' + cond + ' ' + F.money(target) + '.'; toast('Alert armed: ' + it.title); }
    feedItems.unshift(it); form.reset(); hint.textContent = '';
    feedState.filter = met ? 'triggered' : 'armed';
    $all('[data-filter]').forEach(function (c) { c.classList.toggle('is-active', c.dataset.filter === feedState.filter); });
    renderFeed(); });

  // ---- command palette (Ctrl+K): symbols, views, actions ----
  var cmdk = $('[data-cmdk]'), cmdkIn = $('[data-cmdk-input]'), cmdkList = $('[data-cmdk-list]');
  var cmdkSel = 0, cmdkItems = [];
  function openFundamentalsFor(sym) {
    switchView('fundamentals');
    var sel = $('#fd-symbol');
    if (sel && sel.options.length) { sel.value = sym; renderFundamentals(); }
  }
  function cmdkCommands() {
    var cmds = [
      { sect: 'Views', label: 'Portfolio', run: function () { switchView('portfolio'); } },
      { sect: 'Views', label: 'Market Movers', run: function () { switchView('movers'); } },
      { sect: 'Views', label: 'Statistics', run: function () { switchView('stats'); } },
      { sect: 'Views', label: 'Alerts & News', run: function () { switchView('alerts'); } },
      { sect: 'Views', label: 'Fundamentals', run: function () { switchView('fundamentals'); } },
      { sect: 'Views', label: 'History', run: function () { switchView('history'); } },
      { sect: 'Views', label: 'Manage', run: function () { goNative('manage'); } },
      { sect: 'Views', label: 'Advisor', run: function () { goNative('advisor'); } },
      { sect: 'Actions', label: 'Refresh data', run: function () { if (refreshBtn) refreshBtn.click(); } },
      { sect: 'Actions', label: 'Toggle dark mode', run: function () { applyTheme(isDark() ? 'light' : 'dark'); } },
      { sect: 'Actions', label: 'Mark all alerts read', run: function () { var b = $('#mark-all'); if (b) b.click(); } },
      { sect: 'Actions', label: 'Export positions CSV', run: function () { var b = $('#pos-csv'); if (b) b.click(); } }
    ];
    list().sort(function (a, b) { return a.sym.localeCompare(b.sym); }).forEach(function (s) {
      cmds.push({ sect: 'Symbols', label: s.sym, detail: s.name, px: F.money(s.price) + '  ' + F.pct(s.dayPct), upDn: s.dayPct != null && s.dayPct >= 0,
        run: function () { openDrawer(s.sym); } });
    });
    return cmds;
  }
  function cmdkScore(c, q) {
    if (!q) return 1;
    var hay = (c.label + ' ' + (c.detail || '')).toLowerCase();
    if (c.label.toLowerCase().indexOf(q) === 0) return 3;
    if (hay.indexOf(q) >= 0) return 2;
    return 0;
  }
  function renderCmdk() {
    var q = (cmdkIn.value || '').trim().toLowerCase();
    var all = cmdkCommands();
    cmdkItems = all.map(function (c) { return { c: c, s: cmdkScore(c, q) }; })
      .filter(function (x) { return x.s > 0; })
      .sort(function (a, b) { return b.s - a.s; })
      .slice(0, 24).map(function (x) { return x.c; });
    if (cmdkSel >= cmdkItems.length) cmdkSel = 0;
    var html = '', lastSect = null;
    cmdkItems.forEach(function (c, i) {
      if (c.sect !== lastSect) { html += '<div class="cmdk__sect">' + esc(c.sect) + '</div>'; lastSect = c.sect; }
      html += '<div class="cmdk__item' + (i === cmdkSel ? ' is-sel' : '') + '" data-ci="' + i + '" role="option" aria-selected="' + (i === cmdkSel) + '">' +
        '<b>' + esc(c.label) + '</b>' + (c.detail ? '<span style="font-size:var(--fs-meta);color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(c.detail) + '</span>' : '') +
        (c.px ? '<span class="px ' + (c.upDn ? 'up' : 'down') + '">' + c.px + '</span>' : '') + '</div>';
    });
    cmdkList.innerHTML = html || '<div class="empty">No matches.</div>';
    $all('[data-ci]', cmdkList).forEach(function (el) {
      el.addEventListener('click', function () { runCmdk(+el.dataset.ci); });
      el.addEventListener('mousemove', function () { if (cmdkSel !== +el.dataset.ci) { cmdkSel = +el.dataset.ci; renderCmdk(); } });
    });
  }
  function runCmdk(i) { var c = cmdkItems[i]; closeCmdk(); if (c) c.run(); }
  var cmdkPrevFocus = null;
  function openCmdk() {
    if (!cmdk) return;
    cmdkPrevFocus = document.activeElement;
    cmdk.classList.add('open'); cmdkIn.value = ''; cmdkSel = 0; renderCmdk(); cmdkIn.focus();
  }
  function closeCmdk() {
    if (!cmdk || !cmdk.classList.contains('open')) return;
    cmdk.classList.remove('open');
    // Hand focus back where it came from. A command that moves the user itself
    // (openDrawer, switchView) takes focus from here afterwards, which is why
    // runCmdk closes before it runs.
    if (cmdkPrevFocus && cmdkPrevFocus.focus) { try { cmdkPrevFocus.focus(); } catch (e) {} }
    cmdkPrevFocus = null;
  }
  if (cmdk) {
    cmdk.addEventListener('mousedown', function (e) { if (e.target === cmdk) closeCmdk(); });
    cmdkIn.addEventListener('input', function () { cmdkSel = 0; renderCmdk(); });
    cmdkIn.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); cmdkSel = Math.min(cmdkSel + 1, cmdkItems.length - 1); renderCmdk(); scrollCmdkSel(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); cmdkSel = Math.max(cmdkSel - 1, 0); renderCmdk(); scrollCmdkSel(); }
      else if (e.key === 'Enter') { e.preventDefault(); runCmdk(cmdkSel); }
      else if (e.key === 'Escape') { closeCmdk(); }
    });
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); openCmdk(); }
      else if (e.key === 'Escape' && cmdk.classList.contains('open')) closeCmdk();
    });
  }
  function scrollCmdkSel() {
    var el = cmdkList.querySelector('.is-sel'); if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
  }
  var gsearch = $('[data-global-search]');
  if (gsearch) {
    gsearch.addEventListener('click', openCmdk);
    // Opening on `focus` used to blur the field and trap the caller in the
    // palette: tabbing into the topbar ejected keyboard users from the document
    // and nothing after this field was ever reachable in a linear walk. Enter or
    // Space opens it deliberately; Tab now passes straight through.
    gsearch.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openCmdk(); }
    });
  }
  // The phone's route to the palette. The search field is display:none below
  // 980px and Ctrl+K needs a keyboard, so this button is the only way in.
  var searchBtn = $('[data-open-search]');
  if (searchBtn) searchBtn.addEventListener('click', openCmdk);

  // ---- symbol drill-down drawer: click any symbol for its full dossier ----
  var drawerEl = $('[data-drawer]'), drawerScrimEl = $('[data-drawer-scrim]');
  var drawerHd = $('[data-drawer-hd]'), drawerBd = $('[data-drawer-bd]'), drawerFoot = $('[data-drawer-foot]');
  var drawerPrevFocus = null;
  var X_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>';
  function drawerChartHTML(sym) {
    var series = (DATA.priceHist && DATA.priceHist[sym]) || [];
    var s = get(sym);
    if (series.length < 2) {
      return (s && s.hist && s.hist.length > 1)
        ? sparkSVG(s.hist, s.hist[s.hist.length - 1] >= s.hist[0], 440, 110) : '';
    }
    var now = series[series.length - 1][0];
    var data = series.filter(function (p) { return p[0] >= now - phSpanMs('3M'); });
    if (data.length < 2) data = series.slice();
    var W = 440, H = 140, padT = 10, padB = 12, padL = 4, padR = 4;
    var xs = data.map(function (p) { return p[0]; }), ys = data.map(function (p) { return p[1]; });
    var xmin = xs[0], xmax = xs[xs.length - 1], xr = (xmax - xmin) || 1;
    var ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys), yr = (ymax - ymin) || 1;
    function X(t) { return padL + (t - xmin) / xr * (W - padL - padR); }
    function Y(v) { return padT + (1 - (v - ymin) / yr) * (H - padT - padB); }
    var up = ys[ys.length - 1] >= ys[0], color = up ? 'var(--up)' : 'var(--down)';
    var line = data.map(function (p, i) { return (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1); }).join(' ');
    var area = line + ' L' + X(xmax).toFixed(1) + ',' + (H - padB) + ' L' + X(xmin).toFixed(1) + ',' + (H - padB) + ' Z';
    var id = 'dg' + (_sparkSeq++);
    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="width:100%;height:140px;display:block" role="img" aria-label="' + esc(sym) + ' price, last 3 months">' +
      '<defs><linearGradient id="' + id + '" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="' + color + '" stop-opacity=".18"/><stop offset="1" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
      '<path d="' + area + '" fill="url(#' + id + ')"/><path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linejoin="round"/>';
    ((DATA.symLots && DATA.symLots[sym]) || []).forEach(function (l) {
      if (l.ts == null || l.ts < xmin || l.ts > xmax) return;
      var mx = X(l.ts), my = Y(l.price), c = l.side === 'BUY' ? 'var(--up)' : 'var(--down)';
      var tri = l.side === 'BUY'
        ? mx + ',' + (my - 6) + ' ' + (mx - 5) + ',' + (my + 4) + ' ' + (mx + 5) + ',' + (my + 4)
        : mx + ',' + (my + 6) + ' ' + (mx - 5) + ',' + (my - 4) + ' ' + (mx + 5) + ',' + (my - 4);
      svg += '<polygon points="' + tri + '" fill="' + c + '" stroke="var(--surface)" stroke-width="1"><title>' + esc(l.side + ' ' + l.qty + ' @ $' + l.price) + '</title></polygon>';
    });
    svg += '</svg>';
    var chg = (ys[ys.length - 1] - ys[0]) / ys[0] * 100;
    // This is the chart carrying the BUY and SELL markers - the picture behind
    // a cost basis - and it had no scale of any kind: no price against the
    // triangles, no dates, and a footer that said "over range" without ever
    // naming the range. At 140px a tick ladder would crowd the line, so the
    // scale is stated at its bounds instead: the span in dates, the extent in
    // prices. Both are what a marker is read against.
    var dFrom = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' }).format(new Date(xmin));
    var dTo = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' }).format(new Date(xmax));
    return svg +
      '<div class="dc-x"><span>' + dFrom + '</span><span>' + dTo + '</span></div>' +
      '<div class="dc-foot"><span class="num" style="color:' + color + '">' + F.pct(chg) + ' over 3 months</span>' +
      '<span class="dc-hl num">low ' + F.money(ymin) + ' · high ' + F.money(ymax) + '</span></div>';
  }
  function openDrawer(sym) {
    var s = get(sym); if (!s || !drawerEl) return;
    var h = null; (DATA.holdings || []).forEach(function (x) { if (x.sym === sym) h = x; });
    drawerHd.innerHTML = symBadge(sym) +
      '<span class="nm"><b>' + sym + '</b><span>' + esc(s.name) + ' · ' + esc(s.sector) + '</span></span>' +
      '<button class="iconbtn drawer__close" data-drawer-close aria-label="Close">' + X_ICON + '</button>';
    var html = '<div style="display:flex;align-items:baseline;gap:12px">' +
      '<span class="drawer__px">' + F.money(s.price) + '</span>' +
      '<span class="num ' + chgCls(s.dayPct) + '" style="font-size:var(--fs-base);font-weight:600">' + F.pct(s.dayPct) + ' today</span></div>';
    if (h) {
      var mktVal = s.price * h.qty, cost = h.avgCost * h.qty, gl = mktVal - cost, glPct = cost ? gl / cost * 100 : 0;
      var t = totals(holdingRows()); var totalVal = t.mktVal + CASH;
      var box = function (l, v, cls) { return '<div><div class="l">' + l + '</div><div class="v' + (cls ? ' ' + cls : '') + '">' + v + '</div></div>'; };
      html += '<div class="drawer__stats">' +
        box('Quantity', Math.round(h.qty * 10000) / 10000) +
        box('Avg cost', F.money(h.avgCost) +
          ' <span style="font-size:var(--fs-micro);color:var(--muted);font-family:var(--font)">' +
          esc(DATA.engine || 'FIFO') + '</span>') +
        box('Market value', F.money(mktVal)) +
        box('Cost basis', F.money(cost)) +
        box('Unrealized', (gl >= 0 ? '+' : '−') + F.money(Math.abs(gl)) + ' <span style="font-size:var(--fs-micro)">' + F.pct(glPct) + '</span>', gl >= 0 ? 'up' : 'down') +
        box('Weight', totalVal > 0 ? (mktVal / totalVal * 100).toFixed(1) + '%' : '—') + '</div>';
    } else {
      html += '<div style="margin-top:12px;font-size:var(--fs-sm);color:var(--muted)">Watchlist symbol — no open position.</div>';
    }
    var chart = drawerChartHTML(sym);
    if (chart) html += '<div class="drawer__sect">Price · 3M <span class="n">your trades marked</span></div>' + chart;
    var symAll = (DATA.histLots || []).filter(function (l) { return l.symbol === sym; });
    var lots = symAll.slice(0, 12);
    if (lots.length) {
      // "7 most recent" printed the number shown, so an operator checking an avg
      // cost could not tell they were looking at a subset. Name the whole.
      var total = Math.max(lotTotal(sym), symAll.length);
      var lotLabel = total > lots.length ? 'showing ' + lots.length + ' of ' + total
                                         : lots.length + (lots.length === 1 ? ' lot' : ' lots');
      html += '<div class="drawer__sect">Lots <span class="n">' + lotLabel + '</span></div>' +
        '<div class="tbl-wrap"><table class="tbl"><thead><tr><th style="text-align:left">Date</th><th>Side</th><th>Qty</th><th>Price</th></tr></thead><tbody>' +
        lots.map(function (l) {
          return '<tr><td class="num" style="text-align:left">' + esc(l.date) + '</td>' +
            '<td><span class="tag ' + (l.side === 'BUY' ? 'tag--up' : 'tag--down') + '">' + l.side + '</span></td>' +
            '<td class="num">' + l.qty + '</td><td class="num">' + F.money(l.price) + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    }
    var news = (DATA.news || []).filter(function (n) { return n.sym === sym; }).slice(0, 5);
    if (news.length) {
      html += '<div class="drawer__sect">News</div><div class="drawer__news">' +
        news.map(function (n) {
          var title = n.url ? '<a href="' + esc(n.url) + '" target="_blank" rel="noopener">' + esc(n.title) + '</a>' : esc(n.title);
          return '<div style="padding:8px 0;border-bottom:1px solid var(--border)">' +
            '<div style="font-size:var(--fs-sm);font-weight:600;line-height:1.4">' + title + '</div>' +
            '<div style="font-size:var(--fs-micro);color:var(--muted);margin-top:3px">' + esc(n.src) + ' · ' + esc(n.time) + '</div></div>';
        }).join('') + '</div>';
    }
    drawerBd.innerHTML = html;
    drawerFoot.innerHTML = '<button class="btn btn--ghost" data-drawer-close>Close</button>' +
      (symAll.length > lots.length
        ? '<button class="btn btn--ghost" data-drawer-history>All lots →</button>' : '') +
      '<button class="btn" data-drawer-fd>Full fundamentals →</button>';
    var histBtn = $('[data-drawer-history]');
    if (histBtn) histBtn.addEventListener('click', function () { closeDrawer(); switchView('history'); });
    $all('[data-drawer-close]').forEach(function (b) { b.addEventListener('click', closeDrawer); });
    var fdBtn = $('[data-drawer-fd]');
    if (fdBtn) fdBtn.addEventListener('click', function () { closeDrawer(); openFundamentalsFor(sym); });
    drawerPrevFocus = document.activeElement;
    drawerEl.classList.add('open'); drawerScrimEl.classList.add('show');
    drawerBd.scrollTop = 0;
    drawerEl.focus();
  }
  // ---- KPI evidence -------------------------------------------------------
  // Every tile states a conclusion and, until now, offered no way back to the
  // arithmetic behind it. "Unrealized P&L +$815.75" is a claim; this is the
  // route from the claim to its terms and to the rows they were summed from,
  // which is the premise the whole product rests on.
  function evEq(rows, result) {
    return '<table class="ev-eq">' + rows.map(function (r) {
      return '<tr><td class="ev-op">' + (r[2] || '') + '</td><td>' + esc(r[0]) +
        '</td><td class="num' + (r[3] ? ' ' + r[3] : '') + '">' + r[1] + '</td></tr>';
    }).join('') + '<tr class="ev-sum"><td class="ev-op"></td><td>' + esc(result[0]) +
      '</td><td class="num' + (result[2] ? ' ' + result[2] : '') + '">' + result[1] + '</td></tr></table>';
  }
  // The server rounds each term to the cent on its own, and rounds the total
  // from unrounded inputs - so the printed terms can miss the printed total by
  // a penny. On a panel whose whole job is showing the arithmetic, a sum that
  // does not add up is worse than no sum, so the gap is named where it happens
  // rather than hidden by recomputing the total from the rounded terms.
  function evRounding(terms, value) {
    // Compare what is printed, not what is held: each term is shown to the cent,
    // so the check has to round them the same way or it misses a column that
    // visibly does not add up while its underlying floats do.
    var cents = function (v) { return Math.round(v * 100) / 100; };
    var sum = 0;
    for (var i = 0; i < terms.length; i++) sum += cents(terms[i]);
    if (!(Math.abs(sum - cents(value)) >= 0.005)) return '';
    return '<p class="ev-round">Terms are rounded to the cent; the total is taken before rounding, so the column can differ from it by a penny.</p>';
  }
  function evTable(head, body) {
    if (!body.length) return '';
    return '<div class="tbl-wrap"><table class="tbl ev-tbl"><thead><tr>' +
      head.map(function (h, i) { return '<th' + (i ? '' : ' style="text-align:left"') + '>' + esc(h) + '</th>'; }).join('') +
      '</tr></thead><tbody>' + body.map(function (r) {
        return '<tr>' + r.map(function (c, i) {
          return i ? '<td class="num">' + c + '</td>' : '<td>' + c + '</td>';
        }).join('') + '</tr>';
      }).join('') + '</tbody></table></div>';
  }
  function evGoHoldings() {
    closeDrawer();
    var t = $('#holdings-tbl') || $('#holdings-cards');
    if (t) t.scrollIntoView({ block: 'center', behavior: REDUCED ? 'auto' : 'smooth' });
  }
  function evGoView(v) {
    closeDrawer();
    var a = $('[data-view="' + v + '"]');
    if (a) a.click();
  }
  function kpiEvidence(key) {
    var k = DATA.kpi || {};
    var rs = holdingRows().slice().sort(function (a, b) { return b.mktVal - a.mktVal; });
    var t = totals(rs);
    if (key === 'totalValue') {
      return {
        title: 'Total value', value: F.money(k.totalValue || 0),
        what: 'Everything this ledger can put a price on: your open positions at their last recorded price, plus the cash balance you entered.',
        eq: evEq([['Market value', F.money(k.marketValue || 0), ''],
                  ['Buying power', F.money(k.cash || 0), '+']],
                 ['Total value', F.money(k.totalValue || 0)]) +
                 evRounding([k.marketValue || 0, k.cash || 0], k.totalValue || 0),
        table: evTable(['Symbol', 'Value', 'Weight'], rs.map(function (r) {
          return ['<b>' + esc(r.sym) + '</b>', F.money(r.mktVal),
                  ((k.totalValue ? r.mktVal / k.totalValue : 0) * 100).toFixed(1) + '%'];
        }).concat(CASH > 0 ? [['Cash', F.money(CASH), ((k.totalValue ? CASH / k.totalValue : 0) * 100).toFixed(1) + '%']] : [])),
        route: { label: 'See holdings', run: evGoHoldings }
      };
    }
    if (key === 'marketValue') {
      return {
        title: 'Market value', value: F.money(k.marketValue || 0),
        what: 'Each open position\u2019s quantity multiplied by the most recent price recorded for it. Cash is not included.',
        eq: evEq(rs.slice(0, 3).map(function (r, i) {
          return [r.sym + '  ' + (Math.round(r.qty * TEN_THOUSAND) / TEN_THOUSAND) + ' \u00d7 ' + F.money(r.price), F.money(r.mktVal), i ? '+' : ''];
        }).concat(rs.length > 3 ? [[(rs.length - 3) + ' more positions', F.money(rs.slice(3).reduce(function (a, r) { return a + r.mktVal; }, 0)), '+']] : []),
                 ['Market value', F.money(t.mktVal)]) +
             evRounding(rs.slice(0, 3).map(function (r) { return r.mktVal; })
               .concat(rs.length > 3 ? [rs.slice(3).reduce(function (a, r) { return a + r.mktVal; }, 0)] : []), t.mktVal),
        table: evTable(['Symbol', 'Qty', 'Price', 'Value'], rs.map(function (r) {
          return ['<b>' + esc(r.sym) + '</b>', (Math.round(r.qty * TEN_THOUSAND) / TEN_THOUSAND), F.money(r.price), F.money(r.mktVal)];
        })),
        route: { label: 'See holdings', run: evGoHoldings }
      };
    }
    if (key === 'unrealized') {
      return {
        title: 'Unrealized P&L', value: signed(k.unrealized),
        what: 'What your open positions are worth now, less what they cost you. Nothing here has been sold, so none of it is booked.',
        eq: evEq([['Market value', F.money(k.marketValue || 0), ''],
                  ['Cost basis', F.money(k.costBasis || 0), '\u2212']],
                 ['Unrealized P&L', signed(k.unrealized), (k.unrealized || 0) >= 0 ? 'up' : 'down']) +
                 evRounding([k.marketValue || 0, -(k.costBasis || 0)], k.unrealized || 0),
        table: evTable(['Symbol', 'Cost', 'Value', 'Gain'], rs.slice().sort(function (a, b) { return b.gl - a.gl; }).map(function (r) {
          return ['<b>' + esc(r.sym) + '</b>', F.money(r.cost), F.money(r.mktVal),
                  '<span class="' + (r.gl >= 0 ? 'up' : 'down') + '">' + signed(r.gl) + '</span>'];
        })),
        route: { label: 'See holdings', run: evGoHoldings }
      };
    }
    if (key === 'cash') {
      var accts = k.cashAccounts || [];
      return {
        title: 'Buying power', value: F.money(k.cash || 0),
        what: 'The balance you last recorded. PortfolioDB never contacts a broker, so this is exactly as current as your last entry and no more.',
        eq: accts.length > 1 ? evEq(accts.map(function (a, i) {
          return [a.account, F.money(a.cash), i ? '+' : ''];
        }), ['Buying power', F.money(k.cash || 0)]) +
          evRounding(accts.map(function (a) { return a.cash; }), k.cash || 0) : '',
        table: evTable(['Account', 'Balance', 'Recorded'], accts.map(function (a) {
          return ['<b>' + esc(a.account) + '</b>', F.money(a.cash), a.asOf ? esc(String(a.asOf).slice(0, 10)) : '\u2014'];
        })),
        note: 'Update it from Manage \u2192 Cash, or with make set-cash.'
      };
    }
    if (key === 'realized') {
      return {
        title: 'Realized P&L', value: signed(k.realized),
        what: 'Profit and loss already booked by selling, matched first-in-first-out against the lots you bought. Fees paid on a sale reduce it.',
        eq: '',
        table: '',
        note: 'Every match is recomputed from the lots on each read \u2014 there is no stored total to drift.',
        route: { label: 'See every lot', run: function () { evGoView('history'); } }
      };
    }
    if (key === 'totalReturn') {
      return {
        title: 'Total return', value: F.pct(k.totalReturnPct || 0),
        what: 'Everything you have made, booked or not, against what you put in.',
        eq: evEq([['Realized P&L', signed(k.realized), ''],
                  ['Unrealized P&L', signed(k.unrealized), '+'],
                  ['Cost basis', F.money(k.costBasis || 0), '\u00f7']],
                 ['Total return', F.pct(k.totalReturnPct || 0), (k.totalReturnPct || 0) >= 0 ? 'up' : 'down']),
        table: '',
        note: 'Dividends are excluded here. With them it is ' + F.pct(k.totalReturnWithIncomePct || 0) + '.'
      };
    }
    if (key === 'costBasis') {
      return {
        title: 'Cost basis', value: F.money(k.costBasis || 0),
        what: 'What your open lots cost, including the fees you paid to buy them. Sold lots have left this number.',
        eq: '',
        table: evTable(['Symbol', 'Qty', 'Avg cost', 'Cost'], rs.slice().sort(function (a, b) { return b.cost - a.cost; }).map(function (r) {
          return ['<b>' + esc(r.sym) + '</b>', (Math.round(r.qty * TEN_THOUSAND) / TEN_THOUSAND), F.money(r.avgCost), F.money(r.cost)];
        })),
        note: 'Fees paid so far: ' + F.money(k.totalFees || 0) + ', which is ' + F.pct(k.feeDragPct || 0) + ' of cost.',
        route: { label: 'See holdings', run: evGoHoldings }
      };
    }
    if (key === 'deltaLast') {
      var pv = (DATA.pv && (DATA.pv['1D'] || DATA.pv['1W'])) || [];
      var prev = pv.length > 1 ? pv[pv.length - 2][1] : null;
      var last = pv.length ? pv[pv.length - 1][1] : null;
      return {
        title: 'Change since last snapshot', value: signed(k.deltaLast),
        what: 'How the portfolio moved between the two most recent price collections. It is a step, not a day: if the collector missed a run, this spans the gap.',
        eq: (prev != null && last != null)
          ? evEq([['These holdings, latest prices', F.money(last), ''],
                  ['These holdings, previous prices', F.money(prev), '−']],
                 ['Change', signed(k.deltaLast), (k.deltaLast || 0) >= 0 ? 'up' : 'down']) +
                 evRounding([last, -prev], k.deltaLast || 0)
          : '',
        table: '',
        note: 'Counted over the ' + (k.deltaLastSyms || 0) + ' holding' + (k.deltaLastSyms === 1 ? '' : 's') +
          ' priced in both snapshots. Snapshot times and gaps are on the Data Health page.'
      };
    }
    if (key === 'dividends') {
      return {
        title: 'Dividends', value: F.money(k.dividends || 0),
        what: 'Income recorded against your holdings. Nothing is estimated or projected here \u2014 these are payments entered into the ledger.',
        eq: evEq([['All time', F.money(k.dividends || 0), ''],
                  ['Last twelve months', F.money(k.dividendsTtm || 0), ''],
                  ['Cost basis', F.money(k.costBasis || 0), '\u00f7']],
                 ['Yield on cost (TTM)', F.pct(k.yieldOnCostPct || 0)]),
        table: ''
      };
    }
    return null;
  }
  function openEvidence(key) {
    var ev = kpiEvidence(key);
    if (!ev || !drawerEl) return;
    drawerEl.setAttribute('aria-label', ev.title + ' \u2014 how this is calculated');
    // Audited 2026-09-02. Everything below is either a number formatted by this
    // file, a string literal from kpiEvidence, or a value passed through esc():
    // titles and prose are literals, symbols and account names are escaped, and
    // no value here originates in the URL. The three assignments are marked
    // individually so a later edit to any one of them is reviewed on its own.
    // nosemgrep
    drawerHd.innerHTML = '<span class="nm"><b>' + esc(ev.title) + '</b>' +
      '<span class="ev-hd-sub">how this is calculated</span></span>' +
      '<span class="drawer__px num">' + ev.value + '</span>' +
      '<button class="iconbtn drawer__close" data-drawer-close aria-label="Close">' + X_ICON + '</button>';
    var html = '<p class="ev-what">' + esc(ev.what) + '</p>' + (ev.eq || '');
    if (ev.note) html += '<p class="ev-note">' + esc(ev.note) + '</p>';
    if (ev.table) html += '<div class="drawer__sect">Where it comes from</div>' + ev.table;
    // nosemgrep
    drawerBd.innerHTML = html;
    // nosemgrep
    drawerFoot.innerHTML = '<button class="btn btn--ghost" data-drawer-close>Close</button>' +
      (ev.route ? '<button class="btn btn--primary" data-ev-route>' + esc(ev.route.label) + ' \u2192</button>' : '');
    var rb = drawerFoot.querySelector('[data-ev-route]');
    if (rb && ev.route) rb.addEventListener('click', ev.route.run);
    drawerPrevFocus = document.activeElement;
    drawerEl.classList.add('open'); drawerScrimEl.classList.add('show');
    drawerBd.scrollTop = 0;
    drawerEl.focus();
  }
  function closeDrawer() {
    if (!drawerEl || !drawerEl.classList.contains('open')) return;
    drawerEl.classList.remove('open'); drawerScrimEl.classList.remove('show');
    if (drawerPrevFocus && drawerPrevFocus.focus) { try { drawerPrevFocus.focus(); } catch (e) {} }
  }
  if (drawerScrimEl) drawerScrimEl.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && drawerEl && drawerEl.classList.contains('open') &&
        !(cmdk && cmdk.classList.contains('open'))) closeDrawer();
  });
  // Both overlays declare aria-modal="true". Without a trap, Tab walks out of a
  // dialog that has just told assistive tech nothing else is reachable.
  var FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
  function trapTab(container, e) {
    var f = $all(FOCUSABLE, container).filter(function (el) {
      return el.offsetWidth || el.offsetHeight || el.getClientRects().length;
    });
    if (!f.length) { e.preventDefault(); return; }
    var first = f[0], last = f[f.length - 1], active = document.activeElement;
    if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
  }
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Tab') return;
    if (cmdk && cmdk.classList.contains('open')) trapTab(cmdk, e);
    else if (drawerEl && drawerEl.classList.contains('open')) trapTab(drawerEl, e);
  });
  // delegate: any element carrying data-sym opens the dossier (links/controls excluded)
  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    // Controls inside a trigger keep their own behaviour — except .sym-open,
    // which exists precisely to be a table row's keyboard-operable equivalent.
    var ctl = t.closest('a,button,input,select,label');
    if (ctl && !ctl.classList.contains('sym-open')) return;
    if (t.closest('[data-drawer]')) return;
    var kpiEl = t.closest('[data-kpi]');
    if (kpiEl) { openEvidence(kpiEl.getAttribute('data-kpi')); return; }
    var el = t.closest('[data-sym]');
    if (!el) return;
    var sym = el.getAttribute('data-sym');
    if (get(sym)) openDrawer(sym);
  });
  // Enter/Space on a focused div trigger. The .sym-open buttons need nothing
  // here — a button activates natively and arrives at the click delegate above.
  // Space would otherwise scroll the page, so it is prevented rather than left.
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var t = e.target;
    if (!t || !t.getAttribute || t.getAttribute('role') !== 'button') return;
    var kpiKey = t.getAttribute('data-kpi');
    if (kpiKey) { e.preventDefault(); openEvidence(kpiKey); return; }
    var sym = t.getAttribute('data-sym');
    if (!sym || !get(sym)) return;
    e.preventDefault();
    openDrawer(sym);
  });

  // ---- phone disclosure ---------------------------------------------------
  // On a phone the Portfolio view is reordered by CSS so the check-in comes
  // first; the cards that are reference rather than answer keep their heading
  // and open on request. The control is built here rather than written into
  // the markup so that a desktop visit never carries a button it cannot use,
  // and so the state resets to closed on every load - a remembered fold is a
  // page that looks different every time you open it for no reason you can see.
  var CHEVRON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';
  var foldSeq = 0;
  function foldTitle(card) {
    var h = card.querySelector('.card__hd h2');
    return h ? h.textContent.trim() : 'section';
  }
  function setFold(card, open) {
    card.classList.toggle('is-open', open);
    var btn = card.querySelector(':scope > .card__hd > .fold-btn');
    if (!btn) return;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    btn.setAttribute('aria-label', (open ? 'Hide ' : 'Show ') + foldTitle(card));
  }
  function wireFolds() {
    var on = false;
    try { on = window.matchMedia && matchMedia('(max-width:640px)').matches; } catch (e) {}
    $all('[data-fold]').forEach(function (card) {
      var hd = card.querySelector(':scope > .card__hd');
      if (!hd) return;
      // The body is whatever is not the heading: one card puts its table
      // straight into the card with no .card__bd wrapper, and aria-controls
      // has to name what actually disappears.
      var bd = null;
      for (var c = 0; c < card.children.length; c++) {
        if (!card.children[c].classList.contains('card__hd')) { bd = card.children[c]; break; }
      }
      if (!bd) return;
      var btn = card.querySelector(':scope > .card__hd > .fold-btn');
      if (!on) {
        // Desktop shows everything, so the card must not be left folded by a
        // narrow visit earlier in the same session.
        card.classList.remove('foldable', 'is-open');
        if (btn) btn.remove();
        hd.removeAttribute('role');
        return;
      }
      if (!bd.id) bd.id = 'fold-bd-' + (++foldSeq);
      if (!btn) {
        btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'fold-btn';
        btn.setAttribute('aria-controls', bd.id);
        // CHEVRON is a module constant with nothing interpolated into it; there is
        // no value here to control. Marked because the sink shape is the same.
        // nosemgrep
        btn.innerHTML = CHEVRON;
        hd.appendChild(btn);
        // The whole heading is the target, which is a 44px row rather than a
        // 34px icon - but a chip or a select inside it keeps its own job.
        hd.addEventListener('click', function (e) {
          if (e.target.closest('a,button,input,select,label') && !e.target.closest('.fold-btn')) return;
          setFold(card, !card.classList.contains('is-open'));
        });
      }
      card.classList.add('foldable');
      setFold(card, card.classList.contains('is-open'));
    });
  }
  wireFolds();
  try {
    var foldMq = window.matchMedia && matchMedia('(max-width:640px)');
    if (foldMq && foldMq.addEventListener) foldMq.addEventListener('change', wireFolds);
    else if (foldMq && foldMq.addListener) foldMq.addListener(wireFolds);
  } catch (e) {}
  // ---- fundamentals ----
  function fpct(x, d) { if (x == null) return '—'; return (Number(x) * 100).toFixed(d == null ? 1 : d) + '%'; }
  function fratio(x, d) { if (x == null) return '—'; return Number(x).toFixed(d == null ? 2 : d); }
  function statBox(label, val) {
    return '<div><div style="font-size:var(--fs-micro);color:var(--muted);text-transform:uppercase;letter-spacing:.04em">' +
      esc(label) + '</div><div style="font-size:var(--fs-base);font-weight:600;margin-top:2px">' + esc(val || '—') + '</div></div>';
  }
  // These three charts plot magnitude, not direction, and each has its own scale,
  // so hue carried no information - and three arbitrary colours (indigo, green,
  // and a raw #9b59b6 from no palette at all) invited a comparison across charts
  // that the numbers do not support. Positive quarters take a neutral ink; only a
  // negative quarter earns a colour, because only it means something. That also
  // retires sign-as-opacity, which drew a loss-making quarter as the faintest bar
  // on the chart - the exact inverse of how much it matters.
  function barChart(items, key, title) {
    var vals = items.map(function (t) { return t[key] == null ? null : Number(t[key]); });
    if (!vals.some(function (v) { return v != null; }))
      return '<div style="flex:1"><div style="font-size:var(--fs-meta);color:var(--muted);margin-bottom:6px">' + title + '</div><div class="empty" style="padding:14px">no data</div></div>';
    var nums = vals.map(function (v) { return v == null ? 0 : v; });
    var max = Math.max.apply(null, nums.map(Math.abs)) || 1;
    var W = items.length * 26, H = 80;
    var bars = nums.map(function (v, i) {
      var h = Math.abs(v) / max * 56, x = i * 26 + 4, y = v >= 0 ? (62 - h) : 62;
      var lbl = periodLabel((items[i] || {}).period) + ' · ' +
        (vals[i] == null ? 'no data' : (vals[i] < 0 ? '−' : '') + F.compact(Math.abs(vals[i])));
      return '<rect x="' + x + '" y="' + y.toFixed(1) + '" width="16" height="' + h.toFixed(1) + '" rx="2" fill="' +
        (v < 0 ? 'var(--down)' : 'var(--muted)') + '"><title>' + esc(lbl) + '</title></rect>';
    }).join('');
    // Six unlabelled bars state a shape and withhold both of the things needed
    // to read it: how big the biggest one is, and when any of them happened.
    // The peak names the top of the scale, the ends name the span, and every
    // bar carries its own period and value on hover.
    var first = periodLabel((items[0] || {}).period);
    var lastP = periodLabel((items[items.length - 1] || {}).period);
    var span = first === lastP ? first : first + ' – ' + lastP;
    return '<div style="flex:1;min-width:160px">' +
      '<div class="bc-hd"><span>' + title + '</span><span class="bc-peak num">peak ' + F.compact(max) + '</span></div>' +
      '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:80px" preserveAspectRatio="none" role="img" aria-label="' +
      esc(title + ', ' + span + ', peak ' + F.compact(max)) + '">' +
      '<line x1="0" x2="' + W + '" y1="62" y2="62" stroke="var(--border)"/>' + bars + '</svg>' +
      '<div class="bc-x"><span>' + esc(first) + '</span><span>' + esc(lastP) + '</span></div></div>';
  }
  function renderFundamentals() {
    var sel = $('#fd-symbol'), host = $('#fd-body'); if (!sel || !host) return;
    if (!sel.options.length) {
      var uni = DATA.fdUniverse || [];
      sel.innerHTML = uni.map(function (s) { return '<option value="' + s + '">' + s + '</option>'; }).join('');
      if (DATA.fdDefault) sel.value = DATA.fdDefault;
      sel.addEventListener('change', renderFundamentals);
    }
    var sym = sel.value, fd = (DATA.fundamentals || {})[sym];
    var asof = $('#fd-asof'); if (asof) asof.textContent = DATA.asOf ? ('As of ' + DATA.asOf) : '';
    if (!fd) { host.innerHTML = '<div class="empty">No fundamentals on file for ' + esc(sym) + '.</div>'; return; }

    var html = '<section class="card"><div class="card__bd" style="display:flex;gap:24px;flex-wrap:wrap;align-items:center">' +
      '<div><div style="font-size:var(--fs-xl);font-weight:700">' + esc(fd.name) + '</div>' +
      '<div style="font-size:var(--fs-meta);color:var(--muted)">' + esc(sym) + ' · ' + esc(fd.exchange) + '</div></div>' +
      '<div style="flex:1"></div>' + statBox('Sector', fd.sector) + statBox('Industry', fd.industry) +
      statBox('Market cap', fd.metrics && fd.metrics.market_cap != null ? F.compact(fd.metrics.market_cap) : '—') +
      '</div></section>';

    if (fd.isEtf) {
      html += '<section class="card" style="margin-top:20px"><div class="card__bd"><div class="empty">' + esc(sym) +
        ' is an ETF / fund — Financial Datasets carries news only (see Alerts &amp; News), no fundamentals.</div></div></section>';
      host.innerHTML = html; return;
    }

    var m = fd.metrics;
    if (m) {
      // grouped by question being answered: price vs. quality vs. trajectory
      var groups = [
        ['Valuation', [['P/E', fratio(m.pe_ratio)], ['P/S', fratio(m.ps_ratio)],
          ['EV/EBITDA', fratio(m.ev_ebitda)], ['FCF yield', fpct(m.free_cash_flow_yield, 2)]]],
        ['Profitability', [['ROE', fpct(m.return_on_equity)], ['Gross', fpct(m.gross_margin)],
          ['Op margin', fpct(m.operating_margin)], ['Net margin', fpct(m.net_margin)]]],
        ['Growth & balance', [['Rev growth', fpct(m.revenue_growth)], ['EPS growth', fpct(m.earnings_growth)],
          ['D/E', fratio(m.debt_to_equity)], ['Current', fratio(m.current_ratio)]]]
      ];
      html += '<section class="card" style="margin-top:20px"><div class="card__hd"><h2>Valuation &amp; quality</h2></div>' +
        '<div class="card__bd fd-groups">' +
        groups.map(function (g) {
          return '<div class="fd-group"><div class="fd-group__t">' + g[0] + '</div><div class="fd-group__grid">' +
            g[1].map(function (c) { return '<div><div style="font-size:var(--fs-micro);color:var(--muted);text-transform:uppercase;letter-spacing:.04em">' +
              c[0] + '</div><div class="num" style="font-size:var(--fs-xl);font-weight:600;margin-top:3px">' + c[1] + '</div></div>'; }).join('') +
            '</div></div>';
        }).join('') + '</div></section>';
    }

    if (fd.trend && fd.trend.length) {
      html += '<section class="card" style="margin-top:20px"><div class="card__hd"><h2>Quarterly trend</h2><span class="sub">last ' +
        fd.trend.length + ' periods</span></div><div class="card__bd" style="display:flex;gap:20px;flex-wrap:wrap">' +
        barChart(fd.trend, 'revenue', 'Revenue') +
        barChart(fd.trend, 'net_income', 'Net income') +
        barChart(fd.trend, 'fcf', 'Free cash flow') + '</div></section>';
    }

    if (fd.earnings && fd.earnings.length) {
      html += '<section class="card" style="margin-top:20px"><div class="card__hd"><h2>Recent earnings</h2></div>' +
        '<div class="tbl-wrap"><table class="tbl"><thead><tr><th style="text-align:left">Period</th><th>EPS actual</th>' +
        '<th>EPS est.</th><th>Surprise</th><th>Revenue</th></tr></thead><tbody>' +
        fd.earnings.map(function (e) {
          var sc = e.eps_surprise === 'BEAT' ? 'tag--up' : e.eps_surprise === 'MISS' ? 'tag--down' : 'tag--neu';
          return '<tr><td style="text-align:left">' + esc(e.period || '—') + '</td>' +
            '<td class="num">' + (e.eps_actual != null ? Number(e.eps_actual).toFixed(2) : '—') + '</td>' +
            '<td class="num" style="color:var(--muted)">' + (e.eps_estimate != null ? Number(e.eps_estimate).toFixed(2) : '—') + '</td>' +
            '<td>' + (e.eps_surprise ? '<span class="tag ' + sc + '">' + esc(e.eps_surprise) + '</span>' : '—') + '</td>' +
            '<td class="num">' + (e.revenue_actual != null ? F.compact(e.revenue_actual) : '—') + '</td></tr>';
        }).join('') + '</tbody></table></div></section>';
    }

    html += '<div class="row2" style="margin-top:20px;grid-template-columns:1fr 1fr">' +
      '<section class="card"><div class="card__hd"><h2>Recent filings</h2></div><div class="tbl-wrap"><table class="tbl">' +
      '<thead><tr><th style="text-align:left">Date</th><th style="text-align:left">Type</th><th>Link</th></tr></thead><tbody>' +
      (fd.filings && fd.filings.length ? fd.filings.map(function (f) {
        return '<tr><td style="text-align:left" class="num">' + esc(f.date || '—') + '</td><td style="text-align:left">' + esc(f.type || '—') +
          '</td><td>' + (f.url ? '<a href="' + esc(f.url) + '" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">open</a>' : '—') + '</td></tr>';
      }).join('') : '<tr><td colspan="3"><div class="empty">No filings.</div></td></tr>') + '</tbody></table></div></section>' +
      '<section class="card"><div class="card__hd"><h2>Insider activity</h2></div><div class="tbl-wrap"><table class="tbl">' +
      '<thead><tr><th style="text-align:left">Date</th><th style="text-align:left">Name</th><th style="text-align:left">Type</th><th>Value</th></tr></thead><tbody>' +
      (fd.insiders && fd.insiders.length ? fd.insiders.map(function (t) {
        var low = (t.type || '').toLowerCase(), col = /sale|sold/.test(low) ? 'var(--down)' : /purchase|buy/.test(low) ? 'var(--up)' : 'inherit';
        return '<tr><td style="text-align:left" class="num">' + esc(t.date || '—') + '</td><td style="text-align:left">' + esc(t.name || '—') +
          '</td><td style="text-align:left;color:' + col + '">' + esc(t.type || '—') + '</td><td class="num">' + (t.value != null ? F.compact(t.value) : '—') + '</td></tr>';
      }).join('') : '<tr><td colspan="4"><div class="empty">No insider activity.</div></td></tr>') + '</tbody></table></div></section></div>';

    html += '<section class="card" style="margin-top:20px"><div class="card__hd"><h2>Top institutional holders</h2></div>' +
      '<div class="tbl-wrap"><table class="tbl"><thead><tr><th style="text-align:left">Investor</th><th>Period</th><th>Shares</th><th>Value</th></tr></thead><tbody>' +
      (fd.holders && fd.holders.length ? fd.holders.map(function (h) {
        return '<tr><td style="text-align:left">' + esc(h.investor || '—') + '</td><td class="num" style="color:var(--muted)">' + esc(h.period || '—') +
          '</td><td class="num">' + (h.shares != null ? Number(h.shares).toLocaleString('en-US') : '—') + '</td><td class="num">' + (h.value != null ? F.compact(h.value) : '—') + '</td></tr>';
      }).join('') : '<tr><td colspan="4"><div class="empty">No 13F holdings.</div></td></tr>') + '</tbody></table></div></section>';

    host.innerHTML = html;
  }

  // ---- history: all lots + snapshot log ----
  function renderHistory() {
    var host = $('#history-body'); if (!host) return;
    var lots = DATA.histLots || [], snaps = DATA.snapLog || [];
    function lotRow(l) {
      var c = l.side === 'BUY' ? 'tag--up' : 'tag--down';
      return '<tr><td class="num" style="color:var(--faint)">' + l.id + '</td><td><b>' + esc(l.symbol) + '</b></td>' +
        '<td style="text-align:left;color:var(--muted)">' + esc(l.account) + '</td>' +
        '<td><span class="tag ' + c + '">' + l.side + '</span></td>' +
        '<td class="num">' + esc(l.date) + '</td><td class="num">' + l.qty + '</td>' +
        '<td class="num">' + F.money(l.price) + '</td>' +
        '<td class="num" style="color:var(--muted)">' + F.money(l.fees) + '</td>' +
        '<td style="text-align:left;color:var(--muted);max-width:240px;overflow:hidden;text-overflow:ellipsis">' + esc(l.notes) + '</td></tr>';
    }
    var html =
      '<section class="card"><div class="card__hd"><h2>All lots</h2><span class="sub">' +
        (lotTotalAll() > lots.length ? 'showing ' + lots.length + ' of ' + lotTotalAll()
                                     : 'all ' + lots.length) + '</span>' +
        '<button class="btn btn--ghost" id="lots-csv" style="margin-left:auto">Export CSV</button></div>' +
        '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>ID</th><th>Symbol</th><th style="text-align:left">Account</th>' +
        '<th>Side</th><th>Date</th><th>Qty</th><th>Price</th><th>Fees</th><th style="text-align:left">Notes</th></tr></thead><tbody>' +
        (lots.length ? lots.map(lotRow).join('') : '<tr><td colspan="9"><div class="empty">No lots recorded.</div></td></tr>') +
        '</tbody></table></div></section>' +
      '<section class="card" style="margin-top:20px"><div class="card__hd"><h2>Snapshot log</h2><span class="sub">last 30 runs</span></div>' +
        '<div class="tbl-wrap"><table class="tbl"><thead><tr><th style="text-align:left">Timestamp</th><th>Symbols</th>' +
        '<th style="text-align:left">Source</th></tr></thead><tbody>' +
        (snaps.length ? snaps.map(function (s) {
          return '<tr><td class="num" style="text-align:left">' + esc(s.ts) + '</td><td class="num">' + s.symbols +
            '</td><td style="text-align:left;color:var(--muted)">' + esc(s.source) + '</td></tr>';
        }).join('') : '<tr><td colspan="3"><div class="empty">No snapshots.</div></td></tr>') +
        '</tbody></table></div></section>';
    host.innerHTML = html;
    var btn = $('#lots-csv');
    if (btn) btn.addEventListener('click', function () {
      var hdr = ['id', 'symbol', 'account', 'side', 'date', 'qty', 'price', 'fees', 'notes'];
      var body = lots.map(function (l) { return hdr.map(function (k) {
        return '"' + String(l[k] == null ? '' : l[k]).replace(/"/g, '""') + '"'; }).join(','); });
      var csv = [hdr.join(',')].concat(body).join('\n');
      try {
        var blob = new Blob([csv], { type: 'text/csv' }), url = URL.createObjectURL(blob);
        var a = document.createElement('a'); a.href = url; a.download = 'lots.csv'; a.click(); URL.revokeObjectURL(url);
        toast('Exported ' + lots.length + ' lots');
      } catch (e) { toast('Export blocked by browser sandbox'); }
    });
  }

  // ---- positions CSV export (best-effort; sandbox may block) ----
  var posCsv = $('#pos-csv');
  if (posCsv) posCsv.addEventListener('click', function () {
    var rs = holdingRows();
    var hdr = ['symbol', 'name', 'sector', 'qty', 'avgCost', 'price', 'mktVal', 'cost', 'gl', 'glPct'];
    var body = rs.map(function (r) { return hdr.map(function (k) {
      return '"' + String(r[k] == null ? '' : r[k]).replace(/"/g, '""') + '"'; }).join(','); });
    var csv = [hdr.join(',')].concat(body).join('\n');
    try {
      var blob = new Blob([csv], { type: 'text/csv' }), url = URL.createObjectURL(blob);
      var a = document.createElement('a'); a.href = url; a.download = 'positions.csv'; a.click(); URL.revokeObjectURL(url);
      toast('Exported ' + rs.length + ' positions');
    } catch (e) { toast('Export blocked — use Manage › Exports'); }
  });

  // ---- viewport-fit iframe (replaces the old hardcoded height=1180) ----
  // Size our own iframe to the parent viewport so the 100vh app shell
  // (sticky rail, fixed mobile tabbar) owns exactly one screen, with content
  // scrolling inside — no cropping on tall screens, no double scrollbar.
  function fitViewport() {
    try {
      var fe = window.frameElement;
      if (!fe) return;
      var h = window.parent.innerHeight + 'px';
      fe.style.height = h;
      fe.style.width = '100%';
      // Streamlit reserves the *server-side* height (st.iframe(height=…)) on the
      // element container wrapping the iframe. Shrinking only the iframe leaves
      // that reservation behind, so section[data-testid="stMain"] scrolls the
      // difference — which is the second scrollbar: an outer one with a short
      // range sitting next to the real one inside the frame.
      //
      // It has to be flex-basis, not height. The container is a flex item in a
      // column with `flex: 0 0 <server height>`, so the basis wins and an inline
      // height is silently ignored — setting height alone looks like a fix and
      // changes nothing. Both are set because older Streamlit sized by height.
      var box = fe.closest && fe.closest('[data-testid="stElementContainer"]');
      if (box) { box.style.flexBasis = h; box.style.height = h; }
      var pd = window.parent.document;
      pd.documentElement.style.overflow = 'hidden';
      pd.body.style.overflow = 'hidden';
    } catch (e) {}  // sandbox blocked parent access → server-side height stands
  }
  fitViewport();
  try { window.parent.addEventListener('resize', fitViewport); } catch (e) {}

  // ---- init ----
  brandParentPage();
  renderKPIs(); renderMarkets(); renderReturns(); initPVRange(); renderPV(); renderAlloc(); renderTable();
  renderAttribution(); renderWaterfall(); renderRisk();
  initPriceChart(); renderLatestPrices();
  renderMovers(); renderChips(); renderHeat(); renderBreadth();
  renderStats();
  renderFeed();
  // Restore the pane after a soft refresh (consumed once); otherwise open the
  // view from the URL. Keeps the user on Movers/Fundamentals/etc. across refresh.
  var initial = DATA.initialView;
  try {
    var rv = window.parent.sessionStorage.getItem('pdb_refresh_view');
    if (rv) { window.parent.sessionStorage.removeItem('pdb_refresh_view'); initial = rv; }
  } catch (e) {}
  if (initial && initial !== 'portfolio' && TITLES[initial]) switchView(initial, true);
  // Browser back/forward moves between panes (history entries pushed by
  // switchView). Falls back to the URL param when there's no state object.
  try {
    window.parent.addEventListener('popstate', function (ev) {
      var v = (ev.state && ev.state.pdbView) || null;
      if (!v) {
        try { v = new URLSearchParams(window.parent.location.search).get('view'); } catch (e) {}
      }
      v = v || 'portfolio';
      if (TITLES[v]) switchView(v, true);
    });
  } catch (e) {}
  // close the refresh loop: confirm completion + flash any prices that moved
  try {
    if (window.parent.sessionStorage.getItem('pdb_refreshed')) {
      window.parent.sessionStorage.removeItem('pdb_refreshed');
      toast('Updated just now');
    }
  } catch (e) {}
  flashChangedPrices();
})();
