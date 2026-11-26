// Wraps an HTMLAudioElement with the {play(clip) → Promise<{premature, ...}>}
// shape the audio-queue scheduler expects.
//
// Bugs this module pins:
//   R4: the original adapter awaited canplaythrough with no fallback — if the
//       event raced past us, the whole queue hung. Now we also listen for
//       'error' and cap the wait with a timeout.
//
// Diagnostics: every log call carries a third argument — a plain-object
// snapshot of the element + environment (see `snapshot()`), so a stall,
// freeze, jump, premature end or play() rejection can be read back from the
// eventlog together with volume, buffer level, readyState, network type and
// the timing deltas since play() was entered. Observation only: nothing in
// here changes what plays or when.

// Bumped whenever this module's contract changes. Visible in the eventlog
// as `client.playerAdapterLoaded ver=N` on import so we can tell from a
// trace whether the iPhone's PWA actually picked up our latest JS or is
// running a stale SW-cached copy.
export const PLAYER_ADAPTER_VERSION = 'stage8-play-diagnostics';

/** Stall events are rate-limited to one log per this many ms (count still increments). */
export const STALL_LOG_MIN_INTERVAL_MS = 500;
/** No 'timeupdate' for this long while !paused && !ended → 'playFreeze'. */
export const FREEZE_MS = 1500;
/** currentTime moving forward more than this beyond wall-clock expectation → 'playJump'. */
export const JUMP_FORWARD_S = 1.5;
/** currentTime moving backwards by more than this → 'playJump'. */
export const JUMP_BACKWARD_S = 0.5;

/**
 * @typedef {{ url: string, session: string, ts?: number, streamable?: boolean,
 *             stream_url?: string, playlist_url?: string, clip_id?: number|string|null,
 *             trace_id?: string, delivery?: string, audio_format?: string|object|null }} Clip
 *   `url` is the plain <audio src> every browser can play. `playlist_url`
 *   (HLS) or a `/clips/<id>/stream` `stream_url` (chunked HTTP) are preferred
 *   when present because they start before synthesis has finished.
 *   `audio_format` is whatever the server advertised for the clip (string
 *   mime or a {container, encoding, sample_rate, channels} dict) — carried
 *   through purely so format-mismatch corruption is diagnosable.
 */

const now = () => {
  try {
    const p = globalThis.performance;
    if (p && typeof p.now === 'function') return p.now();
  } catch (_) {}
  return Date.now();
};

const round = (v, digits = 3) => {
  if (v == null || Number.isNaN(v)) return null;
  if (typeof v !== 'number' || !Number.isFinite(v)) return v;
  const f = 10 ** digits;
  return Math.round(v * f) / f;
};

/**
 * Summarise `el.buffered` (a TimeRanges) as [[start, end], ...] plus how
 * many ms of media are buffered ahead of currentTime. Never throws.
 */
function bufferedInfo(el) {
  const out = { buffered: [], buffered_ahead_ms: null };
  try {
    const b = el?.buffered;
    const n = b?.length ?? 0;
    const cur = Number(el?.currentTime);
    for (let i = 0; i < n; i++) {
      const s = b.start(i);
      const e = b.end(i);
      out.buffered.push([round(s), round(e)]);
      if (Number.isFinite(cur) && s <= cur && cur <= e) {
        out.buffered_ahead_ms = Math.round((e - cur) * 1000);
      }
    }
  } catch (_) {}
  return out;
}

/**
 * Plain-object picture of the audio element and its surroundings at this
 * instant. Everything optional-chained; never throws. `extra` is merged in
 * last so callers can add clip_id / trace_id / src_kind / t_since_play_ms
 * and any per-clip counters.
 *
 * @param {HTMLAudioElement} el
 * @param {object} [extra]
 */
export function snapshot(el, extra) {
  const out = {};
  try {
    out.currentTime = round(el?.currentTime);
    out.duration = round(el?.duration);
    out.readyState = el?.readyState ?? null;
    out.networkState = el?.networkState ?? null;
    out.paused = el?.paused ?? null;
    out.ended = el?.ended ?? null;
    out.playbackRate = round(el?.playbackRate);
    out.volume = round(el?.volume);
    out.muted = el?.muted ?? null;
    Object.assign(out, bufferedInfo(el));
  } catch (_) {}
  try {
    out.visibility = globalThis.document?.visibilityState ?? null;
  } catch (_) { out.visibility = null; }
  try {
    const nav = globalThis.navigator;
    out.online = nav?.onLine ?? null;
    const c = nav?.connection;
    if (c) {
      out.net_type = c.effectiveType ?? null;
      out.net_rtt = c.rtt ?? null;
      out.net_downlink = c.downlink ?? null;
    }
  } catch (_) {}
  if (extra && typeof extra === 'object') {
    try { Object.assign(out, extra); } catch (_) {}
  }
  return out;
}

/**
 * Per-clip observation: timings, counters and the element listeners that
 * feed them. Returns handles the play() body uses; `end()` is idempotent
 * and detaches everything.
 */
function observeClip(el, clip, srcKind, speed, log, opts) {
  const t0 = now();
  const freezeMs = opts.freezeMs ?? FREEZE_MS;
  const freezeCheckMs = opts.freezeCheckMs ?? Math.max(100, Math.floor(freezeMs / 3));
  const timings = {
    first_progress: null, canplay: null, canplaythrough: null,
    playing: null, ended: null,
  };
  const counts = {
    stall: 0, freeze: 0, jump: 0, suspend: 0, error: 0,
    rate_change: 0, volume_change: 0, pause: 0, timeupdate: 0,
  };
  let maxStallMs = 0;
  let stallStartedAt = null;
  let lastStallLogAt = -Infinity;
  let lastTimeupdateAt = null;   // wall time of last timeupdate (or playing)
  let lastCurrentTime = null;    // el.currentTime at last timeupdate
  let frozen = false;
  let done = false;
  let outcome = null;
  let lastError = null;

  const since = t => (t == null ? null : Math.round(t - t0));

  function snap(more) {
    return snapshot(el, {
      clip_id: clip?.clip_id ?? null,
      trace_id: clip?.trace_id || null,
      src_kind: srcKind,
      delivery: clip?.delivery || null,
      audio_format: clip?.audio_format ?? null,
      t_since_play_ms: Math.round(now() - t0),
      t_first_progress_ms: since(timings.first_progress),
      t_canplay_ms: since(timings.canplay),
      t_canplaythrough_ms: since(timings.canplaythrough),
      t_playing_ms: since(timings.playing),
      t_ended_ms: since(timings.ended),
      stall_count: counts.stall,
      freeze_count: counts.freeze,
      jump_count: counts.jump,
      max_stall_ms: Math.round(maxStallMs),
      ...(more || {}),
    });
  }

  function endStall() {
    if (stallStartedAt == null) return;
    const ms = now() - stallStartedAt;
    if (ms > maxStallMs) maxStallMs = ms;
    stallStartedAt = null;
  }

  function onStall(type) {
    counts.stall++;
    if (stallStartedAt == null) stallStartedAt = now();
    const t = now();
    if (t - lastStallLogAt >= STALL_LOG_MIN_INTERVAL_MS) {
      lastStallLogAt = t;
      log('playStall', `${type} n=${counts.stall}`, snap({ stall_event: type }));
    }
  }

  const onWaiting = () => onStall('waiting');
  const onStalled = () => onStall('stalled');
  const onSuspend = () => { counts.suspend++; };
  const onError = () => {
    counts.error++;
    let code = null, message = null;
    try { code = el?.error?.code ?? null; message = el?.error?.message ?? null; } catch (_) {}
    lastError = { code, message };
    log('playError', `code=${code} ${message || ''}`.trim(),
      snap({ error_code: code, error_message: message }));
  };
  const onRateChange = () => {
    counts.rate_change++;
    let rate = null;
    try { rate = el?.playbackRate; } catch (_) {}
    log('playRateChange', `rate=${rate} expected=${speed}`, snap());
  };
  const onVolumeChange = () => {
    counts.volume_change++;
    let vol = null, muted = null;
    try { vol = el?.volume; muted = el?.muted; } catch (_) {}
    log('playVolumeChange', `vol=${vol} muted=${muted}`, snap());
  };
  const onPause = () => { counts.pause++; };
  const onProgress = () => { if (timings.first_progress == null) timings.first_progress = now(); };
  const onCanPlay = () => { if (timings.canplay == null) timings.canplay = now(); };
  const onCanPlayThrough = () => { if (timings.canplaythrough == null) timings.canplaythrough = now(); };
  const onPlaying = () => {
    const t = now();
    if (timings.playing == null) timings.playing = t;
    endStall();
    lastTimeupdateAt = t;
    frozen = false;
  };
  const onEnded = () => { if (timings.ended == null) timings.ended = now(); };
  const onTimeUpdate = () => {
    counts.timeupdate++;
    const t = now();
    let cur = null, rate = null;
    try { cur = Number(el?.currentTime); rate = Number(el?.playbackRate); } catch (_) {}
    if (Number.isFinite(cur)) {
      if (lastCurrentTime != null && cur !== lastCurrentTime) endStall();
      if (lastCurrentTime != null && lastTimeupdateAt != null && Number.isFinite(rate)
          && Math.abs(rate - speed) < 0.05) {
        const delta = cur - lastCurrentTime;
        const expected = ((t - lastTimeupdateAt) / 1000) * rate;
        const excess = delta - expected;
        if (delta < -JUMP_BACKWARD_S || excess > JUMP_FORWARD_S) {
          counts.jump++;
          log('playJump', `from=${round(lastCurrentTime)} to=${round(cur)} delta=${round(delta)}`,
            snap({ jump_from: round(lastCurrentTime), jump_to: round(cur),
                   jump_delta_s: round(delta), jump_wall_ms: Math.round(t - lastTimeupdateAt) }));
        }
      }
      lastCurrentTime = cur;
    }
    lastTimeupdateAt = t;
    frozen = false;
  };

  const listeners = [
    ['waiting', onWaiting], ['stalled', onStalled], ['suspend', onSuspend],
    ['error', onError], ['ratechange', onRateChange], ['volumechange', onVolumeChange],
    ['pause', onPause], ['progress', onProgress], ['loadeddata', onProgress],
    ['canplay', onCanPlay], ['canplaythrough', onCanPlayThrough],
    ['playing', onPlaying], ['ended', onEnded], ['timeupdate', onTimeUpdate],
  ];
  for (const [type, fn] of listeners) {
    try { el.addEventListener(type, fn); } catch (_) {}
  }

  // Freeze watchdog: the element claims to be playing but currentTime has
  // not moved. Armed once playback has actually started (see markPlaying).
  const freezeTimer = setInterval(() => {
    if (done || lastTimeupdateAt == null) return;
    let paused = null, ended = null;
    try { paused = el?.paused; ended = el?.ended; } catch (_) {}
    if (paused !== false || ended === true) return;
    const silent = now() - lastTimeupdateAt;
    if (silent > freezeMs && !frozen) {
      frozen = true;
      counts.freeze++;
      log('playFreeze', `silent=${Math.round(silent)}ms n=${counts.freeze}`,
        snap({ silent_ms: Math.round(silent) }));
    }
  }, freezeCheckMs);
  try { freezeTimer?.unref?.(); } catch (_) {}

  /** play() resolved — arm the freeze watchdog even if 'playing' never fires. */
  function markPlaying() {
    const t = now();
    if (timings.playing == null) timings.playing = t;
    if (lastTimeupdateAt == null) lastTimeupdateAt = t;
  }

  /** Terminal: detach listeners, emit the per-clip summary once. */
  function end(how, info) {
    if (done) return;
    done = true;
    outcome = how;
    endStall();
    clearInterval(freezeTimer);
    for (const [type, fn] of listeners) {
      try { el.removeEventListener(type, fn); } catch (_) {}
    }
    const tEnd = now();
    let dur = null;
    try { dur = Number(el?.duration); } catch (_) {}
    const expectedMs = Number.isFinite(dur) && dur > 0 ? Math.round((dur / speed) * 1000) : null;
    const wallMs = Math.round(tEnd - t0);
    const playToEndMs = timings.playing == null ? null : Math.round(tEnd - timings.playing);
    log('playStats',
      `outcome=${how} premature=${!!info?.premature} stalls=${counts.stall} `
        + `freezes=${counts.freeze} jumps=${counts.jump} wall=${wallMs}ms expected=${expectedMs ?? '?'}ms`,
      snap({
        outcome: how,
        premature: !!info?.premature,
        expected_duration_s: round(dur),
        expected_wall_ms: expectedMs,
        wall_elapsed_ms: wallMs,
        play_to_end_ms: playToEndMs,
        duration_ms: wallMs,
        suspend_count: counts.suspend,
        error_count: counts.error,
        rate_change_count: counts.rate_change,
        volume_change_count: counts.volume_change,
        pause_count: counts.pause,
        timeupdate_count: counts.timeupdate,
        last_error: lastError,
        ...(info?.extra || {}),
      }));
  }

  return {
    snap, markPlaying, end,
    get done() { return done; },
    get outcome() { return outcome; },
    get t0() { return t0; },
  };
}

/**
 * @param {HTMLAudioElement} audioEl
 * @param {object} [opts]
 * @param {number} [opts.speed]            playbackRate to apply (default 1.2)
 * @param {number} [opts.loadTimeoutMs]    fallback after canplaythrough never fires
 * @param {number} [opts.freezeMs]         timeupdate silence before 'playFreeze' (default 1500)
 * @param {number} [opts.freezeCheckMs]    freeze watchdog poll interval
 * @param {(event: string, detail?: string, extra?: object) => void} [opts.log]
 *   diagnostics hook. `extra` is the snapshot() object for that moment.
 * @param {(on: boolean) => void} [opts.showSpeaking]  — UI hook
 */
export function createPlayerAdapter(audioEl, opts = {}) {
  const speed = opts.speed ?? 1.2;
  const loadTimeoutMs = opts.loadTimeoutMs ?? 5000;
  const log = opts.log || (() => {});
  const showSpeaking = opts.showSpeaking || (() => {});
  let active = null;   // observer for the clip currently owning the element

  return {
    async play(clip) {
      // A previous play() may still be pending (silence() releases the
      // scheduler without resolving it). Its listeners must not bleed into
      // this clip's counters.
      if (active && !active.done) active.end('superseded');

      let directStream = false;
      let srcKind = 'url';
      let src = clip.url;
      if (clip.playlist_url) {
        // HLS delivery path. iOS Safari plays HLS natively via plain
        // <audio src=...> — no MSE, no Range hacks, no chunked-TE
        // quirks. The whole iOS streaming-bug surface goes away here.
        // Desktop browsers fall through to hls.js territory in a future
        // pass; today's desktop tests assert this path runs unchanged.
        const playlist = normalizePlaylistUrl(clip.playlist_url);
        if (playlist) { directStream = true; srcKind = 'playlist'; src = playlist; }
      } else if (clip.streamable && clip.stream_url) {
        const directUrl = normalizeDirectStreamUrl(clip.stream_url);
        if (directUrl) { directStream = true; srcKind = 'stream'; src = directUrl; }
      }

      const obs = observeClip(audioEl, clip, srcKind, speed, log, opts);
      active = obs;
      log('playStart', clip.url, obs.snap({ src }));
      if (srcKind === 'playlist') log('playHls', src, obs.snap());
      else if (srcKind === 'stream') log('playDirectStream', src, obs.snap());
      audioEl.src = src;
      // Set BOTH playbackRate and defaultPlaybackRate. The src setter
      // auto-triggers a load on its own — calling .load() explicitly
      // RESETS iOS Safari's user-activation state on the element, which
      // burns the autoplay unlock from unlockAudio() and any prior play()
      // inside a user gesture. NotAllowedError on every subsequent clip.
      // Trust the src setter to load.
      audioEl.playbackRate = speed;
      audioEl.defaultPlaybackRate = speed;
      showSpeaking(true);

      if (!directStream && audioEl.readyState < 3) {
        log('playWaiting', `rs=${audioEl.readyState}`, obs.snap());
        await new Promise(res => {
          const cleanup = () => {
            audioEl.removeEventListener('canplaythrough', onReady);
            audioEl.removeEventListener('error', onFail);
            clearTimeout(timer);
          };
          const onReady = () => { cleanup(); res(); };
          const onFail  = () => {
            cleanup();
            log('playLoadFail', `code=${audioEl.error && audioEl.error.code}`, obs.snap());
            res();
          };
          audioEl.addEventListener('canplaythrough', onReady, { once: true });
          audioEl.addEventListener('error',         onFail,  { once: true });
          const timer = setTimeout(() => {
            cleanup();
            log('playLoadTimeout', `rs=${audioEl.readyState}`, obs.snap());
            res();
          }, loadTimeoutMs);
        });
      }

      log('playCalling', `rs=${audioEl.readyState}`, obs.snap());
      try {
        const p = audioEl.play();
        if (p && p.then) await p;
        obs.markPlaying();
        log('playOk', String(audioEl.currentSrc || '').split('/').pop(),
          obs.snap({ duration_ms: Math.round(now() - obs.t0) }));
      } catch (err) {
        log('playFail', err && err.name,
          obs.snap({ error_name: err && err.name, error_message: err && err.message,
                     duration_ms: Math.round(now() - obs.t0) }));
        showSpeaking(false);
        obs.end('playFail', { premature: true });
        // Treat as premature so the scheduler can re-queue (iOS NotAllowed).
        return { premature: true, duration: 0, currentTime: 0 };
      }

      return new Promise(resolve => {
        // Safety timer: if 'ended' never fires (seen on iOS after backgrounding
        // and when the audio element stalls mid-clip) the scheduler would
        // stay busy forever and every subsequent clip would silently queue.
        // Cap on duration/speed plus a generous 4 s slack; fall back to 30 s
        // when duration isn't known yet. Note: chunked-transfer clips expose
        // duration=Infinity, which is truthy — must use isFinite, not ||,
        // or setTimeout(_, Infinity) clamps to 1ms and the clip is dropped.
        const dur0 = audioEl.duration;
        const cap = Number.isFinite(dur0) && dur0 > 0
          ? (dur0 / speed) * 1000 + 4000
          : 30000;
        const safety = setTimeout(() => {
          log('playEndTimeout', `cap=${cap}ms cur=${audioEl.currentTime}`,
            obs.snap({ cap_ms: Math.round(cap), duration_ms: Math.round(now() - obs.t0) }));
          finish(false, 'timeout');
        }, cap);
        const finish = (premature, how) => {
          clearTimeout(safety);
          audioEl.removeEventListener('ended', onEnd);
          audioEl.removeEventListener('error', onError);
          showSpeaking(false);
          obs.end(how, { premature, extra: { cap_ms: Math.round(cap) } });
          resolve({
            premature,
            duration: audioEl.duration || 0,
            currentTime: audioEl.currentTime || 0,
          });
        };
        const onEnd = () => {
          const dur = audioEl.duration || 0;
          const cur = audioEl.currentTime || 0;
          finish(dur > 1 && cur > 0 && cur < dur * 0.85, 'ended');
        };
        const onError = () => finish(false, 'error');
        audioEl.addEventListener('ended',  onEnd,   { once: true });
        audioEl.addEventListener('error',  onError, { once: true });
      });
    },

    interrupt() {
      try { audioEl.pause(); } catch (_) {}
      try { audioEl.currentTime = 0; } catch (_) {}
      showSpeaking(false);
    },
  };
}

function normalizeDirectStreamUrl(url) {
  const s = String(url || '');
  if (!s) return '';
  if (s.startsWith('/clips/') && s.endsWith('/stream')) return s;
  if (/^https?:\/\/[^/]+\/clips\/\d+\/stream$/.test(s)) return s;
  if (/^wss?:\/\/[^/]+\/clips\/\d+\/stream$/.test(s)) {
    return s.replace(/^ws/, 'http');
  }
  return '';
}

function normalizePlaylistUrl(url) {
  const s = String(url || '');
  if (!s) return '';
  if (s.startsWith('/clips/') && s.endsWith('/playlist.m3u8')) return s;
  if (/^https?:\/\/[^/]+\/clips\/\d+\/playlist\.m3u8$/.test(s)) return s;
  return '';
}
