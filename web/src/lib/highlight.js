// Lazy syntax highlighting.
//
// highlight.js is the largest asset the client loads (121KB) and the most
// expensive per-block work it does. The old client called
// `hljs.highlightElement` on every `pre code` in the transcript after every
// repaint — 100 turns' worth of grammar matching for a one-turn change.
//
// One shared IntersectionObserver instead: a block is highlighted the first
// time it comes near the viewport, once, ever. Blocks in the 90 turns you
// scrolled past are never parsed at all.

const PREROLL = '600px';   // start work slightly before a block scrolls in

let observer = null;

function ensureObserver() {
  if (observer || typeof IntersectionObserver === 'undefined') return observer;
  observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const block = entry.target;
      observer.unobserve(block);
      paint(block);
    }
  }, { rootMargin: PREROLL });
  return observer;
}

function paint(block) {
  if (typeof hljs === 'undefined') return;
  if (block.dataset.highlighted) return;
  try { hljs.highlightElement(block); } catch (_) {}
}

// Svelte action. Attach to any element whose innerHTML holds rendered
// markdown; re-runs whenever the bound value changes, which is what tells us
// a turn's text grew and may carry new code blocks.
export function lazyHighlight(node) {
  const scan = () => {
    const io = ensureObserver();
    node.querySelectorAll('pre code').forEach(block => {
      if (block.dataset.highlighted || block.dataset.hlQueued) return;
      block.dataset.hlQueued = '1';
      // No IntersectionObserver (very old WebView): fall back to painting
      // immediately rather than leaving code unstyled.
      if (io) io.observe(block);
      else paint(block);
    });
  };
  scan();
  return {
    update: scan,
    destroy() {
      if (!observer) return;
      node.querySelectorAll('pre code').forEach(b => observer.unobserve(b));
    },
  };
}
