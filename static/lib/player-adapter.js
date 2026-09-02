// Wraps an HTMLAudioElement with the {play(clip) → Promise<{premature, ...}>}
// shape the audio-queue scheduler expects.
//
// Bugs this module pins:
//   R4: the original adapter awaited canplaythrough with no fallback — if the
//       event raced past us, the whole queue hung. Now we also listen for
//       'error' and cap the wait with a timeout.

// Bumped whenever this module's contract changes. Visible in the eventlog
// as `client.playerAdapterLoaded ver=N` on import so we can tell from a
// trace whether the iPhone's PWA actually picked up our latest JS or is
// running a stale SW-cached copy.
export const PLAYER_ADAPTER_VERSION = 'stage7-history-typography';

/**
 * @typedef {{ url: string, session: string, ts?: number, streamable?: boolean,
 *             stream_url?: string, playlist_url?: string }} Clip
 *   `url` is the plain <audio src> every browser can play. `playlist_url`
 *   (HLS) or a `/clips/<id>/stream` `stream_url` (chunked HTTP) are preferred
 *   when present because they start before synthesis has finished.
 */

/**
 * @param {HTMLAudioElement} audioEl
 * @param {object} [opts]
 * @param {number} [opts.speed]            playbackRate to apply (default 1.2)
 * @param {number} [opts.loadTimeoutMs]    fallback after canplaythrough never fires
 * @param {(event: string, detail?: string) => void} [opts.log]  — diagnostics hook
 * @param {(on: boolean) => void} [opts.showSpeaking]  — UI hook
 */
export function createPlayerAdapter(audioEl, opts = {}) {
  const speed = opts.speed ?? 1.2;
  const loadTimeoutMs = opts.loadTimeoutMs ?? 5000;
  const log = opts.log || (() => {});
  const showSpeaking = opts.showSpeaking || (() => {});

  return {
    async play(clip) {
      log('playStart', clip.url);
      let directStream = false;
      if (clip.playlist_url) {
        // HLS delivery path. iOS Safari plays HLS natively via plain
        // <audio src=...> — no MSE, no Range hacks, no chunked-TE
        // quirks. The whole iOS streaming-bug surface goes away here.
        // Desktop browsers fall through to hls.js territory in a future
        // pass; today's desktop tests assert this path runs unchanged.
        const playlist = normalizePlaylistUrl(clip.playlist_url);
        if (playlist) {
          directStream = true;
          log('playHls', playlist);
          audioEl.src = playlist;
        } else {
          audioEl.src = clip.url;
        }
      } else if (clip.streamable && clip.stream_url) {
        const directUrl = normalizeDirectStreamUrl(clip.stream_url);
        if (directUrl) {
          directStream = true;
          log('playDirectStream', directUrl);
          audioEl.src = directUrl;
        } else {
          audioEl.src = clip.url;
        }
      } else {
        audioEl.src = clip.url;
      }
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
        log('playWaiting', `rs=${audioEl.readyState}`);
        await new Promise(res => {
          const cleanup = () => {
            audioEl.removeEventListener('canplaythrough', onReady);
            audioEl.removeEventListener('error', onFail);
            clearTimeout(timer);
          };
          const onReady = () => { cleanup(); res(); };
          const onFail  = () => {
            cleanup();
            log('playLoadFail', `code=${audioEl.error && audioEl.error.code}`);
            res();
          };
          audioEl.addEventListener('canplaythrough', onReady, { once: true });
          audioEl.addEventListener('error',         onFail,  { once: true });
          const timer = setTimeout(() => {
            cleanup();
            log('playLoadTimeout', `rs=${audioEl.readyState}`);
            res();
          }, loadTimeoutMs);
        });
      }

      log('playCalling', `rs=${audioEl.readyState}`);
      try {
        const p = audioEl.play();
        if (p && p.then) await p;
        log('playOk', String(audioEl.currentSrc || '').split('/').pop());
      } catch (err) {
        log('playFail', err && err.name);
        showSpeaking(false);
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
          log('playEndTimeout', `cap=${cap}ms cur=${audioEl.currentTime}`);
          finish(false);
        }, cap);
        const finish = premature => {
          clearTimeout(safety);
          audioEl.removeEventListener('ended', onEnd);
          audioEl.removeEventListener('error', onError);
          showSpeaking(false);
          resolve({
            premature,
            duration: audioEl.duration || 0,
            currentTime: audioEl.currentTime || 0,
          });
        };
        const onEnd = () => {
          const dur = audioEl.duration || 0;
          const cur = audioEl.currentTime || 0;
          finish(dur > 1 && cur > 0 && cur < dur * 0.85);
        };
        const onError = () => finish(false);
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
