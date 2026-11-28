<script>
  // One agent's transcript. Every pane mounts its own, reading its own
  // session from the conversation store, so panes never share a scroll
  // position or a set of turns.
  import { tick } from 'svelte';
  import { mergeTimeline } from '@core/timeline.js';
  import { currentTurnItems, explainedError, liveInsertIndex, planTurnErrors, takenOverTurns } from '@core/live-present.js';
  import { liveSegmentItems, withLiveSegments } from '@core/live-timeline.js';
  import Turn from './Turn.svelte';
  import WorkRow from './WorkRow.svelte';
  import { foldTurns } from '@core/history-fold.js';
  import { statusFor } from '../stores/app.svelte.js';
  import { toolActivityFor } from '../stores/chat-choices.svelte.js';
  import LiveTurn from './live/LiveTurn.svelte';
  import RenderSpecCard from './RenderSpecCard.svelte';
  import { withArtifacts } from '@core/render-spec.js';
  import { renderSpecsFor } from '../stores/artifacts.svelte.js';
  import HiddenRun from './HiddenRun.svelte';
  import { hidesLiveTurn } from '@core/coordination.js';
  import {
    concealCoordination, conversation, loadOlder, placeholderFor, revealCoordination,
  } from '../stores/conversations.svelte.js';
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
  // Hidden coordination (docs/protocol.md): under an effective hide a live turn another agent
  // started is not drawn. The policy the rows were read under says it, else the snapshot.
  let visibility = $derived(conv.coordination?.visibility || statusFor(session).coordination_visibility || '');
  let liveHidden = $derived(hidesLiveTurn(visibility, liveState && liveState.turn));
  let liveItems = $derived(liveState && !liveHidden ? currentTurnItems(liveState) : []);
  // Every live event replaces liveItems; the hidden set and the live block's
  // position only change now and then. Both keep their identity (a set) or
  // value (a number) between events, so the turn filter, the merged timeline
  // and the keyed list below are not rebuilt per event.
  let lastHidden = null;
  let hidden = $derived.by(() => (lastHidden = takenOverTurns(conv.turns, liveItems, lastHidden, liveState && liveState.turn)));
  let turns = $derived(hidden.size ? conv.turns.filter(t => !hidden.has(t.id)) : conv.turns);
  // Failed turns the Host explains (docs/turn-errors.md): repeats of one failure with no
  // reply between them draw as the newest with a count; only the newest offers a retry.
  let liveFailure = $derived(liveItems.length && liveState.turn && liveState.turn.status === 'failed'
    ? explainedError(liveState.turn.error) : null);
  let errorPlan = $derived(planTurnErrors(turns, liveFailure ? { key: 'live', error: liveFailure } : null));
  // Read both lists at the top level so the tracking sees them.
  let merged = $derived(mergeTimeline(turns, liveItems.length ? [] : conv.activity));
  // The live turn sits where its turn started, as one keyed entry, so a
  // prompt sent after it settled lands below it and the block is moved,
  // never rebuilt.
  let liveAt = $derived(liveItems.length
    ? liveInsertIndex(merged.map(e => e.item), liveState.turn) : -1);
  // These deriveds are composeTimeline (@core/live-timeline.js) in steps,
  // split so a live event that changes nothing structural rebuilds nothing.
  // A message the turn read while it ran splits it there (withLiveSegments): the
  // turn's start and end are numbers, so a streaming event does not rebuild this.
  let liveStarted = $derived(Number(liveState && liveState.turn && liveState.turn.started_at_ms) || 0);
  let liveEnded = $derived(Number(liveState && liveState.turn && liveState.turn.ended_at_ms) || 0);
  let liveTimeline = $derived(withLiveSegments(merged, liveAt, liveStarted, liveEnded));
  // Native forms (render_spec, issue 428) sit where they were published; always drawn, whatever
  // the coordination setting (docs/protocol.md). A form older than the loaded page waits for it.
  let forms = $derived(renderSpecsFor(session));
  let timeline = $derived(forms.length
    ? withArtifacts(liveTimeline, forms, {
        complete: !conv.hasMore,
        liveStartMs: liveAt >= 0 ? Number(liveState.turn && liveState.turn.started_at_ms) || null : null,
      })
    : liveTimeline);
  // The quiet chat: each settled /log turn's work behind one row (static/lib/history-fold.js).
  // The turn live items show is theirs, already left out of `turns`.
  // busy is its own derived boolean: a status tick that leaves it unchanged does not refold.
  let busy = $derived(!!statusFor(session).busy);
  // The chat's own Tool activity, else the device's (stores/chat-choices.svelte.js).
  let toolMode = $derived(toolActivityFor(session));
  let fold = $derived(foldTurns(turns, { mode: toolMode, busy }));

  // A chat's own Show lasts for this visit: leaving the chat reads it under its setting again.
  $effect(() => {
    const shownSession = session;
    return () => concealCoordination(shownSession);
  });
  const showCoordination = () => revealCoordination(session);

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
      {#if entry.item.hidden_before && prefs.showCoordinationLine}<HiddenRun count={entry.item.hidden_before} onShow={showCoordination} />{/if}
      {#if fold.byRow.has('before:' + entry.item.id)}<WorkRow fold={fold.byRow.get('before:' + entry.item.id)} />{/if}
      {#if !errorPlan.hidden.has(entry.item.id)}<Turn turn={entry.item} strip={fold.stripped.get(entry.item.id)} {errorPlan} {session} />{/if}
      {#if fold.byRow.has(entry.item.id)}<WorkRow fold={fold.byRow.get(entry.item.id)} />{/if}
    {:else if entry.type === 'live'}
      <LiveTurn {liveState} items={liveSegmentItems(liveItems, entry)} last={entry.last !== false} explanations={live.explanations.enabled} {errorPlan} {session} />
    {:else if entry.type === 'artifact'}
      <RenderSpecCard row={entry.item} />
    {:else}
      <div class="turn activity {entry.item.cls}" class:thinking-live={entry.item.thinkingLive}>
        <span class="activity-log-dot"></span>
        <span class="activity-log-label">{entry.item.label}</span>
        <span class="activity-log-summary">{entry.item.summary}</span>
      </div>
    {/if}
  {/each}
  {#if conv.hiddenAfter && prefs.showCoordinationLine}<HiddenRun count={conv.hiddenAfter} onShow={showCoordination} />{/if}
  {#if conv.revealed}
    <div class="turn hidden-run revealed" role="note">
      <span class="hidden-run-text">Showing messages between agents</span>
      <button class="hidden-run-show" type="button" onclick={() => concealCoordination(session)}>Hide</button>
    </div>
  {/if}
  </div>
</div>

{#if showJump}
  <button class="history-jump" type="button" onclick={jumpToLatest}>Latest</button>
{/if}
