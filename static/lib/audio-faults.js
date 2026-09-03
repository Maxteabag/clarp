// Audio fault monitor — records every instance where playback went wrong,
// with enough surrounding state to explain it afterwards.
//
// "Corrupted audio" is never one thing. What the user hears as a stutter, a
// skipped phrase, a clip that cuts off or a clip that never starts comes from
// different failures underneath: the element ran out of buffered data
// (`waiting` / `stalled`), the decoder gave up (`error`), iOS fired `ended`
// early, the playhead jumped, or the playback rate silently reset. This module
// watches the element for the whole life of one clip and turns each of those
// into a structured `audioFault` record. At the end of the clip it emits one
// `audioClipSummary` so healthy clips leave a baseline to compare against.
//
// Every record carries three groups of context:
//   - position:   where in the clip it happened and what the element believed
//                 (readyState, networkState, buffered runway, rate, volume)
//   - latency:    broadcast -> queued -> play-start -> first sound, in ms
//   - conditions: whatever the host supplies — network, visibility, mic state
//                 and level, queue depth, state machine — captured at the
//                 moment of the fault, not reconstructed later.
//
// Pure: no DOM globals, no fetch. The host passes `now`, `emit` and a
// `conditions` callback, which is what makes this testable with a fake
// element and what keeps the wiring in one place (web/src/stores/audio).

export const FaultKind = Object.freeze({
  STALL: 'stall',                 // waiting/stalled, then playing again
  DECODE_ERROR: 'decode-error',   // MediaError mid-clip
  LOAD_FAIL: 'load-fail',         // error before playback started
  LOAD_TIMEOUT: 'load-timeout',   // canplaythrough never came
  PLAY_REJECTED: 'play-rejected', // play() rejected (NotAllowed etc.)
  PREMATURE_END: 'premature-end', // ended before ~85% of duration
  END_TIMEOUT: 'end-timeout',     // ended never fired; safety timer hit
  TIME_JUMP: 'time-jump',         // playhead moved far more than wall time
  RATE_DRIFT: 'rate-drift',       // playbackRate changed under us
  ABORTED: 'aborted',             // abort/emptied while a clip was live
});

export const AUDIO_FAULT_EVENT = 'audioFault';
export const AUDIO_SUMMARY_EVENT = 'audioClipSummary';

const MEDIA_ERROR_NAMES = {
  1: 'MEDIA_ERR_ABORTED',
  2: 'MEDIA_ERR_NETWORK',
  3: 'MEDIA_ERR_DECODE',
  4: 'MEDIA_ERR_SRC_NOT_SUPPORTED',
};

function round(n, places = 3) {
  if (!Number.isFinite(n)) return null;
  const f = 10 ** places;
  return Math.round(n * f) / f;
}

/** How much decoded audio lies ahead of the playhead, in ms. null if unknown. */
export function bufferedAheadMs(audioEl) {
  try {
    const ranges = audioEl.buffered;
    const cur = audioEl.currentTime || 0;
    if (!ranges || typeof ranges.length !== 'number') return null;
    for (let i = 0; i < ranges.length; i++) {
      const start = ranges.start(i);
      const end = ranges.end(i);
      if (cur >= start - 0.05 && cur <= end) return Math.max(0, Math.round((end - cur) * 1000));
    }
    return 0;
  } catch (_) {
    return null;
  }
}

export function deliveryOf(clip) {
  if (!clip) return 'none';
  if (clip.delivery) return String(clip.delivery);
  if (clip.playlist_url) return 'hls';
  if (clip.streamable && clip.stream_url) return 'stream';
  return 'file';
}

/** What the element believed at this instant. */
export function snapshotElement(audioEl) {
  const dur = Number(audioEl.duration);
  const cur = Number(audioEl.currentTime) || 0;
  const err = audioEl.error;
  return {
    current_s: round(cur),
    duration_s: Number.isFinite(dur) ? round(dur) : null,
    position_pct: Number.isFinite(dur) && dur > 0 ? Math.round((cur / dur) * 100) : null,
    ready_state: audioEl.readyState ?? null,
    network_state: audioEl.networkState ?? null,
    buffered_ahead_ms: bufferedAheadMs(audioEl),
    rate: round(audioEl.playbackRate, 2),
    volume: round(audioEl.volume, 2),
    element_muted: !!audioEl.muted,
    paused: !!audioEl.paused,
    seeking: !!audioEl.seeking,
    src_tail: String(audioEl.currentSrc || audioEl.src || '').split('/').slice(-2).join('/'),
    error_code: err && err.code ? err.code : null,
    error_name: err && err.code ? (MEDIA_ERROR_NAMES[err.code] || String(err.code)) : null,
    error_message: err && err.message ? String(err.message).slice(0, 200) : null,
  };
}

/**
 * @param {object} audioEl   HTMLAudioElement or a fake with the same events
 * @param {object} opts
 * @param {() => number} [opts.now]
 * @param {(event: string, detail: object, extra: object) => void} opts.emit
 * @param {() => object} [opts.conditions]  host-supplied environment snapshot
 * @param {number} [opts.speed]             expected playbackRate
 * @param {number} [opts.stallMs]           stalls shorter than this are noise
 * @param {number} [opts.jumpS]             playhead delta beyond expectation
 */
export function createFaultMonitor(audioEl, opts = {}) {
  const now = opts.now || (() => Date.now());
  const emit = opts.emit || (() => {});
  const conditions = opts.conditions || (() => ({}));
  const speed = opts.speed ?? 1.2;
  const stallMs = opts.stallMs ?? 250;
  const jumpS = opts.jumpS ?? 1.5;

  let live = null; // per-clip state; null between clips

  function safeConditions() {
    try { return conditions() || {}; } catch (_) { return { conditions_error: true }; }
  }

  function latency() {
    if (!live) return {};
    const l = {};
    if (live.broadcastAt && live.queuedAt) l.broadcast_to_queued_ms = live.queuedAt - live.broadcastAt;
    if (live.queuedAt) l.queued_to_play_start_ms = live.startedAt - live.queuedAt;
    if (live.firstPlayingAt) l.play_start_to_sound_ms = live.firstPlayingAt - live.startedAt;
    l.since_play_start_ms = now() - live.startedAt;
    return l;
  }

  function record(kind, extraDetail = {}) {
    if (!live) return null;
    live.faults.push(kind);
    const detail = {
      kind,
      clip_id: live.clip.clip_id || null,
      url: live.clip.url,
      session: live.clip.session || '',
      delivery: live.delivery,
      expected_rate: speed,
      ...extraDetail,
      element: snapshotElement(audioEl),
      latency: latency(),
      conditions: safeConditions(),
    };
    emit(AUDIO_FAULT_EVENT, detail, {
      clip_id: live.clip.clip_id || undefined,
      clip_url: live.clip.url,
      trace_id: live.clip.trace_id || undefined,
      duration_ms: extraDetail.stall_ms ?? undefined,
    });
    return detail;
  }

  function stallBegin(reason) {
    if (!live || live.stall) return;
    live.stall = { at: now(), reason, element: snapshotElement(audioEl) };
  }

  function stallEnd(cause) {
    if (!live || !live.stall) return;
    const stall = live.stall;
    live.stall = null;
    const ms = now() - stall.at;
    live.stallCount += 1;
    live.stallTotalMs += ms;
    if (ms >= stallMs) {
      record(FaultKind.STALL, {
        stall_ms: ms,
        stall_reason: stall.reason,
        stall_ended_by: cause,
        at_stall_start: stall.element,
      });
    }
  }

  const handlers = {
    waiting: () => stallBegin('waiting'),
    stalled: () => stallBegin('stalled'),
    playing: () => {
      if (!live) return;
      if (!live.firstPlayingAt) live.firstPlayingAt = now();
      stallEnd('playing');
    },
    error: () => {
      if (!live) return;
      stallEnd('error');
      record(live.firstPlayingAt ? FaultKind.DECODE_ERROR : FaultKind.LOAD_FAIL);
    },
    ratechange: () => {
      if (!live) return;
      const rate = Number(audioEl.playbackRate);
      if (Number.isFinite(rate) && Math.abs(rate - speed) > 0.01) {
        record(FaultKind.RATE_DRIFT, { observed_rate: round(rate, 2) });
      }
    },
    abort: () => { if (live) record(FaultKind.ABORTED, { via: 'abort' }); },
    emptied: () => { if (live) record(FaultKind.ABORTED, { via: 'emptied' }); },
    timeupdate: () => {
      if (!live) return;
      const t = now();
      const cur = Number(audioEl.currentTime) || 0;
      if (live.lastTick) {
        const wall = (t - live.lastTick.at) / 1000;
        const expected = wall * (Number(audioEl.playbackRate) || speed);
        const moved = cur - live.lastTick.cur;
        // Backwards or far ahead of what wall time allows means the playhead
        // did not get there by playing: a seek we did not ask for, a stream
        // that skipped, or a decoder that dropped a chunk.
        if (!audioEl.seeking && (moved < -jumpS || moved - expected > jumpS)) {
          record(FaultKind.TIME_JUMP, {
            from_s: round(live.lastTick.cur),
            to_s: round(cur),
            wall_ms: Math.round(wall * 1000),
          });
        }
      }
      live.lastTick = { at: t, cur };
    },
  };

  function attach() {
    for (const [type, fn] of Object.entries(handlers)) audioEl.addEventListener(type, fn);
  }
  function detach() {
    for (const [type, fn] of Object.entries(handlers)) audioEl.removeEventListener(type, fn);
  }

  return {
    /** Call when the adapter hands a clip to the element. */
    begin(clip, { queuedAt = null } = {}) {
      if (live) this.end({ premature: false, reason: 'superseded' });
      const t = now();
      live = {
        clip: clip || {},
        delivery: deliveryOf(clip),
        startedAt: t,
        queuedAt: queuedAt || clip?._queuedAt || null,
        broadcastAt: clip?.ts || null,
        firstPlayingAt: null,
        stall: null,
        stallCount: 0,
        stallTotalMs: 0,
        lastTick: null,
        faults: [],
      };
      attach();
    },

    /** Adapter-observed faults that have no element event of their own. */
    note(kind, extra = {}) {
      if (!live) return null;
      return record(kind, extra);
    },

    /** Call when the adapter resolves the clip. Returns the summary. */
    end(result = {}) {
      if (!live) return null;
      stallEnd(result.reason || 'ended');
      if (result.premature) record(FaultKind.PREMATURE_END);
      detach();
      const summary = {
        clip_id: live.clip.clip_id || null,
        url: live.clip.url,
        session: live.clip.session || '',
        delivery: live.delivery,
        ok: live.faults.length === 0,
        faults: live.faults.slice(),
        stall_count: live.stallCount,
        stall_total_ms: live.stallTotalMs,
        played_ms: now() - live.startedAt,
        reached_sound: !!live.firstPlayingAt,
        element: snapshotElement(audioEl),
        latency: latency(),
        conditions: safeConditions(),
      };
      emit(AUDIO_SUMMARY_EVENT, summary, {
        clip_id: live.clip.clip_id || undefined,
        clip_url: live.clip.url,
        trace_id: live.clip.trace_id || undefined,
        duration_ms: summary.played_ms,
      });
      live = null;
      return summary;
    },

    get active() { return !!live; },
  };
}
