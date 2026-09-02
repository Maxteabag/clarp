<script>
  // Desktop dock: the composer lives in this row. No identity block and no
  // connection dot — on desktop those sit in the transcript header, and the
  // sidebar already says which conversation is open.
  import DockControls from '../DockControls.svelte';
  import DesktopComposer from './DesktopComposer.svelte';
  import ShortcutBar from './ShortcutBar.svelte';
</script>

<nav class="dock" aria-label="Controls">
  <DesktopComposer />
  <span class="dock-spacer"></span>
  <DockControls />
</nav>
<ShortcutBar />
