<script>
  import { untrack } from 'svelte';
  import { commandItems } from '@core/keymap.js';
  import { executeAction, input, keyContext } from '../../stores/input.svelte.js';
  import { composerRef } from '../../stores/composer.svelte.js';

  let { open = $bindable() } = $props();
  let query = $state('');
  let index = $state(0);
  let inputEl = $state(null);
  let items = $state([]);
  let restoreComposer = $state(false);

  let filtered = $derived.by(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return items;
    return items.filter(item =>
      `${item.label} ${item.action} ${item.group} ${item.key}`.toLowerCase().includes(needle));
  });
  let selected = $derived(Math.max(0, Math.min(index, filtered.length - 1)));

  $effect(() => {
    if (!open) return;
    untrack(() => {
      restoreComposer = input.insert;
      items = commandItems(keyContext());
      query = '';
      index = 0;
    });
    requestAnimationFrame(() => inputEl?.focus());
  });

  $effect(() => { query; index = 0; });

  function close() {
    const restore = restoreComposer;
    open = false;
    if (restore) requestAnimationFrame(() => composerRef.focus());
  }

  function choose(item) {
    if (!item) return;
    const restore = restoreComposer
      && !['quick-switch', 'search', 'overview', 'help', 'focus-sidebar'].includes(item.action);
    open = false;
    requestAnimationFrame(() => {
      executeAction(item.action);
      if (restore) composerRef.focus();
    });
  }

  function keydown(e) {
    if (e.key === 'ArrowDown' || (e.ctrlKey && e.key === 'n')) {
      e.preventDefault(); e.stopPropagation();
      index = Math.min(selected + 1, filtered.length - 1);
    } else if (e.key === 'ArrowUp' || (e.ctrlKey && e.key === 'p')) {
      e.preventDefault(); e.stopPropagation();
      index = Math.max(selected - 1, 0);
    } else if (e.key === 'Enter') {
      e.preventDefault(); e.stopPropagation(); choose(filtered[selected]);
    } else if (e.key === 'Escape') {
      e.preventDefault(); e.stopPropagation(); close();
    }
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="command-backdrop" onclick={close}>
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <section class="command-palette" aria-label="Commands" onclick={e => e.stopPropagation()}>
      <label class="command-search">
        <span aria-hidden="true">›</span>
        <input
          bind:this={inputEl}
          bind:value={query}
          onkeydown={keydown}
          placeholder="Type a command"
          aria-label="Search commands"
          autocomplete="off"
        />
        <kbd>esc</kbd>
      </label>
      <div class="command-list" role="listbox" aria-label="Available commands">
        {#each filtered as item, i (item.action)}
          <button
            class="command-row"
            class:selected={i === selected}
            role="option"
            aria-selected={i === selected}
            onmouseenter={() => (index = i)}
            onclick={() => choose(item)}
          >
            <span class="command-name">{item.label}</span>
            <span class="command-group">{item.group}</span>
            <kbd>{item.key}</kbd>
          </button>
        {:else}
          <p class="command-empty">No matching command</p>
        {/each}
      </div>
      <footer>
        <span><kbd>↑↓</kbd> move</span>
        <span><kbd>↵</kbd> run</span>
        <span class="command-count">{filtered.length} commands</span>
      </footer>
    </section>
  </div>
{/if}

<style>
  .command-backdrop {
    position: fixed;
    inset: 0;
    z-index: 80;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    padding-top: min(18vh, 150px);
    background: rgba(8, 9, 15, .66);
    backdrop-filter: blur(3px);
  }
  .command-palette {
    width: min(560px, calc(100vw - 32px));
    overflow: hidden;
    color: var(--washi);
    background: #1a1b26;
    border: 1px solid #3c3f58;
    border-radius: 6px;
    box-shadow: 0 24px 80px rgba(0, 0, 0, .58);
    animation: command-in 120ms var(--ease) both;
  }
  @keyframes command-in {
    from { opacity: 0; transform: translateY(-5px) scale(.992); }
  }
  .command-search {
    height: 48px;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 13px;
    border-bottom: 1px solid #303246;
    color: #8f92ad;
  }
  .command-search > span { color: #b4b7d2; font: 600 18px/1 var(--font-mono); }
  .command-search input {
    flex: 1;
    min-width: 0;
    border: 0;
    outline: 0;
    background: transparent;
    color: #d4d6e8;
    font: 500 14px/1 var(--font-mono);
    caret-color: #b5b8d2;
  }
  .command-search input::placeholder { color: #686b82; }
  kbd {
    min-width: 21px;
    padding: 2px 5px;
    color: #9a9db8;
    background: #20212d;
    border: 1px solid #36384b;
    border-radius: 3px;
    font: 500 10px/1.2 var(--font-mono);
    text-align: center;
  }
  .command-list {
    max-height: min(52vh, 430px);
    overflow-y: auto;
    padding: 5px;
  }
  .command-row {
    width: 100%;
    min-height: 34px;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto 86px;
    align-items: center;
    gap: 12px;
    padding: 6px 9px;
    color: #aeb1ca;
    background: transparent;
    border: 0;
    border-radius: 3px;
    font: 500 12px/1.2 var(--font-mono);
    text-align: left;
    cursor: default;
  }
  .command-row.selected {
    color: #e0e1ef;
    background: #2a2c3c;
    box-shadow: inset 2px 0 #9da1bd;
  }
  .command-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .command-group {
    color: #60637a;
    font-size: 9px;
    letter-spacing: .09em;
    text-transform: uppercase;
  }
  .command-row kbd { justify-self: end; color: #b8bbd2; }
  .command-empty { margin: 0; padding: 28px; color: #686b82; text-align: center; }
  footer {
    height: 29px;
    display: flex;
    align-items: center;
    gap: 13px;
    padding: 0 10px;
    color: #64677d;
    border-top: 1px solid #303246;
    font: 500 9px/1 var(--font-mono);
  }
  footer span { display: inline-flex; align-items: center; gap: 5px; }
  footer kbd { padding: 1px 4px; font-size: 9px; }
  .command-count { margin-left: auto; }
  :global(html[data-theme="day"]) .command-palette {
    color: var(--washi);
    background: var(--ink-soft);
    border-color: var(--ink-edge-hi);
  }
  :global(html[data-theme="day"]) .command-search,
  :global(html[data-theme="day"]) footer { border-color: var(--ink-edge); color: var(--washi-low); }
  :global(html[data-theme="day"]) .command-search input { color: var(--washi); }
  :global(html[data-theme="day"]) .command-row { color: var(--washi-dim); }
  :global(html[data-theme="day"]) .command-row.selected {
    color: var(--washi);
    background: color-mix(in srgb, var(--accent-blue) 14%, transparent);
  }
</style>
