// Microphone capture, voice-activity detection, transcription.
//
// Ported from app.js section 12 with its behaviour intact — the thresholds,
// the iOS workarounds and the watchdogs here were all calibrated against real
// hardware, and none of them are the kind of thing to "clean up" during a
// port. The only change is that `mic.classList.add('recording')` became
// `mic.recording = true` on a $state object the button reads.

import { Timing } from '@core/protocol.js';
import { clog, trace } from '../lib/net.js';
import { app, flash } from './app.svelte.js';
import {
  machine, playerAdapter, scheduler, tick, unlockAudio,
} from './audio.svelte.js';
import { send, sendText } from './send.svelte.js';

export const mic = $state({
  recording: false,   // always-on listening is armed
  capturing: false,   // a clip is being recorded right now
});

// VAD constants — calibrated on real hardware.
const ENERGY_ON      = Timing.VAD_ENERGY_ON;
const ENERGY_OFF     = Timing.VAD_ENERGY_OFF;
const ENERGY_ON_MS   = Timing.VAD_ENERGY_ON_MS;
const SILENCE_END_MS = Timing.SILENCE_END_MS;
const MIN_UTTER_MS   = Timing.MIN_UTTER_MS;
const GRACE_MS       = Timing.GRACE_MS;

let mediaStream = null;
let audioCtx = null;
let analyser = null;
let vadFrame = null;
let recorder = null;
let recordedChunks = [];
let recordedMime = '';
let captureStartedAt = 0;
let silenceStartedAt = 0;
let alwaysOn = false;
let singleShot = false;
let pendingText = '';
let graceTimer = null;
let captureStopWatchdog = null;
let energyAboveSince = 0;

function pickMimeType() {
  if (!window.MediaRecorder) return '';
  return ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm']
    .find(t => MediaRecorder.isTypeSupported(t)) || '';
}

export function teardownMic() {
  // Stop tracks so the next ensureMic() gets a fresh stream. Used after a
  // zero-byte capture, which on iOS means the audio pipeline went dead even
  // though the JS stream still looks alive.
  try { recorder && recorder.state !== 'inactive' && recorder.stop(); } catch (_) {}
  recorder = null;
  if (mediaStream) {
    try { mediaStream.getTracks().forEach(t => t.stop()); } catch (_) {}
    mediaStream = null;
  }
  if (audioCtx) {
    try { audioCtx.close(); } catch (_) {}
    audioCtx = null;
    analyser = null;
  }
  mic.capturing = false;
}

async function ensureMic() {
  // An existing stream whose tracks died (iOS after backgrounding) must be
  // torn down, not handed to the recorder.
  if (mediaStream) {
    const tracks = mediaStream.getAudioTracks();
    const dead = !tracks.length || tracks.every(t => t.readyState === 'ended' || !t.enabled);
    if (dead) {
      clog('micReacquire', 'previous stream had dead tracks');
      teardownMic();
    } else {
      return;
    }
  }
  // Surface the precise failure mode so "mic failed" reports aren't a mystery.
  if (!window.isSecureContext) {
    clog('micFail', `not a secure context: ${location.origin}`);
    throw new Error(`origin ${location.origin} is not https`);
  }
  if (!navigator.mediaDevices) {
    clog('micFail', `mediaDevices missing (secureContext=${window.isSecureContext})`);
    throw new Error('mediaDevices missing');
  }
  if (typeof navigator.mediaDevices.getUserMedia !== 'function') {
    clog('micFail', 'getUserMedia missing');
    throw new Error('getUserMedia missing');
  }
  if (!window.MediaRecorder) {
    clog('micFail', 'MediaRecorder missing');
    throw new Error('MediaRecorder missing');
  }
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    clog('micFail', `getUserMedia rejected: ${e && e.name}: ${e && e.message}`);
    throw e;
  }
  const Ctx = window.AudioContext || window.webkitAudioContext;
  audioCtx = new Ctx();
  if (audioCtx.state === 'suspended') await audioCtx.resume();
  const src = audioCtx.createMediaStreamSource(mediaStream);
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 512;
  analyser.smoothingTimeConstant = 0.4;
  src.connect(analyser);
}

function readEnergy() {
  const buf = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteFrequencyData(buf);
  const sr = audioCtx.sampleRate || 48000;
  const binHz = sr / 2 / buf.length;
  const lo = Math.floor(300 / binHz);
  const hi = Math.min(buf.length - 1, Math.ceil(3400 / binHz));
  let sum = 0, n = 0, peak = 0;
  for (let i = lo; i <= hi; i++) {
    sum += buf[i]; n++;
    if (buf[i] > peak) peak = buf[i];
  }
  const avg = n ? sum / n : 0;
  // Voice has a strong peak above the average (formants). A flat spectrum is
  // probably broadband noise — score it zero so it doesn't trigger capture.
  if (peak > 0 && avg > 0 && (peak / avg) < 1.25) return 0;
  return avg;
}

function startCapture() {
  if (mic.capturing || !mediaStream) return;
  recordedChunks = [];
  recordedMime = pickMimeType();
  try {
    recorder = recordedMime
      ? new MediaRecorder(mediaStream, { mimeType: recordedMime })
      : new MediaRecorder(mediaStream);
  } catch (e) {
    flash(`recorder: ${e.message}`, 3000);
    return;
  }
  recorder.ondataavailable = e => {
    if (e.data && e.data.size > 0) recordedChunks.push(e.data);
  };
  recorder.onstop = onCaptureEnd;
  // Timeslice mode: on iOS this fires ondataavailable during the recording
  // instead of only on stop, so a dead pipeline is detectable early.
  try { recorder.start(Timing.MEDIA_RECORDER_TIMESLICE_MS); }
  catch (e) { flash(`recorder start: ${e.message}`, 3000); return; }
  mic.capturing = true;
  captureStartedAt = Date.now();
  silenceStartedAt = 0;
  send.captureTarget = app.session;
  tick(660, 0.06);
  // A fresh recording means the user moved on. Drop clips older than 30s so
  // the queue doesn't dump a stale backlog after the gesture lands.
  try { scheduler?.flushOlderThan(30_000); } catch (_) {}
  // Don't interrupt playback yet — a brief VAD trigger from background noise
  // shouldn't stop the current clip mid-sentence.
  try { machine.startRecording(send.captureTarget); } catch (_) {}
}

function stopCapture() {
  if (!mic.capturing || !recorder) return;
  try { recorder.stop(); } catch (_) {}
  clearTimeout(captureStopWatchdog);
  captureStopWatchdog = setTimeout(() => {
    if (!mic.capturing) return;
    clog('captureStopWatchdog', 'recorder onstop did not fire');
    teardownMic();
    recordedChunks = [];
    try { machine.settle(); } catch (_) {}
    flash('Recorder stalled — tap again', 2500);
  }, Timing.CAPTURE_STOP_WATCHDOG_MS);
}

async function onCaptureEnd() {
  clearTimeout(captureStopWatchdog);
  captureStopWatchdog = null;
  mic.capturing = false;
  tick(440, 0.06);
  try { machine.endRecording(); } catch (_) {}

  const dur = Date.now() - captureStartedAt;
  const mime = (recorder && recorder.mimeType) || recordedMime || 'audio/webm';
  const blob = new Blob(recordedChunks, { type: mime });
  recordedChunks = [];
  if (dur < MIN_UTTER_MS || blob.size < 1024) {
    clog('captureBail', `dur=${dur}ms size=${blob.size}B`);
    // Zero bytes means iOS handed us a stream that produces no audio — tear
    // it down so the next ensureMic() reacquires.
    if (blob.size === 0 && dur >= MIN_UTTER_MS) {
      clog('captureZeroByte', 'reacquiring mic');
      teardownMic();
      flash('Mic stalled — tap again', 2500);
    }
    if (pendingText) commitPending();
    else {
      if (singleShot) { singleShot = false; stopAlwaysOn(); }
      try { machine.settle(); } catch (_) {}
    }
    return;
  }

  // Delay the 'Transcribing' flash so quick clips don't trigger it.
  const transcribingTimer = setTimeout(
    () => flash('Transcribing…', 1500),
    Timing.TRANSCRIBING_FLASH_MS,
  );
  let text = '', endsTerminal = false;
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), Timing.TRANSCRIBE_TIMEOUT_MS);
  try {
    const isHandsFree = alwaysOn && !singleShot;
    const r = await fetch('/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': mime, 'X-Hands-Free': isHandsFree ? '1' : '0' },
      body: blob,
      signal: ctrl.signal,
    });
    if (r.ok) {
      const d = await r.json();
      text = ((d && d.text) || '').trim();
      endsTerminal = !!(d && d.ends_terminal);
      if (text) send.pendingHandsFree = send.pendingHandsFree || !!(d && d.hands_free);
      // Every event from here to the next /transcribe is stamped with this
      // id, so SQL can join a whole turn by trace_id.
      if (d && d.trace_id) {
        trace.id = d.trace_id;
        clog('traceStart', trace.id);
      }
    } else {
      const t = await r.text();
      flash(`stt ${r.status}: ${t.slice(0, 80)}`, 3500);
    }
  } catch (err) {
    flash(`stt failed: ${err.message}`, 3500);
  } finally {
    clearTimeout(timeout);
  }
  clearTimeout(transcribingTimer);

  // Only interrupt playback once the capture is known to be real speech —
  // otherwise a stray VAD trigger kills the agent's current clip.
  if (text) playerAdapter?.interrupt();

  if (text) pendingText = pendingText ? `${pendingText} ${text}` : text;
  if (graceTimer) { clearTimeout(graceTimer); graceTimer = null; }
  if (endsTerminal || singleShot) commitPending();
  else if (pendingText) graceTimer = setTimeout(commitPending, GRACE_MS);
}

function commitPending() {
  if (graceTimer) { clearTimeout(graceTimer); graceTimer = null; }
  const txt = pendingText.trim();
  pendingText = '';
  const handsFree = send.pendingHandsFree;
  send.pendingHandsFree = false;
  if (txt) sendText(txt, { handsFree });
  else try { machine.settle(); } catch (_) {}
  if (singleShot) { singleShot = false; stopAlwaysOn(); }
}

function vadTick() {
  if (!alwaysOn || !analyser) return;
  const energy = readEnergy();
  const now = Date.now();
  if (singleShot) {
    // Tap-to-record: no auto-stop, the user controls it with a second tap.
  } else if (!mic.capturing) {
    // Require sustained energy before triggering — guards against a single
    // spike from a chair scrape or a breath.
    if (energy >= ENERGY_ON) {
      if (!energyAboveSince) energyAboveSince = now;
      else if (now - energyAboveSince >= ENERGY_ON_MS) {
        energyAboveSince = 0;
        startCapture();
      }
    } else {
      energyAboveSince = 0;
    }
  } else {
    if (energy < ENERGY_OFF) {
      if (!silenceStartedAt) silenceStartedAt = now;
      else if (now - silenceStartedAt >= SILENCE_END_MS) stopCapture();
    } else silenceStartedAt = 0;
  }
  vadFrame = requestAnimationFrame(vadTick);
}

export async function startAlwaysOn() {
  if (alwaysOn) return;
  try { await ensureMic(); }
  catch (e) { flash(`mic permission: ${e.message || e.name}`, 4500); return; }
  alwaysOn = true;
  singleShot = false;
  mic.recording = true;
  flash('Always-on (Whisper)', 1500);
  vadTick();
}

export function stopAlwaysOn() {
  alwaysOn = false;
  mic.recording = false;
  mic.capturing = false;
  if (vadFrame) cancelAnimationFrame(vadFrame);
  stopCapture();
  flash('Listening off', 1500);
}

export async function micTap() {
  if (alwaysOn) { stopAlwaysOn(); singleShot = false; return; }
  try { await ensureMic(); }
  catch (e) { flash(`mic permission: ${e.message || e.name}`, 4500); return; }
  singleShot = true;
  alwaysOn = true;
  mic.recording = true;
  flash('Recording (tap to cancel)', 1500);
  if (!mic.capturing) startCapture();
  vadTick();
}

/** Remote-action / URL-action entry point. */
export function toggleRecord() {
  unlockAudio();
  if (alwaysOn) { stopAlwaysOn(); singleShot = false; }
  else { singleShot = true; micTap(); }
}

export function resumeAudioContext() {
  if (alwaysOn && audioCtx && audioCtx.state === 'suspended') {
    audioCtx.resume().catch(() => {});
  }
}

