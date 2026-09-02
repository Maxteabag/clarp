<script>
  // Hook-driven banner above the transcript: compacting, waiting for input,
  // or interrupted. The elapsed counter ticks live, matching the CLI spinner.
  import { app, bannerFor } from '../stores/app.svelte.js';
  import { formatElapsed } from './render.js';

  let { session = app.session } = $props();

  let now = $state(Date.now());
  let banner = $derived(bannerFor(session));

  $effect(() => {
    if (!banner?.startedAt) return;
    const id = setInterval(() => { now = Date.now(); }, 500);
    return () => clearInterval(id);
  });

  let elapsed = $derived(
    banner?.startedAt ? (now, formatElapsed(banner.startedAt)) : '');
</script>

{#if banner}
  <div class="agent-banner {banner.cls}" aria-live="polite">
    {#if banner.spinner}<span class="banner-spinner"></span>{/if}
    {#if banner.icon}<span class="banner-icon">{banner.icon}</span>{/if}
    <span class="banner-msg">{banner.msg}</span>
    {#if elapsed}<span class="banner-meta">{elapsed}</span>{/if}
  </div>
{/if}
