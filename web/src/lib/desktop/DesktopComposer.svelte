<script>
  // Desktop composer: a permanent prompt line inside the dock row. No
  // open/close state and no visual-viewport tracking — there is no soft
  // keyboard to dodge, which is most of what the mobile one does.
  import { app } from '../../stores/app.svelte.js';
  import { unlockAudio } from '../../stores/audio.svelte.js';
  import { composerRef } from '../../stores/composer.svelte.js';
  import { send, sendText } from '../../stores/send.svelte.js';
  import { panesState } from '../../stores/panes.svelte.js';

  import { setInsert } from '../../stores/input.svelte.js';

  // A draft belongs to both the pane and its current recipient. Moving pane
  // focus swaps the visible draft instead of carrying text to another agent.
  let drafts = $state({});
  let value = $state('');
  let shownKey = $state('');
  let inputEl = $state(null);
  let draftKey = $derived(`${panesState.tree.activeId}\u0000${app.session}`);

  $effect(() => {
    const key = draftKey;
    if (key === shownKey) return;
    shownKey = key;
    value = drafts[key] || '';
  });

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
    const key = draftKey;
    const text = String(drafts[key] || (key === shownKey ? value : '')).trim();
    if (!text) return;
    // Counts as a user gesture, which is the moment to prime audio.
    unlockAudio();
    const target = app.session;
    drafts[key] = '';
    if (key === shownKey) value = '';
    send.captureTarget = target;
    sendText(text);
  }

  function onInput(e) {
    value = e.currentTarget.value;
    drafts[shownKey || draftKey] = value;
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
  <div class="chat-row">
    <input
      id="chatInput"
      class="chat-input"
      type="text"
      autocomplete="off"
      bind:this={inputEl}
      value={value}
      oninput={onInput}
      onkeydown={keydown}
      onfocus={onFocus}
      onblur={onBlur}
    />
    <button class="chat-send" aria-label="Send" onclick={submit}>
      <svg class="btn-icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
        <path fill="currentColor" d="M3.4 20.4 21 12 3.4 3.6 3.4 10l12 2-12 2z"/>
      </svg>
    </button>
  </div>
</div>
