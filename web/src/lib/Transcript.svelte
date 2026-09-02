<script>
  // One agent's transcript. Every pane mounts its own, reading its own
  // session from the conversation store, so panes never share a scroll
  // position or a set of turns.
  import { tick } from 'svelte';
  import { mergeTimeline } from '@core/timeline.js';
  import Turn from './Turn.svelte';
  import { conversation, loadOlder, placeholderFor } from '../stores/conversations.svelte.js';
  import { prefs } from '../stores/prefs.svelte.js';

  let { session } = $props();

  const BOTTOM_STICKY_PX = 96;

  let bodyEl = $state(null);
  // Pinned means "follow new content". Scrolling up unpins; the Latest
  // button (or reaching the bottom again) re-pins.
  let pinned = $state(true);
  let showJump = $state(false);
  let programmatic = false;

  let conv = $derived(conversation(session));
  let placeholder = $derived(placeholderFor(conv));
  // Read both lists at the top level so the tracking sees them.
  let timeline = $derived(mergeTimeline(conv.turns, conv.activity));

  function nearBottom() {
    if (!bodyEl) return true;
    return bodyEl.scrollHeight - bodyEl.scrollTop - bodyEl.clientHeight <= BOTTOM_STICKY_PX;
  }

  function scrollToBottom() {
    if (!bodyEl) return;
    programmatic = true;
    bodyEl.scrollTop = bodyEl.scrollHeight;
    setTimeout(() => { programmatic = false; }, 0);
  }

  // Content lands across several frames (markdown paints, images size, code
  // highlights), so re-pin a few times rather than once.
  function pinSoon() {
    scrollToBottom();
    requestAnimationFrame(() => requestAnimationFrame(scrollToBottom));
    setTimeout(scrollToBottom, 80);
    setTimeout(scrollToBottom, 250);
  }

  function onScroll() {
    if (programmatic) return;
    pinned = nearBottom();
    showJump = !pinned;
    if (bodyEl && bodyEl.scrollTop < 40 && conv.hasMore) loadOlder(session);
  }

  function jumpToLatest() {
    pinned = true;
    showJump = false;
    pinSoon();
  }

  // New content appended: follow it when pinned, otherwise offer the jump.
  $effect(() => {
    conv.appendSeq;
    session;
    if (!bodyEl) return;
    tick().then(() => {
      if (pinned) pinSoon();
      else showJump = true;
    });
  });

  // A different session in this pane starts pinned to its bottom.
  $effect(() => {
    session;
    pinned = true;
    showJump = false;
  });
</script>

<div
  id="historyBody"
  class="history-body"
  class:hide-tools={prefs.hideTools}
  bind:this={bodyEl}
  onscroll={onScroll}
>
  {#if placeholder}
    <div class="turn assistant has-body"><span class="meta">{placeholder}</span></div>
  {/if}

  <!-- Durable turns and live activity rows in one time-ordered list, keyed
       per source, so Svelte reuses the DOM for every key it already has and
       patches only rows whose revision moved. -->
  {#each timeline as entry (entry.key)}
    {#if entry.type === 'turn'}
      <Turn turn={entry.item} />
    {:else}
      <div class="turn activity {entry.item.cls}" class:thinking-live={entry.item.thinkingLive}>
        <span class="activity-log-dot"></span>
        <span class="activity-log-label">{entry.item.label}</span>
        <span class="activity-log-summary">{entry.item.summary}</span>
      </div>
    {/if}
  {/each}
</div>

{#if showJump}
  <button class="history-jump" type="button" onclick={jumpToLatest}>Latest</button>
{/if}
