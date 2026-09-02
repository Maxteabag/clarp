// Audio playback scheduler — given a state machine and clips, decides what
// plays next.
//
// Bugs this module pins (see TESTS.md):
//   B3 : iOS premature `ended` event resumes the same clip mid-playback.
//   B6 : clips older than `lastAudioTs` are skipped (SSE replay).
//   B15: in `awaiting`, only the addressee's clip plays.
//   B16: state-driven, no safety-net setInterval needed.

import { States } from './state-machine.js';
import { ClipStatus } from './protocol.js';

const MAX_PREMATURE_RETRIES = 2;

/**
 * @typedef {Object} Clip
 * @property {string} url
 * @property {string} session
 * @property {number} ts          // epoch ms — also encoded in filename
 */

/**
 * @typedef {Object} Player
 * @property {(clip: Clip) => Promise<{premature: boolean, duration: number, currentTime: number}>} play
 *   Plays the clip. Resolves with playback info when the clip ends.
 *   `premature` is true if `ended` fired before ~85% of duration (iOS bug).
 */

function _diagLog(event, detail) {
  try {
    fetch('/clog', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({event, detail: String(detail || '')}),
      keepalive: true,
    }).catch(()=>{});
  } catch (_) {}
}

export function createScheduler({
  machine,
  player,
  currentSession,
  log = _diagLog,
  onClipStatus = () => {},
}) {
  const queue = [];
  const seen = new Set();
  let lastAudioTs = 0;
  let busy = false;
  let getCurrentSession = currentSession;

  function setCurrentSession(fn) { getCurrentSession = fn; }

  function ingest(clip) {
    if (!clip || !clip.url) return { accepted: false, reason: 'empty' };
    const key = clip.url;
    if (seen.has(key)) return { accepted: false, reason: 'duplicate' };
    if (clip.ts && lastAudioTs && clip.ts <= lastAudioTs) {
      seen.add(key);
      return { accepted: false, reason: 'old' };
    }
    seen.add(key);
    if (clip.ts > lastAudioTs) lastAudioTs = clip.ts;
    queue.push(clip);
    onClipStatus(clip, ClipStatus.QUEUED);
    tick();
    return { accepted: true };
  }

  function pick() {
    if (queue.length === 0) { log('pickEmpty'); return null; }
    const state = machine.state;
    if (state === States.RECORDING || state === States.TRANSCRIBING) {
      log('pickSkip', state);
      return null;
    }
    if (state === States.AWAITING && !machine.awaitExpired()) {
      const idx = queue.findIndex(c => c.session === machine.expectingFrom);
      if (idx < 0) {
        log('pickAwait', `for=${machine.expectingFrom} qlen=${queue.length}`);
        return null;
      }
      machine.settle();
      return queue.splice(idx, 1)[0];
    }
    if (state === States.AWAITING) machine.settle();
    const cur = getCurrentSession ? getCurrentSession() : '';
    const idx = queue.findIndex(c => c.session === cur);
    return queue.splice(idx >= 0 ? idx : 0, 1)[0];
  }

  async function tick() {
    log('tick', `busy=${busy} qlen=${queue.length} state=${machine.state}`);
    if (busy) return;
    const next = pick();
    if (!next) return;
    log('tickPicked', `${next.url} session=${next.session}`);
    onClipStatus(next, ClipStatus.PLAY_START);
    busy = true;
    try {
      let r;
      let playFailed = false;
      try {
        r = await player.play(next);
      } catch (err) {
        // Player threw — log and treat the clip as finished so the queue
        // keeps draining. Without this catch, the rejection escapes tick()
        // as an unhandled promise rejection AND leaves busy=true.
        log('playRejected', (err && err.message) || String(err));
        onClipStatus(next, ClipStatus.PLAY_FAIL, (err && err.message) || String(err));
        playFailed = true;
        r = { premature: false };
      }
      // iOS premature: iOS fires `ended` early on a stalled stream, or `play()`
      // rejects with NotAllowedError when the autoplay gate hasn't opened. We
      // retry — but cap it. Without the cap, a NotAllowed clip loops forever
      // and every clip after it queues behind, flooding the user once they
      // finally interact.
      if (r && r.premature) {
        next._retries = (next._retries || 0) + 1;
        if (next._retries <= MAX_PREMATURE_RETRIES) {
          queue.unshift(next);
        } else {
          log('playGiveUp', `${next.url} retries=${next._retries}`);
          onClipStatus(next, ClipStatus.PLAY_FAIL, `premature retries=${next._retries}`);
        }
      } else if (!playFailed) {
        onClipStatus(next, ClipStatus.PLAY_OK);
      }
    } finally {
      busy = false;
      // Re-tick in case more clips arrived while we were busy.
      if (queue.length) tick();
    }
  }

  // Re-tick when state machine transitions (e.g. recording → transcribing).
  machine.on(() => { if (!busy) tick(); });

  /**
   * "Shut up" — the user wants to stop the current playback and forget
   * about anything queued. Drops the queue AND releases the busy lock so
   * a future clip can play even when the in-flight clip's play() promise
   * never resolves (iOS quirks: ended doesn't fire, duration is bogus,
   * MSE buffer stalls). Returns the number of queued clips dropped.
   *
   * The currently-playing audio element is NOT touched here — callers
   * (usually the mute button handler) call playerAdapter.interrupt()
   * alongside silence() to also pause/reset the element.
   *
   * Why we release `busy` despite the in-flight clip's promise still
   * pending: the scheduler's `busy` flag is a serialization gate, not a
   * record of "the audio is playing." When the user signals they're done
   * with the current audio, that serialization gate should clear so the
   * next clip isn't waiting on a promise the user no longer cares about.
   */
  function silence() {
    const dropped = queue.length;
    queue.length = 0;
    busy = false;
    if (dropped) log('queueSilenced', `dropped=${dropped}`);
    return dropped;
  }

  /** Drop clips older than `olderThanMs`. Returns the number dropped. */
  function flushOlderThan(olderThanMs) {
    const cutoff = Date.now() - olderThanMs;
    const before = queue.length;
    for (let i = queue.length - 1; i >= 0; i--) {
      if ((queue[i].ts || 0) < cutoff) queue.splice(i, 1);
    }
    const dropped = before - queue.length;
    if (dropped) log('queueFlushed', `dropped=${dropped}`);
    return dropped;
  }

  return {
    ingest,
    tick,
    setCurrentSession,
    flushOlderThan,
    silence,
    get queueLength() { return queue.length; },
    get lastAudioTs() { return lastAudioTs; },
    _peek() { return queue.slice(); },   // for tests only
  };
}
