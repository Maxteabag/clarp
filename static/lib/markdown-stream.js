// Streaming markdown without re-parsing what is settled.
//
// splitCommitted cuts a growing message into committed blocks, each closed
// by a blank line or a closing code fence and never cut inside an open
// fence, plus the open tail. Committed blocks only ever get appended to the
// list, so a view renders each once (createBlockCache) and re-renders only
// the tail. revealStep paces how much of the text is shown per frame so a
// burst drains in about 200 ms on word boundaries.

const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const LINES = /[^\n]*\n|[^\n]+$/g;

export function splitCommitted(text) {
  const src = String(text || '');
  const committed = [];
  let start = 0;
  let pos = 0;
  let fence = null;
  for (const line of src.match(LINES) || []) {
    pos += line.length;
    const complete = line.endsWith('\n');
    const open = FENCE.exec(line);
    if (fence) {
      if (complete && open && open[1][0] === fence[0] && open[1].length >= fence.length
          && !line.slice(open[0].length).trim()) {
        fence = null;
        committed.push(src.slice(start, pos));
        start = pos;
      }
      continue;
    }
    if (open && complete) {
      fence = open[1];
      continue;
    }
    if (complete && !line.trim()) {
      const block = src.slice(start, pos);
      // A blank line straight after a commit belongs to the block before it.
      if (!block.trim() && committed.length) committed[committed.length - 1] += block;
      else committed.push(block);
      start = pos;
    }
  }
  return { committed, tail: src.slice(start) };
}

/** Renders a block once; the oldest entries go past `max`. */
export function createBlockCache(render, max = 400) {
  const cache = new Map();
  return {
    render(block) {
      if (cache.has(block)) {
        const html = cache.get(block);
        cache.delete(block);
        cache.set(block, html);
        return html;
      }
      const html = render(block);
      cache.set(block, html);
      if (cache.size > max) cache.delete(cache.keys().next().value);
      return html;
    },
    clear: () => cache.clear(),
  };
}

export const REVEAL_DRAIN_MS = 200;
const SNAP_CHARS = 16;

/** How many characters of `text` to show after one frame of `dtMs`. */
export function revealStep(text, shown, dtMs = 16, drainMs = REVEAL_DRAIN_MS) {
  const len = String(text || '').length;
  if (shown >= len) return len;
  const pending = len - shown;
  const step = Math.max(Math.ceil((pending * dtMs) / drainMs), Math.ceil(dtMs / 2));
  let next = Math.min(len, shown + step);
  const limit = Math.min(len, next + SNAP_CHARS);
  while (next < limit && !/\s/.test(text[next])) next++;
  return next;
}
