// Sim-e2e: the iOS Safari Range-request pattern.
//
// iOS Safari sends `Range: bytes=0-1` to probe an audio URL BEFORE the
// real progressive fetch. The trace of the cur=0.000001 bug showed two
// GETs 5ms apart, and the audio never advanced past the first
// microsecond. The cause: the original /clips/<id>/stream endpoint
// answered Range probes with chunked Transfer-Encoding + no
// Content-Length, leaving iOS's <audio> element in a "loaded but won't
// decode" state.
//
// This test pins the fix: when `Range:` is present, the endpoint MUST
// respond with 206 Partial Content + Content-Range, NOT chunked TE.
//
// It uses raw fetch (not the FakeAudio's whole flow) because we want to
// assert on the wire-level response headers / status, not playback.

import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect, beforeAll, afterAll } from 'vitest';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const HARNESS = path.join(__dirname, 'server_harness.py');


function startHarness() {
  // Range support lives in the chunked-file delivery's serve_clip_stream.
  // HLS is the default now, so pin chunked-file explicitly for these tests.
  const proc = spawn('python3', [HARNESS], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, CLAUDE_PWA_DELIVERY: 'chunked-file' },
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


describe('sim-e2e: iOS Safari Range probe pattern', () => {
  let ctx;
  let banner;

  beforeAll(async () => {
    ctx = startHarness();
    banner = await nextReply(ctx);
    expect(banner.ready).toBe(true);
  }, 15000);

  afterAll(async () => {
    if (ctx && !ctx.closed) {
      try { await send(ctx, { cmd: 'exit' }); } catch {}
      try { ctx.proc.kill(); } catch {}
    }
  });

  /** Run one synth, return the stream_url to fetch and wait until the
   *  producer is COMPLETE (we want to test the static-file Range branch). */
  async function synthAndWaitComplete(chunks, chunkDelayMs) {
    const baseUrl = `http://127.0.0.1:${banner.port}`;
    const abort = new AbortController();
    const sseIter = sseAudioEvents(baseUrl, abort.signal);

    const synthRes = await send(ctx, {
      cmd: 'synth', text: 'iOS Range probe',
      voice: 'V_MIKE', session: 'claude',
      chunks, chunk_delay_ms: chunkDelayMs,
    });

    let ev;
    while (true) {
      const r = await Promise.race([
        sseIter.next(),
        new Promise((_, rej) =>
          setTimeout(() => rej(new Error('no audio event in 10s')), 10000)),
      ]);
      const cand = r.value;
      if (cand && cand.trace_id === synthRes.trace_id) { ev = cand; break; }
    }
    abort.abort();

    // Wait for the producer to mark complete (chunks * delay + some slack).
    const expectedMs = (chunks.length * chunkDelayMs) + 1500;
    await new Promise(r => setTimeout(r, expectedMs));
    return { baseUrl, ev };
  }

  it('Range: bytes=0-1 returns 206 with Content-Range + 2 bytes', async () => {
    // Reproduce the iOS probe: GET /clips/<id>/stream with Range: bytes=0-1.
    // Before the fix this returned chunked TE; iOS got confused and the
    // <audio> element stalled at cur=0.000001 forever.
    const chunks = ['AAAA', 'BBBB', 'CCCC'];
    const expected = chunks.join('');
    const { baseUrl, ev } = await synthAndWaitComplete(chunks, 60);

    const res = await fetch(baseUrl + ev.stream_url, {
      headers: { 'Range': 'bytes=0-1' },
    });

    expect(res.status).toBe(206);
    // The shape iOS expects to know "this is a normal Range response":
    expect(res.headers.get('Content-Range')).toMatch(
      /^bytes 0-1\/(\d+|\*)$/
    );
    expect(res.headers.get('Content-Length')).toBe('2');
    // And ABSOLUTELY NOT chunked Transfer-Encoding — that's what broke
    // iOS before. Note: Node's fetch may auto-decode chunked, so we check
    // the raw bytes received and that no chunked header surfaces.
    expect(res.headers.get('Transfer-Encoding')).toBeNull();

    const body = new Uint8Array(await res.arrayBuffer());
    expect(body.length).toBe(2);
    expect(String.fromCharCode(...body)).toBe(expected.slice(0, 2));
  }, 15000);

  it('Range: bytes=0- returns 206 with full Content-Range', async () => {
    // The second iOS GET (after the probe) usually asks for the rest of
    // the file. Open-ended Range with the producer COMPLETE should give
    // us Content-Range with a known total.
    const chunks = ['AAAA', 'BBBB', 'CCCC'];
    const expected = chunks.join('');
    const { baseUrl, ev } = await synthAndWaitComplete(chunks, 60);

    const res = await fetch(baseUrl + ev.stream_url, {
      headers: { 'Range': 'bytes=0-' },
    });

    expect(res.status).toBe(206);
    const range = res.headers.get('Content-Range');
    expect(range).toMatch(/^bytes 0-\d+\/\d+$/);
    // Total is known when the producer is complete — no `*` here.
    expect(range).not.toMatch(/\/\*$/);
    expect(res.headers.get('Transfer-Encoding')).toBeNull();

    const body = new Uint8Array(await res.arrayBuffer());
    expect(body.length).toBe(expected.length);
    expect(String.fromCharCode(...body)).toBe(expected);
  }, 15000);

  it('no Range still returns chunked TE for in-flight, 200+Content-Length once complete', async () => {
    // Back-compat: clients that don't send Range (the desktop streaming-
    // player) keep getting the original behavior — chunked TE while live,
    // 200 + Content-Length once the producer has finished.
    const chunks = ['AAAA', 'BBBB', 'CCCC'];
    const expected = chunks.join('');
    const { baseUrl, ev } = await synthAndWaitComplete(chunks, 60);

    const res = await fetch(baseUrl + ev.stream_url);
    expect(res.status).toBe(200);
    expect(res.headers.get('Content-Length')).toBe(String(expected.length));
    const body = new Uint8Array(await res.arrayBuffer());
    expect(body.length).toBe(expected.length);
  }, 15000);
});
