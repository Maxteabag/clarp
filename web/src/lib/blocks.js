// Code blocks and tables in a chat message.
//
// A fenced code block has a header row above the code, split from it by a
// hairline: the language's name on the left ("Code" when the fence names
// none), Copy and Expand icons on the right. Expand, or a tap or click on the
// code, opens it full screen (BlockViewer.svelte). A table wider than its message scrolls sideways
// with an edge fade, and gets an expand icon in the same kind of line; the
// full screen view copies the table's Markdown source.
//
// The markup is built here as strings because the message body is marked's
// HTML string, sanitised and inserted with {@html}. The behaviour (copy, tap,
// width) is one action, blockChrome, on the element that holds that HTML.

import { escapeAttr, escapeHTML } from '@core/html.js';
import { openBlockViewer } from './block-viewer.svelte.js';
import { t } from '../stores/i18n.svelte.js';

// User-visible and accessibility strings of this feature, read from the
// language catalog (lib/i18n/en.json) each time they are shown.
const KEYS = {
  copyCode: 'codeblock.copy',
  copied: 'codeblock.copied',
  expand: 'codeblock.expand',
  wrap: 'codeblock.wrap',
  untitledCode: 'codeblock.untitled',
  codeViewer: 'codeblock.untitled',
  copyTable: 'table.copy',
  expandTable: 'table.expand',
  tableViewer: 'table.fullscreen_title',
  close: 'fullscreen.close',
};
export const STRINGS = Object.defineProperties({}, Object.fromEntries(
  Object.entries(KEYS).map(([name, key]) => [name, { get: () => t(key), enumerable: true }])));

const svg = (body) =>
  `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ` +
  `stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

export const ICONS = {
  copy: svg('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>'),
  check: svg('<path d="M5 12.5l4.5 4.5L19 7.5"/>'),
  expand: svg('<path d="M14 4h6v6"/><path d="M10 20H4v-6"/><path d="M20 4l-6.5 6.5"/><path d="M4 20l6.5-6.5"/>'),
  wrap: svg('<path d="M4 6h16"/><path d="M4 12h13a3 3 0 0 1 0 6h-4"/><path d="M15 16l-2 2 2 2"/><path d="M4 18h5"/>'),
  close: svg('<path d="M6 6l12 12"/><path d="M18 6L6 18"/>'),
};

// Only the leading word of an info string names the language (```rust ignore).
export function fenceLanguage(info) {
  const m = String(info || '').trim().match(/^[^\s{]+/);
  return m ? m[0] : '';
}

// The name a fence tag stands for, as people write it. Language names are
// not translated; an unknown tag is shown with a capital first letter.
const LANGUAGE_NAMES = {
  py: 'Python', python: 'Python', python3: 'Python',
  js: 'JavaScript', javascript: 'JavaScript', mjs: 'JavaScript', cjs: 'JavaScript', jsx: 'JSX',
  ts: 'TypeScript', typescript: 'TypeScript', tsx: 'TSX',
  sh: 'Shell', shell: 'Shell', bash: 'Bash', zsh: 'Zsh', fish: 'Fish', console: 'Console',
  ps1: 'PowerShell', powershell: 'PowerShell', pwsh: 'PowerShell', bat: 'Batch', cmd: 'Batch',
  rs: 'Rust', rust: 'Rust', swift: 'Swift', kt: 'Kotlin', kotlin: 'Kotlin', kts: 'Kotlin',
  java: 'Java', go: 'Go', golang: 'Go', c: 'C', h: 'C', cpp: 'C++', 'c++': 'C++', cc: 'C++', hpp: 'C++',
  cs: 'C#', csharp: 'C#', fs: 'F#', fsharp: 'F#', objc: 'Objective-C', 'objective-c': 'Objective-C',
  rb: 'Ruby', ruby: 'Ruby', php: 'PHP', pl: 'Perl', perl: 'Perl', lua: 'Lua', r: 'R',
  scala: 'Scala', dart: 'Dart', ex: 'Elixir', exs: 'Elixir', elixir: 'Elixir', erl: 'Erlang',
  hs: 'Haskell', haskell: 'Haskell', clj: 'Clojure', clojure: 'Clojure', zig: 'Zig', nim: 'Nim',
  html: 'HTML', xml: 'XML', svg: 'SVG', css: 'CSS', scss: 'SCSS', sass: 'Sass', less: 'Less',
  svelte: 'Svelte', vue: 'Vue', json: 'JSON', jsonc: 'JSON', json5: 'JSON5', jsonl: 'JSON Lines',
  yaml: 'YAML', yml: 'YAML', toml: 'TOML', ini: 'INI', env: 'Env', csv: 'CSV', sql: 'SQL',
  graphql: 'GraphQL', gql: 'GraphQL', proto: 'Protocol Buffers', md: 'Markdown', markdown: 'Markdown',
  tex: 'LaTeX', latex: 'LaTeX', diff: 'Diff', patch: 'Diff', dockerfile: 'Dockerfile', docker: 'Dockerfile',
  makefile: 'Makefile', make: 'Makefile', cmake: 'CMake', nix: 'Nix', hcl: 'HCL', tf: 'Terraform',
  terraform: 'Terraform', slint: 'Slint', kql: 'KQL', dax: 'DAX', regex: 'Regex', vim: 'Vim script',
  txt: 'Text', text: 'Text', plaintext: 'Text', plain: 'Text', log: 'Log', http: 'HTTP', nginx: 'Nginx',
};

export function languageName(tag) {
  const key = String(tag || '').trim().toLowerCase();
  if (!key) return '';
  return LANGUAGE_NAMES[key] || key.charAt(0).toUpperCase() + key.slice(1);
}

const iconButton = (cls, label, icon) =>
  `<button type="button" class="blk-btn ${cls}" aria-label="${escapeAttr(label)}" title="${escapeAttr(label)}">${icon}</button>`;

export function codeBlockHtml(code, info) {
  const lang = fenceLanguage(info);
  const cls = lang ? ` class="language-${escapeAttr(lang)}"` : '';
  return `<div class="code-block">` +
    `<div class="blk-head">` +
    `<span class="blk-lang">${escapeHTML(languageName(lang) || STRINGS.untitledCode)}</span>` +
    `<span class="blk-actions">` +
    iconButton('blk-copy', STRINGS.copyCode, ICONS.copy) +
    iconButton('blk-expand', STRINGS.expand, ICONS.expand) +
    `</span>` +
    `</div>` +
    `<pre><code${cls}>${escapeHTML(String(code).replace(/\n$/, ''))}</code></pre>` +
    `</div>`;
}

// `md` is the table's Markdown source, what the full screen view copies.
export function tableBlockHtml(tableHtml, md) {
  return `<div class="table-block" data-md="${escapeAttr(String(md || '').trim())}">` +
    `<div class="blk-head"><span></span>` +
    iconButton('blk-expand', STRINGS.expandTable, ICONS.expand) +
    `</div>` +
    `<div class="table-wrap">${tableHtml}</div>` +
    `</div>`;
}

// Markdown for a table parsed without marked (render.js's fallback).
export function tableToMarkdown(header, rows, align = []) {
  const cell = (c) => String(c ?? '').replace(/\|/g, '\\|');
  const line = (cells) => `| ${cells.map(cell).join(' | ')} |`;
  const sep = header.map((_, i) => {
    const a = align[i];
    return a === 'center' ? ':---:' : a === 'right' ? '---:' : a === 'left' ? ':---' : '---';
  });
  return [line(header), `| ${sep.join(' | ')} |`, ...rows.map(line)].join('\n');
}

// marked renders code and tables through these. Code: our block with its head
// line. Tables: marked's own cells, wrapped in our block with the source kept.
export const markedBlockExtensions = [
  {
    name: 'code',
    renderer(token) {
      return codeBlockHtml(token.text, token.lang) + '\n';
    },
  },
  {
    name: 'table',
    renderer(token) {
      const p = this.parser;
      let head = '';
      token.header.forEach((c, i) => {
        head += p.renderer.tablecell(p.parseInline(c.tokens), { header: true, align: token.align[i] });
      });
      let body = '';
      token.rows.forEach(row => {
        let r = '';
        row.forEach((c, i) => {
          r += p.renderer.tablecell(p.parseInline(c.tokens), { header: false, align: token.align[i] });
        });
        body += p.renderer.tablerow(r);
      });
      return tableBlockHtml(p.renderer.table(p.renderer.tablerow(head), body), token.raw) + '\n';
    },
  },
];

// ---- tap or drag --------------------------------------------------------

export const TAP_SLOP_PX = 8;
export const LONG_PRESS_MS = 500;

// A press opens the viewer only when it was a tap: it barely moved (a sideways
// drag scrolls the code), it was short (a long press keeps the message menu),
// and it left no text selected inside the block (a drag that selected text).
export function isTap({ dx = 0, dy = 0, ms = 0, selected = false }) {
  if (selected) return false;
  if (ms >= LONG_PRESS_MS) return false;
  return Math.hypot(dx, dy) <= TAP_SLOP_PX;
}

// ---- copy ---------------------------------------------------------------

export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) { /* fall through to the old route */ }
  // Plain http on a LAN has no async clipboard.
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (_) {}
  ta.remove();
  return ok;
}

// The icon turns into a checkmark for a moment; the label says Copied.
const COPIED_MS = 1500;
export function flashCopied(btn) {
  if (!btn) return;
  const label = btn.getAttribute('aria-label');
  clearTimeout(btn._copiedTimer);
  if (!btn._restore) btn._restore = { html: btn.innerHTML, label };
  btn.innerHTML = ICONS.check;
  btn.classList.add('copied');
  btn.setAttribute('aria-label', STRINGS.copied);
  btn.title = STRINGS.copied;
  btn._copiedTimer = setTimeout(() => {
    const r = btn._restore;
    btn._restore = null;
    btn.innerHTML = r.html;
    btn.classList.remove('copied');
    btn.setAttribute('aria-label', r.label);
    btn.title = r.label;
  }, COPIED_MS);
}

// ---- the action ---------------------------------------------------------

function selectionInside(el) {
  const sel = window.getSelection && window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
  const r = sel.getRangeAt(0);
  return el.contains(r.commonAncestorContainer) || r.intersectsNode(el);
}

function openCode(block) {
  const code = block.querySelector('pre code');
  if (!code) return;
  openBlockViewer({
    kind: 'code',
    lang: block.querySelector('.blk-lang')?.textContent || '',
    tag: (code.className.match(/(?:^|\s)language-(\S+)/) || [])[1] || '',
    text: code.textContent,
    // The chat's own markup: highlighted spans when highlight.js has run.
    html: code.dataset.highlighted ? code.innerHTML : '',
  });
}

function openTable(block) {
  const table = block.querySelector('table');
  if (!table) return;
  openBlockViewer({ kind: 'table', html: table.outerHTML, text: block.dataset.md || '' });
}

const EDGE = 2;
function measureTable(block) {
  const wrap = block.querySelector('.table-wrap');
  if (!wrap) return;
  const wide = wrap.scrollWidth > wrap.clientWidth + EDGE;
  block.classList.toggle('wide', wide);
  wrap.classList.toggle('fade-l', wide && wrap.scrollLeft > EDGE);
  wrap.classList.toggle('fade-r', wide && wrap.scrollLeft + wrap.clientWidth < wrap.scrollWidth - EDGE);
}

let resizeObserver = null;
function tableObserver() {
  if (resizeObserver || typeof ResizeObserver === 'undefined') return resizeObserver;
  resizeObserver = new ResizeObserver(entries => {
    for (const e of entries) {
      const block = e.target.closest('.table-block');
      if (block) measureTable(block);
    }
  });
  return resizeObserver;
}

// Svelte action on an element holding rendered markdown. The value is only a
// change signal (the turn's html or its block count), as with lazyHighlight.
export function blockChrome(node) {
  let press = null;
  let clickTimer = 0;
  // Observed elements, so ones a re-render replaced are let go.
  const observed = new Set();

  const scan = () => {
    const ro = tableObserver();
    for (const el of observed) {
      if (!node.contains(el)) { ro?.unobserve(el); observed.delete(el); }
    }
    node.querySelectorAll('.table-block').forEach(block => {
      if (block._chrome) return;
      block._chrome = true;
      const wrap = block.querySelector('.table-wrap');
      if (!wrap) return;
      wrap.addEventListener('scroll', () => measureTable(block), { passive: true });
      if (ro) {
        for (const el of [wrap, wrap.querySelector('table')]) {
          if (el) { ro.observe(el); observed.add(el); }
        }
      }
      measureTable(block);
    });
  };

  const onPointerDown = (e) => {
    const target = e.target.closest('.code-block pre, .table-block.wide .table-wrap');
    press = target && e.isPrimary && e.button === 0
      ? { x: e.clientX, y: e.clientY, t: performance.now(), type: e.pointerType, target }
      : null;
  };

  const onClick = (e) => {
    const btn = e.target.closest('.blk-btn');
    if (btn && node.contains(btn)) {
      e.preventDefault();
      e.stopPropagation();
      if (btn.classList.contains('blk-copy')) {
        const code = btn.closest('.code-block')?.querySelector('pre code');
        if (code) copyText(code.textContent).then(ok => { if (ok) flashCopied(btn); });
      } else if (btn.classList.contains('blk-expand')) {
        const block = btn.closest('.code-block, .table-block');
        if (block?.classList.contains('code-block')) openCode(block);
        else if (block) openTable(block);
      }
      return;
    }
    // A link inside a table cell keeps working.
    if (e.target.closest('a')) return;
    const p = press;
    press = null;
    if (!p || !p.target.contains(e.target)) return;
    const block = p.target.closest('.code-block, .table-block');
    // A table opens on a tap only on a touch screen; with a mouse it has its icon.
    if (block.classList.contains('table-block') && p.type !== 'touch') return;
    const tap = isTap({
      dx: e.clientX - p.x, dy: e.clientY - p.y,
      ms: performance.now() - p.t,
      selected: selectionInside(block),
    });
    if (!tap) return;
    const open = () => (block.classList.contains('code-block') ? openCode(block) : openTable(block));
    if (p.type === 'mouse') {
      // A double click selects a word; only a lone click opens.
      clearTimeout(clickTimer);
      if (e.detail > 1) return;
      clickTimer = setTimeout(() => { if (!selectionInside(block)) open(); }, 260);
    } else {
      open();
    }
  };

  node.addEventListener('pointerdown', onPointerDown, { passive: true });
  node.addEventListener('click', onClick);
  scan();
  return {
    update: scan,
    destroy() {
      clearTimeout(clickTimer);
      node.removeEventListener('pointerdown', onPointerDown);
      node.removeEventListener('click', onClick);
      for (const el of observed) resizeObserver?.unobserve(el);
      observed.clear();
    },
  };
}
