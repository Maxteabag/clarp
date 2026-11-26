// Playback diagnostics: every adapter log call carries a snapshot of the
// element + environment, stalls/freezes/jumps/errors are detected and
// counted, and a per-clip 'playStats' summary is emitted exactly once.
// Observation only — these tests also pin that the play() outcome is the
// same as before the diagnostics were added.

import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  createPlayerAdapter, snapshot, STALL_LOG_MIN_INTERVAL_MS,
} from '../../static/lib/player-adapter.js';

const sleep = ms => new Promise(r => setTimeout(r, ms));

/** Fake element with the surface snapshot() reads, plus listener bookkeeping. */
function makeFakeAudio({ readyState = 4 } = {}) {
  const listeners = new Map();
  const audio = {
    src: '', currentSrc: '', playbackRate: 1, defaultPlaybackRate: 1,
    readyState, networkState: 2, duration: 10, currentTime: 0,
    paused: true, ended: false, volume: 0.8, muted: false, error: null,
    buffered: {
      length: 2,
      start(i) { return [0, 6][i]; },
      end(i) { return [4, 9][i]; },
    },
    addEventListener(type, fn, opts) {
      const list = listeners.get(type) || [];
      const wrapped = opts && opts.once
        ? (...a) => { audio.removeEventListener(type, wrapped); fn(...a); }
        : fn;
      wrapped._orig = fn;
      list.push(wrapped);
      listeners.set(type, list);
    },
    removeEventListener(type, fn) {
      const list = listeners.get(type);
      if (!list) return;
      listeners.set(type, list.filter(x => x !== fn && x._orig !== fn));
    },
    dispatch(type) {
      for (const fn of (listeners.get(type) || []).slice()) fn();
    },
    listenerCount(type) { return (listeners.get(type) || []).length; },
    play() { audio.paused = false; return Promise.resolve(); },
    pause() { audio.paused = true; },
    load() {},
  };
  return audio;
}

/** Collect log calls as {ev, det, snap}. */
function makeLog() {
  const rows = [];
  const log = (ev, det, snap) => rows.push({ ev, det: String(det ?? ''), snap });
  log.rows = rows;
  log.find = ev => rows.filter(r => r.ev === ev);
  log.last = ev => log.find(ev).at(-1);
  return log;
}

const CLIP = {
  url: '/audio/x.mp3', session: 'rachel', clip_id: 42, trace_id: 'tr-1',
  delivery: 'hls', playlist_url: '/clips/42/playlist.m3u8',
  audio_format: { container: 'mpegts', encoding: 'aac', sample_rate: 44100, channels: 1 },
};

afterEach(() => { vi.unstubAllGlobals(); });

describe('snapshot()', () => {
  it('summarises the element, buffer and environment', () => {
    const el = makeFakeAudio();
    el.currentTime = 2.3456; el.playbackRate = 1.2;
    vi.stubGlobal('document', { visibilityState: 'hidden' });
    vi.stubGlobal('navigator', {
      onLine: true, connection: { effectiveType: '4g', rtt: 50, downlink: 10 },
    });
    const s = snapshot(el, { clip_id: 7, src_kind: 'stream' });
    expect(s).toMatchObject({
      clip_id: 7, src_kind: 'stream',
      currentTime: 2.346, duration: 10, readyState: 4, networkState: 2,
      paused: true, ended: false, playbackRate: 1.2, volume: 0.8, muted: false,
      buffered: [[0, 4], [6, 9]],
      buffered_ahead_ms: Math.round((4 - 2.3456) * 1000),
      visibility: 'hidden', online: true,
      net_type: '4g', net_rtt: 50, net_downlink: 10,
    });
  });

  it('reports buffered_ahead_ms=null when currentTime is outside every range', () => {
    const el = makeFakeAudio();
    el.currentTime = 5;
    expect(snapshot(el).buffered_ahead_ms).toBeNull();
  });

  it('never throws on a hostile or missing element', () => {
    expect(() => snapshot(null)).not.toThrow();
    expect(() => snapshot(undefined, { a: 1 })).not.toThrow();
    const evil = new Proxy({}, { get() { throw new Error('boom'); } });
    expect(() => snapshot(evil)).not.toThrow();
    expect(snapshot(undefined, { a: 1 })).toMatchObject({ a: 1 });
    // Infinity duration (chunked transfer) and NaN survive as-is / null.
    const el = makeFakeAudio();
    el.duration = Infinity; el.currentTime = NaN;
    const s = snapshot(el);
    expect(s.duration).toBe(Infinity);
    expect(s.currentTime).toBeNull();
  });
});

describe('createPlayerAdapter diagnostics', () => {
  it('attaches a snapshot with clip identity, src kind and timings to every log call', async () => {
    const audio = makeFakeAudio();
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, loadTimeoutMs: 30 });
    const p = adapter.play(CLIP);
    setTimeout(() => {
      audio.dispatch('progress');
      audio.dispatch('canplay');
      audio.dispatch('canplaythrough');
      audio.dispatch('playing');
    }, 5);
    setTimeout(() => { audio.currentTime = 10; audio.ended = true; audio.dispatch('ended'); }, 40);
    const r = await p;
    expect(r.premature).toBe(false);

    for (const row of log.rows) {
      expect(row.snap, row.ev).toMatchObject({
        clip_id: 42, trace_id: 'tr-1', src_kind: 'playlist', delivery: 'hls',
        audio_format: CLIP.audio_format,
      });
      expect(typeof row.snap.t_since_play_ms).toBe('number');
      expect(row.snap.volume).toBe(0.8);
    }
    expect(log.rows.map(r => r.ev)).toEqual(
      ['playStart', 'playHls', 'playCalling', 'playOk', 'playStats']);
    expect(log.last('playStart').snap.src).toBe('/clips/42/playlist.m3u8');

    const ok = log.last('playOk').snap;
    expect(typeof ok.duration_ms).toBe('number');

    const stats = log.last('playStats').snap;
    expect(stats).toMatchObject({
      outcome: 'ended', premature: false, stall_count: 0, freeze_count: 0, jump_count: 0,
      max_stall_ms: 0, expected_duration_s: 10, expected_wall_ms: Math.round(10 / 1.2 * 1000),
      suspend_count: 0, error_count: 0, last_error: null,
    });
    for (const k of ['t_first_progress_ms', 't_canplay_ms', 't_canplaythrough_ms',
                     't_playing_ms', 't_ended_ms', 'wall_elapsed_ms', 'play_to_end_ms']) {
      expect(typeof stats[k], k).toBe('number');
    }
    expect(stats.t_ended_ms).toBeGreaterThanOrEqual(stats.t_playing_ms);
    expect(stats.duration_ms).toBe(stats.wall_elapsed_ms);
    expect(stats.cap_ms).toBe(Math.round(10 / 1.2 * 1000 + 4000));
  });

  it('reports src_kind=stream / url and null timings for events that never fired', async () => {
    const audio = makeFakeAudio();
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, loadTimeoutMs: 30 });
    const p = adapter.play({ url: '/audio/x.mp3', session: '', streamable: true, stream_url: '/clips/3/stream' });
    setTimeout(() => audio.dispatch('ended'), 5);
    await p;
    const stats = log.last('playStats').snap;
    expect(stats.src_kind).toBe('stream');
    expect(stats.clip_id).toBeNull();
    expect(stats.audio_format).toBeNull();
    expect(stats.t_canplay_ms).toBeNull();
    expect(stats.t_first_progress_ms).toBeNull();
    // play() resolved → t_playing is armed even without a 'playing' event.
    expect(typeof stats.t_playing_ms).toBe('number');

    const log2 = makeLog();
    const audio2 = makeFakeAudio();
    const a3 = createPlayerAdapter(audio2, { log: log2, loadTimeoutMs: 30 });
    const p3 = a3.play({ url: '/audio/y.mp3', session: '' });
    setTimeout(() => audio2.dispatch('ended'), 5);
    await p3;
    expect(log2.last('playStats').snap.src_kind).toBe('url');
  });

  it('logs playStall on waiting/stalled, rate-limited to one per 500ms, and counts all of them', async () => {
    const audio = makeFakeAudio();
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, loadTimeoutMs: 30 });
    const p = adapter.play(CLIP);
    await sleep(5);
    audio.dispatch('playing');
    audio.dispatch('waiting');
    audio.dispatch('stalled');
    audio.dispatch('stalled');
    audio.dispatch('waiting');
    expect(log.find('playStall')).toHaveLength(1);
    expect(log.last('playStall').det).toBe('waiting n=1');
    expect(log.last('playStall').snap.stall_event).toBe('waiting');
    await sleep(STALL_LOG_MIN_INTERVAL_MS + 20);
    audio.dispatch('stalled');
    expect(log.find('playStall')).toHaveLength(2);
    expect(log.last('playStall').det).toBe('stalled n=5');
    // Stall ends when the clock moves again.
    audio.currentTime = 1; audio.dispatch('timeupdate');
    audio.currentTime = 10; audio.dispatch('ended');
    await p;
    const stats = log.last('playStats').snap;
    expect(stats.stall_count).toBe(5);
    expect(stats.max_stall_ms).toBeGreaterThanOrEqual(STALL_LOG_MIN_INTERVAL_MS);
  });

  it('detects a freeze (no timeupdate while !paused && !ended) once per episode', async () => {
    const audio = makeFakeAudio();
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, loadTimeoutMs: 30, freezeMs: 60, freezeCheckMs: 10 });
    const p = adapter.play(CLIP);
    await sleep(5);
    audio.dispatch('playing');             // arms the watchdog
    await sleep(150);                       // silent for > freezeMs
    expect(log.find('playFreeze')).toHaveLength(1);
    expect(log.last('playFreeze').snap.silent_ms).toBeGreaterThanOrEqual(60);
    audio.currentTime = 0.5; audio.dispatch('timeupdate');   // thaws
    await sleep(150);                       // second episode
    expect(log.find('playFreeze')).toHaveLength(2);
    audio.currentTime = 10; audio.dispatch('ended');
    await p;
    expect(log.last('playStats').snap.freeze_count).toBe(2);
  });

  it('does not report a freeze while paused or ended', async () => {
    const audio = makeFakeAudio();
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, loadTimeoutMs: 30, freezeMs: 40, freezeCheckMs: 10 });
    const p = adapter.play(CLIP);
    await sleep(5);
    audio.dispatch('playing');
    audio.paused = true;
    await sleep(120);
    expect(log.find('playFreeze')).toHaveLength(0);
    audio.currentTime = 10; audio.dispatch('ended');
    await p;
  });

  it('detects forward and backward currentTime discontinuities at the configured rate', async () => {
    const audio = makeFakeAudio();
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, speed: 1.2, loadTimeoutMs: 30 });
    const p = adapter.play(CLIP);
    await sleep(5);
    audio.dispatch('playing');
    audio.currentTime = 1.0; audio.dispatch('timeupdate');
    audio.currentTime = 1.2; audio.dispatch('timeupdate');   // normal
    expect(log.find('playJump')).toHaveLength(0);
    audio.currentTime = 5.0; audio.dispatch('timeupdate');   // +3.8s in ~0ms
    expect(log.find('playJump')).toHaveLength(1);
    expect(log.last('playJump').snap).toMatchObject({ jump_from: 1.2, jump_to: 5, jump_delta_s: 3.8 });
    audio.currentTime = 2.0; audio.dispatch('timeupdate');   // backwards
    expect(log.find('playJump')).toHaveLength(2);
    expect(log.last('playJump').snap.jump_delta_s).toBe(-3);
    audio.currentTime = 10; audio.dispatch('ended');
    await p;
    expect(log.last('playStats').snap.jump_count).toBe(2);
  });

  it('ignores discontinuities when the element is not at the expected playbackRate', async () => {
    const audio = makeFakeAudio();
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, speed: 1.2, loadTimeoutMs: 30 });
    const p = adapter.play(CLIP);
    await sleep(5);
    audio.playbackRate = 1.0;           // browser reset it (ratechange)
    audio.dispatch('ratechange');
    audio.currentTime = 1; audio.dispatch('timeupdate');
    audio.currentTime = 9; audio.dispatch('timeupdate');
    expect(log.find('playJump')).toHaveLength(0);
    expect(log.find('playRateChange')).toHaveLength(1);
    expect(log.last('playRateChange').det).toBe('rate=1 expected=1.2');
    audio.currentTime = 10; audio.dispatch('ended');
    await p;
    expect(log.last('playStats').snap.rate_change_count).toBe(1);
  });

  it('logs volume changes and counts suspend/pause without emitting noise for them', async () => {
    const audio = makeFakeAudio();
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, loadTimeoutMs: 30 });
    const p = adapter.play(CLIP);
    await sleep(5);
    audio.volume = 0; audio.muted = true; audio.dispatch('volumechange');
    audio.dispatch('suspend'); audio.dispatch('suspend');
    audio.dispatch('pause');
    expect(log.last('playVolumeChange').det).toBe('vol=0 muted=true');
    expect(log.last('playVolumeChange').snap).toMatchObject({ volume: 0, muted: true });
    expect(log.find('playSuspend')).toHaveLength(0);
    audio.currentTime = 10; audio.dispatch('ended');
    await p;
    expect(log.last('playStats').snap).toMatchObject({
      volume_change_count: 1, suspend_count: 2, pause_count: 1,
    });
  });

  it('logs playError with the media error code/message and finishes with outcome=error', async () => {
    const audio = makeFakeAudio();
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, loadTimeoutMs: 30 });
    const p = adapter.play(CLIP);
    await sleep(5);
    audio.error = { code: 3, message: 'PIPELINE_ERROR_DECODE' };
    audio.dispatch('error');
    const r = await p;
    expect(r.premature).toBe(false);
    expect(log.last('playError').det).toBe('code=3 PIPELINE_ERROR_DECODE');
    expect(log.last('playError').snap).toMatchObject({ error_code: 3, error_message: 'PIPELINE_ERROR_DECODE' });
    expect(log.last('playStats').snap).toMatchObject({
      outcome: 'error', error_count: 1, last_error: { code: 3, message: 'PIPELINE_ERROR_DECODE' },
    });
  });

  it('removes every observer when the clip ends and never logs for it again', async () => {
    const audio = makeFakeAudio();
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, loadTimeoutMs: 30 });
    const p = adapter.play(CLIP);
    await sleep(5);
    const observed = ['waiting', 'stalled', 'suspend', 'error', 'ratechange',
                      'volumechange', 'timeupdate', 'progress', 'playing', 'ended'];
    for (const t of observed) expect(audio.listenerCount(t), t).toBeGreaterThan(0);
    audio.currentTime = 10; audio.dispatch('ended');
    await p;
    for (const t of observed) expect(audio.listenerCount(t), t).toBe(0);
    const n = log.rows.length;
    audio.dispatch('stalled'); audio.dispatch('error'); audio.dispatch('timeupdate');
    expect(log.rows.length).toBe(n);
    expect(log.find('playStats')).toHaveLength(1);
  });

  it('play() rejection: playFail carries the error and stats end with outcome=playFail, premature', async () => {
    const audio = makeFakeAudio();
    audio.play = () => Promise.reject(Object.assign(new Error('gate'), { name: 'NotAllowedError' }));
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, loadTimeoutMs: 30 });
    const r = await adapter.play(CLIP);
    expect(r).toEqual({ premature: true, duration: 0, currentTime: 0 });
    expect(log.last('playFail').det).toBe('NotAllowedError');
    expect(log.last('playFail').snap).toMatchObject({ error_name: 'NotAllowedError', error_message: 'gate' });
    expect(log.last('playStats').snap).toMatchObject({ outcome: 'playFail', premature: true });
    expect(audio.listenerCount('timeupdate')).toBe(0);
  });

  it('premature ended is reflected in playStats', async () => {
    const audio = makeFakeAudio();
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, loadTimeoutMs: 30 });
    const p = adapter.play(CLIP);
    setTimeout(() => { audio.currentTime = 2; audio.dispatch('ended'); }, 5);
    const r = await p;
    expect(r.premature).toBe(true);
    expect(log.last('playStats').snap).toMatchObject({ outcome: 'ended', premature: true, currentTime: 2 });
  });

  it('a new play() supersedes a still-pending clip and detaches its observers', async () => {
    const audio = makeFakeAudio();
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, loadTimeoutMs: 30 });
    const p1 = adapter.play({ ...CLIP, clip_id: 1 });
    await sleep(5);
    const p2 = adapter.play({ ...CLIP, clip_id: 2 });
    await sleep(5);
    const stats1 = log.find('playStats').find(r => r.snap.clip_id === 1);
    expect(stats1.snap.outcome).toBe('superseded');
    // Only clip 2's observers remain: one timeupdate listener.
    expect(audio.listenerCount('timeupdate')).toBe(1);
    audio.dispatch('stalled');
    expect(log.last('playStall').snap.clip_id).toBe(2);
    audio.currentTime = 10; audio.dispatch('ended');
    await p2;
    // p1 is resolved by the ended too (both registered once-listeners) but
    // must not emit a second stats row.
    await p1;
    expect(log.find('playStats').filter(r => r.snap.clip_id === 1)).toHaveLength(1);
  });

  it('load-wait logs carry the snapshot too', async () => {
    const audio = makeFakeAudio({ readyState: 0 });
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, loadTimeoutMs: 20 });
    const p = adapter.play({ url: '/audio/x.mp3', session: '', clip_id: 9 });
    setTimeout(() => audio.dispatch('ended'), 40);
    await p;
    expect(log.last('playWaiting').snap).toMatchObject({ clip_id: 9, readyState: 0 });
    expect(log.last('playLoadTimeout').snap).toMatchObject({ clip_id: 9, src_kind: 'url' });
  });

  it('playEndTimeout carries cap_ms and the summary says outcome=timeout', async () => {
    const audio = makeFakeAudio();
    audio.duration = 0.1;
    const log = makeLog();
    const adapter = createPlayerAdapter(audio, { log, speed: 1.0, loadTimeoutMs: 30 });
    const r = await adapter.play(CLIP);
    expect(r.premature).toBe(false);
    expect(log.last('playEndTimeout').snap.cap_ms).toBe(4100);
    expect(log.last('playStats').snap).toMatchObject({ outcome: 'timeout', cap_ms: 4100 });
  }, 8000);
});
