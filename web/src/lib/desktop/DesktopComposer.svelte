<script>
  // Desktop composer: a permanent prompt line inside the dock row. No
  // open/close state and no visual-viewport tracking — there is no soft
  // keyboard to dodge, which is most of what the mobile one does.
  import { app, statusFor } from '../../stores/app.svelte.js';
  import { movedLine } from '@core/agent-moved.js';
  import { unlockAudio } from '../../stores/audio.svelte.js';
  import { composerRef } from '../../stores/composer.svelte.js';
  import { send, sendText } from '../../stores/send.svelte.js';

  import { setInsert } from '../../stores/input.svelte.js';
  import { COPY } from '@core/host-reach.js';
  import { PRIMARY, isDown, reach } from '../../stores/reach.svelte.js';

  let value = $state('');
  // An agent that moved to another Host takes no turn here: the composer is held, the chat
  // stays readable (contract 125).
  let held = $derived((app.tick, movedLine(statusFor(app.session).moved)));

  // The serving Host does not answer, or this device has no network: the chat is its saved
  // messages, the draft stays, and sending waits (docs/design/host-not-answering.md).
  let readOnly = $derived(isDown(PRIMARY) || !reach.deviceOnline);
  let readOnlyNote = $derived(COPY.readOnly(reach.hosts[PRIMARY]?.label || 'the Host'));
  let inputEl = $state(null);

  // Registered rather than bound, because the click-anywhere-refocuses
  // handler lives on the window in App and has no reference to this tree.
  $effect(() => {
    composerRef.focus = () => {
      inputEl?.focus();
      setInsert(true);
    };
    composerRef.isVisible = () => !!inputEl?.offsetParent;
    return () => {
      composerRef.focus = () => {};
      composerRef.isVisible = () => false;
    };
  });

  function submit() {
    const text = value.trim();
    if (!text || readOnly) return;
    // Counts as a user gesture, which is the moment to prime audio.
    unlockAudio();
    value = '';
    send.captureTarget = app.session;
    sendText(text);
  }

  function keydown(e) {
    if (e.key === 'Enter') { e.preventDefault(); submit(); }
    if (e.key === 'Escape') {
      e.preventDefault();
      inputEl?.blur();
    }
  }

  // The field's own focus state is the only thing that sets insert, so the
  // keyboard context can never disagree with where the caret is — clicking in
  // with the mouse counts exactly the same as pressing `i`.
  function onFocus() {
    setInsert(true);
  }

  function onBlur() {
    setInsert(false);
  }
</script>

<div id="chatBar" class="chat-bar">
  {#if held}
    <div class="chat-row"><div class="chat-held" role="status">{held}. It takes no messages here.</div></div>
  {:else}
  <div class="chat-row">
    <input
      id="chatInput"
      class="chat-input"
      type="text"
      autocomplete="off"
      bind:this={inputEl}
      bind:value
      placeholder={readOnly ? readOnlyNote : undefined}
      title={readOnly ? readOnlyNote : undefined}
      onkeydown={keydown}
      onfocus={onFocus}
      onblur={onBlur}
    />
    <button class="chat-send" aria-label="Send" disabled={readOnly} onclick={submit}>
      <svg class="btn-icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
        <path fill="currentColor" d="M3.4 20.4 21 12 3.4 3.6 3.4 10l12 2-12 2z"/>
      </svg>
    </button>
  </div>
  {/if}
</div>
