<script>
  // Mute, stop and mic. Shared by both docks: the buttons and their gestures
  // are identical on desktop and phone — only their surroundings differ, so
  // splitting the docks should not mean duplicating this.
  import { audio, setMuted, silenceNow, unlockAudio } from '../stores/audio.svelte.js';
  import { mic, micTap, startAlwaysOn } from '../stores/mic.svelte.js';
  import { app, statusFor } from '../stores/app.svelte.js';
  import { stopAgentTurn } from '../stores/send.svelte.js';

  let status = $derived(statusFor(app.session));

  // Mute: a tap silences the clip that is playing; a long press toggles the
  // persistent preference.
  const LONG_PRESS_MS = 500;
  let muteTimer = null;
  let muteLongFired = false;

  function muteDown() {
    muteLongFired = false;
    clearTimeout(muteTimer);
    muteTimer = setTimeout(() => {
      muteLongFired = true;
      setMuted(!audio.muted);
    }, LONG_PRESS_MS);
  }
  function muteCancel() { clearTimeout(muteTimer); }
  function muteClick() {
    if (muteLongFired) { muteLongFired = false; return; }
    silenceNow();
  }

  // Mic: a tap records one utterance; a 1s hold arms always-on listening.
  let micTimer = null;
  let micHoldFired = false;

  function micDown(e) {
    if (e.pointerType !== 'touch' && e.pointerType !== 'pen' && e.button !== 0) return;
    unlockAudio();
    micHoldFired = false;
    clearTimeout(micTimer);
    micTimer = setTimeout(() => { micHoldFired = true; startAlwaysOn(); }, 1000);
  }
  function micRelease() {
    clearTimeout(micTimer);
    if (micHoldFired) { micHoldFired = false; return; }
    micTap();
  }
</script>

<button
  class="mute-audio-btn"
  class:muted={audio.muted}
  class:active={audio.speaking}
  aria-label={audio.muted ? 'Unmute' : 'Stop audio'}
  onpointerdown={muteDown}
  onpointerup={muteCancel}
  onpointercancel={muteCancel}
  onpointerleave={muteCancel}
  onclick={muteClick}
>
  <svg class="btn-icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
    <path fill="currentColor" d="M11 5 6.5 9H3v6h3.5L11 19V5z"/>
    <path stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none" d="M15.5 9.5l5 5m0-5l-5 5"/>
  </svg>
</button>

<button class="stop-btn" class:busy={!!status.busy} aria-label="Stop agent" onclick={stopAgentTurn}>
  <svg class="btn-icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
    <rect x="6.5" y="6.5" width="11" height="11" rx="1.5" fill="currentColor"/>
  </svg>
</button>

<button
  id="mic"
  class="mic"
  class:recording={mic.recording}
  class:capturing={mic.capturing}
  aria-label="Talk"
  onpointerdown={micDown}
  onpointerup={micRelease}
  onpointercancel={() => { clearTimeout(micTimer); micHoldFired = false; }}
>
  <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true">
    <path fill="currentColor" d="M12 14a3 3 0 0 0 3-3V5a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"/>
  </svg>
</button>
