// Sim-e2e: HlsDelivery end-to-end.
//
// Boots the harness with CLAUDE_PWA_DELIVERY=hls, fires a synth with
// real mp3 bytes (ffmpeg won't accept the placeholder "AAAA" bytes the
// other tests use), and asserts:
//   - the SSE audio event carries playlist_url=/clips/<id>/playlist.m3u8
//   - GET /clips/<id>/playlist.m3u8 returns a parseable HLS playlist
//     with at least one #EXTINF entry and an #EXT-X-ENDLIST marker
//   - GET /clips/<id>/segment-0.aac returns non-empty AAC bytes
//
// What this catches that pure-Python tests miss: the full Python →
// ffmpeg → HTTP route round-trip, including the route dispatch in
// server.py picking up /clips/<id>/playlist.m3u8 correctly.

import { spawn, spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect, beforeAll, afterAll } from 'vitest';

import { createPlayerAdapter } from '../../static/lib/player-adapter.js';
import { createScheduler } from '../../static/lib/audio-queue.js';
import { createStateMachine } from '../../static/lib/state-machine.js';
import { FakeAudio, installBrowserGlobals } from './fake-browser.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const HARNESS = path.join(__dirname, 'server_harness.py');


// Generate a tiny real mp3 (0.6s of silence) using lavfi anullsrc. This
// runs ONCE at module load. Used as the "ElevenLabs output" the fake
// synthesizer feeds into ffmpeg via the HlsDelivery session.
function makeSilenceMp3() {
  const r = spawnSync('ffmpeg', [
    '-loglevel', 'error',
    '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono',
    '-t', '0.6',
    '-c:a', 'libmp3lame', '-b:a', '32k',
    '-f', 'mp3', 'pipe:1',
  ], { encoding: 'buffer' });
  if (r.status !== 0) {
    throw new Error('ffmpeg fixture gen failed: ' + r.stderr.toString());
  }
  return r.stdout;     // Buffer of mp3 bytes
}


function startHarness(extraEnv = {}) {
  const proc = spawn('python3', [HARNESS], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, ...extraEnv },
  });
  proc.stderr.on('data', (d) => process.stderr.write(`[harness] ${d}`));
  const ctx = { proc, queue: [], waiters: [], closed: false };
  let buf = '';
  proc.stdout.on('data', (chunk) => {
    buf += chunk.toString();
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (!line.trim()) continue;
      let p;
      try { p = JSON.parse(line); } catch { continue; }
      const w = ctx.waiters.shift();
      if (w) w(p); else ctx.queue.push(p);
    }
  });
  proc.on('close', () => { ctx.closed = true; });
  return ctx;
}

function nextReply(ctx) {
  if (ctx.queue.length) return Promise.resolve(ctx.queue.shift());
  return new Promise((r) => ctx.waiters.push(r));
}

function send(ctx, cmd) {
  ctx.proc.stdin.write(JSON.stringify(cmd) + '\n');
  return nextReply(ctx);
}

async function* sseAudioEvents(baseUrl, abortSignal) {
  const res = await fetch(baseUrl + '/events', {
    signal: abortSignal,
    headers: { 'Accept': 'text/event-stream' },
  });
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const json = line.slice(5).trim();
        if (!json) continue;
        try {
          const ev = JSON.parse(json);
          if (ev && ev.type === 'audio') yield ev;
        } catch {}
      }
    }
  }
}


describe('sim-e2e: HlsDelivery', () => {
  let ctx;
  let banner;
  let mp3;

  beforeAll(async () => {
    // Pre-generate the silent mp3 fixture (~0.6s, ~2-3KB).
    mp3 = makeSilenceMp3();
    expect(mp3.length).toBeGreaterThan(100);
    ctx = startHarness({ CLAUDE_PWA_DELIVERY: 'hls' });
    banner = await nextReply(ctx);
    expect(banner.ready).toBe(true);
  }, 30000);

  afterAll(async () => {
    if (ctx && !ctx.closed) {
      try { await send(ctx, { cmd: 'exit' }); } catch {}
      try { ctx.proc.kill(); } catch {}
    }
  });

  it('synth produces a playable HLS playlist + segments', async () => {
    const baseUrl = `http://127.0.0.1:${banner.port}`;
    const abort = new AbortController();
    const sseIter = sseAudioEvents(baseUrl, abort.signal);

    // Slice the mp3 into 3 chunks so the producer streams in stages
    // (more like the real ElevenLabs flow). hex: prefix so the harness
    // decodes bytes faithfully.
    const slice = Math.ceil(mp3.length / 3);
    const chunks = [
      'hex:' + mp3.subarray(0, slice).toString('hex'),
      'hex:' + mp3.subarray(slice, 2 * slice).toString('hex'),
      'hex:' + mp3.subarray(2 * slice).toString('hex'),
    ];

    const synthRes = await send(ctx, {
      cmd: 'synth', text: 'hello hls',
      voice: 'V_MIKE', session: 'claude',
      chunks, chunk_delay_ms: 80,
    });
    expect(synthRes.trace_id).toBeTypeOf('string');

    // SSE event for this synth.
    let ev;
    while (true) {
      const r = await Promise.race([
        sseIter.next(),
        new Promise((_, rej) =>
          setTimeout(() => rej(new Error('no audio event in 10s')), 10000)),
      ]);
      if (r.value && r.value.trace_id === synthRes.trace_id) {
        ev = r.value; break;
      }
    }
    abort.abort();

    // The HLS delivery's SSE shape: playlist_url instead of stream_url.
    expect(ev.streamable).toBe(true);
    expect(ev.delivery).toBe('hls');
    expect(ev.playlist_url).toMatch(/^\/clips\/\d+\/playlist\.m3u8$/);

    // Wait for ffmpeg to finalize the playlist (it appends #EXT-X-ENDLIST
    // when its stdin closes). Poll the URL until ENDLIST appears.
    const playlistUrl = baseUrl + ev.playlist_url;
    let playlist = '';
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      const r = await fetch(playlistUrl);
      if (r.status === 200) {
        playlist = await r.text();
        if (playlist.includes('#EXT-X-ENDLIST')) break;
      }
      await new Promise(res => setTimeout(res, 100));
    }
    expect(playlist).toContain('#EXTM3U');
    expect(playlist).toContain('#EXT-X-ENDLIST');
    // At least one segment line should be present. The codec is fMP4
    // (`.m4s`) since commit 5dbc59c — MPEG-TS `.aac` was the original
    // format but iOS Safari played fMP4 more reliably.
    const segments = playlist.match(/segment-\d+\.(?:m4s|aac|ts)/g) || [];
    expect(segments.length).toBeGreaterThan(0);

    // Content-Type for the playlist must be the Apple-blessed one or iOS
    // won't recognize it as HLS.
    const playlistRes = await fetch(playlistUrl);
    expect(playlistRes.headers.get('Content-Type'))
      .toBe('application/vnd.apple.mpegurl');

    // Fetch the first segment — non-empty bytes. Codec depends on the
    // active HLS variant: fMP4 (`.m4s`) reports audio/mp4, MPEG-TS
    // (`.aac`/`.ts`) reports audio/aac or video/mp2t.
    const segName = segments[0];
    const segUrl = baseUrl + `/clips/${ev.clip_id}/${segName}`;
    const segRes = await fetch(segUrl);
    expect(segRes.status).toBe(200);
    const segType = segRes.headers.get('Content-Type');
    expect(['video/mp4', 'audio/mp4', 'audio/aac', 'video/mp2t'])
      .toContain(segType);
    const segBytes = new Uint8Array(await segRes.arrayBuffer());
    expect(segBytes.length).toBeGreaterThan(100);
  }, 30000);

  it('client picks HLS path: real player-adapter + FakeAudio play playlist through', async () => {
    // End-to-end through the CLIENT: SSE event with playlist_url →
    // player-adapter sets audio.src = playlist_url → FakeAudio parses
    // playlist → fetches segments → fires ended. Asserts the whole chain.
    const baseUrl = `http://127.0.0.1:${banner.port}`;
    const abort = new AbortController();
    const sseIter = sseAudioEvents(baseUrl, abort.signal);

    // Real client chain.
    const audio = new FakeAudio({ baseUrl });
    installBrowserGlobals(audio);
    const machine = createStateMachine({ awaitDeadlineMs: 60000 });
    const trace = [];
    const log = (ev, det) => trace.push({ ev, det: String(det || '') });
    const adapter = createPlayerAdapter(audio, {
      speed: 1.2, loadTimeoutMs: 30000, log,
    });
    let lastResult = null;
    const scheduler = createScheduler({
      machine,
      player: { async play(clip) { lastResult = await adapter.play(clip); return lastResult; } },
      currentSession: () => 'claude',
      log,
    });

    const synthRes = await send(ctx, {
      cmd: 'synth', text: 'hls client e2e',
      voice: 'V_MIKE', session: 'claude',
      chunks: ['hex:' + mp3.toString('hex')], chunk_delay_ms: 0,
    });

    // Find the audio event with our trace_id.
    let ev;
    while (true) {
      const r = await Promise.race([
        sseIter.next(),
        new Promise((_, rej) =>
          setTimeout(() => rej(new Error('no audio event in 10s')), 10000)),
      ]);
      if (r.value && r.value.trace_id === synthRes.trace_id) {
        ev = r.value; break;
      }
    }
    abort.abort();

    expect(ev.delivery).toBe('hls');
    expect(ev.playlist_url).toBeTypeOf('string');

    // Ingest into the scheduler. URLs absolutized for Node fetch.
    scheduler.ingest({
      url: baseUrl + ev.url,
      session: ev.session,
      ts: Date.now(),
      streamable: !!ev.streamable,
      delivery: ev.delivery,
      playlist_url: baseUrl + ev.playlist_url,
    });

    // Wait for the player-adapter chain to complete.
    await new Promise((resolve, reject) => {
      const t0 = Date.now();
      const tick = () => {
        if (lastResult !== null && !scheduler._peek().length) return resolve();
        if (Date.now() - t0 > 15000) return reject(new Error('client timeout'));
        setTimeout(tick, 50);
      };
      tick();
    });

    // Player-adapter took the HLS path (not streamingPlayer, not chunked).
    const hlsLog = trace.find(t => t.ev === 'playHls');
    expect(hlsLog, `trace = ${JSON.stringify(trace)}`).toBeDefined();
    expect(audio.src).toMatch(/\/clips\/\d+\/playlist\.m3u8$/);

    // FakeAudio walked the playlist + fetched the segments.
    expect(audio._segmentsFetched.length).toBeGreaterThan(0);
    expect(audio._bytesReceived).toBeGreaterThan(100);

    // Scheduler considered the clip done normally — NOT premature, no
    // retry loop. This is the assertion that would've caught the
    // cur=0.000001 bug if it manifested in HLS too.
    expect(lastResult.premature).toBe(false);
    const endedIdx = audio._eventLog.findIndex(e => e.type === 'ended');
    expect(endedIdx).toBeGreaterThanOrEqual(0);
  }, 30000);
});
