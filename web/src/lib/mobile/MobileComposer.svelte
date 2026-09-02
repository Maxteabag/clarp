<script>
  // Phone composer: an overlay the chat button toggles.
  //
  // The soft keyboard shrinks the visual viewport without moving the layout
  // viewport, so a bar fixed to the bottom ends up underneath it. Tracking
  // that offset is the whole reason this is a separate component from the
  // desktop one.
  import { app } from '../../stores/app.svelte.js';
  import { unlockAudio } from '../../stores/audio.svelte.js';
  import { send, sendText } from '../../stores/send.svelte.js';
  import { Timing } from '@core/protocol.js';

  let { open = $bindable() } = $props();

  let value = $state('');
  let inputEl = $state(null);
  let barEl = $state(null);

  function bottomOffset() {
    if (!window.visualViewport) return 0;
    return Math.max(
      0,
      window.innerHeight - window.visualViewport.height - window.visualViewport.offsetTop,
    );
  }

  function updatePosition() {
    if (!barEl) return;
    const off = bottomOffset();
    barEl.style.transform = off > 0 ? `translateY(-${off}px)` : '';
  }

  $effect(() => {
    if (!open || !window.visualViewport) return;
    window.visualViewport.addEventListener('resize', updatePosition);
    window.visualViewport.addEventListener('scroll', updatePosition);
    const t = setTimeout(updatePosition, Timing.CHATBAR_UPDATE_MS);
    return () => {
      clearTimeout(t);
      window.visualViewport.removeEventListener('resize', updatePosition);
      window.visualViewport.removeEventListener('scroll', updatePosition);
    };
  });

  // Deliberately no autofocus on open: showing the composer should not pop
  // the soft keyboard. The user taps the field when they want to type.
  function close() {
    open = false;
    value = '';
    if (barEl) barEl.style.transform = '';
    inputEl?.blur();
  }

  function submit() {
    const text = value.trim();
    if (!text) { close(); return; }
    unlockAudio();
    close();
    send.captureTarget = app.session;
    sendText(text);
  }

  function keydown(e) {
    if (e.key === 'Enter') { e.preventDefault(); submit(); }
    if (e.key === 'Escape') close();
  }
</script>

<div id="chatBar" class="chat-bar" class:hidden={!open} bind:this={barEl}>
  <div class="chat-row">
    <button id="chatClose" class="chat-close" aria-label="Close" onclick={close}>✕</button>
    <input
      id="chatInput"
      class="chat-input"
      type="text"
      autocomplete="off"
      bind:this={inputEl}
      bind:value
      onkeydown={keydown}
    />
    <button class="chat-send" aria-label="Send" onclick={submit}>
      <svg class="btn-icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
        <path fill="currentColor" d="M3.4 20.4 21 12 3.4 3.6 3.4 10l12 2-12 2z"/>
      </svg>
    </button>
  </div>
</div>
