<script>
  // Desktop layout: conversation rail on the left, pane workspace in the
  // middle, and a dock whose main occupant is the permanent composer.
  //
  // The rail | workspace seam is a PaneForge group, so the rail is dragged to
  // size like any other pane. Everything about the rail's width lives here —
  // pixel bounds, the narrow-window rule, the user's collapse preference and
  // the saved width. Sidebar.svelte only renders.
  import { untrack } from 'svelte';
  import { PaneGroup, Pane, PaneResizer } from 'paneforge';
  import PaneNode from './PaneNode.svelte';
  import DesktopDock from './DesktopDock.svelte';
  import Sidebar from './Sidebar.svelte';
  import { panesState } from '../../stores/panes.svelte.js';

  let { onTapAgent, onHoldAgent, onOpenOverview } = $props();

  // Rail geometry, in pixels. PaneForge sizes panes as a percentage of the
  // group, so these are converted against the measured group width and
  // re-applied whenever it changes: the rail keeps its pixel bounds rather
  // than a share of the screen.
  const RAIL = { collapsed: 44, min: 176, initial: 224, max: 320 };
  // Below this window width there is no room for an expanded rail. It becomes
  // an icon strip whatever the stored preference says.
  const NARROW = '(max-width: 1080px)';
  const PREF_COLLAPSED = 'sidebarCollapsed';
  const PREF_WIDTH = 'sidebarWidth';

  let groupEl = $state(null);
  let groupWidth = $state(window.innerWidth);
  $effect(() => {
    if (!groupEl) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) groupWidth = w;
    });
    ro.observe(groupEl);
    return () => ro.disconnect();
  });
  const pct = (px) => Math.min(100, (px / groupWidth) * 100);

  let forced = $state(window.matchMedia(NARROW).matches);
  $effect(() => {
    const mq = window.matchMedia(NARROW);
    const sync = () => { forced = mq.matches; };
    mq.addEventListener('change', sync);
    return () => mq.removeEventListener('change', sync);
  });

  // What the user asked for, kept apart from what the window forces, so a
  // narrow window collapsing the rail never becomes the saved preference.
  let userCollapsed = $state(localStorage.getItem(PREF_COLLAPSED) === '1');
  function setUserCollapsed(v) {
    userCollapsed = v;
    localStorage.setItem(PREF_COLLAPSED, v ? '1' : '0');
  }

  // The rail's expanded width. Saved in pixels, not percent, so a different
  // window size on the next launch gets the same rail.
  let railWidth = $state(clampWidth(parseInt(localStorage.getItem(PREF_WIDTH), 10)));
  function clampWidth(px) {
    return Math.max(RAIL.min, Math.min(RAIL.max, Number.isFinite(px) ? px : RAIL.initial));
  }
  $effect(() => { localStorage.setItem(PREF_WIDTH, String(railWidth)); });

  // What the rail renders as. Mirrored from PaneForge on every layout report,
  // so a drag past the snap point, a keyboard resize and the toggle button all
  // agree without a second source of truth.
  let collapsed = $state(untrack(() => forced || userCollapsed));
  let sidebarPane = $state(null);
  let layoutReady = $state(false);
  let dragging = $state(false);

  // The rail's live expanded width in pixels, updated on every layout report.
  // Kept equal to what the rail is showing on purpose: PaneForge rebuilds the
  // layout from default sizes whenever a size constraint changes — which the
  // pixel-to-percent conversion does on every window resize — so a default
  // that trails the live layout is what stops a resize from resetting it.
  let liveWidth = $state(untrack(() => railWidth));
  let defaultPct = $derived(pct(collapsed ? RAIL.collapsed : liveWidth));

  function onLayoutChange(layout) {
    layoutReady = true;
    if (!sidebarPane) return;
    const isCollapsed = sidebarPane.isCollapsed();
    collapsed = isCollapsed;
    if (!isCollapsed) {
      liveWidth = clampWidth(Math.round((layout[0] / 100) * groupWidth));
      // A drag that ends collapsed passes through the minimum on the way; the
      // width worth remembering is the one it started from, so drags commit
      // on release (below) and everything else commits here.
      if (!dragging) railWidth = liveWidth;
    }
    // A forced collapse is the window's doing, not a preference.
    if (!forced && isCollapsed !== userCollapsed) setUserCollapsed(isCollapsed);
  }
  function onDraggingChange(isDragging) {
    dragging = isDragging;
    if (!isDragging && !collapsed) railWidth = liveWidth;
  }

  // Preference and window rule -> layout. Reads of PaneForge state are
  // untracked so this does not re-run on every drag.
  $effect(() => {
    const want = forced || userCollapsed;
    if (!layoutReady || !sidebarPane) return;
    untrack(() => {
      if (want === sidebarPane.isCollapsed()) return;
      if (want) {
        sidebarPane.collapse();
      } else {
        // expand() only knows a width it collapsed from in this session and
        // otherwise opens to the minimum; follow it with the saved width.
        // Both land in the same frame, so the expanding transition still
        // runs to the final size.
        const target = pct(railWidth);
        sidebarPane.expand();
        sidebarPane.resize(target);
      }
    });
  });

  function toggle() {
    if (!forced) setUserCollapsed(!collapsed);
  }
</script>

<main id="terminal-wrap">
  <PaneGroup direction="horizontal" class="shell-group" bind:ref={groupEl} {onLayoutChange}>
    <Pane
      id="sidebar"
      class="rail-pane"
      order={1}
      bind:this={sidebarPane}
      defaultSize={defaultPct}
      minSize={pct(RAIL.min)}
      maxSize={pct(RAIL.max)}
      collapsible
      collapsedSize={pct(RAIL.collapsed)}
    >
      <Sidebar {collapsed} {forced} onToggle={toggle} {onOpenOverview} />
    </Pane>
    <PaneResizer class="rail-resizer" disabled={forced} {onDraggingChange} aria-label="Resize sidebar" />
    <Pane id="workspace" class="workspace" order={2} minSize={30}>
      <PaneNode node={panesState.tree.root} {onTapAgent} {onHoldAgent} />
    </Pane>
  </PaneGroup>
</main>

<DesktopDock />

<style>
  /* These land on PaneForge components, so Svelte cannot scope them; :global
     keeps them next to the layout they describe instead of in styles.css. */
  :global(.shell-group) { flex: 1 1 auto; min-width: 0; min-height: 0; }
  :global(.workspace) {
    position: relative;
    display: flex;
    min-width: 0;
    padding: 4px;
    background: #10121a;
  }

  /* PaneForge marks the rail collapsing/expanding for as long as a flex-grow
     transition runs, so the toggle animates while a drag stays 1:1. Both
     panes move together, or the workspace snaps while the rail slides. */
  :global(.shell-group:has([data-pane-state="collapsing"], [data-pane-state="expanding"]) > [data-pane]) {
    transition: flex-grow var(--t-soft) var(--ease);
  }

  :global(.rail-resizer) {
    position: relative;
    z-index: 5;
    flex: 0 0 6px;
    margin: 0 -3px;
    background: transparent;
    transition: background var(--t-snap) var(--ease);
  }
  :global(.rail-resizer:hover),
  :global(.rail-resizer[data-active]) { background: #777b96; }
  :global(.rail-resizer[data-enabled="false"]) { pointer-events: none; }
  :global(html[data-theme="day"] .rail-resizer:hover),
  :global(html[data-theme="day"] .rail-resizer[data-active]) { background: var(--accent-blue); }
  :global(html[data-theme="day"] .workspace) { background: var(--ink-edge); }
</style>
