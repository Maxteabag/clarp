<script>
  // Full screen view of a chat code block or a wide table (blocks.js opens it).
  // Code wraps by default with a toggle; a table scrolls both ways with its
  // header row pinned. Copy, close, Escape, and the back gesture close it.
  import { tick } from 'svelte';
  import { blockViewer, closeBlockViewer } from './block-viewer.svelte.js';
  import { ICONS, STRINGS, copyText, flashCopied } from './blocks.js';

  let item = $derived(blockViewer.item);
  let wrap = $state(true);
  let card = $state(null);
  let copyBtn = $state(null);
  let returnFocus = null;
  let pushed = false;

  $effect(() => {
    if (!item) return;
    wrap = true;
    returnFocus = document.activeElement;
    // A history entry of its own, so the back gesture closes the view rather
    // than leaving the app.
    try { window.history.pushState({ ...(window.history.state || {}), blockViewer: true }, ''); pushed = true; } catch (_) {}
    tick().then(() => card?.focus());
    return () => {
      if (pushed && window.history.state?.blockViewer) { pushed = false; window.history.back(); }
      pushed = false;
      try { returnFocus?.focus?.({ preventScroll: true }); } catch (_) {}
    };
  });

  function onPopState() {
    if (!item) return;
    pushed = false;
    closeBlockViewer();
  }

  function onKey(e) {
    if (!item || e.key !== 'Escape') return;
    e.preventDefault();
    e.stopImmediatePropagation();
    closeBlockViewer();
  }

  async function copy() {
    if (await copyText(item.text)) flashCopied(copyBtn);
  }

  // Code the chat had not highlighted yet is highlighted here.
  function highlight(node) {
    if (typeof hljs === 'undefined' || node.dataset.highlighted) return;
    try { hljs.highlightElement(node); } catch (_) {}
  }

  let title = $derived(item ? (item.kind === 'code' ? (item.lang || '') : '') : '');
  let label = $derived(item?.kind === 'table' ? STRINGS.tableViewer : (item?.lang || STRINGS.codeViewer));
</script>

<svelte:window onkeydowncapture={onKey} onpopstate={onPopState} />

{#if item}
  <div class="bv-root" role="dialog" aria-modal="true" aria-label={label} tabindex="-1" bind:this={card}>
    <div class="bv-bar">
      <span class="bv-title">{title}</span>
      {#if item.kind === 'code'}
        <button type="button" class="bv-btn" class:on={wrap} aria-pressed={wrap}
                aria-label={STRINGS.wrap} title={STRINGS.wrap} onclick={() => wrap = !wrap}>
          {@html ICONS.wrap}
        </button>
      {/if}
      <button type="button" class="bv-btn" bind:this={copyBtn}
              aria-label={item.kind === 'code' ? STRINGS.copyCode : STRINGS.copyTable}
              title={item.kind === 'code' ? STRINGS.copyCode : STRINGS.copyTable}
              onclick={copy}>{@html ICONS.copy}</button>
      <button type="button" class="bv-btn" aria-label={STRINGS.close} title={STRINGS.close}
              onclick={closeBlockViewer}>{@html ICONS.close}</button>
    </div>
    <div class="bv-body" class:bv-table={item.kind === 'table'} class:bv-code={item.kind === 'code'}>
      {#if item.kind === 'code'}
        {#key item}
          {#if item.html}
            <pre class:wrap><code class="hljs">{@html item.html}</code></pre>
          {:else}
            <pre class:wrap><code class={item.tag ? `language-${item.tag}` : ''} use:highlight>{item.text}</code></pre>
          {/if}
        {/key}
      {:else}
        {@html item.html}
      {/if}
    </div>
  </div>
{/if}

<style>
  .bv-root {
    position: fixed;
    inset: 0;
    z-index: 1000;
    display: flex;
    flex-direction: column;
    background: var(--ink, #111);
    color: var(--washi, #eee);
    padding: env(safe-area-inset-top) env(safe-area-inset-right) 0 env(safe-area-inset-left);
    outline: none;
  }
  .bv-bar {
    flex: none;
    display: flex;
    align-items: center;
    gap: 4px;
    min-height: 44px;
    padding: 0 6px 0 14px;
    border-bottom: 1px solid var(--ink-edge, #333);
  }
  .bv-title {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font: 500 15px/1.2 var(--font-body);
    color: var(--washi-dim, #bbb);
  }
  .bv-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: var(--washi-dim, #bbb);
    cursor: pointer;
  }
  .bv-btn :global(svg) { width: 20px; height: 20px; }
  .bv-btn:hover { background: color-mix(in srgb, var(--washi) 8%, transparent); color: var(--washi, #eee); }
  .bv-btn.on { color: var(--washi, #eee); background: color-mix(in srgb, var(--washi) 12%, transparent); }
  .bv-btn:global(.copied) { color: var(--moss); }
  .bv-body {
    flex: 1;
    min-height: 0;
    overflow: auto;
    -webkit-overflow-scrolling: touch;
    overscroll-behavior: contain;
    padding-bottom: env(safe-area-inset-bottom);
  }
  .bv-body pre {
    margin: 0;
    padding: 14px 16px 24px;
    font: 400 14px/1.55 var(--font-mono);
    white-space: pre;
    tab-size: 4;
    user-select: text;
    -webkit-user-select: text;
    width: max-content;
    min-width: 100%;
    box-sizing: border-box;
  }
  .bv-body pre.wrap {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    width: auto;
  }
  .bv-body pre code { background: transparent; padding: 0; font: inherit; }
  /* The same dark ground as the code in the chat (the highlight theme is dark). */
  .bv-code { background: #14141d; color: #a9b1d6; }
  .bv-table { padding: 0 0 env(safe-area-inset-bottom); }
  .bv-table :global(table) {
    border-collapse: separate;
    border-spacing: 0;
    min-width: 100%;
    font: 400 14px/1.45 var(--font-body);
  }
  .bv-table :global(th),
  .bv-table :global(td) {
    border-right: 1px solid var(--ink-edge, #333);
    border-bottom: 1px solid var(--ink-edge, #333);
    padding: 6px 10px;
    text-align: left;
    vertical-align: top;
    user-select: text;
    -webkit-user-select: text;
  }
  .bv-table :global(td) { max-width: 40ch; }
  .bv-table :global(th) {
    position: sticky;
    top: 0;
    z-index: 1;
    white-space: nowrap;
    background: var(--ink-soft, #1b1b1b);
    color: var(--washi, #eee);
    font: 500 13px/1.3 var(--font-display);
  }
  .bv-table :global(tbody tr:nth-child(odd) td) { background: color-mix(in srgb, var(--washi) 3%, transparent); }
  .bv-table :global(a) { color: var(--accent-blue); }
</style>
