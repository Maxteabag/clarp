<script>
  // Desktop layout: conversation rail on the left, transcript in the middle,
  // and a dock whose main occupant is the permanent composer.
  import PaneNode from './PaneNode.svelte';
  import DesktopDock from './DesktopDock.svelte';
  import Sidebar from './Sidebar.svelte';
  import { panesState } from '../../stores/panes.svelte.js';

  let { onTapAgent, onHoldAgent, onOpenOverview } = $props();
</script>

<main id="terminal-wrap">
  <Sidebar {onOpenOverview} />
  <div class="workspace">
    <PaneNode node={panesState.tree.root} {onTapAgent} {onHoldAgent} />
  </div>
</main>

<DesktopDock />
