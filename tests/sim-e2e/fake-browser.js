// Minimal browser-globals fake faithful enough that the real client JS
// modules (player-adapter.js, audio-queue.js, streaming-player.js) run
// unmodified under Node. Models only what those modules actually touch.
//
// The behaviors we care about (the ones that have bitten us in prod):
//   - audioEl.duration === Infinity when the response is chunked
//     (Transfer-Encoding without Content-Length). This is the trap that
//     produced bug C-3: setTimeout(fn, Infinity) clamps to 1ms.
//   - 'canplaythrough' fires once enough bytes have arrived, not at
//     stream end. So play() can start before the chunked stream closes.
//   - 'ended' fires when the stream is fully drained.
//   - audioEl.error.code is set when the fetch fails / returns non-2xx.
//
// What we DON'T model: actual audio decoding, accurate currentTime
// progression, MSE timestamp offset math. The tests only need to know
// "did the full payload arrive AND did the scheduler advance through
// the clip normally."

import { EventEmitter } from 'node:events';
// Node 18+ has fetch and WebSocket as globals — no extra deps needed.

/** Anything in the audio element we don't model: */
const NOT_MODELED = Symbol('not-modeled');

export class FakeAudio extends EventEmitter {
  /** @param {{baseUrl?: string}} [opts]
   *  baseUrl prefixes relative URLs (e.g. "/audio/X.mp3" → "<baseUrl>/audio/X.mp3")
   *  so the fake works in Node where there's no document origin to resolve
   *  against. streaming-player.js's fallback explicitly strips the host from
   *  the WS URL when producing the static-file path, so we have to re-add it. */
  constructor(opts = {}) {
    super();
    this._baseUrl = opts.baseUrl || '';
    this._src = '';
    this._readyState = 0;
    this._duration = NaN;
    this._currentTime = 0;
    this._error = null;
    this._paused = true;
    this.playbackRate = 1;
    this.defaultPlaybackRate = 1;
    this.currentSrc = '';
    this._bytesReceived = 0;
    this._fetchAbort = null;
    this._mediaSource = null;
    // Tests can poke at this to verify the chunked path was taken.
    this._lastTransferEncoding = '';
    this._lastContentLength = '';
    this._eventLog = [];
  }

  // ---- HTMLAudioElement-ish surface ----------------------------------

  get src() { return this._src; }
  set src(v) {
    // Resolve relative URLs against the configured base — this is what
    // a real browser does using the document origin.
    if (v && !v.startsWith('blob:') && !/^[a-z]+:\/\//.test(v)) {
      v = this._baseUrl + v;
    }
    this._src = v;
    this.currentSrc = v;
    // Reset per-load state.
    this._readyState = 0;
    this._duration = NaN;
    this._currentTime = 0;
    this._bytesReceived = 0;
    this._error = null;
    this._cancelFetch();
    if (v.startsWith('blob:')) {
      // MSE path: streaming-player attached a MediaSource via createObjectURL.
      // The bytes are fed by sourceBuffer.appendBuffer, not by us fetching.
      // Fire metadata + canplaythrough as soon as the first sourceBuffer
      // append lands (mocked via _mediaSourceBytesAppended below).
      return;
    }
    if (v) this._beginFetch(v);
  }

  get duration() { return this._duration; }
  set duration(d) { this._duration = d; } // tests may force this
  get currentTime() { return this._currentTime; }
  set currentTime(t) { this._currentTime = t; }
  get readyState() { return this._readyState; }
  get error() { return this._error; }
  get paused() { return this._paused; }

  load() { /* The src setter already kicks off the fetch. */ }

  async play() {
    this._paused = false;
    this._dispatch('play');
    this._dispatch('playing');
    return undefined;
  }
  pause() {
    this._paused = true;
    this._dispatch('pause');
  }

  addEventListener(type, fn, opts) {
    if (opts && opts.once) {
      const wrap = (...a) => { this.off(type, wrap); fn(...a); };
      this.on(type, wrap);
    } else {
      this.on(type, fn);
    }
  }
  removeEventListener(type, fn) { this.off(type, fn); }

  // ---- internals ------------------------------------------------------

  _dispatch(type) {
    this._eventLog.push({ t: Date.now(), type });
    this.emit(type);
  }

  _cancelFetch() {
    if (this._fetchAbort) {
      try { this._fetchAbort.abort(); } catch {}
      this._fetchAbort = null;
    }
  }

  /**
   * Real-ish progressive download: fetch the URL, stream the body, fire
   * loadedmetadata + canplaythrough as bytes arrive, fire 'ended' when
   * the stream closes. Models duration=Infinity for chunked responses.
   *
   * Recognises HLS playlists (m3u8): instead of streaming the playlist
   * itself, it parses the segment list, fetches each segment in order,
   * accumulates byte counts, fires loadedmetadata after the first
   * segment, canplaythrough once the playlist is finalised (#EXT-X-ENDLIST),
   * and `ended` once all segments have been fetched. This matches iOS
   * Safari's observable behaviour closely enough for the tests we care
   * about (does the player route to the right URL? did all bytes arrive?).
   */
  async _beginFetch(url) {
    if (/playlist\.m3u8(\?|$)/.test(url)) {
      return this._beginHlsFetch(url);
    }
    const ctrl = new AbortController();
    this._fetchAbort = ctrl;
    let res;
    try {
      res = await fetch(url, { signal: ctrl.signal });
    } catch (e) {
      this._error = { code: 4 /* MEDIA_ERR_SRC_NOT_SUPPORTED */ };
      this._dispatch('error');
      return;
    }
    if (!res.ok) {
      this._error = { code: 4 };
      this._dispatch('error');
      return;
    }
    this._lastTransferEncoding = res.headers.get('transfer-encoding') || '';
    this._lastContentLength = res.headers.get('content-length') || '';
    // Real browser behavior: when the response has no Content-Length (i.e.
    // Transfer-Encoding: chunked), duration is Infinity until the stream
    // ends. When Content-Length is present we use a fixed-bitrate guess
    // so the safety-cap math has a finite duration to work with.
    if (this._lastContentLength) {
      const bytes = Number(this._lastContentLength);
      // Assume ~32 kbps mono mp3-ish — duration in seconds.
      this._duration = bytes / 4000;
    } else {
      this._duration = Infinity;
    }
    this._readyState = 1; // HAVE_METADATA
    this._dispatch('loadedmetadata');

    const reader = res.body.getReader();
    let first = true;
    while (true) {
      let chunk;
      try {
        chunk = await reader.read();
      } catch (e) {
        // aborted — silent return so the next src= can take over.
        return;
      }
      if (chunk.done) break;
      this._bytesReceived += chunk.value.byteLength;
      if (first) {
        first = false;
        this._readyState = 3; // HAVE_FUTURE_DATA — enough to start playing
        this._dispatch('canplay');
        this._readyState = 4; // HAVE_ENOUGH_DATA
        this._dispatch('canplaythrough');
      }
    }
    // Stream is fully drained. In a real browser, currentTime would
    // continue to advance and 'ended' fires at duration. We compress
    // that here: jump currentTime to duration (if finite) and fire ended.
    if (Number.isFinite(this._duration)) {
      this._currentTime = this._duration;
    } else {
      // Chunked case: invent a duration so the queue's premature-detector
      // doesn't trigger. Real browsers update duration to the actual
      // value once the stream closes — same thing here.
      this._duration = this._bytesReceived / 4000;
      this._currentTime = this._duration;
    }
    this._dispatch('ended');
  }

  /** HLS fetch: poll the playlist until #EXT-X-ENDLIST, fetching each
   *  newly-listed segment as it appears. Accumulates segment bytes into
   *  _bytesReceived. Fires loadedmetadata/canplaythrough/ended at the
   *  shape iOS Safari uses for native HLS playback. */
  async _beginHlsFetch(url) {
    this._duration = NaN;
    this._lastTransferEncoding = '';
    this._lastContentLength = '';
    this._segmentsFetched = [];
    const ctrl = new AbortController();
    this._fetchAbort = ctrl;

    const baseUrl = url.replace(/playlist\.m3u8.*$/, '');
    const seenSegments = new Set();
    const deadline = Date.now() + 15000;
    let endlistSeen = false;
    let metadataFired = false;

    while (!endlistSeen && Date.now() < deadline) {
      let res;
      try {
        res = await fetch(url, { signal: ctrl.signal });
      } catch (e) {
        // Likely an abort during shutdown — bail silently.
        return;
      }
      if (res.status === 404) {
        // Playlist not yet written by ffmpeg — retry. iOS Safari does
        // the same thing: polls the playlist until segments appear.
        await new Promise(r => setTimeout(r, 100));
        continue;
      }
      if (!res.ok) {
        this._error = { code: 4 };
        this._dispatch('error');
        return;
      }
      const text = await res.text();
      endlistSeen = /#EXT-X-ENDLIST/.test(text);

      // Each segment URI is a line not starting with '#'.
      for (const line of text.split(/\r?\n/)) {
        const seg = line.trim();
        if (!seg || seg.startsWith('#')) continue;
        if (seenSegments.has(seg)) continue;
        seenSegments.add(seg);

        const segUrl = baseUrl + seg;
        try {
          const segRes = await fetch(segUrl, { signal: ctrl.signal });
          if (!segRes.ok) continue;
          const buf = new Uint8Array(await segRes.arrayBuffer());
          this._bytesReceived += buf.byteLength;
          this._segmentsFetched.push(seg);
          if (!metadataFired) {
            metadataFired = true;
            this._readyState = 1;
            this._dispatch('loadedmetadata');
            this._readyState = 3;
            this._dispatch('canplay');
            this._readyState = 4;
            this._dispatch('canplaythrough');
          }
        } catch {
          // Likely abort — bail.
          return;
        }
      }

      if (!endlistSeen) {
        await new Promise(r => setTimeout(r, 100));
      }
    }

    // Set a plausible duration so the safety-cap doesn't bail. Real
    // browsers learn duration from segment durations parsed out of the
    // playlist; we just use bytes/4000 as a stand-in.
    this._duration = Math.max(this._bytesReceived / 4000, 0.1);
    this._currentTime = this._duration;
    this._dispatch('ended');
  }

  /** Used by FakeMediaSource: signals that bytes have been appended via
   *  the SourceBuffer.appendBuffer path, so we can fire canplaythrough. */
  _mediaSourceBytesAppended(byteLength) {
    this._bytesReceived += byteLength;
    if (this._readyState < 4) {
      this._readyState = 4;
      this._dispatch('canplay');
      this._dispatch('canplaythrough');
    }
  }

  /** Used by FakeMediaSource: signals endOfStream(). Fire ended. */
  _mediaSourceEndOfStream() {
    if (!Number.isFinite(this._duration)) {
      this._duration = this._bytesReceived / 4000;
    }
    this._currentTime = this._duration;
    this._dispatch('ended');
  }
}


// ---- MediaSource fake ----------------------------------------------------

let _msSupported = true;
export function setMediaSourceSupported(supported) { _msSupported = supported; }

export class FakeMediaSource extends EventEmitter {
  static isTypeSupported(_mime) { return _msSupported; }
  constructor() {
    super();
    this.readyState = 'closed';
    this._sourceBuffers = [];
    this._owner = null; // set when createObjectURL wires this to a FakeAudio
    // Real spec: sourceopen fires asynchronously after the URL is attached
    // to an audio element. We emit it on next tick when our owner is set.
  }
  addEventListener(t, fn, o) {
    if (o && o.once) {
      const wrap = (...a) => { this.off(t, wrap); fn(...a); };
      this.on(t, wrap);
    } else {
      this.on(t, fn);
    }
  }
  removeEventListener(t, fn) { this.off(t, fn); }
  addSourceBuffer(_mime) {
    const sb = new FakeSourceBuffer(this);
    this._sourceBuffers.push(sb);
    return sb;
  }
  endOfStream() {
    this.readyState = 'ended';
    if (this._owner) this._owner._mediaSourceEndOfStream();
  }
  _open() {
    this.readyState = 'open';
    this.emit('sourceopen');
  }
}

class FakeSourceBuffer extends EventEmitter {
  constructor(ms) {
    super();
    this._ms = ms;
    this.updating = false;
  }
  addEventListener(t, fn, o) {
    if (o && o.once) {
      const wrap = (...a) => { this.off(t, wrap); fn(...a); };
      this.on(t, wrap);
    } else {
      this.on(t, fn);
    }
  }
  removeEventListener(t, fn) { this.off(t, fn); }
  appendBuffer(buf) {
    this.updating = true;
    const bytes = (buf && (buf.byteLength || buf.length)) || 0;
    setImmediate(() => {
      this.updating = false;
      if (this._ms._owner) this._ms._owner._mediaSourceBytesAppended(bytes);
      this.emit('updateend');
    });
  }
}


// ---- URL.createObjectURL / revokeObjectURL ------------------------------

const _blobMap = new Map();
let _blobSeq = 0;

export function installBrowserGlobals(audioEl) {
  // Always replace URL.createObjectURL — Node 18+ ships a native version
  // that only accepts Blob, but streaming-player passes a MediaSource.
  globalThis.URL.createObjectURL = (obj) => {
    const id = `blob:fake/${++_blobSeq}`;
    _blobMap.set(id, obj);
    // The streaming-player sets `audioEl._mediaSource = mediaSource`
    // BEFORE assigning audio.src = blobUrl, so we can look up the
    // MediaSource by peeking at audioEl._mediaSource on next tick.
    setImmediate(() => {
      if (audioEl._mediaSource &&
          audioEl._mediaSource instanceof FakeMediaSource &&
          audioEl._mediaSource.readyState === 'closed') {
        audioEl._mediaSource._owner = audioEl;
        audioEl._mediaSource._open();
      }
    });
    return id;
  };
  globalThis.URL.revokeObjectURL = (id) => { _blobMap.delete(id); };
  globalThis.MediaSource = FakeMediaSource;
  // WebSocket and fetch are Node built-ins — leave them alone.
  // performance.now() — streaming-player tries it first, falls back to Date.
  if (!globalThis.performance) {
    globalThis.performance = { now: () => Date.now() };
  }
}
