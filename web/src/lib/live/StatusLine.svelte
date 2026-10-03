<script>
  // The one status line of a chat: what is happening now, a timer from the
  // Host clock, and interrupt. Hysteresis keeps fast tools from flashing it;
  // the timer writes its own text once a second instead of re-rendering.
  import { activityFromStatus, clock, statusLine } from '@core/live-present.js';
  import { createDelayedStatus } from '@core/delayed-status.js';
  import { live, liveFor, rosterActivity } from '../../stores/live.svelte.js';
  import { statusFor } from '../../stores/app.svelte.js';
  import { stopAgentTurn } from '../../stores/send.svelte.js';

  let { session } = $props();

  let skewMs = $derived(liveFor(session)?.skewMs || 0);
  let activity = $derived(live.enabled
    ? rosterActivity(session) || activityFromStatus(statusFor(session))
    : activityFromStatus(statusFor(session)));
  let target = $derived.by(() => {
    const line = statusLine(activity, { skewMs });
    if (!line) return null;
    const { time, ...rest } = line;
    return rest;
  });

  let shown = $state(null);
  const delayed = createDelayedStatus({ onChange: v => { shown = v; } });
  $effect(() => { delayed.set(target); });
  $effect(() => () => delayed.dispose());

  // The line names an item (docs/live-items.md §1.3 item_id): tapping it
  // brings that row into view.
  function reveal() {
    const id = shown && shown.itemId;
    if (!id || typeof CSS === 'undefined') return;
    const el = document.querySelector(`[data-item-id="${CSS.escape(id)}"]`);
    if (!el) return;
    el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    el.classList.remove('lv-pulse');
    void el.offsetWidth;
    el.classList.add('lv-pulse');
  }

  let timeEl = $state(null);
  $effect(() => {
    const since = shown && shown.since;
    if (!timeEl || !since) return;
    const paint = () => { timeEl.textContent = clock(Date.now() + skewMs - since); };
    paint();
    const id = setInterval(paint, 1000);
    return () => clearInterval(id);
  });
</script>

{#if shown}
  <div class="status-line {shown.state}" role="status" aria-live="polite">
    <span class="sl-glyph" aria-hidden="true">{shown.state === 'tool' ? '●' : '◌'}</span>
    {#if shown.itemId}
      <button class="sl-text sl-link" type="button" title="Show it" onclick={reveal}>{shown.text}</button>
    {:else}
      <span class="sl-text">{shown.text}</span>
    {/if}
    {#if shown.since}<span class="sl-time" bind:this={timeEl}></span>{/if}
    {#if shown.more}<span class="sl-more">{shown.more}</span>{/if}
    {#if shown.interrupt}
      <button class="sl-stop" type="button" aria-label="Interrupt" title="Interrupt (Esc)"
              onclick={() => stopAgentTurn(session)}>■</button>
    {/if}
  </div>
{/if}
