// Playback: state machine, clip scheduler, the <audio> element, and mute.
//
// The heavy lifting stays in the tested modules under static/lib — this is
// the wiring the old app.js section 4 and 8 did, with the imperative
// `paintMuteBtn()` calls replaced by `$state` the button reads.

import { createStateMachine } from '@core/state-machine.js';
import { createScheduler } from '@core/audio-queue.js';
import { createPlayerAdapter, PLAYER_ADAPTER_VERSION } from '@core/player-adapter.js';
import { ClipStatus, Timing } from '@core/protocol.js';
import { clog } from '../lib/net.js';
import { app, flash, logState } from './app.svelte.js';

const SPEED_RATE = 1.2;

// Tiny silent mp3 (~0.1s) used to "activate" the audio element inside a user
// gesture. iOS Safari requires play() to be called with actual media to grant
// autoplay rights — play() on an empty <audio> resolves but activates nothing.
const SILENT_MP3_DATA_URL = 'data:audio/mpeg;base64,SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjYyLjEyLjEwMAAAAAAAAAAAAAAA//MgxAAAAANIAAAAAExBTUUzLjEwMSAoYmV0YSAzKVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/zIsQnAAADSAAAAABVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//MgxE8AAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/zIMR2AAADSAAAAABVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/8yDEnQAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//MixMQAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVU=';

export const audio = $state({
  muted: localStorage.getItem('pwaMuted') === '1',
  speaking: false,
  unlocked: false,
});

export const machine = createStateMachine({ awaitDeadlineMs: Timing.AWAIT_DEADLINE_MS });
machine.on(ev => logState(ev, machine));

export let player = null;
export let playerAdapter = null;
export let scheduler = null;

export let lastAudioTs = parseInt(localStorage.getItem('lastAudioTs') || '0', 10);

export function bumpLastAudioTs(ts) {
  if (ts > lastAudioTs) {
    lastAudioTs = ts;
    try { localStorage.setItem('lastAudioTs', String(ts)); } catch (_) {}
  }
}

function ackClip(clip, status, error) {
  if (!clip || !clip.url || !ClipStatus.VALID.has(status)) return;
  try {
    fetch('/clips/ack', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        clip_id: clip.clip_id || undefined,
        url: clip.url,
        status,
        error: error || undefined,
        trace_id: clip.trace_id || undefined,
      }),
      keepalive: true,
    }).catch(() => {});
  } catch (_) {}
}

export function initAudio(el) {
  player = el;
  playerAdapter = createPlayerAdapter(player, {
    speed: SPEED_RATE,
    log: clog,
    showSpeaking: (on) => { audio.speaking = !!on; },
  });
  scheduler = createScheduler({
    machine,
    player: playerAdapter,
    currentSession: () => app.session,
    onClipStatus: ackClip,
  });
  clog('playerAdapterLoaded', PLAYER_ADAPTER_VERSION);
}

export { PLAYER_ADAPTER_VERSION };

export function unlockAudio() {
  if (audio.unlocked || !player) return;
  const prevSrc = player.src;
  const wasMuted = player.muted;
  player.muted = true;
  player.src = SILENT_MP3_DATA_URL;
  const p = player.play();
  if (p && p.then) {
    p.then(() => {
      audio.unlocked = true;
      player.pause();
      player.currentTime = 0;
      player.muted = wasMuted;
      // Leaving the silent source in place keeps the element warm; the
      // adapter overwrites src when the first real clip arrives.
      if (prevSrc && prevSrc !== window.location.href) player.src = prevSrc;
      clog('audioUnlocked', 'ok');
    }).catch((err) => {
      audio.unlocked = false;
      player.muted = wasMuted;
      clog('audioUnlockFail', (err && err.name) || String(err));
    });
  }
}

// Mute is a client preference. Each send carries the synthesis policy, so one
// phone cannot silence other clients on the same server.
export function setMuted(on) {
  audio.muted = !!on;
  try { localStorage.setItem('pwaMuted', audio.muted ? '1' : '0'); } catch (_) {}
  if (audio.muted) {
    // Drop anything playing or queued the moment mute engages.
    try { playerAdapter?.interrupt(); } catch (_) {}
    try { scheduler?.silence(); } catch (_) {}
  }
  clog('pwaMuteToggled', audio.muted ? 'on' : 'off');
  flash(audio.muted ? 'Audio muted' : 'Audio on', 1000);
}

/** Single tap: stop the clip that is playing now, keep the mute preference. */
export function silenceNow() {
  playerAdapter?.interrupt();
  const dropped = scheduler?.silence();
  clog('muteClicked', `dropped=${dropped}`);
  flash('Stopped', 600);
}

export function tone(hz, dur, vol = 0.10) {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = hz;
    gain.gain.value = 0.001;
    osc.connect(gain).connect(ctx.destination);
    const t = ctx.currentTime;
    gain.gain.exponentialRampToValueAtTime(vol, t + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.001, t + dur);
    osc.start(t); osc.stop(t + dur + 0.02);
    setTimeout(() => ctx.close().catch(() => {}), Timing.AUDIO_CONTEXT_CLOSE_MS);
  } catch (_) {}
}

export const tick  = (hz, dur) => tone(hz, dur);
export const chime = ok => tone(ok ? 880 : 220, 0.18, 0.18);
