<script>
  // One agent's transcript. Every pane mounts its own, reading its own
  // session from the conversation store, so panes never share a scroll
  // position or a set of turns.
  import { tick } from 'svelte';
  import { mergeTimeline } from '@core/timeline.js';
  import { currentTurnItems, liveInsertIndex, takenOverTurns } from '@core/live-present.js';
  import { withLiveEntry } from '@core/live-timeline.js';
  import Turn from './Turn.svelte';
  import LiveTurn from './live/LiveTurn.svelte';
  import { conversation, loadOlder, placeholderFor } from '../stores/conversations.svelte.js';
  import { live, liveFor, liveRequested, openLive, watchSession } from '../stores/live.svelte.js';
  import { prefs } from '../stores/prefs.svelte.js';

  let { session, active = true } = $props();

  const BOTTOM_STICKY_PX = 96;

  let bodyEl = $state(null);
  // Pinned means "follow new content". Scrolling up unpins; the Latest
  // button (or reaching the bottom again) re-pins.
  let pinned = $state(true);
  let showJump = $state(false);
  let programmatic = false;
  let savedTop = 0;

  let conv = $derived(conversation(session));
  let placeholder = $derived(placeholderFor(conv));
  // The live turn (docs/live-items.md): its items replace the transient
  // activity rows, and stand in for the /log rows that carry them.
  let liveState = $derived(liveFor(session));
  let liveItems = $derived(liveState ? currentTurnItems(liveState) : []);
  // Every live event replaces liveItems; the hidden set and the live block's
  // position only change now and then. Both keep their identity (a set) or
  // value (a number) between events, so the turn filter, the merged timeline
  // and the keyed list below are not rebuilt per event.
  let lastHidden = null;
  let hidden = $derived.by(() => (lastHidden = takenOverTurns(conv.turns, liveItems, lastHidden)));
  let turns = $derived(hidden.size ? conv.turns.filter(t => !hidden.has(t.id)) : conv.turns);
  // Read both lists at the top level so the tracking sees them.
  let merged = $derived(mergeTimeline(turns, liveItems.length ? [] : conv.activity));
  // The live turn sits where its turn started, as one keyed entry, so a
  // prompt sent after it settled lands below it and the block is moved,
  // never rebuilt.
  let liveAt = $derived(liveItems.length
    ? liveInsertIndex(merged.map(e => e.item), liveState.turn) : -1);
  // These deriveds are composeTimeline (@core/live-timeline.js) in steps,
  // split so a live event that changes nothing structural rebuilds nothing.
  let timeline = $derived(withLiveEntry(merged, liveAt));

  // Keep this chat's live items coming while it is on screen.
  $effect(() => {
    if (!session || !live.enabled) return;
    watchSession(session);
    if (!liveRequested(session)) openLive(session);
  });

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
    if (active && bodyEl) savedTop = bodyEl.scrollTop;
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

  // Streaming text, a growing output tail or an image sizing itself changes
  // the height without adding a turn: follow it while pinned.
  let contentEl = $state(null);
  $effect(() => {
    if (!contentEl || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => { if (pinned) scrollToBottom(); });
    ro.observe(contentEl);
    return () => ro.disconnect();
  });

  // Older history lands above the reader. WebKit has no scroll anchoring, so
  // hold the old first turn where it was: note its position before the DOM
  // changes and move the scroll by however far it was pushed down.
  let anchor = null;
  $effect.pre(() => {
    conv.turns;
    if (!bodyEl || !contentEl || pinned) return;
    const first = contentEl.querySelector('.turn');
    anchor = first ? { el: first, top: first.getBoundingClientRect().top } : null;
  });
  // Rows above the reader keep resizing for a few frames after they land
  // (content-visibility estimates, then real sizes), so the anchor is held
  // for a short settle window rather than corrected once.
  const ANCHOR_SETTLE_MS = 600;
  let settleUntil = 0;
  let settleFrame = 0;
  $effect(() => {
    conv.turns;
    const a = anchor;
    anchor = null;
    if (!a || !bodyEl || !a.el.isConnected) return;
    if (Math.abs(a.el.getBoundingClientRect().top - a.top) < 1) return;
    settleUntil = performance.now() + ANCHOR_SETTLE_MS;
    const hold = () => {
      settleFrame = 0;
      if (!bodyEl || !a.el.isConnected) return;
      const moved = a.el.getBoundingClientRect().top - a.top;
      if (Math.abs(moved) >= 1) {
        programmatic = true;
        bodyEl.scrollTop += moved;
        setTimeout(() => { programmatic = false; }, 0);
      }
      if (performance.now() < settleUntil) settleFrame = requestAnimationFrame(hold);
    };
    if (settleFrame) cancelAnimationFrame(settleFrame);
    hold();
  });
  // A deliberate scroll ends the hold at once.
  function releaseAnchor() { settleUntil = 0; }

  // Hidden behind another chat (phone): onScroll remembers where the reader
  // was; coming back returns there, or to the newest content when following.
  $effect(() => {
    if (!bodyEl) return;
    if (!active) return;
    tick().then(() => {
      if (pinned) pinSoon();
      else { programmatic = true; bodyEl.scrollTop = savedTop; setTimeout(() => { programmatic = false; }, 0); }
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
  id={active ? 'historyBody' : undefined}
  class="history-body"
  class:hide-tools={prefs.hideTools}
  bind:this={bodyEl}
  onscroll={onScroll}
  onwheel={releaseAnchor}
  ontouchstart={releaseAnchor}
>
  <div class="history-content" bind:this={contentEl}>
  {#if placeholder}
    <div class="turn assistant has-body"><span class="meta">{placeholder}</span></div>
  {/if}

  <!-- Durable turns and live activity rows in one time-ordered list, keyed
       per source, so Svelte reuses the DOM for every key it already has and
       patches only rows whose revision moved. -->
  {#each timeline as entry (entry.key)}
    {#if entry.type === 'turn'}
      <Turn turn={entry.item} />
    {:else if entry.type === 'live'}
      <LiveTurn {liveState} items={liveItems} explanations={live.explanations.enabled} />
    {:else}
      <div class="turn activity {entry.item.cls}" class:thinking-live={entry.item.thinkingLive}>
        <span class="activity-log-dot"></span>
        <span class="activity-log-label">{entry.item.label}</span>
        <span class="activity-log-summary">{entry.item.summary}</span>
      </div>
    {/if}
  {/each}
  </div>
</div>

{#if showJump}
  <button class="history-jump" type="button" onclick={jumpToLatest}>Latest</button>
{/if}
