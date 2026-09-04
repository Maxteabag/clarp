import { beforeEach, describe, expect, it, vi } from 'vitest';

const clog = vi.fn();

vi.mock('../../web/src/lib/net.js', () => ({ clog }));
vi.mock('../../web/src/stores/app.svelte.js', () => ({
  app: { session: 'rachel' },
  flash: vi.fn(),
  logState: vi.fn(),
}));
vi.mock('@core/state-machine.js', () => ({
  createStateMachine: () => ({ on() {}, state: 'idle' }),
}));
vi.mock('@core/audio-queue.js', () => ({
  createScheduler: () => ({ queueLength: 0 }),
}));
vi.mock('@core/player-adapter.js', () => ({
  PLAYER_ADAPTER_VERSION: 'test',
  createPlayerAdapter: () => ({ interrupt() {} }),
}));
vi.mock('@core/audio-faults.js', () => ({
  createFaultMonitor: () => ({}),
}));
vi.mock('@core/protocol.js', () => ({
  ClipStatus: { VALID: new Set(['played']) },
  Timing: { AWAIT_DEADLINE_MS: 1000, AUDIO_CONTEXT_CLOSE_MS: 1000 },
}));

function installBrowserGlobals() {
  const values = new Map();
  vi.stubGlobal('localStorage', {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  });
  vi.stubGlobal('navigator', {});
  vi.stubGlobal('window', { location: { href: 'https://clarp.test/' } });
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  installBrowserGlobals();
});

describe('audio element unlocking', () => {
  it('ignores a second unlock while play is pending and restores the real mute state', async () => {
    const gate = deferred();
    const player = {
      src: 'https://clarp.test/voice.mp3',
      muted: false,
      currentTime: 4,
      play: vi.fn(() => gate.promise),
      pause: vi.fn(),
    };
    const store = await import('../../web/src/stores/audio.svelte.js');
    store.initAudio(player);

    store.unlockAudio();
    expect(player.muted).toBe(true);
    store.unlockAudio();
    expect(player.play).toHaveBeenCalledTimes(1);

    gate.resolve();
    await gate.promise;
    await Promise.resolve();

    expect(store.audio.unlocked).toBe(true);
    expect(player.muted).toBe(false);
    expect(player.pause).toHaveBeenCalledTimes(1);
  });

  it('restores mute and remains retryable when play returns no promise', async () => {
    const player = {
      src: '', muted: false, currentTime: 0,
      play: vi.fn(() => undefined), pause: vi.fn(),
    };
    const store = await import('../../web/src/stores/audio.svelte.js');
    store.initAudio(player);

    store.unlockAudio();

    expect(player.muted).toBe(false);
    expect(store.audio.unlocked).toBe(false);
    store.unlockAudio();
    expect(player.play).toHaveBeenCalledTimes(2);
  });
});
