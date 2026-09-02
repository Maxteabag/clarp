<script>
  // Phone layout: full-bleed transcript, composer as an overlay, identity in
  // the dock. No rail — there is no room for one, and no pointer to use it.
  import Conversation from '../Conversation.svelte';
  import MobileComposer from './MobileComposer.svelte';
  import MobileDock from './MobileDock.svelte';
  import { app } from '../../stores/app.svelte.js';

  let { onTapAgent, onHoldAgent } = $props();

  let chatOpen = $state(false);
</script>

<main id="terminal-wrap">
  <Conversation session={app.session} {onTapAgent} {onHoldAgent} />
</main>

<MobileComposer bind:open={chatOpen} />
<MobileDock bind:chatOpen {onTapAgent} {onHoldAgent} />
