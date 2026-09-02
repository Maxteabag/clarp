// Simulated-client e2e test for the Phase B audio pipeline.
//
// What this test exercises:
//
//   real Python server  ──►  real SSE  ──►  real client JS (player-adapter,
//   (with stubbed             over real        audio-queue, player-adapter)
//    ElevenLabs)              localhost        running in Node against a
//                             socket           FakeAudio that models the
//                                              browser bits we depend on.
//
// What this catches that pure-Python integration tests miss:
//
//   - Bug C-2 (server side): chunked transfer when the clip is still in
//     flight. The FakeAudio's fetch reads the response stream and asserts
//     the full payload arrived.
//
//   - Bug C-3 (client side): duration=Infinity for chunked responses,
//     setTimeout(fn, Infinity) clamping the safety cap, scheduler
//     marking the clip "ended" at currentTime≈0. The test asserts the
//     scheduler advances normally (not premature, no retry loop) and the
//     play-end logic waits for the real 'ended' from the FakeAudio.
//
// Architecture: a subprocess running tests/sim-e2e/server_harness.py
// exposes JSON-on-stdio control for {ready, synth, exit}. The Node test
// reads `port` from the first banner line, then drives the harness while
// running the real client JS in-process.

import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect, beforeAll, afterAll } from 'vitest';

import { createPlayerAdapter } from '../../static/lib/player-adapter.js';
import { createScheduler } from '../../static/lib/audio-queue.js';
import { createStateMachine, States } from '../../static/lib/state-machine.js';
import {
  FakeAudio, installBrowserGlobals, setMediaSourceSupported,
} from './fake-browser.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const HARNESS = path.join(__dirname, 'server_harness.py');


/** Spawn the harness, read its initial banner with {port, audio_dir, agent_id}.
 *  This test exercises the legacy chunked-file delivery (/clips/<id>/stream
 *  + /audio/<file>.mp3 fallback). HLS is the new default, so we pin the
 *  env var here. */
function startHarness() {
  const proc = spawn('python3', [HARNESS], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, CLAUDE_PWA_DELIVERY: 'chunked-file' },
  });
  proc.stderr.on('data', (d) => {
    // Surface server-side errors to vitest so a stuck test is debuggable.
    process.stderr.write(`[harness] ${d}`);
  });
  const ctx = {
    proc,
    queue: [],           // received reply lines waiting to be awaited
    waiters: [],         // resolvers waiting for the next line
    closed: false,
  };
  let buf = '';
  proc.stdout.on('data', (chunk) => {
    buf += chunk.toString();
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (!line.trim()) continue;
      let parsed;
      try { parsed = JSON.parse(line); }
      catch (e) {
        process.stderr.write(`[harness] non-JSON line: ${line}\n`);
        continue;
      }
      const w = ctx.waiters.shift();
      if (w) w(parsed); else ctx.queue.push(parsed);
    }
  });
  proc.on('close', () => { ctx.closed = true; });
  return ctx;
}

function nextReply(ctx) {
  if (ctx.queue.length) return Promise.resolve(ctx.queue.shift());
  return new Promise((res) => ctx.waiters.push(res));
}

function send(ctx, cmd) {
  ctx.proc.stdin.write(JSON.stringify(cmd) + '\n');
  return nextReply(ctx);
}


/** Tiny SSE client built on fetch. Returns an async iterator of parsed
 *  event objects (we only care about `type:'audio'` events). */
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
    // SSE frame separator is a blank line ("\n\n"). Each frame has lines
    // like "data: {...}". We only care about the data: lines.
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


describe('sim-e2e: send → SSE → audio plays through', () => {
  let ctx;
  let banner;
  beforeAll(async () => {
    ctx = startHarness();
    banner = await nextReply(ctx);
    expect(banner.ready).toBe(true);
    expect(typeof banner.port).toBe('number');
  }, 15000);

  afterAll(async () => {
    if (ctx && !ctx.closed) {
      try { await send(ctx, { cmd: 'exit' }); } catch {}
      try { ctx.proc.kill(); } catch {}
    }
  });

  /**
   * Drive ONE turn end-to-end: synth → SSE → client plays clip.
   * Returns { totalBytes, premature, finalDuration, eventLog,
   *           transferEncoding, contentLength }.
   *
   * `mseSupported` toggles the FakeMediaSource — false simulates iOS
   * Safari (the fallback path that uses /audio/<file> with chunked).
   */
  async function playOneTurn({ chunks, chunkDelayMs, mseSupported }) {
    const baseUrl = `http://127.0.0.1:${banner.port}`;

    // Build the real client chain.
    const audio = new FakeAudio({ baseUrl });
    installBrowserGlobals(audio);
    setMediaSourceSupported(mseSupported);
    const machine = createStateMachine({ awaitDeadlineMs: 60000 });
    const trace = [];
    const log = (ev, det) => trace.push({ t: Date.now(), ev, det: String(det || '') });
    const adapter = createPlayerAdapter(audio, {
      speed: 1.2, loadTimeoutMs: 30000, log,
    });
    let lastResult = null;
    const scheduler = createScheduler({
      machine, player: {
        async play(clip) {
          lastResult = await adapter.play(clip);
          return lastResult;
        },
      },
      currentSession: () => 'claude',
      log, onClipStatus: (clip, status, err) => trace.push({
        t: Date.now(), ev: 'clipStatus', det: `${status} ${err || ''}`,
      }),
    });

    // Move the SM to a state where audio plays (idle). It starts there.
    expect(machine.state).toBe(States.IDLE);

    // Subscribe to SSE BEFORE issuing the synth command so we don't miss
    // the audio event.
    const abort = new AbortController();
    const sseIter = sseAudioEvents(baseUrl, abort.signal);

    // Fire the synth.
    const synthRes = await send(ctx, {
      cmd: 'synth', text: 'hello e2e', voice: 'V_MIKE', session: 'claude',
      chunks, chunk_delay_ms: chunkDelayMs,
    });
    expect(synthRes.queue_id).toBeTypeOf('number');
    expect(synthRes.trace_id).toBeTypeOf('string');

    // Wait for the audio event matching THIS synth's trace_id. Prior
    // tests' clips share the SSE channel, so trace_id is the only
    // unambiguous correlator.
    let ev;
    while (true) {
      const evPromise = sseIter.next();
      const timeout = new Promise((_, rej) =>
        setTimeout(() => rej(new Error('no audio event within 10s')), 10000));
      const r = await Promise.race([evPromise, timeout]);
      const candidate = r.value;
      if (!candidate) throw new Error('SSE closed without audio event');
      if (candidate.trace_id !== synthRes.trace_id) continue;
      ev = candidate;
      break;
    }
    expect(ev.session).toBe('claude');

    // Hand the clip to the scheduler. This is what app.js does on each
    // SSE 'audio' event. In a real browser the relative URLs in the
    // event are resolved against the document origin; here we have no
    // document, so we absolutize them ourselves.
    const wsBase = baseUrl.replace(/^http/, 'ws');
    const streamUrl = ev.stream_url
      ? (ev.stream_url.startsWith('/clips/')
          ? baseUrl + ev.stream_url
          : wsBase + ev.stream_url)
      : undefined;
    scheduler.ingest({
      url: baseUrl + ev.url,
      session: ev.session, ts: ev.ts || Date.now(),
      streamable: !!ev.streamable,
      stream_url: streamUrl,
    });

    // Wait for the scheduler to finish processing this clip. We give it
    // generous time because the chunked-streaming case has to wait for
    // the harness's chunk delays to elapse.
    const totalChunkTime = (chunks.length * chunkDelayMs) + 2000;
    try {
      await waitUntil(() => lastResult !== null && !scheduler._peek().length,
                      totalChunkTime + 5000, 50);
    } catch (e) {
      // Dump diagnostics so the failure is debuggable.
      process.stderr.write(`\n[test] timeout — trace tail:\n`);
      for (const t of trace.slice(-30)) {
        process.stderr.write(`  ${t.t}  ${t.ev}  ${t.det}\n`);
      }
      process.stderr.write(`[test] audio events: ${JSON.stringify(audio._eventLog)}\n`);
      process.stderr.write(`[test] bytes=${audio._bytesReceived} dur=${audio.duration} rs=${audio.readyState}\n`);
      process.stderr.write(`[test] src=${audio.src}\n`);
      throw e;
    }

    abort.abort();
    let completeBytes = 0;
    let completeStatus = 0;
    if (ev.complete_url) {
      const completeRes = await fetch(baseUrl + ev.complete_url);
      completeStatus = completeRes.status;
      completeBytes = new Uint8Array(await completeRes.arrayBuffer()).length;
    }

    return {
      totalBytes: audio._bytesReceived,
      completeBytes,
      completeStatus,
      premature: lastResult?.premature,
      finalDuration: audio.duration,
      eventLog: audio._eventLog,
      transferEncoding: audio._lastTransferEncoding,
      contentLength: audio._lastContentLength,
      ev,
      trace,
    };
  }

  it('plays full audio via iOS fallback path (chunked /audio/<file>)', async () => {
    // Force MSE off — same code path iOS takes.
    const chunks = ['AAAA', 'BBBB', 'CCCC', 'DDDD'];
    const expectedBytes = chunks.join('').length; // 16
    const result = await playOneTurn({
      chunks, chunkDelayMs: 80, mseSupported: false,
    });

    // The audio event must carry streamable so the client picks the right
    // path. (Pins the herald-forwarding chain.)
    expect(result.ev.streamable).toBe(true);
    expect(result.ev.stream_url).toMatch(/^\/clips\/\d+\/stream$/);
    expect(result.ev.complete_url).toMatch(/^\/clips\/\d+\/complete\.mp3$/);

    // Full payload arrived via the chunked response.
    expect(result.totalBytes).toBe(expectedBytes);
    expect(result.completeStatus).toBe(200);
    expect(result.completeBytes).toBe(expectedBytes);

    // The server used chunked transfer (i.e. our C-2 fix engaged).
    expect(result.transferEncoding.toLowerCase()).toBe('chunked');
    expect(result.contentLength).toBe('');

    // Scheduler considered the clip done normally, NOT premature. This
    // is the assertion that pins bug C-3: if the safety cap fired
    // instantly on duration=Infinity, premature would be true (or the
    // scheduler would have retried, leaving lastResult unset).
    expect(result.premature).toBe(false);

    // The 'ended' event fired (not just the safety cap timing out).
    const endedIdx = result.eventLog.findIndex(e => e.type === 'ended');
    expect(endedIdx).toBeGreaterThanOrEqual(0);
  }, 30000);

  it('plays full audio via MSE/WS streaming path', async () => {
    const chunks = ['AAAA', 'BBBB', 'CCCC'];
    const expectedBytes = chunks.join('').length; // 12
    const result = await playOneTurn({
      chunks, chunkDelayMs: 80, mseSupported: true,
    });
    expect(result.ev.streamable).toBe(true);
    expect(result.totalBytes).toBe(expectedBytes);
    expect(result.premature).toBe(false);
    const endedIdx = result.eventLog.findIndex(e => e.type === 'ended');
    expect(endedIdx).toBeGreaterThanOrEqual(0);
  }, 30000);
});


function waitUntil(predicate, timeoutMs, intervalMs) {
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    const tick = () => {
      let ok;
      try { ok = predicate(); }
      catch (e) { return reject(e); }
      if (ok) return resolve();
      if (Date.now() - t0 > timeoutMs) {
        return reject(new Error(`waitUntil timeout after ${timeoutMs}ms`));
      }
      setTimeout(tick, intervalMs);
    };
    tick();
  });
}
