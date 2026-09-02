<script>
  let { direction = 'vertical', onResize, onReset } = $props();

  let dragging = $state(false);
  let startPos = 0;

  function onPointerDown(e) {
    e.preventDefault();
    dragging = true;
    startPos = direction === 'vertical' ? e.clientX : e.clientY;

    function onPointerMove(moveEvent) {
      if (!dragging) return;
      const currentPos = direction === 'vertical' ? moveEvent.clientX : moveEvent.clientY;
      const deltaPx = currentPos - startPos;
      startPos = currentPos;
      // Convert pixel delta into ~percentage ratio delta
      const deltaRatio = deltaPx / (direction === 'vertical' ? window.innerWidth : window.innerHeight);
      onResize?.(deltaRatio);
    }

    function onPointerUp() {
      dragging = false;
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
    }

    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
  }
</script>

<div
  class="split-resizer {direction}"
  class:dragging
  role="separator"
  aria-orientation={direction}
  tabindex="-1"
  onpointerdown={onPointerDown}
  ondblclick={onReset}
>
  <div class="resizer-handle"></div>
</div>

<style>
  .split-resizer {
    position: relative;
    z-index: 5;
    background: rgba(255, 255, 255, 0.08);
    transition: background 0.15s ease;
    flex-shrink: 0;
    user-select: none;
  }
  .split-resizer.vertical {
    width: 6px;
    margin: 0 -3px;
    cursor: col-resize;
  }
  .split-resizer.horizontal {
    height: 6px;
    margin: -3px 0;
    cursor: row-resize;
  }
  .split-resizer:hover, .split-resizer.dragging {
    background: #7aa2f7;
  }
  .resizer-handle {
    width: 100%;
    height: 100%;
  }
</style>
