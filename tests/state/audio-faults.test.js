// Audio fault monitor: every way playback goes wrong becomes one structured
// record with position, latency and conditions attached.

import { describe, it, expect, beforeEach } from 'vitest';
import {
  createFaultMonitor, FaultKind, AUDIO_FAULT_EVENT, AUDIO_SUMMARY_EVENT,
  bufferedAheadMs, deliveryOf,
} from '../../static/lib/audio-faults.js';

function makeFakeAudio() {
  const listeners = new Map();
  const audio = {
    src: '', currentSrc: '', playbackRate: 1.2, volume: 1, muted: false,
    paused: false, seeking: false, readyState: 4, networkState: 2,
    duration: 10, currentTime: 0, error: null,
    buffered: { length: 0, start() { return 0; }, end() { return 0; } },
    addEventListener(type, fn) {
      const list = listeners.get(type) || [];
      list.push(fn);
      listeners.set(type, list);
    },
    removeEventListener(type, fn) {
      listeners.set(type, (listeners.get(type) || []).filter(x => x !== fn));
    },
    dispatch(type) { for (const fn of (listeners.get(type) || []).slice()) fn(); },
    listenerCount() { let n = 0; for (const l of listeners.values()) n += l.length; return n; },
  };
  return audio;
}

function ranges(...pairs) {
  return {
    length: pairs.length,
    start(i) { return pairs[i][0]; },
    end(i) { return pairs[i][1]; },
  };
}

describe('audio fault monitor', () => {
  let clock, audio, events, monitor, conditions;
  const clip = { url: '/audio/1700000000000.mp3', session: 'rachel', clip_id: 7,
                 trace_id: 'abc', ts: 1_000_000 };

  beforeEach(() => {
    clock = 1_002_000;
    audio = makeFakeAudio();
    events = [];
    conditions = { online: true, visibility: 'visible', mic_level: 12 };
    monitor = createFaultMonitor(audio, {
      now: () => clock,
      emit: (event, detail, extra) => events.push({ event, detail, extra }),
      conditions: () => conditions,
      speed: 1.2,
      stallMs: 250,
    });
  });

  it('a clean clip emits only a summary that says so', () => {
    monitor.begin(clip, { queuedAt: 1_001_500 });
    clock += 300; audio.dispatch('playing');
    clock += 5000; audio.currentTime = 6;
    const summary = monitor.end({ premature: false });
    expect(events.map(e => e.event)).toEqual([AUDIO_SUMMARY_EVENT]);
    expect(summary.ok).toBe(true);
    expect(summary.faults).toEqual([]);
    expect(summary.reached_sound).toBe(true);
    expect(summary.latency).toEqual({
      broadcast_to_queued_ms: 1500,
      queued_to_play_start_ms: 500,
      play_start_to_sound_ms: 300,
      since_play_start_ms: 5300,
    });
    expect(events[0].extra).toEqual({ clip_id: 7, clip_url: clip.url, trace_id: 'abc', duration_ms: 5300 });
    expect(audio.listenerCount()).toBe(0);
  });

  it('a stall is measured from waiting to playing and carries both snapshots', () => {
    monitor.begin(clip);
    audio.dispatch('playing');
    audio.currentTime = 3.2; audio.readyState = 2; audio.networkState = 2;
    audio.buffered = ranges([0, 3.2]);
    audio.dispatch('waiting');
    clock += 800;
    audio.readyState = 4; audio.buffered = ranges([0, 8]);
    audio.dispatch('playing');
    const faults = events.filter(e => e.event === AUDIO_FAULT_EVENT);
    expect(faults).toHaveLength(1);
    const d = faults[0].detail;
    expect(d.kind).toBe(FaultKind.STALL);
    expect(d.stall_ms).toBe(800);
    expect(d.stall_reason).toBe('waiting');
    expect(d.at_stall_start.ready_state).toBe(2);
    expect(d.at_stall_start.buffered_ahead_ms).toBe(0);
    expect(d.element.buffered_ahead_ms).toBe(4800);
    expect(d.element.current_s).toBe(3.2);
    expect(d.element.position_pct).toBe(32);
    expect(d.conditions).toEqual(conditions);
    expect(faults[0].extra.duration_ms).toBe(800);
    const summary = monitor.end({});
    expect(summary.stall_count).toBe(1);
    expect(summary.stall_total_ms).toBe(800);
    expect(summary.ok).toBe(false);
  });

  it('a stall shorter than the floor is counted but not reported', () => {
    monitor.begin(clip);
    audio.dispatch('playing');
    audio.dispatch('stalled');
    clock += 100;
    audio.dispatch('playing');
    expect(events.filter(e => e.event === AUDIO_FAULT_EVENT)).toHaveLength(0);
    const summary = monitor.end({});
    expect(summary.stall_count).toBe(1);
    expect(summary.ok).toBe(true);
  });

  it('a stall still open when the clip ends is closed by the end', () => {
    monitor.begin(clip);
    audio.dispatch('playing');
    audio.dispatch('waiting');
    clock += 3000;
    monitor.end({ premature: true });
    const kinds = events.filter(e => e.event === AUDIO_FAULT_EVENT).map(e => e.detail.kind);
    expect(kinds).toEqual([FaultKind.STALL, FaultKind.PREMATURE_END]);
    expect(events[0].detail.stall_ended_by).toBe('ended');
  });

  it('an error before any sound is a load failure; after sound it is a decode error', () => {
    monitor.begin(clip);
    audio.error = { code: 4, message: 'unsupported' };
    audio.dispatch('error');
    expect(events[0].detail.kind).toBe(FaultKind.LOAD_FAIL);
    expect(events[0].detail.element.error_name).toBe('MEDIA_ERR_SRC_NOT_SUPPORTED');
    monitor.end({});
    events.length = 0;

    monitor.begin(clip);
    audio.error = null;
    audio.dispatch('playing');
    audio.error = { code: 3, message: 'decode' };
    audio.dispatch('error');
    expect(events[0].detail.kind).toBe(FaultKind.DECODE_ERROR);
    expect(events[0].detail.element.error_name).toBe('MEDIA_ERR_DECODE');
  });

  it('the playhead jumping further than wall time allows is a time jump', () => {
    monitor.begin(clip);
    audio.dispatch('playing');
    audio.currentTime = 1.0; audio.dispatch('timeupdate');
    clock += 250; audio.currentTime = 1.3; audio.dispatch('timeupdate'); // normal at 1.2x
    clock += 250; audio.currentTime = 4.0; audio.dispatch('timeupdate'); // skipped ~2.4s
    clock += 250; audio.currentTime = 0.5; audio.dispatch('timeupdate'); // went backwards
    const jumps = events.filter(e => e.detail.kind === FaultKind.TIME_JUMP);
    expect(jumps).toHaveLength(2);
    expect(jumps[0].detail).toMatchObject({ from_s: 1.3, to_s: 4, wall_ms: 250 });
    expect(jumps[1].detail).toMatchObject({ from_s: 4, to_s: 0.5 });
  });

  it('a seek in progress is not a time jump', () => {
    monitor.begin(clip);
    audio.currentTime = 1; audio.dispatch('timeupdate');
    audio.seeking = true;
    clock += 100; audio.currentTime = 9; audio.dispatch('timeupdate');
    expect(events.filter(e => e.event === AUDIO_FAULT_EVENT)).toHaveLength(0);
  });

  it('the rate changing away from what we set is drift; matching it is not', () => {
    monitor.begin(clip);
    audio.playbackRate = 1.2; audio.dispatch('ratechange');
    audio.playbackRate = 1.0; audio.dispatch('ratechange');
    const drifts = events.filter(e => e.detail.kind === FaultKind.RATE_DRIFT);
    expect(drifts).toHaveLength(1);
    expect(drifts[0].detail.observed_rate).toBe(1);
    expect(drifts[0].detail.expected_rate).toBe(1.2);
  });

  it('adapter-observed faults are recorded through note()', () => {
    monitor.begin(clip);
    monitor.note(FaultKind.LOAD_TIMEOUT, { ready_state: 1 });
    monitor.note(FaultKind.PLAY_REJECTED, { error_name: 'NotAllowedError' });
    monitor.note(FaultKind.END_TIMEOUT, { cap_ms: 30000 });
    const kinds = events.map(e => e.detail.kind);
    expect(kinds).toEqual([FaultKind.LOAD_TIMEOUT, FaultKind.PLAY_REJECTED, FaultKind.END_TIMEOUT]);
    expect(events[1].detail.error_name).toBe('NotAllowedError');
  });

  it('nothing is recorded outside a clip, and begin() closes a clip left open', () => {
    audio.dispatch('waiting'); audio.dispatch('error');
    expect(monitor.note(FaultKind.END_TIMEOUT)).toBeNull();
    expect(events).toHaveLength(0);
    monitor.begin(clip);
    monitor.begin({ ...clip, clip_id: 8 });
    expect(events.map(e => e.event)).toEqual([AUDIO_SUMMARY_EVENT]);
    expect(events[0].detail.clip_id).toBe(7);
    expect(monitor.active).toBe(true);
  });

  it('a conditions callback that throws does not lose the fault', () => {
    const m = createFaultMonitor(audio, {
      now: () => clock, emit: (e, d) => events.push({ event: e, detail: d }),
      conditions: () => { throw new Error('boom'); },
    });
    m.begin(clip);
    audio.dispatch('error');
    expect(events[0].detail.kind).toBe(FaultKind.LOAD_FAIL);
    expect(events[0].detail.conditions).toEqual({ conditions_error: true });
  });

  it('delivery is inferred from the clip shape', () => {
    expect(deliveryOf({ url: 'x', playlist_url: '/clips/1/playlist.m3u8' })).toBe('hls');
    expect(deliveryOf({ url: 'x', streamable: true, stream_url: '/clips/1/stream' })).toBe('stream');
    expect(deliveryOf({ url: 'x' })).toBe('file');
    expect(deliveryOf({ url: 'x', delivery: 'raw-pcm' })).toBe('raw-pcm');
  });

  it('buffered runway is measured from the range under the playhead', () => {
    audio.currentTime = 5; audio.buffered = ranges([0, 2], [4.5, 9]);
    expect(bufferedAheadMs(audio)).toBe(4000);
    audio.currentTime = 3;
    expect(bufferedAheadMs(audio)).toBe(0);
    audio.buffered = undefined;
    expect(bufferedAheadMs(audio)).toBeNull();
  });
});
