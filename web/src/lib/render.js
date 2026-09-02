// Turn-body rendering: markdown, media extensions, tool cards.
//
// Ported from app.js unchanged in behaviour. Still string-building HTML
// rather than components, deliberately: the output is markdown converted by
// `marked` and sanitised by DOMPurify, so it arrives as an HTML string either
// way and has to go through {@html}. Rebuilding it as components would mean
// re-implementing a markdown renderer, which is exactly the wheel the
// vendored parser exists to avoid.

import { escapeAttr, escapeHTML } from '@core/html.js';
import { ActivityStatus } from '@core/protocol.js';
import { stripVoiceMarkup } from '@core/voice-markup.js';

export function pathTail(p) {
  if (!p) return '';
  return String(p).split('/').slice(-2).join('/');
}

export function activityStatusClass(status) {
  if (status === 'error') return 'error';
  if (status === 'running') return 'running';
  if (status === 'ok') return 'ok';
  return 'recorded';
}

function normalizeMediaReference(ref) {
  const raw = String(ref || '').trim();
  const clarp = raw.match(/^clarp-media:\/\/asset\/([A-Za-z0-9_-]+)$/);
  if (clarp) return `/media/${clarp[1]}`;
  const media = raw.match(/^\/?media\/([A-Za-z0-9_-]+)$/);
  if (media) return `/media/${media[1]}`;
  return '';
}

function renderMediaMarkdownExtensions(text) {
  return String(text || '')
    .replace(/```(?:clarp-gallery|gallery)\s*\n([\s\S]*?)```/gi, (_, body) => {
      const items = [];
      String(body || '').split(/\r?\n/).forEach(line => {
        const m = line.trim().match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
        if (!m) return;
        const src = normalizeMediaReference(m[2]);
        if (!src) return;
        items.push(`<img src="${escapeAttr(src)}" alt="${escapeAttr(m[1] || 'Image')}" loading="lazy">`);
      });
      if (!items.length) return _;
      return `<div class="media-gallery">${items.join('')}</div>`;
    })
    .replace(/!\[([^\]]*)\]\((clarp-media:\/\/asset\/[^)\s]+|\/media\/[^)\s]+|media\/[^)\s]+)\)/g,
      (_, alt, ref) => {
        const src = normalizeMediaReference(ref);
        if (!src) return _;
        return `![${alt}](${src})`;
      });
}

// <speak>...</speak> is the voice-channel marker the server uses to pick what
// gets synthesised. The transcript should show the inner text but not the
// markup, so strip the tags before any markdown processing.
export function renderText(s, streaming = false) {
  const text = renderMediaMarkdownExtensions(stripVoiceMarkup(s, { streaming }));
  if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
    marked.setOptions({ breaks: true, gfm: true });
    return DOMPurify.sanitize(marked.parse(text));
  }
  return renderTextFallback(text);
}

// Safety net for a load where either vendored library is missing (offline-LAN
// cache miss, integrity failure). Regex markdown — worse output, but a turn
// still renders something readable instead of raw source.
function renderTextFallback(s) {
  const tokens = [];
  const STUB = (i) => `\0CB${i}\0`;
  let work = String(s)
    .replace(/```(\w*)\n?([\s\S]*?)```/g, (_, _lang, code) =>
      STUB(tokens.push({ kind: 'pre', code }) - 1))
    .replace(/`([^`\n]+?)`/g, (_, code) =>
      STUB(tokens.push({ kind: 'code', code }) - 1));
  work = extractTables(work, tokens, STUB);
  work = escapeHTML(work);
  work = work.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_, text, url) =>
      `<a href="${escapeAttr(url)}" target="_blank" rel="noopener">${text}</a>`);
  work = work.replace(/(?<!href=")(https?:\/\/[^\s<]+)/g,
    url => `<a href="${escapeAttr(url)}" target="_blank" rel="noopener">${url}</a>`);
  return work.replace(/\0CB(\d+)\0/g, (_, i) => {
    const tk = tokens[Number(i)];
    if (tk.kind === 'pre')   return `<pre><code>${escapeHTML(tk.code)}</code></pre>`;
    if (tk.kind === 'code')  return `<code>${escapeHTML(tk.code)}</code>`;
    if (tk.kind === 'table') return renderTableHtml(tk);
    return '';
  });
}

// GitHub-flavored tables: header row, dash separator (optional `:` alignment),
// then data rows. Each match becomes one STUB so the rest of the renderer
// treats it as opaque, the same way code blocks travel.
function extractTables(text, tokens, STUB) {
  const lines = text.split('\n');
  const out = [];
  let i = 0;
  const ROW = /^\s*\|(.+)\|\s*$/;
  const SEP = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$/;
  while (i < lines.length) {
    if (i + 1 < lines.length && ROW.test(lines[i]) && SEP.test(lines[i + 1])) {
      const header = splitCells(lines[i]);
      const align  = parseAlign(lines[i + 1]);
      const rows = [];
      let j = i + 2;
      while (j < lines.length && ROW.test(lines[j])) {
        rows.push(splitCells(lines[j]));
        j++;
      }
      if (rows.length > 0) {
        out.push(STUB(tokens.push({ kind: 'table', header, rows, align }) - 1));
        i = j;
        continue;
      }
    }
    out.push(lines[i]);
    i++;
  }
  return out.join('\n');
}

function splitCells(line) {
  const trimmed = line.replace(/^\s*\|/, '').replace(/\|\s*$/, '');
  const parts = [];
  let buf = '';
  for (let k = 0; k < trimmed.length; k++) {
    const c = trimmed[k];
    if (c === '\\' && trimmed[k + 1] === '|') { buf += '|'; k++; continue; }
    if (c === '|') { parts.push(buf.trim()); buf = ''; continue; }
    buf += c;
  }
  parts.push(buf.trim());
  return parts;
}

function parseAlign(sepLine) {
  return splitCells(sepLine).map(c => {
    const L = c.startsWith(':'), R = c.endsWith(':');
    if (L && R) return 'center';
    if (R)      return 'right';
    if (L)      return 'left';
    return '';
  });
}

function renderTableHtml(tk) {
  const al = (i) => tk.align[i] ? ` style="text-align:${tk.align[i]}"` : '';
  const head = `<thead><tr>${tk.header.map((c, i) =>
    `<th${al(i)}>${escapeHTML(c)}</th>`).join('')}</tr></thead>`;
  const body = `<tbody>${tk.rows.map(r =>
    `<tr>${r.map((c, i) =>
      `<td${al(i)}>${escapeHTML(c)}</td>`).join('')}</tr>`).join('')}</tbody>`;
  return `<div class="table-wrap"><table>${head}${body}</table></div>`;
}

// ---- tool cards ---------------------------------------------------------

export function renderTool(t) {
  if (!t || !t.name) return '';
  const name = t.name;
  const status = t.status || 'recorded';
  let summary = t.summary || '', body = '';
  if (name === 'Edit') {
    summary = summary || pathTail(t.file_path);
    body = `<pre class="tool-diff old">${escapeHTML(t.old || '')}</pre>` +
           `<pre class="tool-diff new">${escapeHTML(t.new || '')}</pre>` +
           (t.replace_all ? `<div class="tool-flag">replace_all</div>` : '');
  } else if (name === 'MultiEdit') {
    summary = summary || `${pathTail(t.file_path)} · ${t.edit_count} edits`;
    body = (t.edits || []).map(e =>
      `<pre class="tool-diff old">${escapeHTML(e.old)}</pre>` +
      `<pre class="tool-diff new">${escapeHTML(e.new)}</pre>`).join('');
  } else if (name === 'Write') {
    summary = summary || pathTail(t.file_path);
    body = `<pre class="tool-diff new">${escapeHTML(t.content || '')}</pre>`;
  } else if (name === 'Read') {
    summary = summary || (pathTail(t.file_path) + ((t.offset || t.limit) ? ` @${t.offset || 0}+${t.limit || ''}` : ''));
  } else if (name === 'Bash') {
    summary = summary || (t.description || (t.command || '').slice(0, 60)).trim();
    body = (t.description ? `<div class="tool-desc">${escapeHTML(t.description)}</div>` : '') +
           `<pre class="tool-cmd">$ ${escapeHTML(t.command || '')}</pre>`;
  } else if (name === 'Glob' || name === 'Grep') {
    summary = summary || ((t.pattern || '') + (t.path ? ' in ' + pathTail(t.path) : ''));
    body = `<pre class="tool-cmd">${escapeHTML(t.pattern || '')}${t.path ? ' in ' + escapeHTML(t.path) : ''}</pre>`;
  } else if (name === 'TodoWrite') {
    const todos = t.todos || [];
    const inProg = todos.find(td => td.status === 'in_progress');
    summary = summary || (inProg ? inProg.content : `${todos.length} items`);
    body = '<ul class="tool-todos">' + todos.map(td =>
      `<li class="todo-${escapeHTML(td.status)}">${escapeHTML(td.content)}</li>`).join('') + '</ul>';
  } else if (t.input) {
    const k = Object.keys(t.input)[0];
    summary = summary || (k ? `${k}=${t.input[k]}` : '');
    body = '<pre class="tool-cmd">' + escapeHTML(JSON.stringify(t.input, null, 2)) + '</pre>';
  }
  if (t.result) body += `<pre class="tool-cmd tool-result">${escapeHTML(t.result)}</pre>`;
  const label = status === ActivityStatus.ERROR ? 'failed'
    : status === ActivityStatus.OK ? 'done'
      : status === ActivityStatus.RUNNING ? ActivityStatus.RUNNING : 'logged';
  const openAttr = (name === 'Edit' || name === 'MultiEdit') ? ' open' : '';
  const cls = activityStatusClass(status);
  return `<details${openAttr} class="tool ${cls}"><summary class="tool-head"><span class="tool-status ${cls}"></span><span class="tool-name">${escapeHTML(name)}</span><span class="tool-summary">${escapeHTML(summary)}</span><span class="tool-state">${escapeHTML(label)}</span></summary>${body}</details>`;
}

export function renderTurnBody(t) {
  const body = t.text
    ? `<div class="body">${renderText(t.text, t.kind === 'live')}</div>`
    : '';
  return body + (t.tools || []).map(renderTool).join('');
}

// ---- formatting ---------------------------------------------------------

export function formatElapsed(turnStartedAtSec) {
  if (!turnStartedAtSec) return '';
  const elapsedSec = Math.max(0, Math.floor(Date.now() / 1000) - turnStartedAtSec);
  if (elapsedSec < 60) return elapsedSec + 's';
  const m = Math.floor(elapsedSec / 60);
  const r = elapsedSec % 60;
  return `${m}m ${r}s`;
}
