#!/usr/bin/env node
// Drive the real scheduler + state machine through a scripted multi-agent
// conversation. Output is rendered as a chat transcript: User / Mike /
// Rachel / Bella, each line is the actual text that would be heard.
//
//   $ node scripts/simulate-conversation.mjs
//
// The herald / permission flow is mocked here (server-side not yet built):
// when a non-current agent wants to speak we emit a tiny "X here, ready for
// an update" herald, hold the real content in a per-session buffer, and
// release it only after the user grants permission via a phrase the intent
// regex accepts. This lets you preview the full UX before we wire it for real.

import { createStateMachine } from '../static/lib/state-machine.js';
import { createScheduler } from '../static/lib/audio-queue.js';

// ---- intent: a tiny port of lib/intent.py's regex pass ------------------

const AFFIRMS = /\b(yes|yeah|yep|sure|ok|okay|go\s*ahead|go\s*on|tell\s*me|talk|shoot|what(?:'s|\s*is)?\s*(?:up|it)|i'?m\s*listening)\b/i;
const DECLINES = /\b(not\s*now|later|hold\s*on|wait|hush|be\s*quiet|stop\s*talking)\b/i;

function classifyIntent(text, candidates) {
  const grants = [], declines = [];
  const norm = text.toLowerCase().replace(/[.,;:!?\"]+/g, ' ').replace(/\s+/g, ' ').trim();
  for (const cand of candidates) {
    const re = new RegExp(`\\b${cand.toLowerCase()}\\b`);
    const m = re.exec(norm);
    if (!m) {
      // bare decline + single pending → assume that one
      if (candidates.length === 1 && DECLINES.test(norm)) declines.push(cand);
      continue;
    }
    const words = norm.split(' ');
    const idx = words.findIndex(w => w === cand.toLowerCase());
    const win = words.slice(Math.max(0, idx - 3), idx + 4).join(' ');
    if (AFFIRMS.test(win)) { grants.push(cand); continue; }
    if (DECLINES.test(win)) { declines.push(cand); continue; }
    // trailing question after a vocative name
    if (new RegExp(`\\b${cand.toLowerCase()}\\s*\\?`).test(text.toLowerCase())) {
      grants.push(cand); continue;
    }
  }
  return { grants, declines };
}

// ---- pretty printer -----------------------------------------------------

const personas = {
  claude: 'Mike',
  rachel: 'Rachel',
  bella: 'Bella',
};
const say = (who, text, tag = '') => {
  const label = tag ? `${who} ${tag}` : who;
  console.log(`${label.padEnd(18)} ${text}`);
};

// ---- the world ----------------------------------------------------------

let pane = 'claude';
const machine = createStateMachine({
  now: () => Date.now(),
  awaitDeadlineMs: 30_000,
});

// Per-session held buffer for content that's waiting on permission.
const heldBuffer = new Map();   // sid -> [{ text }]
const pendingHeralds = new Set();   // sids whose heralds we played, awaiting answer

const player = {
  async play(clip) {
    say(clip.who, clip.text, clip.tag);
    await new Promise(r => setTimeout(r, 50));
    return { premature: false, duration: 1, currentTime: 1 };
  },
};

const scheduler = createScheduler({
  machine, player,
  currentSession: () => pane,
  log: () => {},
});

let clipId = 0;
let lastTs = 0;
function nextClip(sid, text, opts = {}) {
  lastTs = Math.max(Date.now(), lastTs + 1);
  return {
    url: `c${++clipId}`,
    session: sid,
    ts: lastTs,
    who: personas[sid] || sid,
    text,
    tag: opts.tag || '',
  };
}

// Real reply (just plays through scheduler).
function emit(sid, text) {
  scheduler.ingest(nextClip(sid, text));
}

// Background agent wants to talk while user is with someone else.
// Server-side (when implemented): play a short herald, buffer the real
// content. Simulation: same semantics.
function raiseHand(sid, text) {
  const persona = personas[sid];
  scheduler.ingest(nextClip(sid, `${persona} here, ready for an update.`, { tag: '(herald)' }));
  const buf = heldBuffer.get(sid) || [];
  buf.push({ text });
  heldBuffer.set(sid, buf);
  pendingHeralds.add(sid);
}

// the user speaks: print, route through state machine, and classify intent
// against any pending heralds.
async function user(textToSay, targetSid = null) {
  say('User', textToSay);
  // 1) Intent check against pending heralds (a non-recording utterance can
  // grant permission to a previously-heralded agent).
  if (pendingHeralds.size) {
    const candidates = [...pendingHeralds].map(s => personas[s]);
    const res = classifyIntent(textToSay, candidates);
    for (const personaName of res.grants) {
      const sid = Object.entries(personas).find(([, n]) => n === personaName)[0];
      pendingHeralds.delete(sid);
      const buf = heldBuffer.get(sid) || [];
      heldBuffer.delete(sid);
      // Briefly switch the addressee so the scheduler treats each buffered
      // clip as the focal one.
      pane = sid;
      for (const c of buf) emit(sid, c.text);
    }
  }
  // 2) If this utterance was an addressed prompt to an agent, drive the
  // state machine through recording → awaiting.
  if (targetSid) {
    pane = targetSid;
    machine.startRecording(targetSid);
    await sleep(20);
    machine.endRecording();
    await sleep(20);
    machine.send(targetSid);
  }
}

function lookAt(sid) {
  say(`(user looks at ${personas[sid]}'s pane)`, '');
  pane = sid;
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ---- the script ---------------------------------------------------------

(async () => {
  console.log('==================== claude-pwa conversation =====================\n');

  // Beat 1: focused turn with Mike.
  await user('Hey Mike, what is two plus two?', 'claude');
  await sleep(120);
  emit('claude', 'Four.');
  await sleep(120);

  // Beat 2: pivot to Rachel. Mike pops back in with a background follow-up
  // — that should HERALD, not just barge in, because user is awaiting Rachel.
  await user('Rachel, what do you think about lunch?', 'rachel');
  await sleep(80);
  raiseHand('claude', 'By the way, four is also two times two.');
  await sleep(120);
  emit('rachel', 'Pasta sounds good.');
  await sleep(300);

  // Beat 3: user grants Mike permission to speak.
  await user('Sure, Mike, what is it?');
  await sleep(300);

  // Beat 4: pivot to Bella. Rachel and Mike both raise hands while Bella
  // thinks. User picks one to grant.
  await user('Bella, recommend a song.', 'bella');
  await sleep(50);
  raiseHand('rachel', 'Actually, soup is even better.');
  await sleep(40);
  raiseHand('claude', 'I have three quick math facts about pasta.');
  await sleep(120);
  emit('bella', 'Bossa nova vibes.');
  await sleep(300);

  // Beat 5: user grants Rachel only (Mike's update stays buffered).
  await user('Go ahead, Rachel.');
  await sleep(200);

  // Beat 6: user explicitly addresses Mike to release his too.
  await user('Mike?');
  await sleep(400);

  // Buffer should be drained at this point.
  console.log('\nremaining buffered:', [...heldBuffer.entries()].map(
    ([s, b]) => `${personas[s]}=${b.length}`).join(' ') || 'none');
})();
