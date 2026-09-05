<script>
  import { AVATAR_PALETTE, avatarUrl } from '../stores/app.svelte.js';

  let {
    name = '', session = '', url = '', class: className = '', decorative = true,
    children,
  } = $props();

  let src = $derived(url || avatarUrl(name, session));
  let failed = $state(false);
  let initial = $derived(String(name || '?').trim().slice(0, 1).toUpperCase());

  // Content-versioned avatar URLs change when the portrait changes. A failure
  // belongs to one URL only; let a new version try immediately.
  $effect(() => { src; failed = false; });
</script>

<span
  class="agent-avatar-image {className}"
  style={`--avatar-fallback:${AVATAR_PALETTE[name] || '#5b6078'}`}
  aria-hidden={decorative ? 'true' : undefined}
>
  <span class="avatar-media">
    <span class="avatar-fallback">{initial}</span>
    {#if src && !failed}
      <img src={src} alt={decorative ? '' : name} onerror={() => (failed = true)} />
    {/if}
  </span>
  {@render children?.()}
</span>

<style>
  .agent-avatar-image {
    position: relative;
    display: inline-flex;
    flex: none;
    width: var(--avatar-size, 28px);
    height: var(--avatar-size, 28px);
    border-radius: var(--avatar-radius, 7px);
    color: #171822;
    font: 700 calc(var(--avatar-size, 28px) * .46)/1 var(--font-mono);
  }
  .avatar-media {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    overflow: hidden;
    border-radius: inherit;
    background: color-mix(in srgb, var(--avatar-fallback) 74%, #888ba2);
    box-shadow: inset 0 0 0 1px rgba(222, 224, 241, .13);
  }
  .avatar-fallback { opacity: .82; }
  img {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    object-position: center;
  }
</style>
