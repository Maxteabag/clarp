// R4: the adapter must not hang when canplaythrough never fires.

import { describe, it, expect, vi } from 'vitest';
import { createPlayerAdapter } from '../../static/lib/player-adapter.js';

/** Tiny EventTarget-backed fake of the audio element interface we use. */
function makeFakeAudio({ readyState = 0, neverReady = false } = {}) {
  const listeners = new Map();
  const calls = { play: 0, load: 0, pause: 0 };
  let _rs = readyState;
  const audio = {
    src: '', playbackRate: 1, get readyState() { return _rs; },
    duration: 0, currentTime: 0, currentSrc: '', error: null,
    addEventListener(type, fn) {
      if (neverReady && type === 'canplaythrough') return;
      const list = listeners.get(type) || [];
      list.push(fn);
      listeners.set(type, list);
    },
    removeEventListener(type, fn) {
      const list = listeners.get(type);
      if (!list) return;
      listeners.set(type, list.filter(x => x !== fn));
    },
    dispatch(type) {
      for (const fn of (listeners.get(type) || []).slice()) fn();
    },
    play() { calls.play++; return Promise.resolve(); },
    pause() { calls.pause++; },
    load() { calls.load++; },
    _setReady(n) { _rs = n; },
  };
  audio.calls = calls;
  return audio;
}

describe('createPlayerAdapter', () => {
  it('falls back to url for a streamable clip with an unknown stream_url shape', async () => {
    const audio = makeFakeAudio({ readyState: 4 });
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 30 });
    const p = adapter.play({
      url: '/audio/x.mp3',
      session: 'rachel',
      streamable: true,
      stream_url: '/somewhere/else',
    });
    setTimeout(() => audio.dispatch('ended'), 30);
    await p;
    expect(audio.src).toBe('/audio/x.mp3');
  });

  it('routes HLS playlist_url straight to audio.src', async () => {
    // HlsDelivery emits { delivery: "hls", playlist_url: "/clips/N/playlist.m3u8" }.
    // The adapter must set audio.src directly — iOS plays HLS natively, no
    // MSE, no Range games.
    const audio = makeFakeAudio({ readyState: 4 });
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 30 });
    const p = adapter.play({
      url: '/clips/7/playlist.m3u8',
      session: 'mike',
      streamable: true,
      delivery: 'hls',
      playlist_url: '/clips/7/playlist.m3u8',
    });
    setTimeout(() => audio.dispatch('ended'), 30);
    await p;
    expect(audio.src).toBe('/clips/7/playlist.m3u8');
  });

  it('routes clip-id streams to direct progressive HTTP playback', async () => {
    const audio = makeFakeAudio({ readyState: 4 });
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 30 });
    const p = adapter.play({
      url: '/audio/x.mp3',
      session: 'rachel',
      streamable: true,
      stream_url: '/clips/42/stream',
    });
    setTimeout(() => audio.dispatch('ended'), 30);
    await p;
    expect(audio.src).toBe('/clips/42/stream');
  });

  it('falls through to <audio src> when clip is not streamable', async () => {
    const audio = makeFakeAudio({ readyState: 4 });
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 30 });
    const p = adapter.play({
      url: '/audio/x.mp3', session: '', streamable: false,
    });
    setTimeout(() => audio.dispatch('ended'), 30);
    await p;
    expect(audio.src).toBe('/audio/x.mp3');
  });

  it('R4 — does not hang forever when canplaythrough never fires', async () => {
    const audio = makeFakeAudio({ neverReady: true });
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 30 });
    const before = Date.now();
    const p = adapter.play({ url: '/audio/x.mp3', session: '' });
    // Drive 'ended' to finish the play promise after play() resolves.
    setTimeout(() => audio.dispatch('ended'), 60);
    const r = await p;
    const elapsed = Date.now() - before;
    expect(audio.calls.play).toBe(1);
    expect(elapsed).toBeLessThan(500);
    expect(r.premature).toBe(false);
  });

  it('returns premature=true when play() rejects (NotAllowedError)', async () => {
    const audio = makeFakeAudio({ readyState: 4 });
    audio.play = () => Promise.reject(Object.assign(new Error('na'), { name: 'NotAllowedError' }));
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 30 });
    const r = await adapter.play({ url: '/audio/x.mp3', session: '' });
    expect(r.premature).toBe(true);
  });

  it('detects iOS premature ended (< 85% of duration)', async () => {
    const audio = makeFakeAudio({ readyState: 4 });
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 30 });
    const p = adapter.play({ url: '/audio/x.mp3', session: '' });
    // Simulate playback ending way too early.
    setTimeout(() => {
      audio.duration = 10; audio.currentTime = 2;
      audio.dispatch('ended');
    }, 30);
    const r = await p;
    expect(r.premature).toBe(true);
  });

  it('interrupt pauses and resets', () => {
    const audio = makeFakeAudio({ readyState: 4 });
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 30 });
    adapter.interrupt();
    expect(audio.calls.pause).toBe(1);
    expect(audio.currentTime).toBe(0);
  });

  it('resolves the play promise when ended never fires (safety cap)', async () => {
    const audio = makeFakeAudio({ readyState: 4 });
    audio.duration = 1;       // 1 s clip → cap ≈ 1/1.2 s + 4 s
    const adapter = createPlayerAdapter(audio, { speed: 1.2, loadTimeoutMs: 30 });
    const before = Date.now();
    const r = await adapter.play({ url: '/audio/x.mp3', session: '' });
    const elapsed = Date.now() - before;
    expect(elapsed).toBeGreaterThanOrEqual(4000);
    expect(elapsed).toBeLessThan(6000);
    expect(r.premature).toBe(false);
  }, 8000);

  it('sets both playbackRate AND defaultPlaybackRate so audio plays at 1.2× not 1×', async () => {
    const audio = makeFakeAudio({ readyState: 4 });
    const adapter = createPlayerAdapter(audio, { speed: 1.2 });
    const p = adapter.play({ url: '/audio/x.mp3', session: '' });
    setTimeout(() => audio.dispatch('ended'), 5);
    await p;
    expect(audio.playbackRate).toBe(1.2);
    expect(audio.defaultPlaybackRate).toBe(1.2);
  });

  it('resolves when error fires during playback', async () => {
    const audio = makeFakeAudio({ readyState: 4 });
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 30 });
    const p = adapter.play({ url: '/audio/x.mp3', session: '' });
    setTimeout(() => audio.dispatch('error'), 10);
    const r = await p;
    expect(r.premature).toBe(false);
  });

  it('safety cap fires even when stalled event arrives (no ended)', async () => {
    // 'stalled' is informational only — our adapter doesn't listen for it,
    // so this is the same as the "ended never fires" path. Pin the contract.
    const audio = makeFakeAudio({ readyState: 4 });
    audio.duration = 0.5;
    const adapter = createPlayerAdapter(audio, { speed: 1.0, loadTimeoutMs: 30 });
    const before = Date.now();
    const p = adapter.play({ url: '/audio/x.mp3', session: '' });
    // Dispatch stalled — the adapter ignores it; safety cap must still trip.
    setTimeout(() => audio.dispatch('stalled'), 50);
    const r = await p;
    expect(Date.now() - before).toBeGreaterThanOrEqual(4000);
    expect(r.premature).toBe(false);
  }, 8000);

  it('chunked-transfer clip (duration=Infinity) does NOT fire safety cap instantly', async () => {
    // Bug observed live on 2026-05-26 (trace d065b46a): the /audio fix
    // serves in-progress clips with Transfer-Encoding: chunked. Browsers
    // expose those as `audioEl.duration === Infinity` until the stream
    // closes. The old `dur0 = audioEl.duration || 0` accepted Infinity
    // (it's truthy) so `cap = (Infinity / speed) * 1000 + 4000 = Infinity`,
    // and setTimeout(fn, Infinity) clamps non-finite delays → fires
    // immediately. Result: scheduler thought the clip ended at
    // currentTime=0.000001 and silently dropped every chunked clip.
    //
    // Expected: when duration isn't a finite positive number, fall back
    // to the 30-second cap (same as the dur0===0 case).
    const audio = makeFakeAudio({ readyState: 4 });
    audio.duration = Infinity;
    const adapter = createPlayerAdapter(audio, { speed: 1.2, loadTimeoutMs: 30 });
    const before = Date.now();
    const p = adapter.play({ url: '/audio/x.mp3', session: '' });
    // Dispatch 'ended' after 100ms — simulating the server closing the
    // chunked stream cleanly. The adapter should wait for this, NOT bail
    // out instantly.
    setTimeout(() => audio.dispatch('ended'), 100);
    const r = await p;
    const elapsed = Date.now() - before;
    expect(elapsed).toBeGreaterThanOrEqual(80);
    expect(elapsed).toBeLessThan(500);
    expect(r.premature).toBe(false);
  });

  it('interrupt during pending play does not hang — safety cap resolves it', async () => {
    const audio = makeFakeAudio({ readyState: 4 });
    audio.duration = 0.2;
    const adapter = createPlayerAdapter(audio, { speed: 1.0, loadTimeoutMs: 30 });
    const before = Date.now();
    const p = adapter.play({ url: '/audio/x.mp3', session: '' });
    // External code calls interrupt — currentTime resets, audio pauses,
    // but neither 'ended' nor 'error' fires. The cap must rescue us.
    setTimeout(() => adapter.interrupt(), 50);
    await p;
    expect(Date.now() - before).toBeGreaterThanOrEqual(4000);
    expect(audio.calls.pause).toBeGreaterThan(0);
  }, 8000);
});

describe('createPlayerAdapter fault hooks', () => {
  function makeMonitor() {
    const calls = [];
    return {
      calls,
      begin(clip, info) { calls.push(['begin', clip.url, info.queuedAt]); },
      note(kind, extra) { calls.push(['note', kind, extra]); },
      end(result) { calls.push(['end', result.premature, result.reason]); },
    };
  }

  it('brackets a clean clip with begin and end and reports nothing else', async () => {
    const audio = makeFakeAudio({ readyState: 4 });
    const faults = makeMonitor();
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 30, faults });
    const p = adapter.play({ url: '/audio/a.mp3', session: 'rachel', _queuedAt: 123 });
    setTimeout(() => audio.dispatch('ended'), 10);
    await p;
    expect(faults.calls).toEqual([['begin', '/audio/a.mp3', 123], ['end', false, undefined]]);
  });

  it('reports the load timeout the element never signals', async () => {
    const audio = makeFakeAudio({ readyState: 1, neverReady: true });
    const faults = makeMonitor();
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 20, faults });
    const p = adapter.play({ url: '/audio/a.mp3', session: 'rachel' });
    setTimeout(() => audio.dispatch('ended'), 60);
    await p;
    expect(faults.calls[1]).toEqual(['note', 'load-timeout', { ready_state: 1, waited_ms: 20 }]);
  });

  it('a rejected play() is recorded and the clip is closed', async () => {
    const audio = makeFakeAudio({ readyState: 4 });
    const err = new Error('gate'); err.name = 'NotAllowedError';
    audio.play = () => Promise.reject(err);
    const faults = makeMonitor();
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 20, faults });
    const r = await adapter.play({ url: '/audio/a.mp3', session: 'rachel' });
    expect(r.premature).toBe(true);
    expect(faults.calls.slice(1)).toEqual([
      ['note', 'play-rejected', { error_name: 'NotAllowedError' }],
      ['end', false, 'play-rejected'],
    ]);
  });

  it('a monitor that throws never breaks playback', async () => {
    const audio = makeFakeAudio({ readyState: 4 });
    const faults = { begin() { throw new Error('x'); }, note() { throw new Error('x'); }, end() { throw new Error('x'); } };
    const adapter = createPlayerAdapter(audio, { loadTimeoutMs: 20, faults });
    const p = adapter.play({ url: '/audio/a.mp3', session: 'rachel' });
    setTimeout(() => audio.dispatch('ended'), 10);
    await expect(p).resolves.toMatchObject({ premature: false });
  });
});
