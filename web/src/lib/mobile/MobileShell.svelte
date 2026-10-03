<script>
  // Phone layout: full-bleed transcript, composer as an overlay, identity in
  // the dock. No rail — there is no room for one, and no pointer to use it.
  import { untrack } from 'svelte';
  import Conversation from '../Conversation.svelte';
  import MobileComposer from './MobileComposer.svelte';
  import MobileDock from './MobileDock.svelte';
  import { app } from '../../stores/app.svelte.js';

  let { onTapAgent, onHoldAgent } = $props();

  let chatOpen = $state(false);

  // The last few chats stay mounted (hidden), so switching agents shows the
  // chat as it was, scroll position included, instead of rebuilding every
  // turn. The open one is always the newest entry.
  const KEEP_MOUNTED = 4;
  let mounted = $state([]);
  $effect(() => {
    const s = app.session;
    if (!s) return;
    untrack(() => {
      if (mounted[mounted.length - 1] === s) return;
      mounted = [...mounted.filter(x => x !== s), s].slice(-KEEP_MOUNTED);
    });
  });
</script>

<main id="terminal-wrap">
  {#each mounted as session (session)}
    <Conversation {session} active={session === app.session} {onTapAgent} {onHoldAgent} />
  {/each}
</main>

<MobileComposer bind:open={chatOpen} />
<MobileDock bind:chatOpen {onTapAgent} {onHoldAgent} />
