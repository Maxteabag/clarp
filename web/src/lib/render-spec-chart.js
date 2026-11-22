// The chart component of clarp.form v2 (issue 673): bar, line or pie, as SVG, display only.
// Values are integers drawn divided by 10^decimals (formatScaled); numeric x stays as given.
//
// Colours are the categorical slots --rs-c1..--rs-c8 (styles.css), assigned in series order
// (pie: point order), never cycled: a pie's points 9 to 12 take slots 1 to 4 again with a
// hatch, so no two slices look alike. Marks follow the house rules for charts: 2px lines,
// 4px rounded bar ends on the data side, a 2px surface gap between fills, faint grid, text in
// text colours, a legend whenever there are two or more series (a pie: always), and a native
// tooltip per mark. The plot is drawn at the card's real width, again whenever it changes.

import { formatScaled } from '@core/render-spec.js';

const SVG = 'http://www.w3.org/2000/svg';
const HEIGHT = 200;
const SLOTS = 8;

function svg(tag, attrs = {}, parent = null) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  if (parent) parent.append(node);
  return node;
}
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}
const slot = i => `var(--rs-c${(i % SLOTS) + 1})`;
const hatched = i => i >= SLOTS;

function tip(node, words) { svg('title', {}, node).textContent = words; }

/** Round tick steps (1, 2, 5 × 10^n) covering [lo, hi], zero included. */
function ticks(lo, hi, count = 4) {
  lo = Math.min(lo, 0); hi = Math.max(hi, 0);
  if (lo === hi) hi = lo + 1;
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map(m => m * mag).find(s => s >= raw);
  const out = [];
  for (let v = Math.floor(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.round(v / step) * step);
  if (out[out.length - 1] < hi) out.push(out[out.length - 1] + step);
  return out;
}

function hatchDefs(root, uid, count) {
  if (count <= SLOTS) return;
  const defs = svg('defs', {}, root);
  for (let i = SLOTS; i < count; i++) {
    const p = svg('pattern', { id: `${uid}-h${i}`, width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' }, defs);
    svg('rect', { width: 6, height: 6, style: `fill:${slot(i)}` }, p);
    svg('line', { x1: 0, y1: 0, x2: 0, y2: 6, style: 'stroke:var(--rs-surface);stroke-width:2' }, p);
  }
}
const fillFor = (uid, i) => (hatched(i) ? `url(#${uid}-h${i})` : slot(i));

let uidSeq = 0;

/** A chart element; `props` as the reader gives them. */
export function drawChart(props) {
  const uid = `rs-chart-${++uidSeq}`;
  const figure = el('figure', 'rs-chart');
  figure.dataset.kind = props.kind;
  if (props.title) figure.append(el('figcaption', 'rs-chart-title', props.title));
  const value = y => formatScaled(y, props.decimals) + (props.unit ? ` ${props.unit}` : '');

  // Legend: always for a pie (its points), else for two or more series.
  const entries = props.kind === 'pie'
    ? props.series[0].points.map((p, i) => ({ name: String(p.x), i, extra: value(p.y) }))
    : props.series.length > 1 ? props.series.map((s, i) => ({ name: s.name, i })) : [];
  if (entries.length) {
    const legend = el('ul', 'rs-legend');
    for (const entry of entries) {
      const item = el('li', 'rs-legend-item');
      const swatch = el('span', 'rs-swatch' + (hatched(entry.i) ? ' rs-hatched' : ''));
      swatch.style.setProperty('--rs-swatch', slot(entry.i));
      item.append(swatch, el('span', 'rs-legend-name', entry.name));
      if (entry.extra) item.append(el('span', 'rs-legend-value', entry.extra));
      legend.append(item);
    }
    figure.append(legend);
  }

  const plot = el('div', 'rs-plot');
  plot.setAttribute('role', 'img');
  plot.setAttribute('aria-label', [props.title, ...props.series.map(s => `${s.name}: ` + s.points.map(p => `${p.x} ${value(p.y)}`).join(', '))].filter(Boolean).join('. '));
  figure.append(plot);

  let width = 0;
  const render = () => {
    const w = Math.round(plot.clientWidth);
    if (!w || w === width) return;
    width = w;
    plot.replaceChildren(props.kind === 'pie' ? pie(props, uid, w, value) : cartesian(props, uid, w, value));
  };
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(render).observe(plot);
  requestAnimationFrame(render);
  return figure;
}

function cartesian(props, uid, w, value) {
  const root = svg('svg', { width: w, height: HEIGHT, viewBox: `0 0 ${w} ${HEIGHT}`, class: 'rs-chart-svg' });
  const all = props.series.flatMap(s => s.points.map(p => p.y));
  const yt = ticks(Math.min(...all), Math.max(...all));
  const yLo = yt[0], yHi = yt[yt.length - 1];
  const labelW = Math.max(...yt.map(t => value(t).length)) * 6.5 + 8;
  const left = Math.ceil(labelW) + (props.yLabel ? 16 : 0), right = 10, top = 8;
  const bottom = 22 + (props.xLabel ? 16 : 0);
  const pw = Math.max(40, w - left - right), ph = HEIGHT - top - bottom;
  const Y = v => top + ph - ((v - yLo) / (yHi - yLo)) * ph;

  const grid = svg('g', { class: 'rs-grid' }, root);
  for (const t of yt) {
    svg('line', { x1: left, x2: left + pw, y1: Y(t), y2: Y(t), class: t === 0 ? 'rs-baseline' : 'rs-gridline' }, grid);
    svg('text', { x: left - 6, y: Y(t) + 4, 'text-anchor': 'end', class: 'rs-tick' }, grid).textContent = value(t);
  }
  if (props.yLabel) {
    svg('text', { x: 10, y: top + ph / 2, transform: `rotate(-90 10 ${top + ph / 2})`, 'text-anchor': 'middle', class: 'rs-axis-label' }, root).textContent = props.yLabel;
  }
  if (props.xLabel) {
    svg('text', { x: left + pw / 2, y: HEIGHT - 4, 'text-anchor': 'middle', class: 'rs-axis-label' }, root).textContent = props.xLabel;
  }

  // x: categories in the given order (first appearance across series), or numbers to scale (line).
  const toScale = props.kind === 'line' && props.numericX;
  let X, xTicks;
  if (toScale) {
    const xs = props.series.flatMap(s => s.points.map(p => p.x));
    const lo = Math.min(...xs), hi = Math.max(...xs);
    X = x => left + (hi === lo ? pw / 2 : ((x - lo) / (hi - lo)) * pw);
    const room = Math.max(2, Math.floor(pw / 60));
    const step = Math.max(1, Math.ceil((hi - lo) / room));
    xTicks = [];
    for (let v = lo; v <= hi; v += step) xTicks.push({ x: X(v), label: String(v) });
  } else {
    const cats = [];
    for (const s of props.series) for (const p of s.points) if (!cats.includes(p.x)) cats.push(p.x);
    const band = pw / cats.length;
    const index = new Map(cats.map((c, i) => [c, i]));
    X = x => left + band * (index.get(x) + 0.5);
    X.band = band;
    const every = Math.max(1, Math.ceil(cats.length / Math.max(1, Math.floor(pw / 56))));
    xTicks = cats.map((c, i) => ({ x: X(c), label: String(c), shown: i % every === 0 })).filter(t => t.shown);
  }
  const xg = svg('g', { class: 'rs-grid' }, root);
  for (const t of xTicks) {
    const label = t.label.length > 12 ? t.label.slice(0, 11) + '…' : t.label;
    svg('text', { x: t.x, y: top + ph + 15, 'text-anchor': 'middle', class: 'rs-tick' }, xg).textContent = label;
  }

  const marks = svg('g', { class: 'rs-marks' }, root);
  if (props.kind === 'bar') {
    const n = props.series.length;
    const groupW = Math.min(X.band * 0.8, 28 * n + 2 * (n - 1));
    const barW = Math.max(1, (groupW - 2 * (n - 1)) / n);
    const base = Y(0);
    props.series.forEach((s, si) => {
      for (const p of s.points) {
        const x = X(p.x) - groupW / 2 + si * (barW + 2);
        const y = Y(p.y);
        const h = Math.abs(base - y);
        const r = Math.min(4, barW / 2, h);
        // Rounded on the data end only, square on the baseline.
        const up = p.y >= 0;
        const d = up
          ? `M${x},${base} V${y + r} Q${x},${y} ${x + r},${y} H${x + barW - r} Q${x + barW},${y} ${x + barW},${y + r} V${base} Z`
          : `M${x},${base} V${y - r} Q${x},${y} ${x + r},${y} H${x + barW - r} Q${x + barW},${y} ${x + barW},${y - r} V${base} Z`;
        const bar = svg('path', { d, class: 'rs-bar', style: `fill:${slot(si)}` }, marks);
        tip(bar, `${n > 1 ? s.name + ' · ' : ''}${p.x}: ${value(p.y)}`);
      }
    });
  } else {
    props.series.forEach((s, si) => {
      const pts = s.points.map(p => [X(p.x), Y(p.y)]);
      svg('polyline', { points: pts.map(q => q.join(',')).join(' '), class: 'rs-line', style: `stroke:${slot(si)}` }, marks);
      if (s.points.length <= 40) {
        s.points.forEach((p, i) => {
          const dot = svg('circle', { cx: pts[i][0], cy: pts[i][1], r: 4, class: 'rs-dot', style: `fill:${slot(si)}` }, marks);
          tip(dot, `${props.series.length > 1 ? s.name + ' · ' : ''}${p.x}: ${value(p.y)}`);
        });
      }
    });
  }
  return root;
}

function pie(props, uid, w, value) {
  const points = props.series[0].points;
  const size = Math.min(HEIGHT, w);
  const root = svg('svg', { width: w, height: size, viewBox: `0 0 ${w} ${size}`, class: 'rs-chart-svg' });
  hatchDefs(root, uid, points.length);
  const total = points.reduce((n, p) => n + p.y, 0);
  const cx = w / 2, cy = size / 2, r = size / 2 - 4;
  const marks = svg('g', { class: 'rs-marks' }, root);
  if (total <= 0) {
    svg('circle', { cx, cy, r, class: 'rs-pie-empty' }, marks);
    return root;
  }
  let angle = -Math.PI / 2;
  points.forEach((p, i) => {
    const share = p.y / total;
    if (share <= 0) return;
    const words = `${p.x}: ${value(p.y)} (${Math.round(share * 100)}%)`;
    if (share >= 0.9999) {
      tip(svg('circle', { cx, cy, r, class: 'rs-slice', style: `fill:${fillFor(uid, i)}` }, marks), words);
      return;
    }
    const end = angle + share * 2 * Math.PI;
    const [x1, y1] = [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
    const [x2, y2] = [cx + r * Math.cos(end), cy + r * Math.sin(end)];
    const d = `M${cx},${cy} L${x1},${y1} A${r},${r} 0 ${share > 0.5 ? 1 : 0} 1 ${x2},${y2} Z`;
    tip(svg('path', { d, class: 'rs-slice', style: `fill:${fillFor(uid, i)}` }, marks), words);
    angle = end;
  });
  return root;
}
