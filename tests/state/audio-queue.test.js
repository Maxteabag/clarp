// B3, B6, B15, B16: audio queue scheduling.

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { createStateMachine, States } from '../../static/lib/state-machine.js';
import { createScheduler } from '../../static/lib/audio-queue.js';

/** Build a player that completes immediately and records what played. */
function makePlayer({ premature = false } = {}) {
  const played = [];
  const player = {
    played,
    play(clip) {
      played.push(clip);
      return Promise.resolve({ premature, duration: 2, currentTime: 2 });
    },
  };
  return player;
}

describe('audio queue scheduler', () => {
  let clock = 0;
  let machine;
  let player;
  let currentSession = 'claude';

  beforeEach(() => {
    clock = 1_000_000;
    machine = createStateMachine({
      now: () => clock,
      awaitDeadlineMs: 60_000,
    });
    player = makePlayer();
  });

  it('does not play while recording (B16)', async () => {
    const s = createScheduler({
      machine, player, currentSession: () => currentSession,
    });
    machine.startRecording('claude');
    const r = s.ingest({ url: '/audio/1.mp3', session: 'rachel', ts: 10 });
    expect(r.accepted).toBe(true);
    // Give the microtask queue a turn.
    await Promise.resolve();
    expect(player.played).toEqual([]);
  });

  it('plays the addressee clip first in AWAITING (B15)', async () => {
    const s = createScheduler({
      machine, player, currentSession: () => 'claude',
    });
    machine.startRecording('claude');
    machine.endRecording();
    machine.send('claude');
    // Background agent's clip arrives first.
    s.ingest({ url: '/audio/r1.mp3', session: 'rachel', ts: 10 });
    await new Promise(r => setTimeout(r, 0));
    // Should NOT have played Rachel — we're awaiting Mike.
    expect(player.played).toEqual([]);
    // Mike's clip arrives.
    s.ingest({ url: '/audio/m1.mp3', session: 'claude', ts: 11 });
    await new Promise(r => setTimeout(r, 0));
    // Mike plays first, then Rachel drains.
    expect(player.played.map(c => c.url)).toEqual([
      '/audio/m1.mp3', '/audio/r1.mp3',
    ]);
  });

  it('falls back to FIFO when AWAITING deadline elapses (B15)', async () => {
    const s = createScheduler({
      machine, player, currentSession: () => 'claude',
    });
    machine.startRecording('claude');
    machine.endRecording();
    machine.send('claude');
    s.ingest({ url: '/audio/r1.mp3', session: 'rachel', ts: 10 });
    // Walk past the deadline.
    clock += 65_000;
    // Re-tick (the safety net would do this; in real client, state machine
    // transition or new ingest triggers it).
    s.ingest({ url: '/audio/r2.mp3', session: 'rachel', ts: 11 });
    await new Promise(r => setTimeout(r, 0));
    expect(player.played.map(c => c.url)).toEqual([
      '/audio/r1.mp3', '/audio/r2.mp3',
    ]);
  });

  it('skips clips older than lastAudioTs (B6)', async () => {
    const s = createScheduler({
      machine, player, currentSession: () => 'claude',
    });
    // Two clips: a recent one, then an older replay.
    s.ingest({ url: '/audio/new.mp3', session: 'claude', ts: 100 });
    await new Promise(r => setTimeout(r, 0));
    const r = s.ingest({ url: '/audio/old.mp3', session: 'claude', ts: 50 });
    expect(r.accepted).toBe(false);
    expect(r.reason).toBe('old');
    expect(player.played.map(c => c.url)).toEqual(['/audio/new.mp3']);
  });

  it('dedupes by url (B6)', () => {
    const s = createScheduler({
      machine, player, currentSession: () => 'claude',
    });
    const a = s.ingest({ url: '/audio/1.mp3', session: 'claude', ts: 5 });
    const b = s.ingest({ url: '/audio/1.mp3', session: 'claude', ts: 5 });
    expect(a.accepted).toBe(true);
    expect(b.accepted).toBe(false);
    expect(b.reason).toBe('duplicate');
  });

  it('re-queues a clip that ended prematurely (B3)', async () => {
    let premature = true;
    const player2 = {
      played: [],
      play(clip) {
        player2.played.push(clip);
        const wasPrem = premature;
        premature = false; // second attempt completes normally
        return Promise.resolve({ premature: wasPrem, duration: 5, currentTime: 1 });
      },
    };
    const s = createScheduler({
      machine, player: player2, currentSession: () => 'claude',
    });
    s.ingest({ url: '/audio/1.mp3', session: 'claude', ts: 1 });
    await new Promise(r => setTimeout(r, 0));
    // Played twice — first attempt fired ended prematurely, scheduler resumed.
    expect(player2.played.map(c => c.url)).toEqual([
      '/audio/1.mp3', '/audio/1.mp3',
    ]);
  });

  it('keeps draining the queue when a player.play() rejection bubbles', async () => {
    // Pin: if the player adapter ever throws/rejects synchronously, the
    // scheduler must NOT deadlock — it has to clear `busy` and play the next
    // clip. (The real adapter's safety timer makes this hard to hit, but a
    // future change could regress it.)
    let calls = 0;
    const player2 = {
      played: [],
      async play(clip) {
        player2.played.push(clip);
        calls++;
        if (calls === 1) throw new Error('player exploded');
        return { premature: false, duration: 2, currentTime: 2 };
      },
    };
    const s = createScheduler({
      machine, player: player2, currentSession: () => 'claude',
    });
    s.ingest({ url: '/audio/1.mp3', session: 'claude', ts: 1 });
    s.ingest({ url: '/audio/2.mp3', session: 'claude', ts: 2 });
    // Give microtasks several turns so the rejected play resolves and the
    // next tick happens.
    for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0));
    expect(player2.played.map(c => c.url)).toEqual([
      '/audio/1.mp3', '/audio/2.mp3',
    ]);
  });

  // "Shut up" semantics — the mute button's job. Drops the queue AND
  // releases the busy lock so a new turn can play even if the previously
  // playing clip's `ended` event never fires (iOS / MSE quirks).
  it('silence() drops queued clips and unblocks the scheduler', async () => {
    let resolvePlay = () => {};
    const stuckPlayer = {
      played: [],
      play(clip) {
        stuckPlayer.played.push(clip);
        return new Promise(res => { resolvePlay = res; });   // never auto-resolves
      },
    };
    const s = createScheduler({
      machine, player: stuckPlayer, currentSession: () => 'claude',
    });
    machine.startRecording('claude'); machine.endRecording(); machine.send('claude');
    s.ingest({ url: '/audio/stuck.mp3', session: 'claude', ts: 10 });
    await Promise.resolve(); await Promise.resolve();
    expect(stuckPlayer.played).toHaveLength(1);   // playing the stuck clip
    // While stuck, queue up more.
    s.ingest({ url: '/audio/next1.mp3', session: 'claude', ts: 20 });
    s.ingest({ url: '/audio/next2.mp3', session: 'claude', ts: 30 });
    expect(s.queueLength).toBe(2);

    // User hits "shut up": drop the queue + release busy.
    const dropped = s.silence();
    expect(dropped).toBe(2);
    expect(s.queueLength).toBe(0);

    // A brand-new clip arriving after silence() must play immediately,
    // even though the stuck clip's play() promise never resolved.
    s.ingest({ url: '/audio/fresh.mp3', session: 'claude', ts: 40 });
    await Promise.resolve(); await Promise.resolve();
    const urls = stuckPlayer.played.map(c => c.url);
    expect(urls).toContain('/audio/fresh.mp3');
  });

  it('silence() returns 0 when nothing was queued', () => {
    const s = createScheduler({
      machine, player, currentSession: () => 'claude',
    });
    expect(s.silence()).toBe(0);
  });

  it('drains backlog after a hanging clip eventually resolves', async () => {
    // The real-world bug: player promise sat unresolved (no 'ended' on iOS)
    // and three clips queued up behind it. When the safety cap finally
    // resolves the hanging promise, the scheduler must flush all of them.
    let release;
    const held = new Promise(res => { release = res; });
    const player2 = {
      played: [],
      async play(clip) {
        player2.played.push(clip);
        if (player2.played.length === 1) await held;   // hang forever
        return { premature: false, duration: 2, currentTime: 2 };
      },
    };
    const s = createScheduler({
      machine, player: player2, currentSession: () => 'claude',
    });
    s.ingest({ url: '/audio/1.mp3', session: 'claude', ts: 1 });
    await new Promise(r => setTimeout(r, 0));
    // Three more arrive while 1 is hung.
    s.ingest({ url: '/audio/2.mp3', session: 'claude', ts: 2 });
    s.ingest({ url: '/audio/3.mp3', session: 'claude', ts: 3 });
    s.ingest({ url: '/audio/4.mp3', session: 'claude', ts: 4 });
    // Only the first started so far.
    expect(player2.played.map(c => c.url)).toEqual(['/audio/1.mp3']);
    release();
    for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0));
    expect(player2.played.map(c => c.url)).toEqual([
      '/audio/1.mp3', '/audio/2.mp3', '/audio/3.mp3', '/audio/4.mp3',
    ]);
  });

  it('SSE replay after a played clip is rejected as duplicate, not replayed', async () => {
    // SSE backfill on reconnect replays the last 5 minutes of events. If a
    // clip already played, ingesting the same url again must be a no-op.
    const s = createScheduler({
      machine, player, currentSession: () => 'claude',
    });
    s.ingest({ url: '/audio/x.mp3', session: 'claude', ts: 100 });
    await new Promise(r => setTimeout(r, 0));
    expect(player.played.map(c => c.url)).toEqual(['/audio/x.mp3']);
    // Simulate SSE backfill — same url with same or older ts.
    const r1 = s.ingest({ url: '/audio/x.mp3', session: 'claude', ts: 100 });
    const r2 = s.ingest({ url: '/audio/x.mp3', session: 'claude', ts: 90  });
    expect(r1.accepted).toBe(false);
    expect(r1.reason).toBe('duplicate');
    expect(r2.accepted).toBe(false);
    await new Promise(r => setTimeout(r, 0));
    // Still only one playback.
    expect(player.played.length).toBe(1);
  });

  it('gives up on a clip after MAX_PREMATURE_RETRIES so NotAllowed cant loop forever', async () => {
    // Real bug: iOS NotAllowedError sets premature=true, scheduler re-queues,
    // tick again, NotAllowed again — infinite loop. New clips pile up behind.
    const player2 = {
      played: [],
      async play(clip) {
        player2.played.push(clip);
        return { premature: true, duration: 0, currentTime: 0 };
      },
    };
    const s = createScheduler({
      machine, player: player2, currentSession: () => 'claude',
    });
    s.ingest({ url: '/audio/loop.mp3', session: 'claude', ts: 1 });
    for (let i = 0; i < 10; i++) await new Promise(r => setTimeout(r, 0));
    // Capped at 3 attempts (initial + 2 retries).
    expect(player2.played.length).toBeLessThanOrEqual(3);
    // Queue is empty afterward — clip was given up on, not stuck.
    expect(s.queueLength).toBe(0);
  });

  it('flushOlderThan drops stale clips so a backlog can be cleared on user gesture', async () => {
    // Hold the player so we can observe queue contents.
    let release;
    const held = new Promise(res => { release = res; });
    const player2 = {
      played: [],
      async play(clip) {
        player2.played.push(clip);
        if (player2.played.length === 1) await held;
        return { premature: false, duration: 2, currentTime: 2 };
      },
    };
    const s = createScheduler({
      machine, player: player2, currentSession: () => 'claude',
    });
    // Distinct ordered timestamps keep the replay guard from rejecting b when
    // two Date.now() calls happen within the same millisecond on a fast runner.
    const now = Date.now();
    try {
      expect(s.ingest({ url: '/audio/a.mp3', session: 'claude', ts: now - 60_001 }).accepted).toBe(true);
      await new Promise(r => setTimeout(r, 0));
      expect(s.ingest({ url: '/audio/b.mp3', session: 'claude', ts: now - 60_000 }).accepted).toBe(true);
      expect(s.ingest({ url: '/audio/c.mp3', session: 'claude', ts: now }).accepted).toBe(true);
      expect(s.queueLength).toBe(2);
      const dropped = s.flushOlderThan(30_000);
      expect(dropped).toBe(1);   // only b is dropped; a is already playing, c is fresh
    } finally {
      release();
    }
    for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0));
    // a finishes, then c plays (b was dropped).
    expect(player2.played.map(p => p.url)).toEqual(['/audio/a.mp3', '/audio/c.mp3']);
  });

  it('multi-agent flow: talk to Mike → Rachel → Bella, then return to Mike — plays in correct order', async () => {
    // The whole point of the scheduler. The user is the human in front of
    // a PWA chip; `currentSession` follows whichever pane they're looking at.
    // The state machine encodes "we just sent to X, expect a reply from them
    // first". When multiple agents have queued replies and the user returns
    // to Mike, currentSession-priority should drain Mike's clips before the
    // unrelated background ones.
    let activePane = 'claude';
    const s = createScheduler({
      machine, player, currentSession: () => activePane,
    });
    const speakTo = (sid) => {
      activePane = sid;
      machine.startRecording(sid);
      machine.endRecording();
      machine.send(sid);                    // now AWAITING for=sid
    };
    const settle = () => new Promise(r => setTimeout(r, 0));

    // 1. the user talks to Mike. Mike replies. Plays immediately (addressee match).
    speakTo('claude');
    s.ingest({ url: '/audio/mike1.mp3', session: 'claude', ts: 10 });
    await settle();
    expect(player.played.map(c => c.url)).toEqual(['/audio/mike1.mp3']);

    // 2. User pivots to Rachel. Rachel replies. Plays.
    speakTo('rachel');
    s.ingest({ url: '/audio/rachel1.mp3', session: 'rachel', ts: 20 });
    await settle();
    expect(player.played.map(c => c.url)).toEqual([
      '/audio/mike1.mp3', '/audio/rachel1.mp3',
    ]);

    // 3. User pivots to Bella. While she's thinking, Mike and Rachel each
    // emit a background follow-up; neither should play yet because we're
    // awaiting Bella.
    speakTo('bella');
    s.ingest({ url: '/audio/mike2.mp3',   session: 'claude', ts: 30 });
    s.ingest({ url: '/audio/rachel2.mp3', session: 'rachel', ts: 31 });
    await settle();
    expect(player.played.length).toBe(2);   // unchanged

    // 4. Bella replies. Hers plays first (addressee), then queue drains in
    // currentSession-priority order. CurrentSession is still bella, so the
    // remaining clips drain FIFO by ts (no matching session).
    s.ingest({ url: '/audio/bella1.mp3',  session: 'bella',  ts: 32 });
    await settle();
    await settle();   // multiple ticks to drain three clips
    await settle();
    expect(player.played.map(c => c.url)).toEqual([
      '/audio/mike1.mp3', '/audio/rachel1.mp3',
      '/audio/bella1.mp3',                          // addressee played first
      '/audio/mike2.mp3', '/audio/rachel2.mp3',     // then FIFO drains
    ]);

    // 5. New scenario: user pivots back to Mike's pane (no fresh recording —
    // they just look at his chip). currentSession follows the pane. To
    // honestly test currentSession-priority we need three clips QUEUED
    // before the scheduler picks one — otherwise it's just FIFO on the
    // first ingest. Swap to a held player and release once all three are in.
    let release;
    const held = new Promise(res => { release = res; });
    let heldOnce = false;
    const heldPlayer = {
      played: [],
      async play(clip) {
        heldPlayer.played.push(clip);
        if (!heldOnce) { heldOnce = true; await held; }
        return { premature: false, duration: 2, currentTime: 2 };
      },
    };
    const s2 = createScheduler({
      machine, player: heldPlayer, currentSession: () => activePane,
    });
    activePane = 'claude';
    // Prime: one clip starts playing and holds the scheduler busy.
    s2.ingest({ url: '/audio/primer.mp3', session: 'claude', ts: 39 });
    await settle();
    // Three more arrive in arbitrary order while the primer is held.
    s2.ingest({ url: '/audio/rachel3.mp3', session: 'rachel', ts: 40 });
    s2.ingest({ url: '/audio/mike3.mp3',   session: 'claude', ts: 41 });
    s2.ingest({ url: '/audio/bella2.mp3',  session: 'bella',  ts: 42 });
    release();
    for (let i = 0; i < 5; i++) await settle();
    // Mike (current pane) drains first; the other two follow in arrival order.
    const tail = heldPlayer.played.map(c => c.url);
    expect(tail).toEqual([
      '/audio/primer.mp3',                          // was already playing
      '/audio/mike3.mp3',                           // currentSession priority
      '/audio/rachel3.mp3', '/audio/bella2.mp3',    // then FIFO
    ]);
  });

  it('addressee-first tie-break when both clips wait at the same time', async () => {
    // Use a player that holds the first play() open until we release it,
    // so the second clip queues alongside the first.
    let release;
    const held = new Promise(res => { release = res; });
    const player2 = {
      played: [],
      async play(clip) {
        player2.played.push(clip);
        if (player2.played.length === 1) await held;
        return { premature: false, duration: 2, currentTime: 2 };
      },
    };
    const s = createScheduler({
      machine, player: player2, currentSession: () => 'rachel',
    });
    // First clip starts playing immediately (and is held).
    s.ingest({ url: '/audio/m1.mp3', session: 'claude', ts: 1 });
    await new Promise(r => setTimeout(r, 0));
    // Two more arrive while m1 is held: one from rachel (addressee), one
    // from claude (background).
    s.ingest({ url: '/audio/c2.mp3', session: 'claude', ts: 2 });
    s.ingest({ url: '/audio/r1.mp3', session: 'rachel', ts: 3 });
    release();
    await new Promise(r => setTimeout(r, 0));
    await new Promise(r => setTimeout(r, 0));
    // After m1 finishes, addressee (rachel) plays before the other claude.
    expect(player2.played.map(c => c.url)).toEqual([
      '/audio/m1.mp3', '/audio/r1.mp3', '/audio/c2.mp3',
    ]);
  });

  it('reports clip lifecycle statuses', async () => {
    const statuses = [];
    let resolvePlay;
    const player2 = {
      play: vi.fn(() => new Promise(resolve => { resolvePlay = resolve; })),
    };
    const s = createScheduler({
      machine,
      player: player2,
      currentSession: () => 'claude',
      log: () => {},
      onClipStatus: (clip, status) => statuses.push([clip.url, status]),
    });

    s.ingest({ url: '/audio/1.mp3', session: 'claude', ts: 1 });
    expect(statuses).toEqual([
      ['/audio/1.mp3', 'queued'],
      ['/audio/1.mp3', 'play-start'],
    ]);

    resolvePlay({ premature: false });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(statuses.at(-1)).toEqual(['/audio/1.mp3', 'play-ok']);
  });
});
